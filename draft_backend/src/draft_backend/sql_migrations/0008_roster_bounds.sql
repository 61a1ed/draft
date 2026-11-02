-- Minimum and maximum roster bounds, optional-round roster locks, and minimum-roster budget protection.
ALTER TABLE dl.league ADD COLUMN min_roster_size integer;
UPDATE dl.league SET min_roster_size = roster_size WHERE min_roster_size IS NULL;
ALTER TABLE dl.league ALTER COLUMN min_roster_size SET NOT NULL;
ALTER TABLE dl.league ADD CONSTRAINT league_roster_bounds_check
    CHECK (min_roster_size > 0 AND min_roster_size <= roster_size AND roster_size <= 12);

CREATE FUNCTION dl.default_league_roster_bounds() RETURNS trigger LANGUAGE plpgsql
SET search_path = pg_catalog, dl, pg_temp AS $$
BEGIN
    IF NEW.min_roster_size IS NULL THEN
        NEW.min_roster_size := NEW.roster_size;
    END IF;
    RETURN NEW;
END $$;
REVOKE ALL ON FUNCTION dl.default_league_roster_bounds() FROM PUBLIC, dl_api;
CREATE TRIGGER league_default_roster_bounds
BEFORE INSERT OR UPDATE ON dl.league
FOR EACH ROW EXECUTE FUNCTION dl.default_league_roster_bounds();

ALTER TABLE dl.team ADD COLUMN roster_locked boolean NOT NULL DEFAULT false;

DROP FUNCTION dl.create_league(text, integer, integer);
CREATE FUNCTION dl.create_league(
    p_name text,
    p_roster_size integer,
    p_pick_seconds integer DEFAULT 60,
    p_min_roster_size integer DEFAULT NULL
)
RETURNS integer LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, dl, pg_temp AS $$
DECLARE u integer := require_writer();
        min_size integer;
        result integer;
BEGIN
    IF p_min_roster_size IS NOT NULL THEN
        IF p_min_roster_size < 6 OR p_roster_size > 12 OR p_min_roster_size > p_roster_size THEN
            RAISE SQLSTATE 'PT400' USING MESSAGE = 'invalid_roster_bounds';
        END IF;
        min_size := p_min_roster_size;
    ELSE
        IF p_roster_size IS NOT NULL AND p_roster_size > 12 THEN
            RAISE SQLSTATE 'PT400' USING MESSAGE = 'invalid_roster_bounds';
        END IF;
        min_size := CASE WHEN p_roster_size >= 6 THEN 6 ELSE 1 END;
    END IF;
    INSERT INTO league (name, commissioner_user_id, roster_size, min_roster_size, pick_seconds)
    VALUES (p_name, u, p_roster_size, min_size, p_pick_seconds) RETURNING id INTO result;
    RETURN result;
END $$;
REVOKE ALL ON FUNCTION dl.create_league(text, integer, integer, integer) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION dl.create_league(text, integer, integer, integer) TO dl_api;

DROP FUNCTION dl.update_league(integer, integer, text, integer, integer);
CREATE FUNCTION dl.update_league(
    p_league integer,
    p_revision integer,
    p_name text,
    p_roster_size integer,
    p_pick_seconds integer,
    p_min_roster_size integer DEFAULT NULL
)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, dl, pg_temp AS $$
DECLARE min_size integer;
BEGIN
    PERFORM admit(p_league, p_revision, 'configure');
    IF p_min_roster_size IS NOT NULL THEN
        IF p_min_roster_size < 6 OR p_roster_size > 12 OR p_min_roster_size > p_roster_size THEN
            RAISE SQLSTATE 'PT400' USING MESSAGE = 'invalid_roster_bounds';
        END IF;
        min_size := p_min_roster_size;
    ELSE
        IF p_roster_size IS NOT NULL AND p_roster_size > 12 THEN
            RAISE SQLSTATE 'PT400' USING MESSAGE = 'invalid_roster_bounds';
        END IF;
        min_size := CASE WHEN p_roster_size >= 6 THEN 6 ELSE 1 END;
    END IF;
    UPDATE league
    SET name = p_name,
        roster_size = p_roster_size,
        min_roster_size = min_size,
        pick_seconds = p_pick_seconds,
        revision = revision + 1
    WHERE id = p_league;
END $$;
REVOKE ALL ON FUNCTION dl.update_league(integer, integer, text, integer, integer, integer) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION dl.update_league(integer, integer, text, integer, integer, integer) TO dl_api;

CREATE OR REPLACE VIEW dl.draft_state WITH (security_invoker = true) AS
WITH counts AS (
    SELECT l.*, t.n AS team_count, p.n AS active_pick_count,
           p.max_pick_number,
           t.n * l.roster_size AS required_pick_count,
           coalesce(t.n >= 2 AND t.placed = t.n AND t.lo = 1 AND t.hi = t.n
             AND t.metadata_complete = t.n
             AND (SELECT count(*) FROM dl.draftable) >= t.n * l.roster_size,
             false) AS ready
    FROM dl.league l
    CROSS JOIN LATERAL (
        SELECT count(*)::integer n, count(draft_position)::integer placed,
               min(draft_position) lo, max(draft_position) hi,
               count(*) FILTER (WHERE name IS NOT NULL AND color_rgb IS NOT NULL)
                   AS metadata_complete
        FROM dl.team WHERE league_id = l.id
    ) t
    CROSS JOIN LATERAL (
        SELECT count(*)::integer n, coalesce(max(pick_number), 0)::integer max_pick_number
        FROM dl.active_pick WHERE league_id = l.id
    ) p
), next_turn AS (
    SELECT c.*,
        CASE WHEN c.started_at IS NOT NULL THEN (
            SELECT gs::integer
            FROM generate_series(c.max_pick_number + 1, c.required_pick_count) AS gs
            JOIN dl.team tm
              ON tm.league_id = c.id
             AND tm.draft_position = dl.snake_position(gs::integer, c.team_count)
            WHERE NOT tm.roster_locked
            ORDER BY gs
            LIMIT 1
        ) END AS next_pick_number
    FROM counts c
), state AS (
    SELECT *,
        CASE WHEN started_at IS NULL THEN 'setup'
             WHEN next_pick_number IS NULL THEN 'completed'
             ELSE 'drafting' END AS status
    FROM next_turn
)
SELECT s.id AS league_id, s.revision, s.status, s.roster_size, s.team_count,
       s.active_pick_count, s.required_pick_count,
       s.started_at IS NULL AND s.ready AS can_start,
       s.next_pick_number, t.id AS next_team_id,
       (SELECT id FROM dl.active_pick WHERE league_id = s.id
        ORDER BY pick_number DESC LIMIT 1) AS last_pick_id,
       CASE WHEN s.status = 'drafting'
            THEN s.turn_started_at + s.pick_seconds * interval '1 second' END AS deadline,
       s.min_roster_size,
       CASE WHEN s.status = 'drafting' AND t.id IS NOT NULL THEN (
           SELECT greatest(
               0::bigint,
               (t.point_budget - coalesce(tp.spent, 0::bigint)) - coalesce((
                   SELECT sum(cheapest.point_cost)
                   FROM (
                       SELECT d.point_cost
                       FROM dl.draftable d
                       WHERE NOT EXISTS (
                           SELECT 1 FROM dl.active_pick ap
                           WHERE ap.league_id = s.id AND ap.draftable_id = d.id
                       )
                       ORDER BY d.point_cost ASC, d.id ASC
                       LIMIT greatest(0, s.min_roster_size - (tp.picks + 1))
                   ) cheapest
               ), 0::bigint)
           )::integer
           FROM (
               SELECT sum(ap.point_cost) AS spent, count(*)::integer AS picks
               FROM dl.active_pick ap
               WHERE ap.league_id = s.id
                 AND dl.snake_position(ap.pick_number, s.team_count) = t.draft_position
           ) tp
       ) END AS max_spendable_points
FROM state s LEFT JOIN dl.team t
  ON t.league_id = s.id
 AND t.draft_position = dl.snake_position(s.next_pick_number, s.team_count);

CREATE FUNCTION dl.can_lock_team_roster(p_team integer) RETURNS boolean
LANGUAGE sql STABLE SET search_path = pg_catalog, dl, pg_temp AS $$
    SELECT EXISTS (
        SELECT FROM team t
        JOIN league l ON l.id = t.league_id
        JOIN draft_state s ON s.league_id = l.id
        WHERE t.id = p_team
          AND s.status = 'drafting'
          AND NOT t.roster_locked
          AND (t.manager_user_id = user_id() OR l.commissioner_user_id = user_id())
          AND (
              SELECT count(*) FROM roster r
              WHERE r.league_id = l.id AND r.team_id = t.id
          ) BETWEEN l.min_roster_size AND l.roster_size - 1
    )
$$;
GRANT EXECUTE ON FUNCTION dl.can_lock_team_roster(integer) TO PUBLIC;

CREATE FUNCTION dl.can_unlock_team_roster(p_team integer) RETURNS boolean
LANGUAGE sql STABLE SET search_path = pg_catalog, dl, pg_temp AS $$
    SELECT EXISTS (
        SELECT FROM team t
        JOIN league l ON l.id = t.league_id
        WHERE t.id = p_team
          AND l.started_at IS NOT NULL
          AND t.roster_locked
          AND l.commissioner_user_id = user_id()
          AND NOT EXISTS (SELECT FROM tournament WHERE league_id = l.id)
    )
$$;
GRANT EXECUTE ON FUNCTION dl.can_unlock_team_roster(integer) TO PUBLIC;

CREATE OR REPLACE VIEW dl.team_points WITH (security_invoker = true) AS
SELECT t.id, t.league_id, t.name, t.manager_user_id, t.draft_position, t.point_budget,
       coalesce(p.spent, 0::bigint) AS points_spent,
       t.point_budget - coalesce(p.spent, 0::bigint) AS points_remaining,
       t.color_rgb,
       t.name IS NOT NULL AND t.color_rgb IS NOT NULL AS metadata_complete,
       dl.can_set_team_metadata(t.id) AS can_set_metadata,
       t.roster_locked,
       coalesce(p.picks, 0)::integer AS pick_count,
       dl.can_lock_team_roster(t.id) AS can_lock_roster,
       dl.can_unlock_team_roster(t.id) AS can_unlock_roster
FROM dl.team t
CROSS JOIN LATERAL (
    SELECT sum(r.point_cost) AS spent, count(*)::integer AS picks
    FROM dl.roster r
    WHERE r.league_id = t.league_id AND r.team_id = t.id
) p;

CREATE FUNCTION dl.set_team_roster_lock(
    p_league integer,
    p_revision integer,
    p_team integer,
    p_locked boolean
)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, dl, pg_temp AS $$
DECLARE rev integer;
        before_team integer;
        after_team integer;
        ts timestamptz := clock_timestamp();
BEGIN
    PERFORM require_writer();
    IF p_locked IS NULL THEN
        RAISE SQLSTATE 'PT400' USING MESSAGE = 'invalid_value';
    END IF;
    SELECT revision INTO rev FROM league WHERE id = p_league;
    IF NOT FOUND THEN
        RAISE SQLSTATE 'PT404' USING MESSAGE = 'league_not_found';
    END IF;
    IF rev IS DISTINCT FROM p_revision THEN
        RAISE SQLSTATE 'PT409' USING MESSAGE = 'stale_revision';
    END IF;
    IF NOT EXISTS (SELECT FROM team WHERE id = p_team AND league_id = p_league) THEN
        RAISE SQLSTATE 'PT404' USING MESSAGE = 'team_not_found';
    END IF;
    IF p_locked THEN
        IF NOT can_lock_team_roster(p_team) THEN
            RAISE SQLSTATE 'PT409' USING MESSAGE = 'action_not_permitted';
        END IF;
    ELSE
        IF NOT can_unlock_team_roster(p_team) THEN
            RAISE SQLSTATE 'PT409' USING MESSAGE = 'action_not_permitted';
        END IF;
    END IF;
    SELECT next_team_id INTO before_team FROM draft_state WHERE league_id = p_league;
    UPDATE team SET roster_locked = p_locked WHERE id = p_team AND league_id = p_league;
    SELECT next_team_id INTO after_team FROM draft_state WHERE league_id = p_league;
    UPDATE league
    SET revision = revision + 1,
        turn_started_at = CASE WHEN before_team IS DISTINCT FROM after_team
                               THEN ts ELSE turn_started_at END
    WHERE id = p_league;
END $$;
REVOKE ALL ON FUNCTION dl.set_team_roster_lock(integer, integer, integer, boolean) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION dl.set_team_roster_lock(integer, integer, integer, boolean) TO dl_api;

CREATE OR REPLACE FUNCTION dl.draft_action(
    p_league integer,
    p_revision integer,
    p_kind text,
    p_draftable integer DEFAULT NULL,
    p_charge_points boolean DEFAULT true
)
RETURNS integer LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, dl, pg_temp AS $$
DECLARE a dl.permitted_action;
        ts timestamptz;
        result integer;
        cost integer;
        remaining bigint;
        current_picks integer;
        min_size integer;
        reserve_needed bigint;
BEGIN
    IF p_kind IS NULL OR p_kind NOT IN ('start', 'pick', 'undo')
       OR p_charge_points IS NULL OR (p_kind <> 'pick' AND NOT p_charge_points)
       OR ((p_kind = 'pick') IS DISTINCT FROM (p_draftable IS NOT NULL)) THEN
        RAISE SQLSTATE 'PT400' USING MESSAGE = 'invalid_draft_action';
    END IF;
    a := admit(p_league, p_revision, p_kind);
    IF NOT p_charge_points AND NOT EXISTS (
        SELECT FROM league WHERE id = p_league AND commissioner_user_id = a.actor_user_id
    ) THEN
        RAISE SQLSTATE 'PT409' USING MESSAGE = 'action_not_permitted';
    END IF;
    ts := clock_timestamp();
    CASE p_kind
        WHEN 'start' THEN NULL;
        WHEN 'pick' THEN
            SELECT point_cost INTO cost FROM draftable WHERE id = p_draftable;
            IF NOT FOUND THEN
                RAISE SQLSTATE 'PT400' USING MESSAGE = 'invalid_reference';
            END IF;
            -- A commissioner may explicitly make this acquisition free.
            IF NOT p_charge_points THEN cost := 0; END IF;
            -- Charge the snake-order team, even when its commissioner selects.
            SELECT t.points_remaining, t.pick_count, s.min_roster_size
            INTO STRICT remaining, current_picks, min_size
            FROM draft_state s JOIN team_points t ON t.id = s.next_team_id
            WHERE s.league_id = p_league;
            IF cost > remaining THEN
                RAISE SQLSTATE 'PT409' USING MESSAGE = 'point_budget_exceeded';
            END IF;
            IF cost > 0 AND current_picks + 1 < min_size THEN
                SELECT coalesce(sum(cheapest.point_cost), 0)
                INTO STRICT reserve_needed
                FROM (
                    SELECT d.point_cost
                    FROM draftable d
                    WHERE d.id <> p_draftable
                      AND NOT EXISTS (
                          SELECT 1 FROM active_pick p
                          WHERE p.league_id = p_league AND p.draftable_id = d.id
                      )
                    ORDER BY d.point_cost ASC, d.id ASC
                    LIMIT (min_size - (current_picks + 1))
                ) cheapest;
                IF cost > remaining - reserve_needed THEN
                    RAISE SQLSTATE 'PT409' USING MESSAGE = 'min_roster_budget_exceeded';
                END IF;
            END IF;
            INSERT INTO pick (league_id, pick_number, draftable_id, selected_by,
                              selected_at, point_cost)
            VALUES (p_league, a.pick_number, p_draftable, a.actor_user_id, ts, cost)
            RETURNING id INTO result;
        WHEN 'undo' THEN
            UPDATE pick SET undone_by = a.actor_user_id, undone_at = ts
            WHERE id = a.target_pick_id RETURNING id INTO result;
            UPDATE team t
            SET roster_locked = false
            FROM league l
            WHERE t.league_id = p_league
              AND l.id = p_league
              AND t.roster_locked
              AND (
                  SELECT count(*) FROM roster r
                  WHERE r.league_id = p_league AND r.team_id = t.id
              ) < l.min_roster_size;
    END CASE;
    UPDATE league SET revision = revision + 1, turn_started_at = ts,
        started_at = CASE WHEN p_kind = 'start' THEN ts ELSE started_at END
    WHERE id = p_league;
    RETURN result;
END $$;
