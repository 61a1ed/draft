-- Learned moves for Pokémon Champions Regulation M-C from scripts/snapshot_showdown.py.
-- Moves are display/filter metadata for draftables, replaced on catalog detail imports.
CREATE TABLE dl.move (
    name text PRIMARY KEY CHECK (btrim(name) <> ''),
    type text NOT NULL CHECK (btrim(type) <> ''),
    category text NOT NULL CHECK (category IN ('Physical', 'Special', 'Status')),
    power integer CHECK (power > 0),
    accuracy integer CHECK (accuracy BETWEEN 1 AND 100),
    pp integer CHECK (pp > 0),
    description text CHECK (btrim(description) <> '')
);

CREATE TABLE dl.draftable_move (
    draftable_id integer NOT NULL REFERENCES dl.draftable(id) ON DELETE CASCADE,
    move_name text NOT NULL REFERENCES dl.move(name) ON DELETE CASCADE,
    PRIMARY KEY (draftable_id, move_name)
);
