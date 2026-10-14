-- Loaded by scripts/bootstrap.py, which owns the installation transaction.
-- Run as a dedicated non-superuser schema owner; API role must be different.
-- All API writes MUST run in SERIALIZABLE transactions (checked below).
-- Retry serialization failures as whole transactions with unchanged arguments.
-- A stale revision is an application conflict, not a reason to change/retry it.
-- Read policy: commissioner and team managers see the entire league. Signed-in
-- users may read the global catalog and user display names. No invitations yet.
-- Catalog provisioning is administrative; no API writes to those tables.
CREATE SCHEMA dl;
REVOKE ALL ON SCHEMA dl FROM PUBLIC;

CREATE TABLE dl.app_user (
    id integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    issuer text NOT NULL,
    subject text NOT NULL,
    UNIQUE (issuer, subject),
    display_name text NOT NULL
);
-- Immutable content-addressed images, read publicly through the HTTP gateway.
CREATE TABLE dl.asset (
    hash text PRIMARY KEY CHECK (hash = encode(sha256(data), 'hex')),
    content_type text NOT NULL CHECK (content_type = 'image/png'),
    data bytea NOT NULL CHECK (octet_length(data) > 0)
);
CREATE TABLE dl.draftable (
    id integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name text NOT NULL CHECK (btrim(name) <> ''),
    point_cost integer NOT NULL CHECK (point_cost >= 0),
    dex integer CHECK (dex > 0),
    species text CHECK (btrim(species) <> ''),
    form text NOT NULL DEFAULT '',
    image_hash text REFERENCES dl.asset(hash),
    UNIQUE (dex, form),
    CHECK ((dex IS NULL AND species IS NULL AND image_hash IS NULL AND form = '')
        OR (dex IS NOT NULL AND species IS NOT NULL AND image_hash IS NOT NULL))
);
CREATE TABLE dl.league (
    id integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name text NOT NULL CHECK (btrim(name) <> ''),
    commissioner_user_id integer NOT NULL REFERENCES dl.app_user,
    roster_size integer NOT NULL CHECK (roster_size > 0),
    pick_seconds integer NOT NULL DEFAULT 60 CHECK (pick_seconds > 0),
    revision integer NOT NULL DEFAULT 0 CHECK (revision >= 0),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    started_at timestamptz,
    turn_started_at timestamptz,
    CHECK ((started_at IS NULL) = (turn_started_at IS NULL))
);
CREATE TABLE dl.team (
    id integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    league_id integer NOT NULL REFERENCES dl.league,
    name text NOT NULL CHECK (btrim(name) <> ''),
    manager_user_id integer NOT NULL REFERENCES dl.app_user,
    draft_position integer CHECK (draft_position > 0),
    point_budget integer NOT NULL CHECK (point_budget >= 0),
    UNIQUE (league_id, manager_user_id),
    UNIQUE (league_id, draft_position)
);
CREATE TABLE dl.pick (
    id integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    league_id integer NOT NULL REFERENCES dl.league,
    pick_number integer NOT NULL CHECK (pick_number > 0),
    draftable_id integer NOT NULL REFERENCES dl.draftable,
    selected_by integer NOT NULL REFERENCES dl.app_user,
    selected_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    undone_by integer REFERENCES dl.app_user,
    undone_at timestamptz,
    point_cost integer NOT NULL CHECK (point_cost >= 0),
    CHECK ((undone_by IS NULL) = (undone_at IS NULL))
);
CREATE UNIQUE INDEX pick_active_number_uq ON dl.pick (league_id, pick_number)
    WHERE undone_at IS NULL;
CREATE UNIQUE INDEX pick_active_draftable_uq ON dl.pick (league_id, draftable_id)
    WHERE undone_at IS NULL;
CREATE INDEX pick_history_idx ON dl.pick (league_id, id DESC);

-- Small auth/read boundary. These claims must come from the trusted gateway.
CREATE FUNCTION dl.user_id() RETURNS integer LANGUAGE sql STABLE
SET search_path = pg_catalog, dl, pg_temp AS $$
    SELECT (nullif(current_setting('request.jwt.claims', true), '')::jsonb
            ->> 'user_id')::integer
$$;
CREATE FUNCTION dl.can_read(p_league integer) RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, dl, pg_temp AS $$
    SELECT EXISTS (SELECT FROM league WHERE id = p_league
                   AND commissioner_user_id = dl.user_id())
        OR EXISTS (SELECT FROM team WHERE league_id = p_league
                   AND manager_user_id = dl.user_id())
$$;
ALTER TABLE dl.league ENABLE ROW LEVEL SECURITY;
ALTER TABLE dl.team ENABLE ROW LEVEL SECURITY;
ALTER TABLE dl.pick ENABLE ROW LEVEL SECURITY;
CREATE POLICY league_read ON dl.league FOR SELECT USING (dl.can_read(id));
CREATE POLICY team_read ON dl.team FOR SELECT USING (dl.can_read(league_id));
CREATE POLICY pick_read ON dl.pick FOR SELECT USING (dl.can_read(league_id));

CREATE FUNCTION dl.snake_position(p_pick integer, p_teams integer)
RETURNS integer LANGUAGE sql IMMUTABLE STRICT
SET search_path = pg_catalog, dl, pg_temp AS $$
    SELECT CASE WHEN p_pick > 0 AND p_teams > 0 THEN
        CASE WHEN ((p_pick - 1) / p_teams) % 2 = 0
             THEN (p_pick - 1) % p_teams + 1
             ELSE p_teams - (p_pick - 1) % p_teams END END
$$;

CREATE VIEW dl.active_pick WITH (security_invoker = true) AS
SELECT * FROM dl.pick WHERE undone_at IS NULL;

CREATE VIEW dl.draft_state WITH (security_invoker = true) AS
WITH counts AS (
    SELECT l.*, t.n AS team_count, p.n AS active_pick_count,
           t.n * l.roster_size AS required_pick_count,
           coalesce(t.n >= 2 AND t.placed = t.n AND t.lo = 1 AND t.hi = t.n
             AND (SELECT count(*) FROM dl.draftable) >= t.n * l.roster_size,
             false) AS ready
    FROM dl.league l
    CROSS JOIN LATERAL (
        SELECT count(*)::integer n, count(draft_position)::integer placed,
               min(draft_position) lo, max(draft_position) hi
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

CREATE VIEW dl.roster WITH (security_invoker = true) AS
SELECT p.league_id, t.id AS team_id, p.draftable_id,
       p.id AS pick_id, p.pick_number, p.selected_by, p.selected_at, p.point_cost
FROM dl.active_pick p
JOIN dl.draft_state s ON s.league_id = p.league_id
JOIN dl.team t ON t.league_id = p.league_id
 AND t.draft_position = dl.snake_position(p.pick_number, s.team_count);

-- Derived from active purchase prices: undo refunds points without a balance write.
CREATE VIEW dl.team_points WITH (security_invoker = true) AS
SELECT t.*, coalesce(p.spent, 0::bigint) AS points_spent,
       t.point_budget - coalesce(p.spent, 0::bigint) AS points_remaining
FROM dl.team t
CROSS JOIN LATERAL (
    SELECT sum(r.point_cost) AS spent FROM dl.roster r
    WHERE r.league_id = t.league_id AND r.team_id = t.id
) p;

CREATE VIEW dl.permitted_action WITH (security_invoker = true) AS
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
WHERE s.status <> 'setup' AND s.last_pick_id IS NOT NULL;

-- One SELECT gives a consistent frontend snapshot; catalog is fetched separately.
CREATE VIEW dl.league_snapshot WITH (security_invoker = true) AS
SELECT l.id AS league_id, l.revision, clock_timestamp() AS server_now,
       to_jsonb(l) AS league, to_jsonb(s) AS state,
       coalesce((SELECT jsonb_agg(t ORDER BY t.draft_position NULLS LAST, t.id)
                 FROM dl.team_points t WHERE t.league_id = l.id), '[]'::jsonb) AS teams,
       coalesce((SELECT jsonb_agg(r ORDER BY r.pick_number)
                 FROM dl.roster r WHERE r.league_id = l.id), '[]'::jsonb) AS rosters,
       coalesce((SELECT jsonb_agg(p ORDER BY p.id DESC)
                 FROM dl.pick p WHERE p.league_id = l.id), '[]'::jsonb) AS history,
       coalesce((SELECT jsonb_agg(a ORDER BY a.kind)
                 FROM dl.permitted_action a WHERE a.league_id = l.id
                   AND a.actor_user_id = dl.user_id()), '[]'::jsonb) AS actions
FROM dl.league l JOIN dl.draft_state s ON s.league_id = l.id;

-- Private helpers: do not grant EXECUTE to the API role.
CREATE FUNCTION dl.require_writer() RETURNS integer LANGUAGE plpgsql
SET search_path = pg_catalog, dl, pg_temp AS $$
DECLARE u integer := dl.user_id();
BEGIN
    IF current_setting('transaction_isolation') <> 'serializable' THEN
        RAISE EXCEPTION 'API writes require a SERIALIZABLE transaction';
    END IF;
    IF u IS NULL OR NOT EXISTS (SELECT FROM app_user WHERE id = u) THEN
        RAISE SQLSTATE 'PT401' USING MESSAGE = 'sign_in_required';
    END IF;
    RETURN u;
END $$;
CREATE FUNCTION dl.admit(p_league integer, p_revision integer, p_kind text)
RETURNS dl.permitted_action LANGUAGE plpgsql
SET search_path = pg_catalog, dl, pg_temp AS $$
DECLARE u integer := require_writer(); a dl.permitted_action; rev integer;
BEGIN
    IF NOT can_read(p_league) THEN
        RAISE SQLSTATE 'PT404' USING MESSAGE = 'league_not_found';
    END IF;
    SELECT revision INTO rev FROM league WHERE id = p_league;
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
CREATE FUNCTION dl.bump(p_league integer) RETURNS void LANGUAGE sql
SET search_path = pg_catalog, dl, pg_temp AS $$
    UPDATE league SET revision = revision + 1 WHERE id = p_league
$$;

-- Six setup operations. Configuration (including names/managers) freezes at start.
CREATE FUNCTION dl.create_league(p_name text, p_roster_size integer, p_pick_seconds integer DEFAULT 60)
RETURNS integer LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, dl, pg_temp AS $$
DECLARE u integer := require_writer(); result integer;
BEGIN
    INSERT INTO league (name, commissioner_user_id, roster_size, pick_seconds)
    VALUES (p_name, u, p_roster_size, p_pick_seconds) RETURNING id INTO result;
    RETURN result;
END $$;
CREATE FUNCTION dl.update_league(p_league integer, p_revision integer, p_name text,
    p_roster_size integer, p_pick_seconds integer)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, dl, pg_temp AS $$
BEGIN
    PERFORM admit(p_league, p_revision, 'configure');
    UPDATE league SET name = p_name, roster_size = p_roster_size,
        pick_seconds = p_pick_seconds, revision = revision + 1 WHERE id = p_league;
END $$;
CREATE FUNCTION dl.add_team(p_league integer, p_revision integer, p_name text,
    p_manager integer, p_point_budget integer)
RETURNS integer LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, dl, pg_temp AS $$
DECLARE result integer;
BEGIN
    PERFORM admit(p_league, p_revision, 'configure');
    INSERT INTO team (league_id, name, manager_user_id, point_budget)
    VALUES (p_league, p_name, p_manager, p_point_budget) RETURNING id INTO result;
    PERFORM bump(p_league);
    RETURN result;
END $$;
CREATE FUNCTION dl.update_team(p_league integer, p_revision integer, p_team integer,
    p_name text, p_manager integer, p_point_budget integer)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, dl, pg_temp AS $$
BEGIN
    PERFORM admit(p_league, p_revision, 'configure');
    UPDATE team SET name = p_name, manager_user_id = p_manager,
        point_budget = p_point_budget
    WHERE id = p_team AND league_id = p_league;
    IF NOT FOUND THEN RAISE SQLSTATE 'PT404' USING MESSAGE = 'team_not_found'; END IF;
    PERFORM bump(p_league);
END $$;
CREATE FUNCTION dl.remove_team(p_league integer, p_revision integer, p_team integer)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, dl, pg_temp AS $$
BEGIN
    PERFORM admit(p_league, p_revision, 'configure');
    DELETE FROM team WHERE id = p_team AND league_id = p_league;
    IF NOT FOUND THEN RAISE SQLSTATE 'PT404' USING MESSAGE = 'team_not_found'; END IF;
    PERFORM bump(p_league);
END $$;
CREATE FUNCTION dl.set_draft_order(p_league integer, p_revision integer, p_teams integer[])
RETURNS void LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, dl, pg_temp AS $$
DECLARE actual integer[]; supplied integer[];
BEGIN
    PERFORM admit(p_league, p_revision, 'configure');
    SELECT array_agg(id ORDER BY id) INTO actual FROM team WHERE league_id = p_league;
    SELECT array_agg(id ORDER BY id) INTO supplied FROM unnest(p_teams) AS x(id);
    IF array_ndims(p_teams) IS DISTINCT FROM 1 OR coalesce(cardinality(actual), 0) < 2
       OR supplied IS DISTINCT FROM actual THEN
        RAISE SQLSTATE 'PT400' USING MESSAGE = 'order_must_contain_every_team_once';
    END IF;
    UPDATE team SET draft_position = NULL WHERE league_id = p_league;
    UPDATE team t SET draft_position = x.ord::integer
    FROM unnest(p_teams) WITH ORDINALITY AS x(id, ord)
    WHERE t.league_id = p_league AND t.id = x.id;
    PERFORM bump(p_league);
END $$;

-- Live draft: start / pick / undo. Revision identifies the precise viewed turn.
-- Returns the selected/undone pick ID, or NULL for start. New revision = old + 1.
-- Indexed catalog lookup + active roster total validate spending; no catalog cross product.
-- FK + partial unique indexes validate claims; permitted_action handles the turn.
CREATE FUNCTION dl.draft_action(p_league integer, p_revision integer, p_kind text,
    p_draftable integer DEFAULT NULL, p_charge_points boolean DEFAULT true)
RETURNS integer LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, dl, pg_temp AS $$
DECLARE a dl.permitted_action; ts timestamptz; result integer;
        cost integer; remaining bigint;
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
            SELECT t.points_remaining INTO STRICT remaining
            FROM draft_state s JOIN team_points t ON t.id = s.next_team_id
            WHERE s.league_id = p_league;
            IF cost > remaining THEN
                RAISE SQLSTATE 'PT409' USING MESSAGE = 'point_budget_exceeded';
            END IF;
            INSERT INTO pick (league_id, pick_number, draftable_id, selected_by,
                              selected_at, point_cost)
            VALUES (p_league, a.pick_number, p_draftable, a.actor_user_id, ts, cost)
            RETURNING id INTO result;
        WHEN 'undo' THEN
            UPDATE pick SET undone_by = a.actor_user_id, undone_at = ts
            WHERE id = a.target_pick_id RETURNING id INTO result;
    END CASE;
    UPDATE league SET revision = revision + 1, turn_started_at = ts,
        started_at = CASE WHEN p_kind = 'start' THEN ts ELSE started_at END
    WHERE id = p_league;
    RETURN result;
END $$;

-- Every supported mutation touches league exactly once. Delivery is post-commit.
-- Relay only to authorized subscribers; reconnecting clients fetch a snapshot.
CREATE FUNCTION dl.notify_league() RETURNS trigger LANGUAGE plpgsql
SET search_path = pg_catalog, dl, pg_temp AS $$
BEGIN
    PERFORM pg_notify('dl_changed', NEW.id::text);
    RETURN NEW;
END $$;
CREATE TRIGGER league_changed AFTER INSERT OR UPDATE ON dl.league
FOR EACH ROW EXECUTE FUNCTION dl.notify_league();

REVOKE ALL ON ALL TABLES IN SCHEMA dl FROM PUBLIC;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA dl FROM PUBLIC;

-- Deployment (dl_api is a separate, already-created NOLOGIN role).
-- Configure the request transaction as SERIALIZABLE before any query.
-- configure the impersonated API role's default_transaction_isolation.
-- GRANT USAGE ON SCHEMA dl TO dl_api;
-- GRANT SELECT (id, display_name) ON dl.app_user TO dl_api;
-- GRANT SELECT ON dl.draftable, dl.league, dl.team, dl.pick,
--   dl.active_pick, dl.draft_state, dl.roster, dl.team_points,
--   dl.permitted_action, dl.league_snapshot TO dl_api;
-- GRANT EXECUTE ON FUNCTION dl.user_id(), dl.can_read(integer),
--   dl.snake_position(integer,integer),
--   dl.create_league(text,integer,integer),
--   dl.update_league(integer,integer,text,integer,integer),
--   dl.add_team(integer,integer,text,integer,integer),
--   dl.update_team(integer,integer,integer,text,integer,integer),
--   dl.remove_team(integer,integer,integer),
--   dl.set_draft_order(integer,integer,integer[]),
--   dl.draft_action(integer,integer,text,integer,boolean) TO dl_api;
-- Do NOT grant table DML, schema CREATE, or EXECUTE on the private helpers.
-- Frontend: fetch league_snapshot filtered by league_id; render deadline locally.
-- Expiry does not change permissions. Commissioner and manager can both pick.
-- Fetch draftable once; derive availability from the snapshot's active rosters.
