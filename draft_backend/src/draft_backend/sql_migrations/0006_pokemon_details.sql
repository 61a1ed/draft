-- Battle details copied from Pokémon Showdown by scripts/snapshot_showdown.py.
-- Existing rows stay NULL until a catalog import supplies details; generic
-- draftables (no dex) may stay NULL. Details are all present or all absent.
CREATE FUNCTION dl.valid_names(p_names text[], p_max integer) RETURNS boolean
LANGUAGE sql IMMUTABLE SET search_path = pg_catalog AS $$
    SELECT cardinality(p_names) BETWEEN 1 AND p_max AND array_ndims(p_names) = 1
       AND NOT EXISTS (SELECT FROM unnest(p_names) n WHERE n IS NULL OR btrim(n) = '')
$$;

ALTER TABLE dl.draftable
    ADD COLUMN types text[] CHECK (dl.valid_names(types, 2)),
    ADD COLUMN abilities text[] CHECK (dl.valid_names(abilities, 3)),
    ADD COLUMN hidden_ability text CHECK (btrim(hidden_ability) <> ''),
    ADD COLUMN hp integer CHECK (hp BETWEEN 1 AND 255),
    ADD COLUMN attack integer CHECK (attack BETWEEN 1 AND 255),
    ADD COLUMN defense integer CHECK (defense BETWEEN 1 AND 255),
    ADD COLUMN special_attack integer CHECK (special_attack BETWEEN 1 AND 255),
    ADD COLUMN special_defense integer CHECK (special_defense BETWEEN 1 AND 255),
    ADD COLUMN speed integer CHECK (speed BETWEEN 1 AND 255),
    ADD CONSTRAINT draftable_details_check CHECK (
        num_nulls(types, abilities, hp, attack, defense, special_attack, special_defense,
                  speed) IN (0, 8)
        AND (types IS NULL OR dex IS NOT NULL)
        AND (hidden_ability IS NULL OR types IS NOT NULL));

-- Megas are display data for a draftable, not separately draftable. Reimports
-- replace a draftable's Megas; picks never reference this table.
CREATE TABLE dl.mega_evolution (
    id integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    draftable_id integer NOT NULL REFERENCES dl.draftable,
    name text NOT NULL CHECK (btrim(name) <> ''),
    types text[] NOT NULL CHECK (dl.valid_names(types, 2)),
    abilities text[] NOT NULL CHECK (dl.valid_names(abilities, 3)),
    hidden_ability text CHECK (btrim(hidden_ability) <> ''),
    hp integer NOT NULL CHECK (hp BETWEEN 1 AND 255),
    attack integer NOT NULL CHECK (attack BETWEEN 1 AND 255),
    defense integer NOT NULL CHECK (defense BETWEEN 1 AND 255),
    special_attack integer NOT NULL CHECK (special_attack BETWEEN 1 AND 255),
    special_defense integer NOT NULL CHECK (special_defense BETWEEN 1 AND 255),
    speed integer NOT NULL CHECK (speed BETWEEN 1 AND 255),
    image_hash text REFERENCES dl.asset(hash),
    UNIQUE (draftable_id, name)
);
