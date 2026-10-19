-- Assigned teams may wait for their manager to choose a name and color.
-- Existing teams retain their names and receive black; future teams start pending.
ALTER TABLE dl.team ALTER COLUMN name DROP NOT NULL;
ALTER TABLE dl.team DROP CONSTRAINT team_name_check;
ALTER TABLE dl.team ADD CONSTRAINT team_name_check CHECK (name ~ '[^[:space:]]');
ALTER TABLE dl.team ADD COLUMN color_rgb integer
    CHECK (color_rgb BETWEEN 0 AND 16777215);
UPDATE dl.team SET color_rgb = 0;

COMMENT ON COLUMN dl.team.color_rgb
IS '24-bit RGB packed as 0xRRGGBB. NULL means no color chosen; 0 is black.';

-- A public, read-only permission predicate shared by snapshots and the writer.
-- EXISTS returns false for anonymous requests and nonexistent teams.
CREATE FUNCTION dl.can_set_team_metadata(p_team integer) RETURNS boolean
LANGUAGE sql STABLE SET search_path = pg_catalog, dl, pg_temp AS $$
    SELECT EXISTS (
        SELECT FROM team t JOIN league l ON l.id = t.league_id
        WHERE t.id = p_team AND l.started_at IS NULL
          AND (t.manager_user_id = user_id() OR l.commissioner_user_id = user_id())
    )
$$;
GRANT EXECUTE ON FUNCTION dl.can_set_team_metadata(integer) TO PUBLIC;

-- Retain the established commissioner-only administration signatures. A NULL
-- name leaves the team pending; changing its manager or budget preserves color.
CREATE OR REPLACE FUNCTION dl.add_team(p_league integer, p_revision integer, p_name text,
    p_manager integer, p_point_budget integer)
RETURNS integer LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, dl, pg_temp AS $$
DECLARE result integer;
BEGIN
    PERFORM admit(p_league, p_revision, 'configure');
    INSERT INTO team (league_id, name, manager_user_id, point_budget)
    VALUES (p_league, regexp_replace(p_name, '^[[:space:]]+|[[:space:]]+$', '', 'g'),
            p_manager, p_point_budget) RETURNING id INTO result;
    PERFORM bump(p_league);
    RETURN result;
END $$;

CREATE OR REPLACE FUNCTION dl.update_team(p_league integer, p_revision integer, p_team integer,
    p_name text, p_manager integer, p_point_budget integer)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, dl, pg_temp AS $$
BEGIN
    PERFORM admit(p_league, p_revision, 'configure');
    UPDATE team SET name = regexp_replace(p_name, '^[[:space:]]+|[[:space:]]+$', '', 'g'),
        manager_user_id = p_manager,
        point_budget = p_point_budget
    WHERE id = p_team AND league_id = p_league;
    IF NOT FOUND THEN RAISE SQLSTATE 'PT404' USING MESSAGE = 'team_not_found'; END IF;
    PERFORM bump(p_league);
END $$;

-- Both fields are supplied together. Repeated choices and commissioner
-- corrections are permitted until draft start; there is no submission flag.
CREATE FUNCTION dl.set_team_metadata(p_league integer, p_revision integer, p_team integer,
    p_name text, p_color_rgb integer)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, dl, pg_temp AS $$
DECLARE rev integer;
        normalized_name text := regexp_replace(p_name, '^[[:space:]]+|[[:space:]]+$', '', 'g');
BEGIN
    PERFORM require_writer();
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
REVOKE ALL ON FUNCTION dl.set_team_metadata(integer, integer, integer, text, integer)
FROM PUBLIC;
GRANT EXECUTE ON FUNCTION dl.set_team_metadata(integer, integer, integer, text, integer)
TO dl_api;

-- Metadata joins the existing prerequisites for draft start. The projection
-- retains its shape, so existing draft and tournament permissions still apply.
CREATE OR REPLACE VIEW dl.draft_state WITH (security_invoker = true) AS
WITH counts AS (
    SELECT l.*, t.n AS team_count, p.n AS active_pick_count,
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
        SELECT count(*)::integer n FROM dl.active_pick WHERE league_id = l.id
    ) p
), state AS (
    SELECT *,
        CASE WHEN started_at IS NULL THEN 'setup'
             WHEN active_pick_count = required_pick_count THEN 'completed'
             ELSE 'drafting' END AS status,
        CASE WHEN started_at IS NOT NULL AND active_pick_count < required_pick_count
             THEN active_pick_count + 1 END AS next_pick_number
    FROM counts
)
SELECT s.id AS league_id, s.revision, s.status, s.roster_size, s.team_count,
       s.active_pick_count, s.required_pick_count,
       s.started_at IS NULL AND s.ready AS can_start,
       s.next_pick_number, t.id AS next_team_id,
       (SELECT id FROM dl.active_pick WHERE league_id = s.id
        ORDER BY pick_number DESC LIMIT 1) AS last_pick_id,
       CASE WHEN s.status = 'drafting'
            THEN s.turn_started_at + s.pick_seconds * interval '1 second' END AS deadline
FROM state s LEFT JOIN dl.team t
  ON t.league_id = s.id
 AND t.draft_position = dl.snake_position(s.next_pick_number, s.team_count);

-- Existing columns retain their order. league_snapshot aggregates this composite
-- row as JSON, so the new fields are included without another snapshot query.
CREATE OR REPLACE VIEW dl.team_points WITH (security_invoker = true) AS
SELECT t.id, t.league_id, t.name, t.manager_user_id, t.draft_position, t.point_budget,
       coalesce(p.spent, 0::bigint) AS points_spent,
       t.point_budget - coalesce(p.spent, 0::bigint) AS points_remaining,
       t.color_rgb,
       t.name IS NOT NULL AND t.color_rgb IS NOT NULL AS metadata_complete,
       dl.can_set_team_metadata(t.id) AS can_set_metadata
FROM dl.team t
CROSS JOIN LATERAL (
    SELECT sum(r.point_cost) AS spent FROM dl.roster r
    WHERE r.league_id = t.league_id AND r.team_id = t.id
) p;
