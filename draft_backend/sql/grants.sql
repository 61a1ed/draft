-- Run as dl_owner after schema.sql, inside the bootstrap transaction.
-- These are the schema's recommended API grants, without broader table access.
GRANT USAGE ON SCHEMA dl TO dl_api;
GRANT SELECT (id, display_name) ON dl.app_user TO dl_api;
GRANT SELECT ON dl.asset, dl.draftable, dl.league, dl.team, dl.pick,
    dl.active_pick, dl.draft_state, dl.roster, dl.team_points,
    dl.permitted_action, dl.league_snapshot TO dl_api;
GRANT EXECUTE ON FUNCTION dl.user_id(), dl.can_read(integer),
    dl.snake_position(integer,integer),
    dl.create_league(text,integer,integer),
    dl.update_league(integer,integer,text,integer,integer),
    dl.add_team(integer,integer,text,integer,integer),
    dl.update_team(integer,integer,integer,text,integer,integer),
    dl.remove_team(integer,integer,integer),
    dl.set_draft_order(integer,integer,integer[]),
    dl.draft_action(integer,integer,text,integer,boolean) TO dl_api;

-- Only the login code selects this role. It cannot read or modify leagues.
GRANT USAGE ON SCHEMA dl TO dl_auth;
GRANT SELECT ON dl.app_user TO dl_auth;
GRANT INSERT (issuer, subject, display_name), UPDATE (display_name)
    ON dl.app_user TO dl_auth;
GRANT USAGE ON SEQUENCE dl.app_user_id_seq TO dl_auth;

-- Keep future owner-created functions private unless explicitly exposed.
ALTER DEFAULT PRIVILEGES REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC;
