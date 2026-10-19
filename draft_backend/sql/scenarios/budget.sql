-- Scenario: Zero budget and free picks
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
WHERE league_id IN (SELECT league_id FROM dl_test.scenario WHERE slug = 'budget');
DELETE FROM dl.team
WHERE league_id IN (SELECT league_id FROM dl_test.scenario WHERE slug = 'budget');
WITH retired AS (
    DELETE FROM dl_test.scenario WHERE slug = 'budget' RETURNING league_id
)
DELETE FROM dl.league WHERE id IN (SELECT league_id FROM retired);

WITH created AS (
    INSERT INTO dl.league (
        name, commissioner_user_id, roster_size, pick_seconds, started_at, turn_started_at
    )
    SELECT 'Scenario: Zero budget and free picks', id, 3, 90,
           current_timestamp - interval '10 minutes', current_timestamp
    FROM dl.app_user WHERE issuer = 'urn:draft:test' AND subject = 'alice'
    RETURNING id
)
INSERT INTO dl_test.scenario (slug, league_id) SELECT 'budget', id FROM created;

INSERT INTO dl.team (league_id, name, manager_user_id, draft_position, point_budget, color_rgb)
SELECT f.league_id, v.name, u.id, v.position, v.budget, 0
FROM (VALUES
    ('alice', 'Alice - Ember', 1, 100),
    ('bob', 'Bob - Tide', 2, 0),
    ('carol', 'Carol - Grove', 3, 100)
) AS v(subject, name, position, budget)
JOIN dl.app_user u ON u.issuer = 'urn:draft:test' AND u.subject = v.subject
CROSS JOIN dl_test.scenario f WHERE f.slug = 'budget';

-- Alice's first acquisition is explicitly free. Bob now has the turn and zero budget.
-- Catalog prices remain unchanged; the recorded purchase price is the waived amount.
INSERT INTO dl.pick (league_id, pick_number, draftable_id, selected_by, selected_at, point_cost)
SELECT f.league_id, 1, d.id, u.id, current_timestamp - interval '1 minute', 0
FROM dl_test.scenario f
CROSS JOIN LATERAL (SELECT id FROM dl.draftable ORDER BY point_cost DESC, id LIMIT 1) d
JOIN dl.app_user u ON u.issuer = 'urn:draft:test' AND u.subject = 'alice'
WHERE f.slug = 'budget';

-- A supported live SQL edit must touch the league so SSE subscribers refetch it.
UPDATE dl.league SET revision = revision + 1
WHERE id = (SELECT league_id FROM dl_test.scenario WHERE slug = 'budget');
