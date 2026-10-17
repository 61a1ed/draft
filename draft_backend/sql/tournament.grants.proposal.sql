-- REVIEW ONLY: run as dl_owner in the proposal installation transaction, after
-- tournament.proposal.sql, tournament.mutators.proposal.sql,
-- tournament.results.proposal.sql and tournament.lifecycle.proposal.sql.
-- Controlled SQL calls only: no table DML, sequences, private helpers or raw
-- evidence reads. Read policies/HTTP routes remain a separate implementation.
GRANT EXECUTE ON FUNCTION
    dl.configure_tournament(integer, integer, text),
    dl.configure_stage(integer, integer, integer, text, text, integer, integer, integer),
    dl.publish_tournament(integer, integer, timestamp without time zone),
    dl.set_round_window(integer, integer, timestamptz, timestamptz),
    dl.set_team_availability(integer, integer, text, integer, jsonb),
    dl.copy_team_availability_week(integer, integer, date, date),
    dl.submit_result(integer, integer, text, integer, integer, text),
    dl.record_result(integer, integer, text, integer, text, integer, integer, text),
    dl.correct_result(integer, integer, text, text, integer, integer, text),
    dl.set_match_vod(integer, integer, text),
    dl.withdraw_team(integer, integer, integer, text),
    dl.create_tiebreak_round(integer, integer, integer[], timestamptz, timestamptz, text),
    dl.publish_final(integer, integer, integer[], timestamptz, timestamptz),
    dl.tournament_draft_undo_allowed(integer)
TO dl_api;
