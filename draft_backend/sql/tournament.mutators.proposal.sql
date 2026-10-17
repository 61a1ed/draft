-- REVIEW ONLY. Install as dl_owner after schema.sql and tournament.proposal.sql,
-- in the SAME transaction as the other tournament proposal files and grants.
-- The HTTP RPC allowlist and normal bootstrap deliberately do not load these.
-- All calls use SERIALIZABLE and whole-transaction retries with original inputs.

-- Private shared helpers. Entry points authenticate even when an edit is a no-op.
CREATE FUNCTION dl.tournament_commissioner(p_league integer) RETURNS integer
LANGUAGE plpgsql SET search_path = pg_catalog, dl, pg_temp AS $$
DECLARE u integer := require_writer();
BEGIN
    IF NOT EXISTS (SELECT FROM league WHERE id = p_league) THEN
        RAISE SQLSTATE 'PT404' USING MESSAGE = 'league_not_found';
    END IF;
    IF NOT EXISTS (SELECT FROM league WHERE id = p_league AND commissioner_user_id = u) THEN
        RAISE SQLSTATE 'PT409' USING MESSAGE = 'action_not_permitted';
    END IF;
    RETURN u;
END $$;

CREATE FUNCTION dl.tournament_check_revision(p_league integer, p_revision integer)
RETURNS void LANGUAGE plpgsql SET search_path = pg_catalog, dl, pg_temp AS $$
DECLARE rev integer;
BEGIN
    SELECT revision INTO rev FROM tournament WHERE league_id = p_league;
    IF NOT FOUND THEN RAISE SQLSTATE 'PT404' USING MESSAGE = 'tournament_not_found'; END IF;
    IF rev IS DISTINCT FROM p_revision THEN
        RAISE SQLSTATE 'PT409' USING MESSAGE = 'stale_revision';
    END IF;
END $$;

CREATE FUNCTION dl.tournament_bump(p_league integer) RETURNS void
LANGUAGE sql SET search_path = pg_catalog, dl, pg_temp AS $$
    UPDATE tournament SET revision = revision + 1 WHERE league_id = p_league
$$;

CREATE FUNCTION dl.tournament_validate_zone(p_zone text) RETURNS void
LANGUAGE plpgsql SET search_path = pg_catalog, dl, pg_temp AS $$
BEGIN
    IF p_zone IS NULL OR NOT EXISTS (SELECT FROM pg_timezone_names WHERE name = p_zone) THEN
        RAISE SQLSTATE 'PT400' USING MESSAGE = 'invalid_time_zone';
    END IF;
END $$;

CREATE FUNCTION dl.tournament_validate_availability_window(p_starts_at timestamptz, p_ends_at timestamptz)
RETURNS void LANGUAGE plpgsql SET search_path = pg_catalog, dl, pg_temp AS $$
BEGIN
    IF p_starts_at IS NULL OR p_ends_at IS NULL OR NOT isfinite(p_starts_at)
       OR NOT isfinite(p_ends_at) OR p_starts_at >= p_ends_at THEN
        RAISE SQLSTATE 'PT400' USING MESSAGE = 'invalid_availability_window';
    END IF;
END $$;

-- One atomic constructor for the implemented round-robin/two-team-final format.
-- The tables remain reusable; this operation does not accept a configurable graph.
-- Freshness is the completed draft's LEAGUE revision. Later operations use the
-- independently revisioned tournament created here, initially at revision zero.
CREATE FUNCTION dl.publish_tournament(p_league integer, p_league_revision integer,
    p_first_suggested_date date, p_season_best_of integer DEFAULT 3,
    p_final_best_of integer DEFAULT 5)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, dl, pg_temp AS $$
DECLARE u integer := tournament_commissioner(p_league); rev integer;
        first_stage integer;
        ring integer[]; n integer; round_no integer; pair_no integer; round_id integer;
BEGIN
    SELECT revision INTO rev FROM league WHERE id = p_league;
    IF rev IS DISTINCT FROM p_league_revision THEN
        RAISE SQLSTATE 'PT409' USING MESSAGE = 'stale_revision';
    END IF;
    IF EXISTS (SELECT FROM tournament WHERE league_id = p_league) THEN
        RAISE SQLSTATE 'PT409' USING MESSAGE = 'tournament_already_published';
    END IF;
    IF NOT EXISTS (SELECT FROM draft_state WHERE league_id = p_league AND status = 'completed'
                   AND team_count >= 2) THEN
        RAISE SQLSTATE 'PT409' USING MESSAGE = 'draft_not_completed';
    END IF;
    IF p_first_suggested_date IS NULL OR NOT isfinite(p_first_suggested_date) THEN
        RAISE SQLSTATE 'PT400' USING MESSAGE = 'invalid_suggested_date';
    END IF;
    IF p_season_best_of IS NULL OR p_season_best_of < 1 OR p_season_best_of % 2 <> 1
       OR p_final_best_of IS NULL OR p_final_best_of < 1 OR p_final_best_of % 2 <> 1 THEN
        RAISE SQLSTATE 'PT400' USING MESSAGE = 'invalid_best_of';
    END IF;
    -- Concurrent publication either serializes or retries into the already
    -- published check; never return a raw uniqueness error for the losing caller.
    INSERT INTO tournament (league_id, published_by) VALUES (p_league, u)
    ON CONFLICT (league_id) DO NOTHING;
    IF NOT FOUND THEN
        RAISE SQLSTATE 'PT409' USING MESSAGE = 'tournament_already_published';
    END IF;
    INSERT INTO tournament_stage (tournament_id, position, name, format, best_of)
    VALUES (p_league, 1, 'Season', 'round_robin', p_season_best_of)
    RETURNING id INTO first_stage;
    INSERT INTO tournament_stage (tournament_id, position, name, format, best_of,
                                  source_stage_id, qualifier_count)
    VALUES (p_league, 2, 'Final', 'single_elimination', p_final_best_of, first_stage, 2);
    INSERT INTO stage_entry (stage_id, team_id, entered_by)
    SELECT first_stage, id, u FROM team WHERE league_id = p_league;
    SELECT array_agg(id ORDER BY draft_position, id) INTO ring FROM team WHERE league_id = p_league;
    n := cardinality(ring);
    IF n % 2 = 1 THEN ring := array_append(ring, NULL::integer); n := n + 1; END IF;
    FOR round_no IN 1..n - 1 LOOP
        INSERT INTO tournament_round (stage_id, number, created_by, suggested_start_date)
        VALUES (first_stage, round_no, u, p_first_suggested_date + (round_no - 1) * 7)
        RETURNING id INTO round_id;
        FOR pair_no IN 1..n / 2 LOOP
            IF ring[pair_no] IS NOT NULL AND ring[n - pair_no + 1] IS NOT NULL THEN
                INSERT INTO match (round_id, team_a_id, team_b_id)
                VALUES (round_id, ring[pair_no], ring[n - pair_no + 1]);
            END IF;
        END LOOP;
        ring := ARRAY[ring[1], ring[n]] || ring[2:n - 1];
    END LOOP;
    PERFORM notify_league_accounts(p_league);
END $$;

-- A date edit changes the UI hint only; NULL clears it. Reports never consult it.
CREATE FUNCTION dl.set_round_suggested_date(p_round integer, p_revision integer,
    p_suggested_start_date date)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, dl, pg_temp AS $$
DECLARE league_id integer;
BEGIN
    PERFORM require_writer();
    SELECT s.tournament_id INTO league_id FROM tournament_round r
    JOIN tournament_stage s ON s.id = r.stage_id WHERE r.id = p_round;
    PERFORM tournament_commissioner(league_id);
    PERFORM tournament_check_revision(league_id, p_revision);
    IF p_suggested_start_date IS NOT NULL AND NOT isfinite(p_suggested_start_date) THEN
        RAISE SQLSTATE 'PT400' USING MESSAGE = 'invalid_suggested_date';
    END IF;
    PERFORM tournament_bump(league_id);
    UPDATE tournament_round SET suggested_start_date = p_suggested_start_date WHERE id = p_round;
    PERFORM notify_league_accounts(league_id);
END $$;

CREATE FUNCTION dl.tournament_availability_owner(p_team integer) RETURNS integer
LANGUAGE plpgsql SET search_path = pg_catalog, dl, pg_temp AS $$
DECLARE u integer := require_writer(); t dl.team;
BEGIN
    SELECT * INTO t FROM team WHERE id = p_team;
    IF NOT FOUND THEN
        RAISE SQLSTATE 'PT404' USING MESSAGE = 'team_not_found';
    END IF;
    IF t.manager_user_id <> u THEN
        RAISE SQLSTATE 'PT409' USING MESSAGE = 'action_not_permitted';
    END IF;
    RETURN t.league_id;
END $$;

-- Atomic full replacement. [] clears windows but keeps a revisioned header.
-- JSON endpoints must be ISO timestamps with an explicit Z or +/-HH:MM offset;
-- they are actual instants, never reinterpreted when the display zone changes.
CREATE FUNCTION dl.set_team_availability(p_team integer, p_revision integer, p_time_zone text,
    p_display_step_minutes integer, p_windows jsonb)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, dl, pg_temp AS $$
DECLARE league_id integer := tournament_availability_owner(p_team); rev integer;
        window_value jsonb; starts timestamptz; ends timestamptz;
BEGIN
    PERFORM tournament_validate_zone(p_time_zone);
    IF p_display_step_minutes IS NULL OR p_display_step_minutes <= 0
       OR jsonb_typeof(p_windows) IS DISTINCT FROM 'array' THEN
        RAISE SQLSTATE 'PT400' USING MESSAGE = 'invalid_availability';
    END IF;
    SELECT revision INTO rev FROM team_availability WHERE team_id = p_team;
    IF rev IS DISTINCT FROM p_revision THEN
        RAISE SQLSTATE 'PT409' USING MESSAGE = 'stale_revision';
    END IF;
    IF rev IS NULL THEN
        INSERT INTO team_availability (team_id, time_zone, display_step_minutes)
        VALUES (p_team, p_time_zone, p_display_step_minutes)
        ON CONFLICT (team_id) DO NOTHING;
        IF NOT FOUND THEN RAISE SQLSTATE 'PT409' USING MESSAGE = 'stale_revision'; END IF;
    ELSE
        UPDATE team_availability SET time_zone = p_time_zone,
            display_step_minutes = p_display_step_minutes, revision = revision + 1 WHERE team_id = p_team;
    END IF;
    DELETE FROM availability_window WHERE team_id = p_team;
    FOR window_value IN SELECT value FROM jsonb_array_elements(p_windows) LOOP
        IF jsonb_typeof(window_value) IS DISTINCT FROM 'object' THEN
            RAISE SQLSTATE 'PT400' USING MESSAGE = 'invalid_availability';
        END IF;
        IF (SELECT count(*) FROM jsonb_object_keys(window_value)) <> 2
           OR jsonb_typeof(window_value -> 'starts_at') IS DISTINCT FROM 'string'
           OR jsonb_typeof(window_value -> 'ends_at') IS DISTINCT FROM 'string'
           OR (window_value ->> 'starts_at') !~* '^[0-9]{4}-[0-9]{2}-[0-9]{2}[T ][0-9]{2}:[0-9]{2}(:[0-9]{2}(\.[0-9]+)?)?(Z|[+-][0-9]{2}:[0-9]{2})$'
           OR (window_value ->> 'ends_at') !~* '^[0-9]{4}-[0-9]{2}-[0-9]{2}[T ][0-9]{2}:[0-9]{2}(:[0-9]{2}(\.[0-9]+)?)?(Z|[+-][0-9]{2}:[0-9]{2})$' THEN
            RAISE SQLSTATE 'PT400' USING MESSAGE = 'invalid_availability';
        END IF;
        BEGIN
            starts := (window_value ->> 'starts_at')::timestamptz;
            ends := (window_value ->> 'ends_at')::timestamptz;
        EXCEPTION WHEN invalid_datetime_format OR datetime_field_overflow THEN
            RAISE SQLSTATE 'PT400' USING MESSAGE = 'invalid_availability';
        END;
        PERFORM tournament_validate_availability_window(starts, ends);
        INSERT INTO availability_window (team_id, starts_at, ends_at)
        VALUES (p_team, starts, ends) ON CONFLICT (team_id, starts_at, ends_at) DO NOTHING;
    END LOOP;
    PERFORM notify_league_accounts(league_id);
END $$;

-- Copy the intersection with [source local midnight, source+7 days) into the
-- target week, retaining each local wall time and adding to existing windows.
-- Gaps are rejected atomically; overlaps are allowed and exact duplicates fold.
CREATE FUNCTION dl.copy_team_availability_week(p_team integer, p_revision integer,
    p_source date, p_target date)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, dl, pg_temp AS $$
DECLARE league_id integer := tournament_availability_owner(p_team); a dl.team_availability;
        source_start timestamptz; source_end timestamptz; w record;
        local_start timestamp; local_end timestamp; starts timestamptz; ends timestamptz;
BEGIN
    SELECT * INTO a FROM team_availability WHERE team_id = p_team;
    IF NOT FOUND THEN RAISE SQLSTATE 'PT404' USING MESSAGE = 'availability_not_found'; END IF;
    IF a.revision IS DISTINCT FROM p_revision THEN
        RAISE SQLSTATE 'PT409' USING MESSAGE = 'stale_revision';
    END IF;
    IF p_source IS NULL OR p_target IS NULL OR NOT isfinite(p_source) OR NOT isfinite(p_target) THEN
        RAISE SQLSTATE 'PT400' USING MESSAGE = 'invalid_availability_week';
    END IF;
    source_start := p_source::timestamp AT TIME ZONE a.time_zone;
    source_end := (p_source + 7)::timestamp AT TIME ZONE a.time_zone;
    UPDATE team_availability SET revision = revision + 1 WHERE team_id = p_team;
    FOR w IN SELECT greatest(starts_at, source_start) AS starts_at,
                    least(ends_at, source_end) AS ends_at
             FROM availability_window WHERE team_id = p_team
             AND starts_at < source_end AND ends_at > source_start LOOP
        local_start := (w.starts_at AT TIME ZONE a.time_zone) + (p_target - p_source) * interval '1 day';
        local_end := (w.ends_at AT TIME ZONE a.time_zone) + (p_target - p_source) * interval '1 day';
        starts := local_start AT TIME ZONE a.time_zone;
        ends := local_end AT TIME ZONE a.time_zone;
        IF starts AT TIME ZONE a.time_zone <> local_start OR ends AT TIME ZONE a.time_zone <> local_end THEN
            RAISE SQLSTATE 'PT400' USING MESSAGE = 'nonexistent_local_boundary';
        END IF;
        PERFORM tournament_validate_availability_window(starts, ends);
        INSERT INTO availability_window (team_id, starts_at, ends_at)
        VALUES (p_team, starts, ends) ON CONFLICT (team_id, starts_at, ends_at) DO NOTHING;
    END LOOP;
    PERFORM notify_league_accounts(league_id);
END $$;

-- The draft action view also prevents undo after tournament publication.
-- Its predicate read and publication run in SERIALIZABLE transactions.
CREATE OR REPLACE VIEW dl.permitted_action WITH (security_invoker = true) AS
SELECT s.league_id, s.revision, l.commissioner_user_id AS actor_user_id,
       'configure'::text AS kind, NULL::integer AS pick_number, NULL::integer AS target_pick_id
FROM dl.draft_state s JOIN dl.league l ON l.id = s.league_id
WHERE s.status = 'setup'
UNION ALL
SELECT s.league_id, s.revision, l.commissioner_user_id, 'start', NULL::integer, NULL::integer
FROM dl.draft_state s JOIN dl.league l ON l.id = s.league_id WHERE s.can_start
UNION ALL
SELECT DISTINCT s.league_id, s.revision, a.actor, 'pick', s.next_pick_number, NULL::integer
FROM dl.draft_state s JOIN dl.league l ON l.id = s.league_id
JOIN dl.team t ON t.id = s.next_team_id
CROSS JOIN LATERAL (VALUES (l.commissioner_user_id), (t.manager_user_id)) a(actor)
WHERE s.status = 'drafting'
UNION ALL
SELECT s.league_id, s.revision, l.commissioner_user_id, 'undo', NULL::integer, s.last_pick_id
FROM dl.draft_state s JOIN dl.league l ON l.id = s.league_id
WHERE s.status <> 'setup' AND s.last_pick_id IS NOT NULL AND NOT EXISTS (
    SELECT FROM dl.tournament WHERE league_id = s.league_id
);

-- Owner default privileges keep new functions private until explicitly granted.
