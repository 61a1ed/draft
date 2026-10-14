-- Scenario: Live snake draft
-- Run as the administrator of a disposable test database. This file is plain SQL.
-- Reload replaces only this tracked fixture; users, catalog, and other leagues survive.
BEGIN;
DO $$
BEGIN
    IF to_regclass('dl_test.marker') IS NULL THEN
        RAISE EXCEPTION 'Scenario fixtures require a marked test database';
    END IF;
    IF NOT EXISTS (SELECT FROM dl_test.marker WHERE singleton) THEN
        RAISE EXCEPTION 'Scenario fixtures require a marked test database';
    END IF;
END $$;
SET LOCAL ROLE dl_owner;
CREATE TABLE IF NOT EXISTS dl_test.scenario (
    slug text PRIMARY KEY,
    league_id integer NOT NULL UNIQUE REFERENCES dl.league(id)
);
DO $$
BEGIN
    IF (SELECT count(*) FROM dl.app_user
        WHERE google_sub IN ('test:alice', 'test:bob', 'test:carol')) <> 3 THEN
        RAISE EXCEPTION 'Scenario fixtures require alice, bob, and carol test users';
    END IF;
    IF (SELECT count(*) FROM dl.draftable) < 9 THEN
        RAISE EXCEPTION 'Scenario fixtures require at least nine catalog entries';
    END IF;
END $$;

-- IDs come from this registry, so renaming a fixture in the UI does not orphan it.
DELETE FROM dl.pick
WHERE league_id IN (SELECT league_id FROM dl_test.scenario WHERE slug = 'live');
DELETE FROM dl.team
WHERE league_id IN (SELECT league_id FROM dl_test.scenario WHERE slug = 'live');
WITH retired AS (
    DELETE FROM dl_test.scenario WHERE slug = 'live' RETURNING league_id
)
DELETE FROM dl.league WHERE id IN (SELECT league_id FROM retired);

WITH created AS (
    INSERT INTO dl.league (
        name, commissioner_user_id, roster_size, pick_seconds, started_at, turn_started_at
    )
    SELECT 'Scenario: Live snake draft', id, 3, 90,
           current_timestamp - interval '10 minutes', current_timestamp
    FROM dl.app_user WHERE google_sub = 'test:alice'
    RETURNING id
)
INSERT INTO dl_test.scenario (slug, league_id) SELECT 'live', id FROM created;

INSERT INTO dl.team (league_id, name, manager_user_id, draft_position, point_budget)
SELECT f.league_id, v.name, u.id, v.position, v.budget
FROM (VALUES
    ('test:alice', 'Alice - Ember', 1, 100),
    ('test:bob', 'Bob - Tide', 2, 100),
    ('test:carol', 'Carol - Grove', 3, 100)
) AS v(subject, name, position, budget)
JOIN dl.app_user u ON u.google_sub = v.subject
CROSS JOIN dl_test.scenario f WHERE f.slug = 'live';

-- Snake positions are derived by the same SQL function used in the snapshot.
WITH catalog AS (
    SELECT id, point_cost, row_number() OVER (ORDER BY point_cost, id) AS n
    FROM dl.draftable
)
INSERT INTO dl.pick (league_id, pick_number, draftable_id, selected_by, selected_at, point_cost)
SELECT f.league_id, p.n, d.id, t.manager_user_id,
       current_timestamp - (10 - p.n) * interval '1 minute', d.point_cost
FROM generate_series(1, 4) AS p(n)
JOIN catalog d ON d.n = p.n
CROSS JOIN dl_test.scenario f
JOIN dl.team t ON t.league_id = f.league_id
             AND t.draft_position = dl.snake_position(p.n, 3)
WHERE f.slug = 'live';

-- A supported live SQL edit must touch the league so SSE subscribers refetch it.
UPDATE dl.league SET revision = revision + 1
WHERE id = (SELECT league_id FROM dl_test.scenario WHERE slug = 'live');
COMMIT;

