-- REVIEW PROPOSAL: result/VOD writers, not an installed migration or HTTP API.
-- Apply after schema.sql, tournament.proposal.sql and tournament.mutators.proposal.sql.
-- Public entry points require SERIALIZABLE and trusted request.jwt.claims.
-- Helpers remain private; no table DML or helper EXECUTE belongs to the API role.

CREATE FUNCTION dl.tournament_result_context(p_match integer)
RETURNS TABLE (match_row dl.match, league_id integer, wins_required integer,
               manager_a integer, manager_b integer)
LANGUAGE sql STABLE
SET search_path = pg_catalog, dl, pg_temp AS $$
    SELECT m, t.league_id, s.wins_required,
           a.manager_user_id, b.manager_user_id
    FROM match m
    JOIN tournament_round r ON r.id = m.round_id
    JOIN tournament_stage s ON s.id = r.stage_id
    JOIN tournament t ON t.league_id = s.tournament_id
    JOIN team a ON a.id = m.team_a_id AND a.league_id = t.league_id
    JOIN team b ON b.id = m.team_b_id AND b.league_id = t.league_id
    JOIN stage_entry ea ON ea.stage_id = s.id AND ea.team_id = a.id
    JOIN stage_entry eb ON eb.stage_id = s.id AND eb.team_id = b.id
    WHERE m.id = p_match
$$;

CREATE FUNCTION dl.tournament_validate_result(p_wins_required integer,
    p_outcome_kind text, p_team_a_game_wins integer, p_team_b_game_wins integer,
    p_awarded_winner_side text)
RETURNS void LANGUAGE plpgsql IMMUTABLE
SET search_path = pg_catalog, dl, pg_temp AS $$
BEGIN
    IF ((p_outcome_kind = 'played' AND p_awarded_winner_side IS NULL
         AND p_team_a_game_wins >= 0 AND p_team_b_game_wins >= 0
         AND ((p_team_a_game_wins = p_wins_required
               AND p_team_b_game_wins < p_wins_required)
              OR (p_team_b_game_wins = p_wins_required
                  AND p_team_a_game_wins < p_wins_required)))
        OR (p_outcome_kind = 'forfeit'
            AND p_team_a_game_wins IS NULL AND p_team_b_game_wins IS NULL
            AND p_awarded_winner_side IN ('a', 'b'))) IS NOT TRUE THEN
        RAISE SQLSTATE 'PT400' USING MESSAGE = 'invalid_result';
    END IF;
END $$;

-- A current vote must belong to this match, side and its fixed manager. An
-- administrator-authored assertion never counts as a participant's vote.
CREATE FUNCTION dl.tournament_current_report(p_match integer, p_submission integer,
    p_side text, p_author integer, p_wins_required integer)
RETURNS dl.result_submission LANGUAGE plpgsql STABLE
SET search_path = pg_catalog, dl, pg_temp AS $$
DECLARE report dl.result_submission;
BEGIN
    SELECT * INTO report FROM result_submission
    WHERE id = p_submission AND match_id = p_match;
    IF NOT FOUND OR report.reporting_side IS DISTINCT FROM p_side
       OR report.submitted_by IS DISTINCT FROM p_author THEN
        RAISE SQLSTATE 'PT409' USING MESSAGE = 'invalid_current_submission';
    END IF;
    PERFORM tournament_validate_result(p_wins_required, report.outcome_kind,
        report.team_a_game_wins, report.team_b_game_wins, report.awarded_winner_side);
    RETURN report;
END $$;

-- Shared by record/correct. Callers
-- authorize and validate first, then advance tournament and match revisions.
-- This helper only appends history and moves the official pointer. It does not
-- change revisions, notify, alter player pointers or decide whether play is open.
CREATE FUNCTION dl.tournament_append_commissioner_result(p_match integer,
    p_actor integer, p_reason text, p_outcome_kind text,
    p_team_a_game_wins integer, p_team_b_game_wins integer,
    p_awarded_winner_side text, p_selected_submission integer DEFAULT NULL)
RETURNS integer LANGUAGE plpgsql
SET search_path = pg_catalog, dl, pg_temp AS $$
DECLARE submission integer := p_selected_submission; decision integer;
BEGIN
    IF submission IS NULL THEN
        INSERT INTO result_submission (match_id, reporting_side, submitted_by,
            outcome_kind, team_a_game_wins, team_b_game_wins, awarded_winner_side)
        VALUES (p_match, NULL, p_actor, p_outcome_kind,
            p_team_a_game_wins, p_team_b_game_wins, p_awarded_winner_side)
        RETURNING id INTO submission;
    END IF;
    INSERT INTO match_decision (match_id, selected_submission_id,
        authority, decided_by, reason)
    VALUES (p_match, submission, 'commissioner', p_actor, p_reason)
    RETURNING id INTO decision;
    UPDATE match SET current_decision_id = decision WHERE id = p_match;
    RETURN decision;
END $$;

CREATE FUNCTION dl.submit_result(p_match integer,
    p_expected_previous_submission_id integer, p_outcome_kind text,
    p_team_a_game_wins integer DEFAULT NULL, p_team_b_game_wins integer DEFAULT NULL,
    p_awarded_winner_side text DEFAULT NULL)
RETURNS integer LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, dl, pg_temp AS $$
DECLARE u integer := require_writer(); ctx record; m dl.match; side text;
        previous integer; submission integer; decision integer;
        a dl.result_submission; b dl.result_submission;
BEGIN
    SELECT * INTO ctx FROM tournament_result_context(p_match);
    IF NOT FOUND THEN
        RAISE SQLSTATE 'PT404' USING MESSAGE = 'match_not_found';
    END IF;
    m := ctx.match_row;
    side := CASE WHEN u = ctx.manager_a THEN 'a' WHEN u = ctx.manager_b THEN 'b' END;
    IF side IS NULL THEN
        RAISE SQLSTATE 'PT409' USING MESSAGE = 'action_not_permitted';
    END IF;
    IF m.current_decision_id IS NOT NULL
       OR EXISTS (SELECT FROM match_disposition WHERE match_id = p_match) THEN
        RAISE SQLSTATE 'PT409' USING MESSAGE = 'match_not_open';
    END IF;
    previous := CASE WHEN side = 'a' THEN m.team_a_submission_id
                     ELSE m.team_b_submission_id END;
    IF previous IS DISTINCT FROM p_expected_previous_submission_id THEN
        RAISE SQLSTATE 'PT409' USING MESSAGE = 'stale_submission';
    END IF;
    PERFORM tournament_validate_result(ctx.wins_required, p_outcome_kind,
        p_team_a_game_wins, p_team_b_game_wins, p_awarded_winner_side);

    -- The tournament row is the shared competitive write point. Keep this order
    -- on every retry, preserving the caller's original own-report expectation.
    PERFORM tournament_bump(ctx.league_id);
    UPDATE match SET revision = revision + 1 WHERE id = p_match;
    INSERT INTO result_submission (match_id, reporting_side, submitted_by,
        outcome_kind, team_a_game_wins, team_b_game_wins, awarded_winner_side)
    VALUES (p_match, side, u, p_outcome_kind,
        p_team_a_game_wins, p_team_b_game_wins, p_awarded_winner_side)
    RETURNING id INTO submission;
    IF side = 'a' THEN
        UPDATE match SET team_a_submission_id = submission WHERE id = p_match;
        m.team_a_submission_id := submission;
    ELSE
        UPDATE match SET team_b_submission_id = submission WHERE id = p_match;
        m.team_b_submission_id := submission;
    END IF;

    IF m.team_a_submission_id IS NOT NULL THEN
        a := tournament_current_report(p_match, m.team_a_submission_id,
            'a', ctx.manager_a, ctx.wins_required);
    END IF;
    IF m.team_b_submission_id IS NOT NULL THEN
        b := tournament_current_report(p_match, m.team_b_submission_id,
            'b', ctx.manager_b, ctx.wins_required);
    END IF;
    IF m.team_a_submission_id IS NOT NULL AND m.team_b_submission_id IS NOT NULL
       AND ROW(a.outcome_kind, a.team_a_game_wins, a.team_b_game_wins, a.awarded_winner_side)
           IS NOT DISTINCT FROM
           ROW(b.outcome_kind, b.team_a_game_wins, b.team_b_game_wins, b.awarded_winner_side) THEN
        INSERT INTO match_decision (match_id, selected_submission_id,
            supporting_submission_id, authority)
        VALUES (p_match, a.id, b.id, 'agreement') RETURNING id INTO decision;
        UPDATE match SET current_decision_id = decision WHERE id = p_match;
    END IF;
    PERFORM notify_league_accounts(ctx.league_id);
    RETURN submission;
END $$;

CREATE FUNCTION dl.record_result(p_match integer, p_revision integer, p_reason text,
    p_selected_submission integer DEFAULT NULL, p_outcome_kind text DEFAULT NULL,
    p_team_a_game_wins integer DEFAULT NULL, p_team_b_game_wins integer DEFAULT NULL,
    p_awarded_winner_side text DEFAULT NULL)
RETURNS integer LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, dl, pg_temp AS $$
DECLARE u integer := require_writer(); ctx record; m dl.match;
        report dl.result_submission; decision integer;
BEGIN
    SELECT * INTO ctx FROM tournament_result_context(p_match);
    IF NOT FOUND THEN
        RAISE SQLSTATE 'PT404' USING MESSAGE = 'match_not_found';
    END IF;
    m := ctx.match_row;
    u := tournament_commissioner(ctx.league_id);
    IF m.revision IS DISTINCT FROM p_revision THEN
        RAISE SQLSTATE 'PT409' USING MESSAGE = 'stale_revision';
    END IF;
    IF m.current_decision_id IS NOT NULL
       OR EXISTS (SELECT FROM match_disposition WHERE match_id = p_match) THEN
        RAISE SQLSTATE 'PT409' USING MESSAGE = 'match_not_open';
    END IF;
    IF p_reason IS NULL OR btrim(p_reason) = '' THEN
        RAISE SQLSTATE 'PT400' USING MESSAGE = 'reason_required';
    END IF;
    IF p_selected_submission IS NOT NULL THEN
        IF p_outcome_kind IS NOT NULL OR p_team_a_game_wins IS NOT NULL
           OR p_team_b_game_wins IS NOT NULL OR p_awarded_winner_side IS NOT NULL THEN
            RAISE SQLSTATE 'PT400' USING MESSAGE = 'select_report_or_supply_result';
        END IF;
        IF p_selected_submission = m.team_a_submission_id THEN
            report := tournament_current_report(p_match, p_selected_submission,
                'a', ctx.manager_a, ctx.wins_required);
        ELSIF p_selected_submission = m.team_b_submission_id THEN
            report := tournament_current_report(p_match, p_selected_submission,
                'b', ctx.manager_b, ctx.wins_required);
        ELSE
            RAISE SQLSTATE 'PT409' USING MESSAGE = 'submission_not_current';
        END IF;
    ELSE
        PERFORM tournament_validate_result(ctx.wins_required, p_outcome_kind,
            p_team_a_game_wins, p_team_b_game_wins, p_awarded_winner_side);
    END IF;
    PERFORM tournament_bump(ctx.league_id);
    UPDATE match SET revision = revision + 1 WHERE id = p_match;
    decision := tournament_append_commissioner_result(p_match, u, p_reason,
        p_outcome_kind, p_team_a_game_wins, p_team_b_game_wins,
        p_awarded_winner_side, p_selected_submission);
    PERFORM notify_league_accounts(ctx.league_id);
    RETURN decision;
END $$;

CREATE FUNCTION dl.correct_result(p_match integer, p_revision integer, p_reason text,
    p_outcome_kind text, p_team_a_game_wins integer DEFAULT NULL,
    p_team_b_game_wins integer DEFAULT NULL, p_awarded_winner_side text DEFAULT NULL)
RETURNS integer LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, dl, pg_temp AS $$
DECLARE u integer := require_writer(); ctx record; m dl.match;
        previous dl.result_submission; decision integer;
BEGIN
    SELECT * INTO ctx FROM tournament_result_context(p_match);
    IF NOT FOUND THEN
        RAISE SQLSTATE 'PT404' USING MESSAGE = 'match_not_found';
    END IF;
    m := ctx.match_row;
    u := tournament_commissioner(ctx.league_id);
    IF m.revision IS DISTINCT FROM p_revision THEN
        RAISE SQLSTATE 'PT409' USING MESSAGE = 'stale_revision';
    END IF;
    IF m.current_decision_id IS NULL THEN
        RAISE SQLSTATE 'PT409' USING MESSAGE = 'official_result_required';
    END IF;
    IF p_reason IS NULL OR btrim(p_reason) = '' THEN
        RAISE SQLSTATE 'PT400' USING MESSAGE = 'reason_required';
    END IF;
    PERFORM tournament_validate_result(ctx.wins_required, p_outcome_kind,
        p_team_a_game_wins, p_team_b_game_wins, p_awarded_winner_side);
    SELECT s.* INTO STRICT previous FROM match_decision d
    JOIN result_submission s ON s.id = d.selected_submission_id AND s.match_id = d.match_id
    WHERE d.id = m.current_decision_id AND d.match_id = p_match;
    IF ROW(previous.outcome_kind, previous.team_a_game_wins,
           previous.team_b_game_wins, previous.awarded_winner_side)
       IS NOT DISTINCT FROM ROW(p_outcome_kind, p_team_a_game_wins,
                                p_team_b_game_wins, p_awarded_winner_side) THEN
        RETURN m.current_decision_id;
    END IF;
    -- Excluded official results remain correctable factual history. No operation
    -- here edits their disposition, certified qualifiers or frozen participants.
    PERFORM tournament_bump(ctx.league_id);
    UPDATE match SET revision = revision + 1 WHERE id = p_match;
    decision := tournament_append_commissioner_result(p_match, u, p_reason,
        p_outcome_kind, p_team_a_game_wins, p_team_b_game_wins, p_awarded_winner_side);
    PERFORM notify_league_accounts(ctx.league_id);
    RETURN decision;
END $$;

CREATE FUNCTION dl.set_match_vod(p_match integer, p_revision integer, p_vod_url text)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, dl, pg_temp AS $$
DECLARE u integer := require_writer(); ctx record; m dl.match;
BEGIN
    SELECT * INTO ctx FROM tournament_result_context(p_match);
    IF NOT FOUND THEN
        RAISE SQLSTATE 'PT404' USING MESSAGE = 'match_not_found';
    END IF;
    m := ctx.match_row;
    IF u <> ctx.manager_a AND u <> ctx.manager_b
       AND NOT EXISTS (SELECT FROM league
                       WHERE id = ctx.league_id AND commissioner_user_id = u) THEN
        RAISE SQLSTATE 'PT409' USING MESSAGE = 'action_not_permitted';
    END IF;
    IF m.revision IS DISTINCT FROM p_revision THEN
        RAISE SQLSTATE 'PT409' USING MESSAGE = 'stale_revision';
    END IF;
    -- NULL removes the link. Require an absolute HTTP(S) link with a host;
    -- reject credentials, whitespace/control characters and browser backslashes.
    IF p_vod_url IS NOT NULL AND (length(p_vod_url) > 4096
       OR p_vod_url !~* '^https?://([a-z0-9]([a-z0-9.-]*[a-z0-9])?|\[[0-9a-f:.]+\])(:[0-9]{1,5})?([/?#][^[:space:][:cntrl:]]*)?$'
       OR strpos(p_vod_url, chr(92)) > 0) THEN
        RAISE SQLSTATE 'PT400' USING MESSAGE = 'invalid_vod_url';
    END IF;
    IF m.vod_url IS NOT DISTINCT FROM p_vod_url THEN RETURN; END IF;
    UPDATE match SET vod_url = p_vod_url, revision = revision + 1 WHERE id = p_match;
    PERFORM notify_league_accounts(ctx.league_id);
END $$;

REVOKE ALL ON FUNCTION
    dl.tournament_result_context(integer),
    dl.tournament_validate_result(integer,text,integer,integer,text),
    dl.tournament_current_report(integer,integer,text,integer,integer),
    dl.tournament_append_commissioner_result(integer,integer,text,text,integer,integer,text,integer),
    dl.submit_result(integer,integer,text,integer,integer,text),
    dl.record_result(integer,integer,text,integer,text,integer,integer,text),
    dl.correct_result(integer,integer,text,text,integer,integer,text),
    dl.set_match_vod(integer,integer,text)
FROM PUBLIC;
