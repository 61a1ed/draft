-- Ordinary resources now use HTTP query invalidation and polling. Keep the private
-- helper signatures called by previously installed tournament functions, but retire
-- their account-wide wake-ups without rewriting those historical migrations.
CREATE OR REPLACE FUNCTION dl.notify_account(p_user integer) RETURNS void LANGUAGE plpgsql STRICT
SET search_path = pg_catalog, dl, pg_temp AS $$
BEGIN
    RETURN;
END $$;

CREATE OR REPLACE FUNCTION dl.notify_league_accounts(p_league integer)
RETURNS void LANGUAGE plpgsql
SET search_path = pg_catalog, dl, pg_temp AS $$
BEGIN
    RETURN;
END $$;

DROP TRIGGER favorites_list_changed ON dl.favorites_list;
DROP TRIGGER favorites_entry_changed ON dl.favorites_entry;

-- A selected draft uses one channel, regardless of the number of its managers.
-- The original league/team triggers cover draft RPCs and direct administrative
-- edits. PostgreSQL folds duplicates and delivers only after the write commits.
CREATE OR REPLACE FUNCTION dl.notify_league() RETURNS trigger LANGUAGE plpgsql
SET search_path = pg_catalog, dl, pg_temp AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        PERFORM pg_notify('dl_draft_' || OLD.id::text, '');
        RETURN OLD;
    END IF;
    PERFORM pg_notify('dl_draft_' || NEW.id::text, '');
    RETURN NEW;
END $$;

CREATE OR REPLACE FUNCTION dl.notify_team() RETURNS trigger LANGUAGE plpgsql
SET search_path = pg_catalog, dl, pg_temp AS $$
BEGIN
    IF TG_OP <> 'INSERT' THEN
        PERFORM pg_notify('dl_draft_' || OLD.league_id::text, '');
    END IF;
    IF TG_OP <> 'DELETE' THEN
        PERFORM pg_notify('dl_draft_' || NEW.league_id::text, '');
        RETURN NEW;
    END IF;
    RETURN OLD;
END $$;

REVOKE ALL ON FUNCTION dl.notify_account(integer), dl.notify_league_accounts(integer),
    dl.notify_league(), dl.notify_team() FROM PUBLIC, dl_api;
