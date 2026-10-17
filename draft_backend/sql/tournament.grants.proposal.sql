-- REVIEW ONLY: run as dl_owner in the proposal installation transaction, after
-- tournament.proposal.sql, tournament.mutators.proposal.sql,
-- tournament.results.proposal.sql and tournament.lifecycle.proposal.sql.
-- All tables and views inherit public SELECT from the schema owner defaults.
-- Only controlled calls receive write authority; HTTP routes are separate work.
GRANT EXECUTE ON FUNCTION
    dl.publish_tournament(integer, integer, date, integer, integer),
    dl.set_round_suggested_date(integer, integer, date),
    dl.set_team_availability(integer, integer, text, integer, jsonb),
    dl.copy_team_availability_week(integer, integer, date, date),
    dl.submit_result(integer, integer, text, integer, integer, text),
    dl.record_result(integer, integer, text, integer, text, integer, integer, text),
    dl.correct_result(integer, integer, text, text, integer, integer, text),
    dl.set_match_vod(integer, integer, text),
    dl.withdraw_team(integer, integer, integer, text),
    dl.create_tiebreak_round(integer, integer, integer[], text, date),
    dl.publish_final(integer, integer, integer[], date)
TO dl_api;
