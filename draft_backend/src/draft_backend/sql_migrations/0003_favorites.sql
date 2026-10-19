-- Account-owned, reusable catalog favorites. Applied atomically as dl_owner.
-- Keep the installation baseline and previously applied migrations unchanged.
-- Lists are independent of teams, leagues, draft availability, and draft revision.

-- ============================================================================
-- BIG TODO: RESTRICT FAVORITES READ ACCESS BEFORE TREATING LISTS AS PRIVATE.
-- Public reads are deliberately retained for now, matching the existing schema.
-- Both tables and favorites_list_snapshot inherit SELECT grants to PUBLIC.
-- An owner_user_id and owner-only writers DO NOT protect a manager's strategy.
-- Future work must revoke public SELECT on these tables/view, enforce owner-only
-- reads in PostgreSQL (including direct table access), and authorize every API
-- read. Filtering the UI or adding an owner filter to one endpoint is insufficient.
-- Until that work lands, anonymous users and other managers can read every list.
-- ============================================================================

CREATE TABLE dl.favorites_list (
    id integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    owner_user_id integer NOT NULL REFERENCES dl.app_user(id),
    name text NOT NULL CHECK (name ~ '[^[:space:]]'),
    revision integer NOT NULL DEFAULT 0 CHECK (revision >= 0)
);
-- Names may repeat. The account and stable ID identify a list, not its name.
CREATE INDEX favorites_list_owner_idx ON dl.favorites_list (owner_user_id, id);

CREATE TABLE dl.favorites_entry (
    favorites_list_id integer NOT NULL REFERENCES dl.favorites_list(id) ON DELETE CASCADE,
    draftable_id integer NOT NULL REFERENCES dl.draftable(id),
    ordinal integer NOT NULL CHECK (ordinal > 0),
    PRIMARY KEY (favorites_list_id, draftable_id),
    -- Check at statement end so a single UPDATE can swap occupied positions.
    CONSTRAINT favorites_entry_ordinal_uq UNIQUE (favorites_list_id, ordinal)
        DEFERRABLE INITIALLY IMMEDIATE
);

CREATE VIEW dl.favorites_list_snapshot WITH (security_invoker = true) AS
SELECT l.id, l.owner_user_id, l.name, l.revision,
    coalesce((SELECT jsonb_agg(
        jsonb_build_object('draftable_id', e.draftable_id, 'ordinal', e.ordinal)
        ORDER BY e.ordinal)
        FROM dl.favorites_entry e WHERE e.favorites_list_id = l.id),
        '[]'::jsonb) AS entries
FROM dl.favorites_list l;

-- Private helpers. The list row serializes its membership, order, and name edits.
-- A concurrent write retries the unchanged request; an outdated revision then
-- becomes stale_revision, matching the existing SERIALIZABLE write convention.
CREATE FUNCTION dl.favorites_admit(p_list integer, p_revision integer) RETURNS void
LANGUAGE plpgsql SET search_path = pg_catalog, dl, pg_temp AS $$
DECLARE u integer := require_writer(); l dl.favorites_list;
BEGIN
    SELECT * INTO l FROM favorites_list WHERE id = p_list FOR UPDATE;
    IF NOT FOUND THEN
        RAISE SQLSTATE 'PT404' USING MESSAGE = 'favorites_list_not_found';
    END IF;
    IF l.owner_user_id <> u THEN
        RAISE SQLSTATE 'PT409' USING MESSAGE = 'action_not_permitted';
    END IF;
    IF l.revision IS DISTINCT FROM p_revision THEN
        RAISE SQLSTATE 'PT409' USING MESSAGE = 'stale_revision';
    END IF;
END $$;

CREATE FUNCTION dl.favorites_bump(p_list integer) RETURNS void LANGUAGE sql
SET search_path = pg_catalog, dl, pg_temp AS $$
    UPDATE favorites_list SET revision = revision + 1 WHERE id = p_list
$$;

CREATE FUNCTION dl.create_favorites_list(p_name text) RETURNS integer
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, dl, pg_temp AS $$
DECLARE u integer := require_writer(); result integer;
        normalized_name text := regexp_replace(p_name, '^[[:space:]]+|[[:space:]]+$', '', 'g');
BEGIN
    IF normalized_name IS NULL OR normalized_name = '' THEN
        RAISE SQLSTATE 'PT400' USING MESSAGE = 'invalid_favorites_name';
    END IF;
    INSERT INTO favorites_list (owner_user_id, name)
    VALUES (u, normalized_name)
    RETURNING id INTO result;
    RETURN result;
END $$;

CREATE FUNCTION dl.rename_favorites_list(p_list integer, p_revision integer, p_name text)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, dl, pg_temp AS $$
DECLARE normalized_name text := regexp_replace(p_name, '^[[:space:]]+|[[:space:]]+$', '', 'g');
BEGIN
    PERFORM favorites_admit(p_list, p_revision);
    IF normalized_name IS NULL OR normalized_name = '' THEN
        RAISE SQLSTATE 'PT400' USING MESSAGE = 'invalid_favorites_name';
    END IF;
    UPDATE favorites_list
    SET name = normalized_name, revision = revision + 1
    WHERE id = p_list;
END $$;

CREATE FUNCTION dl.delete_favorites_list(p_list integer, p_revision integer)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, dl, pg_temp AS $$
BEGIN
    PERFORM favorites_admit(p_list, p_revision);
    -- The checked header is removed, so there is no surviving revision to bump.
    DELETE FROM favorites_list WHERE id = p_list;
END $$;

CREATE FUNCTION dl.add_favorites_entry(p_list integer, p_revision integer, p_draftable integer)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, dl, pg_temp AS $$
BEGIN
    PERFORM favorites_admit(p_list, p_revision);
    INSERT INTO favorites_entry (favorites_list_id, draftable_id, ordinal)
    SELECT p_list, p_draftable, coalesce(max(ordinal), 0) + 1
    FROM favorites_entry WHERE favorites_list_id = p_list;
    PERFORM favorites_bump(p_list);
END $$;

CREATE FUNCTION dl.remove_favorites_entry(p_list integer, p_revision integer,
    p_draftable integer)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, dl, pg_temp AS $$
BEGIN
    PERFORM favorites_admit(p_list, p_revision);
    DELETE FROM favorites_entry
    WHERE favorites_list_id = p_list AND draftable_id = p_draftable;
    IF NOT FOUND THEN
        RAISE SQLSTATE 'PT404' USING MESSAGE = 'favorites_entry_not_found';
    END IF;
    -- Gaps are valid. Removing a member does not change remaining ordinals.
    PERFORM favorites_bump(p_list);
END $$;

CREATE FUNCTION dl.set_favorites_order(p_list integer, p_revision integer,
    p_draftables integer[])
RETURNS void LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, dl, pg_temp AS $$
DECLARE actual integer[]; supplied integer[];
BEGIN
    PERFORM favorites_admit(p_list, p_revision);
    SELECT coalesce(array_agg(draftable_id ORDER BY draftable_id), '{}'::integer[])
    INTO actual FROM favorites_entry WHERE favorites_list_id = p_list;
    SELECT coalesce(array_agg(id ORDER BY id), '{}'::integer[])
    INTO supplied FROM unnest(p_draftables) AS x(id);
    -- PostgreSQL gives an empty array no dimension. [] is valid only when the
    -- current list is empty; NULL, duplicates, missing IDs, and extra IDs fail.
    IF p_draftables IS NULL
       OR (cardinality(p_draftables) > 0 AND array_ndims(p_draftables) IS DISTINCT FROM 1)
       OR supplied IS DISTINCT FROM actual THEN
        RAISE SQLSTATE 'PT400' USING MESSAGE = 'order_must_contain_every_draftable_once';
    END IF;
    UPDATE favorites_entry e SET ordinal = x.ord::integer
    FROM unnest(p_draftables) WITH ORDINALITY AS x(id, ord)
    WHERE e.favorites_list_id = p_list AND e.draftable_id = x.id;
    PERFORM favorites_bump(p_list);
END $$;

-- Empty account wake-ups contain no list data. Duplicate notifications from
-- entries and header revision changes fold into one notification at commit.
-- Direct administrative SQL also wakes affected owners; RPCs own revisions.
CREATE FUNCTION dl.notify_favorites_list() RETURNS trigger LANGUAGE plpgsql
SET search_path = pg_catalog, dl, pg_temp AS $$
BEGIN
    IF TG_OP <> 'INSERT' THEN
        PERFORM dl.notify_account(OLD.owner_user_id);
    END IF;
    IF TG_OP <> 'DELETE' THEN
        PERFORM dl.notify_account(NEW.owner_user_id);
        RETURN NEW;
    END IF;
    RETURN OLD;
END $$;
CREATE TRIGGER favorites_list_changed AFTER INSERT OR UPDATE OR DELETE ON dl.favorites_list
FOR EACH ROW EXECUTE FUNCTION dl.notify_favorites_list();

CREATE FUNCTION dl.notify_favorites_entry() RETURNS trigger LANGUAGE plpgsql
SET search_path = pg_catalog, dl, pg_temp AS $$
BEGIN
    IF TG_OP <> 'INSERT' THEN
        PERFORM dl.notify_account(owner_user_id) FROM dl.favorites_list
        WHERE id = OLD.favorites_list_id;
    END IF;
    IF TG_OP <> 'DELETE' THEN
        PERFORM dl.notify_account(owner_user_id) FROM dl.favorites_list
        WHERE id = NEW.favorites_list_id;
        RETURN NEW;
    END IF;
    -- During ON DELETE CASCADE the header may already be absent; its own
    -- deletion trigger notifies the owner using OLD.owner_user_id.
    RETURN OLD;
END $$;
CREATE TRIGGER favorites_entry_changed AFTER INSERT OR UPDATE OR DELETE ON dl.favorites_entry
FOR EACH ROW EXECUTE FUNCTION dl.notify_favorites_entry();

REVOKE ALL ON FUNCTION
    dl.favorites_admit(integer, integer),
    dl.favorites_bump(integer),
    dl.notify_favorites_list(),
    dl.notify_favorites_entry()
FROM PUBLIC, dl_api;

REVOKE ALL ON FUNCTION
    dl.create_favorites_list(text),
    dl.rename_favorites_list(integer, integer, text),
    dl.delete_favorites_list(integer, integer),
    dl.add_favorites_entry(integer, integer, integer),
    dl.remove_favorites_entry(integer, integer, integer),
    dl.set_favorites_order(integer, integer, integer[])
FROM PUBLIC;

GRANT EXECUTE ON FUNCTION
    dl.create_favorites_list(text),
    dl.rename_favorites_list(integer, integer, text),
    dl.delete_favorites_list(integer, integer),
    dl.add_favorites_entry(integer, integer, integer),
    dl.remove_favorites_entry(integer, integer, integer),
    dl.set_favorites_order(integer, integer, integer[])
TO dl_api;
