-- Scenario: Undo and replacement
-- Load with scripts/scenarios.py; the runner owns the transaction.
-- Reload replaces only this tracked fixture; users, catalog, and other leagues survive.
DO $$
BEGIN
    IF (SELECT count(*) FROM dl.app_user
        WHERE issuer = 'urn:draft:test' AND subject IN ('alice', 'bob', 'carol')) <> 3 THEN
        RAISE EXCEPTION 'Scenario fixtures require alice, bob, and carol test users';
    END IF;
    IF (SELECT count(*) FROM dl.draftable) < 9 THEN
        RAISE EXCEPTION 'Scenario fixtures require at least nine catalog entries';
    END IF;
END $$;

-- IDs come from this registry, so renaming a fixture in the UI does not orphan it.
DELETE FROM dl.pick
WHERE league_id IN (SELECT league_id FROM dl_test.scenario WHERE slug = 'undo');
DELETE FROM dl.team
WHERE league_id IN (SELECT league_id FROM dl_test.scenario WHERE slug = 'undo');
WITH retired AS (
    DELETE FROM dl_test.scenario WHERE slug = 'undo' RETURNING league_id
)
DELETE FROM dl.league WHERE id IN (SELECT league_id FROM retired);

WITH created AS (
    INSERT INTO dl.league (
        name, commissioner_user_id, roster_size, pick_seconds, started_at, turn_started_at
    )
    SELECT 'Scenario: Undo and replacement', id, 3, 90,
           current_timestamp - interval '10 minutes', current_timestamp
    FROM dl.app_user WHERE issuer = 'urn:draft:test' AND subject = 'alice'
    RETURNING id
)
INSERT INTO dl_test.scenario (slug, league_id) SELECT 'undo', id FROM created;

INSERT INTO dl.team (league_id, name, manager_user_id, draft_position, point_budget, color_rgb)
SELECT f.league_id, v.name, u.id, v.position, v.budget, 0
FROM (VALUES
    ('alice', 'Alice - Ember', 1, 100),
    ('bob', 'Bob - Tide', 2, 100),
    ('carol', 'Carol - Grove', 3, 100)
) AS v(subject, name, position, budget)
JOIN dl.app_user u ON u.issuer = 'urn:draft:test' AND u.subject = v.subject
CROSS JOIN dl_test.scenario f WHERE f.slug = 'undo';

-- Pick two was undone, then replaced with a different entry. The old entry is available.
WITH catalog AS (
    SELECT id, point_cost, row_number() OVER (ORDER BY point_cost, id) AS n
    FROM dl.draftable
)
INSERT INTO dl.pick (
    league_id, pick_number, draftable_id, selected_by, selected_at,
    undone_by, undone_at, point_cost
)
SELECT f.league_id, v.pick_number, d.id, t.manager_user_id,
       current_timestamp - v.minutes_ago * interval '1 minute',
       CASE WHEN v.undone THEN commissioner.id END,
       CASE WHEN v.undone THEN current_timestamp - interval '3 minutes' END,
       d.point_cost
FROM (VALUES (1, 1, false, 6), (2, 2, true, 4), (2, 3, false, 2))
     AS v(pick_number, catalog_number, undone, minutes_ago)
JOIN catalog d ON d.n = v.catalog_number
CROSS JOIN dl_test.scenario f
JOIN dl.team t ON t.league_id = f.league_id
             AND t.draft_position = dl.snake_position(v.pick_number, 3)
JOIN dl.app_user commissioner ON commissioner.issuer = 'urn:draft:test' AND commissioner.subject = 'alice'
WHERE f.slug = 'undo';

-- A supported live SQL edit must touch the league so SSE subscribers refetch it.
UPDATE dl.league SET revision = revision + 1
WHERE id = (SELECT league_id FROM dl_test.scenario WHERE slug = 'undo');
