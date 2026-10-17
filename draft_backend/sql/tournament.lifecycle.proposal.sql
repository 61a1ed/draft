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
-- announced criteria. Publishing the final field certifies its two members;
-- their order has no meaning and ties between two advancing teams need no ruling.

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
    IF p_reason IS NULL OR btrim(p_reason) = '' THEN
        RAISE SQLSTATE 'PT400' USING MESSAGE = 'reason_required';
    END IF;
    SELECT s.id, s.qualification_finalized_at INTO source_stage, finalized_at
    FROM tournament_stage s
    WHERE s.tournament_id = p_league AND s.source_stage_id IS NULL;
    IF NOT FOUND THEN
        RAISE SQLSTATE 'PT409' USING MESSAGE = 'stage_not_found';
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
            INSERT INTO match_disposition (match_id, withdrawal_team_id)
            VALUES (fixture.id, p_team);
        END LOOP;
    END IF;
    -- After qualification, record withdrawal only. Leave the final and all
    -- source results intact, with no automatic forfeit, replacement or closure.
    PERFORM dl.notify_league_accounts(p_league);
END $$;

CREATE FUNCTION dl.create_tiebreak_round(p_stage integer, p_revision integer,
    p_teams integer[], p_reason text, p_suggested_start_date date DEFAULT NULL)
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
    IF stage.format <> 'round_robin' OR stage.source_stage_id IS NOT NULL THEN
        RAISE SQLSTATE 'PT409' USING MESSAGE = 'tiebreak_requires_source_round_robin';
    END IF;
    IF stage.qualification_finalized_at IS NOT NULL THEN
        RAISE SQLSTATE 'PT409' USING MESSAGE = 'qualification_already_finalized';
    END IF;
    IF p_reason IS NULL OR btrim(p_reason) = '' THEN
        RAISE SQLSTATE 'PT400' USING MESSAGE = 'reason_required';
    END IF;
    IF p_suggested_start_date IS NOT NULL AND NOT isfinite(p_suggested_start_date) THEN
        RAISE SQLSTATE 'PT400' USING MESSAGE = 'invalid_suggested_date';
    END IF;
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
        (stage_id, number, purpose, tiebreak_reason, created_by, suggested_start_date)
    VALUES (p_stage, round_number, 'tiebreak', btrim(p_reason), u, p_suggested_start_date)
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
    p_qualifiers integer[], p_suggested_start_date date DEFAULT NULL)
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
    SELECT * INTO source_stage FROM tournament_stage
    WHERE tournament_id = p_league AND source_stage_id IS NULL;
    IF NOT FOUND THEN
        RAISE SQLSTATE 'PT409' USING MESSAGE = 'stage_not_found';
    END IF;
    SELECT * INTO final_stage FROM tournament_stage
    WHERE tournament_id = p_league AND source_stage_id = source_stage.id;
    IF NOT FOUND THEN
        RAISE SQLSTATE 'PT409' USING MESSAGE = 'stage_not_found';
    END IF;
    IF source_stage.qualification_finalized_at IS NOT NULL THEN
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
    -- Input order is irrelevant. Canonical A/B orientation has no seed/rank meaning.
    SELECT array_agg(team_id ORDER BY team_id) INTO qualifiers
    FROM unnest(p_qualifiers) AS selected(team_id);
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
    IF p_suggested_start_date IS NOT NULL AND NOT isfinite(p_suggested_start_date) THEN
        RAISE SQLSTATE 'PT400' USING MESSAGE = 'invalid_suggested_date';
    END IF;
    IF EXISTS (
        SELECT FROM match m JOIN tournament_round r ON r.id = m.round_id
        WHERE r.stage_id = source_stage.id AND m.current_decision_id IS NULL
          AND NOT EXISTS (SELECT FROM match_disposition d WHERE d.match_id = m.id)
    ) THEN
        RAISE SQLSTATE 'PT409' USING MESSAGE = 'stage_matches_unresolved';
    END IF;

    PERFORM dl.tournament_bump(p_league);
    UPDATE tournament_stage
    SET qualification_finalized_at = clock_timestamp(), qualification_finalized_by = u
    WHERE id = source_stage.id;
    INSERT INTO stage_entry (stage_id, team_id, entered_by)
    SELECT final_stage.id, team_id, u FROM unnest(qualifiers) AS selected(team_id);
    INSERT INTO tournament_round (stage_id, number, purpose, created_by, suggested_start_date)
    VALUES (final_stage.id, 1, 'normal', u, p_suggested_start_date)
    RETURNING id INTO final_round;
    INSERT INTO match (round_id, team_a_id, team_b_id)
    VALUES (final_round, qualifiers[1], qualifiers[2]) RETURNING id INTO result;
    PERFORM dl.notify_league_accounts(p_league);
    RETURN result;
END $$;

COMMENT ON FUNCTION dl.create_tiebreak_round(integer, integer, integer[], text, date)
IS 'Commissioner certifies selected eligible entrants remain tied under announced external criteria. Validates completion and freshness; does not compute rankings.';
COMMENT ON FUNCTION dl.publish_final(integer, integer, integer[], date)
IS 'Commissioner certifies two eligible qualifiers, without ordering them. Atomically freezes final membership and publishes its fixture; does not compute rankings.';

REVOKE ALL ON FUNCTION
    dl.withdraw_team(integer, integer, integer, text),
    dl.create_tiebreak_round(integer, integer, integer[], text, date),
    dl.publish_final(integer, integer, integer[], date)
FROM PUBLIC;
