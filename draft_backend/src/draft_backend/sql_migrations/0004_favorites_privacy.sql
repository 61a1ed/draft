-- Favorites are private to their account, including lists created before this
-- migration. The trusted gateway sets request.jwt.claims per transaction.
REVOKE SELECT ON dl.favorites_list, dl.favorites_entry, dl.favorites_list_snapshot
FROM PUBLIC;
GRANT SELECT ON dl.favorites_list, dl.favorites_entry, dl.favorites_list_snapshot
TO dl_api;

ALTER TABLE dl.favorites_list ENABLE ROW LEVEL SECURITY;
ALTER TABLE dl.favorites_entry ENABLE ROW LEVEL SECURITY;

CREATE POLICY favorites_list_owner_read ON dl.favorites_list
FOR SELECT TO dl_api
USING (owner_user_id = dl.user_id());

CREATE POLICY favorites_entry_owner_read ON dl.favorites_entry
FOR SELECT TO dl_api
USING (EXISTS (
    SELECT 1 FROM dl.favorites_list l
    WHERE l.id = favorites_list_id AND l.owner_user_id = dl.user_id()
));

-- favorites_list_snapshot already uses security_invoker, so both policies also
-- protect its header and entries. Keep owner bypass for the SECURITY DEFINER
-- write functions, which enforce ownership and revision checks themselves.
