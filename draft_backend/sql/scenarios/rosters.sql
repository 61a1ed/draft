-- Scenario: Completed full rosters (8-12)
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
WHERE league_id IN (SELECT league_id FROM dl_test.scenario WHERE slug = 'rosters');
DELETE FROM dl.team
WHERE league_id IN (SELECT league_id FROM dl_test.scenario WHERE slug = 'rosters');
WITH retired AS (
    DELETE FROM dl_test.scenario WHERE slug = 'rosters' RETURNING league_id
)
DELETE FROM dl.league WHERE id IN (SELECT league_id FROM retired);

WITH created AS (
    INSERT INTO dl.league (
        name, commissioner_user_id, roster_size, min_roster_size, pick_seconds, started_at, turn_started_at
    )
    SELECT 'Scenario: Completed full rosters (8-12)', id, 12, 8, 90,
           current_timestamp - interval '45 minutes', current_timestamp
    FROM dl.app_user WHERE issuer = 'urn:draft:test' AND subject = 'alice'
    RETURNING id
)
INSERT INTO dl_test.scenario (slug, league_id) SELECT 'rosters', id FROM created;

INSERT INTO dl.team (
    league_id, name, manager_user_id, draft_position, point_budget, color_rgb, roster_locked
)
SELECT f.league_id, v.name, u.id, v.position, v.budget, 0, v.locked
FROM (VALUES
    ('alice', 'Alice - Ember', 1, 100, true),
    ('bob', 'Bob - Tide', 2, 100, true),
    ('carol', 'Carol - Grove', 3, 100, true),
    ('eve', 'Eve - Zephyr', 4, 100, false)
) AS v(subject, name, position, budget, locked)
JOIN dl.app_user u ON u.issuer = 'urn:draft:test' AND u.subject = v.subject
CROSS JOIN dl_test.scenario f WHERE f.slug = 'rosters';

-- Teams A, B, C locked after rounds 8, 9, and 10; Team D drafted through round 12.
WITH planned_picks (team_pos, round_num, dex_num, form_name) AS (
    VALUES
        -- Team A (Alice, 8 picks: 100 pts)
        (1, 1, 547, ''),                          -- Whimsicott (21)
        (1, 2, 1000, ''),                         -- Gholdengo (19)
        (1, 3, 149, 'Original, Mega'),            -- Dragonite (18)
        (1, 4, 308, 'Original, Mega'),            -- Medicham (12)
        (1, 5, 279, ''),                          -- Pelipper (16)
        (1, 6, 904, ''),                          -- Overqwil (11)
        (1, 7, 405, ''),                          -- Luxray (2)
        (1, 8, 351, ''),                          -- Castform (1)
        -- Team B (Bob, 9 picks: 100 pts)
        (2, 1, 670, 'Original, Mega'),            -- Floette (21)
        (2, 2, 663, ''),                          -- Talonflame (18)
        (2, 3, 983, ''),                          -- Kingambit (19)
        (2, 4, 681, ''),                          -- Aegislash (15)
        (2, 5, 571, ''),                          -- Zoroark (9)
        (2, 6, 454, ''),                          -- Toxicroak (4)
        (2, 7, 691, 'Original, Mega'),            -- Dragalge (8)
        (2, 8, 211, ''),                          -- Qwilfish (3)
        (2, 9, 841, ''),                          -- Flapple (3)
        -- Team C (Carol, 10 picks: 100 pts)
        (3, 1, 6, 'Original, Mega X, Mega Y'),    -- Charizard (23)
        (3, 2, 3, 'Original, Mega'),              -- Venusaur (17)
        (3, 3, 350, ''),                          -- Milotic (14)
        (3, 4, 923, ''),                          -- Pawmot (8)
        (3, 5, 959, ''),                          -- Tinkaton (12)
        (3, 6, 208, 'Original, Mega'),            -- Steelix (6)
        (3, 7, 697, ''),                          -- Tyrantrum (5)
        (3, 8, 931, 'Green, Blue, Yellow, White'),-- Squawkabilly (4)
        (3, 9, 135, ''),                          -- Jolteon (6)
        (3, 10, 745, 'Dusk'),                     -- Lycanroc (Dusk) (5)
        -- Team D (Eve, 12 picks: 100 pts)
        (4, 1, 715, ''),                          -- Noivern (9)
        (4, 2, 609, 'Original, Mega'),            -- Chandelure (11)
        (4, 3, 38, ''),                           -- Ninetales (14)
        (4, 4, 876, 'Male'),                      -- Indeedee (Male) (12)
        (4, 5, 709, ''),                          -- Trevenant (5)
        (4, 6, 700, ''),                          -- Sylveon (15)
        (4, 7, 257, 'Original, Mega'),            -- Blaziken (12)
        (4, 8, 395, ''),                          -- Empoleon (11)
        (4, 9, 354, 'Original, Mega'),            -- Banette (4)
        (4, 10, 863, ''),                         -- Perrserker (4)
        (4, 11, 687, 'Original, Mega'),           -- Malamar (2)
        (4, 12, 853, '')                          -- Grapploct (1)
),
snake_slots AS (
    SELECT p.team_pos, p.round_num, p.dex_num, p.form_name,
           (p.round_num - 1) * 4 + CASE
               WHEN p.round_num % 2 = 1 THEN p.team_pos
               ELSE 5 - p.team_pos
           END AS pick_number
    FROM planned_picks p
),
resolved_picks AS (
    SELECT s.pick_number,
           s.team_pos,
           d.id AS draftable_id,
           d.point_cost,
           row_number() OVER (ORDER BY s.pick_number) AS seq
    FROM snake_slots s
    JOIN dl.draftable d ON d.dex = s.dex_num AND d.form = s.form_name
)
INSERT INTO dl.pick (league_id, pick_number, draftable_id, selected_by, selected_at, point_cost)
SELECT f.league_id, r.pick_number, r.draftable_id, t.manager_user_id,
       current_timestamp - (45 - r.seq) * interval '1 minute', r.point_cost
FROM resolved_picks r
CROSS JOIN dl_test.scenario f
JOIN dl.team t ON t.league_id = f.league_id AND t.draft_position = r.team_pos
WHERE f.slug = 'rosters'
ORDER BY r.pick_number;

-- A supported live SQL edit must touch the league so SSE subscribers refetch it.
UPDATE dl.league SET revision = revision + 1
WHERE id = (SELECT league_id FROM dl_test.scenario WHERE slug = 'rosters');
