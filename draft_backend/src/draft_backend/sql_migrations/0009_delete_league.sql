-- Commissioner league tombstoning (soft deletion).
ALTER TABLE dl.league ADD COLUMN deleted_at timestamptz;

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
    WHERE l.deleted_at IS NULL
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

CREATE OR REPLACE FUNCTION dl.can_set_team_metadata(p_team integer) RETURNS boolean
LANGUAGE sql STABLE SET search_path = pg_catalog, dl, pg_temp AS $$
    SELECT EXISTS (
        SELECT FROM team t JOIN league l ON l.id = t.league_id
        WHERE t.id = p_team AND l.deleted_at IS NULL AND l.started_at IS NULL
          AND (t.manager_user_id = user_id() OR l.commissioner_user_id = user_id())
    )
$$;
GRANT EXECUTE ON FUNCTION dl.can_set_team_metadata(integer) TO PUBLIC;

CREATE OR REPLACE FUNCTION dl.can_unlock_team_roster(p_team integer) RETURNS boolean
LANGUAGE sql STABLE SET search_path = pg_catalog, dl, pg_temp AS $$
    SELECT EXISTS (
        SELECT FROM team t
        JOIN league l ON l.id = t.league_id
        WHERE t.id = p_team
          AND l.deleted_at IS NULL
          AND l.started_at IS NOT NULL
          AND t.roster_locked
          AND l.commissioner_user_id = user_id()
          AND NOT EXISTS (SELECT FROM tournament WHERE league_id = l.id)
    )
$$;
GRANT EXECUTE ON FUNCTION dl.can_unlock_team_roster(integer) TO PUBLIC;

CREATE OR REPLACE FUNCTION dl.admit(p_league integer, p_revision integer, p_kind text)
RETURNS dl.permitted_action LANGUAGE plpgsql
SET search_path = pg_catalog, dl, pg_temp AS $$
DECLARE u integer := require_writer(); a dl.permitted_action; rev integer;
BEGIN
    SELECT revision INTO rev FROM league WHERE id = p_league AND deleted_at IS NULL;
    IF NOT FOUND THEN
        RAISE SQLSTATE 'PT404' USING MESSAGE = 'league_not_found';
    END IF;
    IF rev IS DISTINCT FROM p_revision THEN
        RAISE SQLSTATE 'PT409' USING MESSAGE = 'stale_revision';
    END IF;
    SELECT * INTO a FROM permitted_action
    WHERE league_id = p_league AND revision = p_revision
      AND actor_user_id = u AND kind = p_kind;
    IF NOT FOUND THEN
        RAISE SQLSTATE 'PT409' USING MESSAGE = 'action_not_permitted';
    END IF;
    RETURN a;
END $$;
REVOKE ALL ON FUNCTION dl.admit(integer, integer, text) FROM PUBLIC, dl_api;

CREATE OR REPLACE FUNCTION dl.set_team_metadata(
    p_league integer,
    p_revision integer,
    p_team integer,
    p_name text,
    p_color_rgb integer
)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, dl, pg_temp AS $$
DECLARE rev integer;
        normalized_name text := regexp_replace(p_name, '^[[:space:]]+|[[:space:]]+$', '', 'g');
BEGIN
    PERFORM require_writer();
    SELECT revision INTO rev FROM league WHERE id = p_league AND deleted_at IS NULL;
    IF NOT FOUND THEN
        RAISE SQLSTATE 'PT404' USING MESSAGE = 'league_not_found';
    END IF;
    IF rev IS DISTINCT FROM p_revision THEN
        RAISE SQLSTATE 'PT409' USING MESSAGE = 'stale_revision';
    END IF;
    IF NOT EXISTS (SELECT FROM team WHERE id = p_team AND league_id = p_league) THEN
        RAISE SQLSTATE 'PT404' USING MESSAGE = 'team_not_found';
    END IF;
    IF NOT can_set_team_metadata(p_team) THEN
        RAISE SQLSTATE 'PT409' USING MESSAGE = 'action_not_permitted';
    END IF;
    IF normalized_name IS NULL OR normalized_name = '' OR p_color_rgb IS NULL
       OR p_color_rgb NOT BETWEEN 0 AND 16777215 THEN
        RAISE SQLSTATE 'PT400' USING MESSAGE = 'invalid_team_metadata';
    END IF;
    UPDATE team SET name = normalized_name, color_rgb = p_color_rgb
    WHERE id = p_team AND league_id = p_league;
    PERFORM bump(p_league);
END $$;
REVOKE ALL ON FUNCTION dl.set_team_metadata(integer, integer, integer, text, integer) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION dl.set_team_metadata(integer, integer, integer, text, integer) TO dl_api;

CREATE OR REPLACE FUNCTION dl.set_team_roster_lock(
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
    SELECT revision INTO rev FROM league WHERE id = p_league AND deleted_at IS NULL;
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

CREATE OR REPLACE FUNCTION dl.tournament_commissioner(p_league integer) RETURNS integer
LANGUAGE plpgsql SET search_path = pg_catalog, dl, pg_temp AS $$
DECLARE u integer := require_writer();
BEGIN
    IF NOT EXISTS (SELECT FROM league WHERE id = p_league AND deleted_at IS NULL) THEN
        RAISE SQLSTATE 'PT404' USING MESSAGE = 'league_not_found';
    END IF;
    IF NOT EXISTS (
        SELECT FROM league WHERE id = p_league AND deleted_at IS NULL AND commissioner_user_id = u
    ) THEN
        RAISE SQLSTATE 'PT409' USING MESSAGE = 'action_not_permitted';
    END IF;
    RETURN u;
END $$;
REVOKE ALL ON FUNCTION dl.tournament_commissioner(integer) FROM PUBLIC, dl_api;

CREATE OR REPLACE FUNCTION dl.tournament_availability_owner(p_team integer) RETURNS integer
LANGUAGE plpgsql SET search_path = pg_catalog, dl, pg_temp AS $$
DECLARE u integer := require_writer(); t dl.team;
BEGIN
    SELECT tm.* INTO t FROM team tm JOIN league l ON l.id = tm.league_id
    WHERE tm.id = p_team AND l.deleted_at IS NULL;
    IF NOT FOUND THEN
        RAISE SQLSTATE 'PT404' USING MESSAGE = 'team_not_found';
    END IF;
    IF t.manager_user_id <> u THEN
        RAISE SQLSTATE 'PT409' USING MESSAGE = 'action_not_permitted';
    END IF;
    RETURN t.league_id;
END $$;
REVOKE ALL ON FUNCTION dl.tournament_availability_owner(integer) FROM PUBLIC, dl_api;

CREATE OR REPLACE FUNCTION dl.tournament_result_context(p_match integer)
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
    JOIN league l ON l.id = t.league_id
    JOIN team a ON a.id = m.team_a_id AND a.league_id = t.league_id
    JOIN team b ON b.id = m.team_b_id AND b.league_id = t.league_id
    JOIN stage_entry ea ON ea.stage_id = s.id AND ea.team_id = a.id
    JOIN stage_entry eb ON eb.stage_id = s.id AND eb.team_id = b.id
    WHERE m.id = p_match AND l.deleted_at IS NULL
$$;
REVOKE ALL ON FUNCTION dl.tournament_result_context(integer) FROM PUBLIC, dl_api;

CREATE FUNCTION dl.delete_league(p_league integer, p_revision integer)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, dl, pg_temp AS $$
DECLARE u integer := require_writer();
        l dl.league;
BEGIN
    SELECT * INTO l FROM league WHERE id = p_league AND deleted_at IS NULL;
    IF NOT FOUND THEN
        RAISE SQLSTATE 'PT404' USING MESSAGE = 'league_not_found';
    END IF;
    IF l.commissioner_user_id <> u THEN
        RAISE SQLSTATE 'PT409' USING MESSAGE = 'action_not_permitted';
    END IF;
    IF l.revision IS DISTINCT FROM p_revision THEN
        RAISE SQLSTATE 'PT409' USING MESSAGE = 'stale_revision';
    END IF;
    UPDATE league
    SET deleted_at = clock_timestamp(),
        revision = revision + 1
    WHERE id = p_league;
END $$;
REVOKE ALL ON FUNCTION dl.delete_league(integer, integer) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION dl.delete_league(integer, integer) TO dl_api;
