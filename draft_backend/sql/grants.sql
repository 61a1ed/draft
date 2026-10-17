-- Run as dl_owner after schema.sql, inside the bootstrap transaction.
-- All schema data is publicly readable; these grants authorize controlled writes.
GRANT EXECUTE ON FUNCTION
    dl.create_league(text,integer,integer),
    dl.update_league(integer,integer,text,integer,integer),
    dl.add_team(integer,integer,text,integer,integer),
    dl.update_team(integer,integer,integer,text,integer,integer),
    dl.remove_team(integer,integer,integer),
    dl.set_draft_order(integer,integer,integer[]),
    dl.draft_action(integer,integer,text,integer,boolean) TO dl_api;

-- Only the login code selects this role. It can upsert account identities.
GRANT INSERT (issuer, subject, display_name), UPDATE (display_name)
    ON dl.app_user TO dl_auth;
GRANT USAGE ON SEQUENCE dl.app_user_id_seq TO dl_auth;
