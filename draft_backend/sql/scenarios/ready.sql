-- Scenario: Ready to start
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
WHERE league_id IN (SELECT league_id FROM dl_test.scenario WHERE slug = 'ready');
DELETE FROM dl.team
WHERE league_id IN (SELECT league_id FROM dl_test.scenario WHERE slug = 'ready');
WITH retired AS (
    DELETE FROM dl_test.scenario WHERE slug = 'ready' RETURNING league_id
)
DELETE FROM dl.league WHERE id IN (SELECT league_id FROM retired);

WITH created AS (
    INSERT INTO dl.league (
        name, commissioner_user_id, roster_size, pick_seconds, started_at, turn_started_at
    )
    SELECT 'Scenario: Ready to start', id, 3, 90,
           NULL, NULL
    FROM dl.app_user WHERE issuer = 'urn:draft:test' AND subject = 'alice'
    RETURNING id
)
INSERT INTO dl_test.scenario (slug, league_id) SELECT 'ready', id FROM created;

INSERT INTO dl.team (league_id, name, manager_user_id, draft_position, point_budget, color_rgb)
SELECT f.league_id, v.name, u.id, v.position, v.budget, 0
FROM (VALUES
    ('alice', 'Alice - Ember', 1, 100),
    ('bob', 'Bob - Tide', 2, 100),
    ('carol', 'Carol - Grove', 3, 100)
) AS v(subject, name, position, budget)
JOIN dl.app_user u ON u.issuer = 'urn:draft:test' AND u.subject = v.subject
CROSS JOIN dl_test.scenario f WHERE f.slug = 'ready';

-- A supported live SQL edit must touch the league so SSE subscribers refetch it.
UPDATE dl.league SET revision = revision + 1
WHERE id = (SELECT league_id FROM dl_test.scenario WHERE slug = 'ready');
