-- REVIEW ONLY. Install as dl_owner after schema.sql and tournament.proposal.sql,
-- in the SAME transaction as the other tournament proposal files and grants.
-- The HTTP RPC allowlist and normal bootstrap deliberately do not load these.
-- All calls use SERIALIZABLE and whole-transaction retries with original inputs.

-- Private shared helpers. Entry points authenticate even when an edit is a no-op.
CREATE FUNCTION dl.tournament_commissioner(p_league integer) RETURNS integer
LANGUAGE plpgsql SET search_path = pg_catalog, dl, pg_temp AS $$
DECLARE u integer := require_writer();
BEGIN
    IF NOT can_read(p_league) THEN
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

CREATE FUNCTION dl.tournament_validate_window(p_opens_at timestamptz, p_closes_at timestamptz)
RETURNS void LANGUAGE plpgsql SET search_path = pg_catalog, dl, pg_temp AS $$
BEGIN
    IF p_opens_at IS NULL OR p_closes_at IS NULL OR NOT isfinite(p_opens_at)
       OR NOT isfinite(p_closes_at) OR p_opens_at >= p_closes_at THEN
        RAISE SQLSTATE 'PT400' USING MESSAGE = 'invalid_playing_window';
    END IF;
END $$;

-- NULL revision means the caller saw no tournament. Creation starts at zero.
CREATE FUNCTION dl.configure_tournament(p_league integer, p_revision integer, p_time_zone text)
RETURNS integer LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, dl, pg_temp AS $$
DECLARE existing dl.tournament;
BEGIN
    PERFORM tournament_commissioner(p_league);
    PERFORM tournament_validate_zone(p_time_zone);
    SELECT * INTO existing FROM tournament WHERE league_id = p_league;
    IF FOUND THEN
        PERFORM tournament_check_revision(p_league, p_revision);
        IF existing.published_at IS NOT NULL THEN
            RAISE SQLSTATE 'PT409' USING MESSAGE = 'tournament_already_published';
        END IF;
        UPDATE tournament SET time_zone = p_time_zone, revision = revision + 1
        WHERE league_id = p_league;
    ELSE
        IF p_revision IS NOT NULL THEN
            RAISE SQLSTATE 'PT409' USING MESSAGE = 'stale_revision';
        END IF;
        -- A concurrent invisible conflicting insert raises a serialization
        -- failure; a whole-call retry then sees a stale expected NULL revision.
        INSERT INTO tournament (league_id, time_zone) VALUES (p_league, p_time_zone)
        ON CONFLICT (league_id) DO NOTHING;
        IF NOT FOUND THEN RAISE SQLSTATE 'PT409' USING MESSAGE = 'stale_revision'; END IF;
    END IF;
    PERFORM notify_league_accounts(p_league);
    RETURN p_league;
END $$;

-- Position is the stable setup key. Only the supported two-stage pipeline can
-- be configured; callers normally supply best_of 3 and 5 respectively.
CREATE FUNCTION dl.configure_stage(p_league integer, p_revision integer, p_position integer,
    p_name text, p_format text, p_best_of integer, p_source_stage integer DEFAULT NULL,
    p_qualifier_count integer DEFAULT NULL)
RETURNS integer LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, dl, pg_temp AS $$
DECLARE result integer;
BEGIN
    PERFORM tournament_commissioner(p_league);
    PERFORM tournament_check_revision(p_league, p_revision);
    IF EXISTS (SELECT FROM tournament WHERE league_id = p_league AND published_at IS NOT NULL) THEN
        RAISE SQLSTATE 'PT409' USING MESSAGE = 'tournament_already_published';
    END IF;
    IF p_position IS NULL OR p_position NOT IN (1, 2)
       OR p_name IS NULL OR btrim(p_name) = ''
       OR p_best_of IS NULL OR p_best_of < 1 OR p_best_of % 2 <> 1
       OR (p_position = 1 AND (p_format IS DISTINCT FROM 'round_robin'
           OR p_source_stage IS NOT NULL OR p_qualifier_count IS NOT NULL))
       OR (p_position = 2 AND (p_format IS DISTINCT FROM 'single_elimination'
           OR p_qualifier_count IS DISTINCT FROM 2 OR NOT EXISTS (
               SELECT FROM tournament_stage WHERE id = p_source_stage
               AND tournament_id = p_league AND position = 1 AND format = 'round_robin'))) THEN
        RAISE SQLSTATE 'PT400' USING MESSAGE = 'unsupported_tournament_pipeline';
    END IF;
    PERFORM tournament_bump(p_league);
    INSERT INTO tournament_stage (tournament_id, position, name, format, best_of,
                                 source_stage_id, qualifier_count)
    VALUES (p_league, p_position, p_name, p_format, p_best_of, p_source_stage, p_qualifier_count)
    ON CONFLICT (tournament_id, position) DO UPDATE SET
        name = EXCLUDED.name, format = EXCLUDED.format, best_of = EXCLUDED.best_of,
        source_stage_id = EXCLUDED.source_stage_id, qualifier_count = EXCLUDED.qualifier_count
    RETURNING id INTO result;
    PERFORM notify_league_accounts(p_league);
    RETURN result;
END $$;

-- Start is a LOCAL wall-clock timestamp in tournament.time_zone. Each boundary
-- is converted separately, so DST weeks can be 167/169 actual hours long.
-- Nonexistent local boundaries (spring-forward gaps) are rejected; PostgreSQL's
-- standard-time interpretation is used for ambiguous fall-back boundaries.
CREATE FUNCTION dl.publish_tournament(p_league integer, p_revision integer,
    p_starts_at timestamp without time zone)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, dl, pg_temp AS $$
DECLARE u integer := tournament_commissioner(p_league); t dl.tournament;
        first_stage dl.tournament_stage; final_stage dl.tournament_stage;
        ring integer[]; n integer; round_no integer; pair_no integer; round_id integer;
        local_start timestamp; local_end timestamp; opens timestamptz; closes timestamptz;
BEGIN
    PERFORM tournament_check_revision(p_league, p_revision);
    SELECT * INTO STRICT t FROM tournament WHERE league_id = p_league;
    IF t.published_at IS NOT NULL THEN
        RAISE SQLSTATE 'PT409' USING MESSAGE = 'tournament_already_published';
    END IF;
    IF NOT EXISTS (SELECT FROM draft_state WHERE league_id = p_league AND status = 'completed'
                   AND team_count >= 2) THEN
        RAISE SQLSTATE 'PT409' USING MESSAGE = 'draft_not_completed';
    END IF;
    PERFORM tournament_validate_zone(t.time_zone);
    IF p_starts_at IS NULL OR NOT isfinite(p_starts_at) THEN
        RAISE SQLSTATE 'PT400' USING MESSAGE = 'invalid_local_start';
    END IF;
    SELECT * INTO first_stage FROM tournament_stage WHERE tournament_id = p_league AND position = 1;
    SELECT * INTO final_stage FROM tournament_stage WHERE tournament_id = p_league AND position = 2;
    IF (SELECT count(*) FROM tournament_stage WHERE tournament_id = p_league) <> 2
       OR first_stage.format IS DISTINCT FROM 'round_robin'
       OR first_stage.source_stage_id IS NOT NULL OR first_stage.qualifier_count IS NOT NULL
       OR final_stage.format IS DISTINCT FROM 'single_elimination'
       OR final_stage.source_stage_id IS DISTINCT FROM first_stage.id
       OR final_stage.qualifier_count IS DISTINCT FROM 2 THEN
        RAISE SQLSTATE 'PT409' USING MESSAGE = 'unsupported_tournament_pipeline';
    END IF;
    IF EXISTS (SELECT FROM stage_entry e JOIN tournament_stage s ON s.id = e.stage_id
               WHERE s.tournament_id = p_league)
       OR EXISTS (SELECT FROM tournament_round r JOIN tournament_stage s ON s.id = r.stage_id
                  WHERE s.tournament_id = p_league)
       OR EXISTS (SELECT FROM tournament_stage WHERE tournament_id = p_league
                  AND standings_finalized_at IS NOT NULL) THEN
        RAISE SQLSTATE 'PT409' USING MESSAGE = 'tournament_schedule_already_exists';
    END IF;
    PERFORM tournament_bump(p_league);
    SELECT array_agg(id ORDER BY draft_position, id) INTO ring FROM team WHERE league_id = p_league;
    INSERT INTO stage_entry (stage_id, team_id, entered_by)
    SELECT first_stage.id, id, u FROM team WHERE league_id = p_league;
    n := cardinality(ring);
    IF n % 2 = 1 THEN ring := array_append(ring, NULL::integer); n := n + 1; END IF;
    FOR round_no IN 1..n - 1 LOOP
        local_start := p_starts_at + (round_no - 1) * interval '1 week';
        local_end := p_starts_at + round_no * interval '1 week';
        opens := local_start AT TIME ZONE t.time_zone;
        closes := local_end AT TIME ZONE t.time_zone;
        IF opens AT TIME ZONE t.time_zone <> local_start
           OR closes AT TIME ZONE t.time_zone <> local_end THEN
            RAISE SQLSTATE 'PT400' USING MESSAGE = 'nonexistent_local_boundary';
        END IF;
        PERFORM tournament_validate_window(opens, closes);
        INSERT INTO tournament_round (stage_id, number, created_by, opens_at, closes_at)
        VALUES (first_stage.id, round_no, u, opens, closes) RETURNING id INTO round_id;
        FOR pair_no IN 1..n / 2 LOOP
            IF ring[pair_no] IS NOT NULL AND ring[n - pair_no + 1] IS NOT NULL THEN
                INSERT INTO match (round_id, team_a_id, team_b_id)
                VALUES (round_id, ring[pair_no], ring[n - pair_no + 1]);
            END IF;
        END LOOP;
        ring := ARRAY[ring[1], ring[n]] || ring[2:n - 1];
    END LOOP;
    UPDATE tournament SET published_at = clock_timestamp(), published_by = u WHERE league_id = p_league;
    PERFORM notify_league_accounts(p_league);
END $$;

CREATE FUNCTION dl.set_round_window(p_round integer, p_revision integer,
    p_opens_at timestamptz, p_closes_at timestamptz)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, dl, pg_temp AS $$
DECLARE league_id integer;
BEGIN
    PERFORM require_writer();
    SELECT s.tournament_id INTO league_id FROM tournament_round r
    JOIN tournament_stage s ON s.id = r.stage_id WHERE r.id = p_round;
    PERFORM tournament_commissioner(league_id);
    PERFORM tournament_check_revision(league_id, p_revision);
    PERFORM tournament_validate_window(p_opens_at, p_closes_at);
    PERFORM tournament_bump(league_id);
    UPDATE tournament_round SET opens_at = p_opens_at, closes_at = p_closes_at WHERE id = p_round;
    PERFORM notify_league_accounts(league_id);
END $$;

CREATE FUNCTION dl.tournament_availability_owner(p_team integer) RETURNS integer
LANGUAGE plpgsql SET search_path = pg_catalog, dl, pg_temp AS $$
DECLARE u integer := require_writer(); t dl.team;
BEGIN
    SELECT * INTO t FROM team WHERE id = p_team;
    IF NOT FOUND OR NOT can_read(t.league_id) THEN
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
        PERFORM tournament_validate_window(starts, ends);
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
        PERFORM tournament_validate_window(starts, ends);
        INSERT INTO availability_window (team_id, starts_at, ends_at)
        VALUES (p_team, starts, ends) ON CONFLICT (team_id, starts_at, ends_at) DO NOTHING;
    END LOOP;
    PERFORM notify_league_accounts(league_id);
END $$;

-- Read helper exposes only the caller's authorized undo eligibility, keeping
-- tournament tables private while the existing security-invoker draft view works.
-- admit() consumes this view, so the existing draft_action undo is guarded too.
-- Its predicate read and publish_tournament's draft read are in SERIALIZABLE
-- transactions: undo/publication cannot both commit from incompatible snapshots.
CREATE FUNCTION dl.tournament_draft_undo_allowed(p_league integer) RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, dl, pg_temp AS $$
    SELECT can_read(p_league) AND NOT EXISTS (
        SELECT FROM tournament WHERE league_id = p_league AND published_at IS NOT NULL)
$$;

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
WHERE s.status <> 'setup' AND s.last_pick_id IS NOT NULL AND dl.tournament_draft_undo_allowed(s.league_id);

REVOKE ALL ON ALL FUNCTIONS IN SCHEMA dl FROM PUBLIC;
