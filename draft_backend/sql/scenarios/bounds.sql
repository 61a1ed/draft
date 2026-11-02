-- Scenario: Roster bounds (8-12, 7 picked)
-- Load with scripts/scenarios.py; the runner owns the transaction.
-- Reload replaces only this tracked fixture; users, catalog, and other leagues survive.
DO $$
BEGIN
    IF (SELECT count(*) FROM dl.app_user
        WHERE issuer = 'urn:draft:test' AND subject IN ('alice', 'bob', 'carol', 'eve')) <> 4 THEN
        RAISE EXCEPTION 'Scenario fixtures require alice, bob, carol, and eve test users';
    END IF;
    IF (SELECT count(*) FROM dl.draftable) < 48 THEN
        RAISE EXCEPTION 'Scenario fixtures require at least forty-eight catalog entries';
    END IF;
END $$;

-- IDs come from this registry, so renaming a fixture in the UI does not orphan it.
DELETE FROM dl.pick
WHERE league_id IN (SELECT league_id FROM dl_test.scenario WHERE slug = 'bounds');
DELETE FROM dl.team
WHERE league_id IN (SELECT league_id FROM dl_test.scenario WHERE slug = 'bounds');
WITH retired AS (
    DELETE FROM dl_test.scenario WHERE slug = 'bounds' RETURNING league_id
)
DELETE FROM dl.league WHERE id IN (SELECT league_id FROM retired);

WITH created AS (
    INSERT INTO dl.league (
        name, commissioner_user_id, roster_size, min_roster_size, pick_seconds, started_at, turn_started_at
    )
    SELECT 'Scenario: Roster bounds (8-12, 7 picked)', id, 12, 8, 90,
           current_timestamp - interval '30 minutes', current_timestamp
    FROM dl.app_user WHERE issuer = 'urn:draft:test' AND subject = 'alice'
    RETURNING id
)
INSERT INTO dl_test.scenario (slug, league_id) SELECT 'bounds', id FROM created;

INSERT INTO dl.team (league_id, name, manager_user_id, draft_position, point_budget, color_rgb)
SELECT f.league_id, v.name, u.id, v.position, v.budget, 0
FROM (VALUES
    ('alice', 'Alice - Ember', 1, 100),
    ('bob', 'Bob - Tide', 2, 100),
    ('carol', 'Carol - Grove', 3, 100),
    ('eve', 'Eve - Zephyr', 4, 100)
) AS v(subject, name, position, budget)
JOIN dl.app_user u ON u.issuer = 'urn:draft:test' AND u.subject = v.subject
CROSS JOIN dl_test.scenario f WHERE f.slug = 'bounds';

-- Each of the 4 teams has completed 7 rounds (28 picks total) in snake order.
WITH catalog AS (
    SELECT id, point_cost, row_number() OVER (ORDER BY point_cost, id) AS n
    FROM dl.draftable
)
INSERT INTO dl.pick (league_id, pick_number, draftable_id, selected_by, selected_at, point_cost)
SELECT f.league_id, p.n, d.id, t.manager_user_id,
       current_timestamp - (30 - p.n) * interval '1 minute', d.point_cost
FROM generate_series(1, 28) AS p(n)
JOIN catalog d ON d.n = p.n
CROSS JOIN dl_test.scenario f
JOIN dl.team t ON t.league_id = f.league_id
             AND t.draft_position = dl.snake_position(p.n, 4)
WHERE f.slug = 'bounds';

-- A supported live SQL edit must touch the league so SSE subscribers refetch it.
UPDATE dl.league SET revision = revision + 1
WHERE id = (SELECT league_id FROM dl_test.scenario WHERE slug = 'bounds');
