-- REVIEW PROPOSAL: apply after tournament.proposal.sql and the shared tournament
-- writer/result helpers. This file does not enable the HTTP feature.
-- All entrypoints require a SERIALIZABLE transaction and whole-call retries.
-- Retain the original expected tournament revision on every retry.
--
-- Qualification is commissioner-certified. The commissioner selects the teams
-- using the announced competition criteria and the current normal-round and
-- individual tiebreak-round evidence. These writers validate eligibility,
-- completeness and freshness; they do not implement an automatic ranking engine.
-- Selecting a tiebreak field certifies that those entrants remain tied under the
-- announced criteria. Publishing the ordered final field certifies places 1/2
-- and that all qualification ties have been resolved under those criteria.

CREATE FUNCTION dl.withdraw_team(p_league integer, p_revision integer,
    p_team integer, p_reason text)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, dl, pg_temp AS $$
DECLARE
    u integer := dl.tournament_commissioner(p_league);
    source_stage integer;
    finalized_at timestamptz;
    fixture dl.match;
BEGIN
    PERFORM dl.tournament_check_revision(p_league, p_revision);
    IF NOT EXISTS (SELECT FROM tournament
                   WHERE league_id = p_league AND published_at IS NOT NULL) THEN
        RAISE SQLSTATE 'PT409' USING MESSAGE = 'tournament_not_published';
    END IF;
    IF p_reason IS NULL OR btrim(p_reason) = '' THEN
        RAISE SQLSTATE 'PT400' USING MESSAGE = 'reason_required';
    END IF;
    SELECT s.id, s.standings_finalized_at INTO source_stage, finalized_at
    FROM tournament_stage s
    WHERE s.tournament_id = p_league AND s.source_stage_id IS NULL
      AND s.format = 'round_robin';
    IF NOT FOUND THEN
        RAISE SQLSTATE 'PT409' USING MESSAGE = 'invalid_tournament_pipeline';
    END IF;
    IF NOT EXISTS (
        SELECT FROM team t JOIN stage_entry e ON e.team_id = t.id
        WHERE t.id = p_team AND t.league_id = p_league
          AND e.stage_id = source_stage
    ) THEN
        RAISE SQLSTATE 'PT404' USING MESSAGE = 'team_not_found';
    END IF;
    IF EXISTS (SELECT FROM tournament_withdrawal WHERE team_id = p_team) THEN
        RAISE SQLSTATE 'PT409' USING MESSAGE = 'team_already_withdrawn';
    END IF;

    PERFORM dl.tournament_bump(p_league);
    INSERT INTO tournament_withdrawal (team_id, decided_by, reason)
    VALUES (p_team, u, btrim(p_reason));

    IF finalized_at IS NULL THEN
        -- Only newly removed fixtures change. A disposition already recorded for
        -- an opponent's withdrawal, including its original attribution, survives.
        FOR fixture IN
            SELECT m.* FROM match m
            JOIN tournament_round r ON r.id = m.round_id
            WHERE r.stage_id = source_stage
              AND (m.team_a_id = p_team OR m.team_b_id = p_team)
              AND NOT EXISTS (SELECT FROM match_disposition d WHERE d.match_id = m.id)
            ORDER BY m.id
        LOOP
            UPDATE match SET revision = revision + 1 WHERE id = fixture.id;
            INSERT INTO match_disposition (match_id, cancelled, decided_by, reason)
            VALUES (fixture.id,
                    fixture.current_decision_id IS NULL
                        AND fixture.team_a_submission_id IS NULL
                        AND fixture.team_b_submission_id IS NULL,
                    u, btrim(p_reason));
        END LOOP;
    ELSE
        -- Final publication freezes source results and both final entrants.
        -- Nonfinalists and already official finals need no match changes.
        SELECT m.* INTO fixture
        FROM match m
        JOIN tournament_round r ON r.id = m.round_id
        JOIN tournament_stage s ON s.id = r.stage_id
        WHERE s.tournament_id = p_league AND s.source_stage_id = source_stage
          AND s.format = 'single_elimination'
          AND (m.team_a_id = p_team OR m.team_b_id = p_team)
          AND m.current_decision_id IS NULL;
        IF FOUND THEN
            IF EXISTS (SELECT FROM match_disposition WHERE match_id = fixture.id) THEN
                RAISE SQLSTATE 'PT409' USING MESSAGE = 'final_fixture_removed';
            END IF;
            UPDATE match SET revision = revision + 1 WHERE id = fixture.id;
            -- Private result helper appends the commissioner report/decision and
            -- moves only the official pointer; both participant pointers survive.
            PERFORM dl.tournament_append_commissioner_result(
                fixture.id, u, btrim(p_reason), 'forfeit', NULL, NULL,
                CASE WHEN fixture.team_a_id = p_team THEN 'b' ELSE 'a' END);
        END IF;
    END IF;
    PERFORM dl.notify_league_accounts(p_league);
END $$;

CREATE FUNCTION dl.create_tiebreak_round(p_stage integer, p_revision integer,
    p_teams integer[], p_opens_at timestamptz, p_closes_at timestamptz, p_reason text)
RETURNS integer LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, dl, pg_temp AS $$
DECLARE
    u integer := dl.require_writer();
    stage tournament_stage;
    result integer;
    round_number integer;
BEGIN
    SELECT * INTO stage FROM tournament_stage WHERE id = p_stage;
    IF NOT FOUND THEN
        RAISE SQLSTATE 'PT404' USING MESSAGE = 'stage_not_found';
    END IF;
    u := dl.tournament_commissioner(stage.tournament_id);
    PERFORM dl.tournament_check_revision(stage.tournament_id, p_revision);
    IF NOT EXISTS (SELECT FROM tournament
                   WHERE league_id = stage.tournament_id AND published_at IS NOT NULL) THEN
        RAISE SQLSTATE 'PT409' USING MESSAGE = 'tournament_not_published';
    END IF;
    IF stage.format <> 'round_robin' OR stage.source_stage_id IS NOT NULL THEN
        RAISE SQLSTATE 'PT409' USING MESSAGE = 'tiebreak_requires_source_round_robin';
    END IF;
    IF stage.standings_finalized_at IS NOT NULL THEN
        RAISE SQLSTATE 'PT409' USING MESSAGE = 'qualification_already_finalized';
    END IF;
    IF p_reason IS NULL OR btrim(p_reason) = '' THEN
        RAISE SQLSTATE 'PT400' USING MESSAGE = 'reason_required';
    END IF;
    PERFORM dl.tournament_validate_window(p_opens_at, p_closes_at);
    IF p_teams IS NULL OR cardinality(p_teams) < 2
       OR array_ndims(p_teams) IS DISTINCT FROM 1 THEN
        RAISE SQLSTATE 'PT400' USING MESSAGE = 'at_least_two_teams_required';
    END IF;
    IF EXISTS (SELECT FROM unnest(p_teams) AS selected(team_id) WHERE team_id IS NULL)
       OR (SELECT count(DISTINCT team_id) FROM unnest(p_teams) AS selected(team_id))
          <> cardinality(p_teams) THEN
        RAISE SQLSTATE 'PT400' USING MESSAGE = 'distinct_teams_required';
    END IF;
    IF EXISTS (
        SELECT FROM unnest(p_teams) AS selected(team_id)
        LEFT JOIN stage_entry e ON e.stage_id = p_stage AND e.team_id = selected.team_id
        LEFT JOIN team t ON t.id = e.team_id AND t.league_id = stage.tournament_id
        WHERE t.id IS NULL OR EXISTS (
            SELECT FROM tournament_withdrawal w WHERE w.team_id = selected.team_id
        )
    ) THEN
        RAISE SQLSTATE 'PT409' USING MESSAGE = 'ineligible_tiebreak_team';
    END IF;
    -- Covers all normal fixtures and every earlier tiebreak attempt. A removed
    -- fixture is complete for qualification even if its retained reports differ.
    IF EXISTS (
        SELECT FROM match m JOIN tournament_round r ON r.id = m.round_id
        WHERE r.stage_id = p_stage AND m.current_decision_id IS NULL
          AND NOT EXISTS (SELECT FROM match_disposition d WHERE d.match_id = m.id)
    ) THEN
        RAISE SQLSTATE 'PT409' USING MESSAGE = 'stage_matches_unresolved';
    END IF;

    PERFORM dl.tournament_bump(stage.tournament_id);
    SELECT coalesce(max(number), 0) + 1 INTO round_number
    FROM tournament_round WHERE stage_id = p_stage;
    INSERT INTO tournament_round
        (stage_id, number, purpose, tiebreak_reason, created_by, opens_at, closes_at)
    VALUES (p_stage, round_number, 'tiebreak', btrim(p_reason), u, p_opens_at, p_closes_at)
    RETURNING id INTO result;
    -- Canonical pair orientation makes the complete fixture preview deterministic.
    INSERT INTO match (round_id, team_a_id, team_b_id)
    SELECT result, a.team_id, b.team_id
    FROM unnest(p_teams) AS a(team_id) CROSS JOIN unnest(p_teams) AS b(team_id)
    WHERE a.team_id < b.team_id ORDER BY a.team_id, b.team_id;
    PERFORM dl.notify_league_accounts(stage.tournament_id);
    RETURN result;
END $$;

CREATE FUNCTION dl.publish_final(p_league integer, p_revision integer,
    p_qualifiers integer[], p_opens_at timestamptz, p_closes_at timestamptz)
RETURNS integer LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, dl, pg_temp AS $$
DECLARE
    u integer := dl.tournament_commissioner(p_league);
    source_stage tournament_stage;
    final_stage tournament_stage;
    qualifiers integer[];
    final_round integer;
    result integer;
BEGIN
    PERFORM dl.tournament_check_revision(p_league, p_revision);
    IF NOT EXISTS (SELECT FROM tournament
                   WHERE league_id = p_league AND published_at IS NOT NULL) THEN
        RAISE SQLSTATE 'PT409' USING MESSAGE = 'tournament_not_published';
    END IF;
    SELECT * INTO source_stage FROM tournament_stage
    WHERE tournament_id = p_league AND source_stage_id IS NULL AND format = 'round_robin';
    IF NOT FOUND THEN
        RAISE SQLSTATE 'PT409' USING MESSAGE = 'invalid_tournament_pipeline';
    END IF;
    SELECT * INTO final_stage FROM tournament_stage
    WHERE tournament_id = p_league AND source_stage_id = source_stage.id
      AND format = 'single_elimination' AND qualifier_count = 2
      AND position > source_stage.position;
    IF NOT FOUND THEN
        RAISE SQLSTATE 'PT409' USING MESSAGE = 'invalid_tournament_pipeline';
    END IF;
    IF source_stage.standings_finalized_at IS NOT NULL
       OR EXISTS (SELECT FROM stage_entry WHERE stage_id = final_stage.id)
       OR EXISTS (SELECT FROM tournament_round WHERE stage_id = final_stage.id) THEN
        RAISE SQLSTATE 'PT409' USING MESSAGE = 'final_already_published';
    END IF;
    IF p_qualifiers IS NULL OR cardinality(p_qualifiers) <> 2
       OR array_ndims(p_qualifiers) IS DISTINCT FROM 1 THEN
        RAISE SQLSTATE 'PT400' USING MESSAGE = 'exactly_two_qualifiers_required';
    END IF;
    IF EXISTS (SELECT FROM unnest(p_qualifiers) AS selected(team_id) WHERE team_id IS NULL)
       OR (SELECT count(DISTINCT team_id) FROM unnest(p_qualifiers) AS selected(team_id)) <> 2 THEN
        RAISE SQLSTATE 'PT400' USING MESSAGE = 'distinct_qualifiers_required';
    END IF;
    -- Normalize array bounds while retaining the certified first/second order.
    SELECT array_agg(team_id ORDER BY place) INTO qualifiers
    FROM unnest(p_qualifiers) WITH ORDINALITY AS selected(team_id, place);
    IF EXISTS (
        SELECT FROM unnest(qualifiers) AS selected(team_id)
        LEFT JOIN stage_entry e ON e.stage_id = source_stage.id AND e.team_id = selected.team_id
        LEFT JOIN team t ON t.id = e.team_id AND t.league_id = p_league
        WHERE t.id IS NULL OR EXISTS (
            SELECT FROM tournament_withdrawal w WHERE w.team_id = selected.team_id
        )
    ) THEN
        RAISE SQLSTATE 'PT409' USING MESSAGE = 'ineligible_qualifier';
    END IF;
    PERFORM dl.tournament_validate_window(p_opens_at, p_closes_at);
    IF EXISTS (
        SELECT FROM match m JOIN tournament_round r ON r.id = m.round_id
        WHERE r.stage_id = source_stage.id AND m.current_decision_id IS NULL
          AND NOT EXISTS (SELECT FROM match_disposition d WHERE d.match_id = m.id)
    ) THEN
        RAISE SQLSTATE 'PT409' USING MESSAGE = 'stage_matches_unresolved';
    END IF;

    PERFORM dl.tournament_bump(p_league);
    UPDATE stage_entry e SET final_place = selected.place::integer
    FROM unnest(qualifiers) WITH ORDINALITY AS selected(team_id, place)
    WHERE e.stage_id = source_stage.id AND e.team_id = selected.team_id;
    UPDATE tournament_stage
    SET standings_finalized_at = clock_timestamp(), standings_finalized_by = u
    WHERE id = source_stage.id;
    INSERT INTO stage_entry (stage_id, team_id, seed, entered_by)
    SELECT final_stage.id, team_id, place::integer, u
    FROM unnest(qualifiers) WITH ORDINALITY AS selected(team_id, place);
    INSERT INTO tournament_round (stage_id, number, purpose, created_by, opens_at, closes_at)
    VALUES (final_stage.id, 1, 'normal', u, p_opens_at, p_closes_at)
    RETURNING id INTO final_round;
    INSERT INTO match (round_id, team_a_id, team_b_id)
    VALUES (final_round, qualifiers[1], qualifiers[2]) RETURNING id INTO result;
    PERFORM dl.notify_league_accounts(p_league);
    RETURN result;
END $$;

COMMENT ON FUNCTION dl.create_tiebreak_round(integer, integer, integer[], timestamptz, timestamptz, text)
IS 'Commissioner certifies selected eligible entrants remain tied under announced external criteria. Validates completion and freshness; does not compute rankings.';
COMMENT ON FUNCTION dl.publish_final(integer, integer, integer[], timestamptz, timestamptz)
IS 'Commissioner certifies the ordered two eligible qualifiers under announced external criteria. Atomically freezes places 1/2 and publishes their final; does not compute rankings.';

REVOKE ALL ON FUNCTION
    dl.withdraw_team(integer, integer, integer, text),
    dl.create_tiebreak_round(integer, integer, integer[], timestamptz, timestamptz, text),
    dl.publish_final(integer, integer, integer[], timestamptz, timestamptz)
FROM PUBLIC;
