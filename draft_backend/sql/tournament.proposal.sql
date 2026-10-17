-- REVIEW PROPOSAL: tournament storage, not a migration or enabled feature.
-- Apply only to a disposable database AFTER schema.sql when reviewing/testing.
-- This file supplies storage/read projections. Controlled writers live in
-- tournament.mutators.proposal.sql, tournament.results.proposal.sql and
-- tournament.lifecycle.proposal.sql, with explicit tournament.grants.proposal.sql.
-- Install all five proposal files as dl_owner in one disposable-DB transaction,
-- in that order. These files are not loaded by bootstrap or the HTTP gateway.
--
-- One tournament per league; one series per match; scores count games won.
-- Initial publisher supports a single round robin followed by a two-team final.
-- Qualification ties can add repeatable tiebreak rounds within the first stage.
-- Creating/repeating those rounds is a required commissioner app operation.
-- Both players report independently; matching current reports become official.
-- Official corrections are commissioner-only. Withdrawal has fixed consequences;
-- final publication freezes the two qualifiers, with no replacement finalists.
-- Setup supplies best_of = 3 and 5 respectively; these are configuration values.
-- No manager replacement, individual game records, recurring calendars,
-- appointment booking, generic metadata, rule-version infrastructure, or new
-- triggers. Availability is advisory and independent of match/result lifecycle.
--
-- Permission boundary: all new tables have RLS enabled, with no read policies or
-- API table grants. Views are security-invoker. The companion grants expose only
-- controlled SERIALIZABLE writers and a scoped draft-undo eligibility helper.
-- Participants and commissioner see current participant reports; league members
-- see official results. Independent reporting means independent authorship here,
-- not blind reporting. Detailed read policies still need review.
--
-- INVARIANT OWNERSHIP
-- CHECK/UNIQUE/FK constraints below enforce local shapes and selected ownership
-- relationships. The companion writers enforce publication immutability, actor
-- authorization, complete schedules, agreement and scores against stage.best_of.
-- The write contracts are specified at the end; qualification choices are
-- commissioner-certified against announced criteria, not automatically ranked.

-- One-to-one extension of a league, independently revisioned from the draft.
-- time_zone defines calendar/round presentation, not a fixed UTC offset.
-- Writers validate named time zones against PostgreSQL's supported zone names.
CREATE TABLE dl.tournament (
    league_id integer PRIMARY KEY REFERENCES dl.league(id),
    time_zone text NOT NULL CHECK (btrim(time_zone) <> ''),
    revision integer NOT NULL DEFAULT 0 CHECK (revision >= 0),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    published_at timestamptz,
    published_by integer REFERENCES dl.app_user(id),
    CHECK ((published_at IS NULL) = (published_by IS NULL))
);

-- Common typed rules. No rule payload is copied to individual matches.
-- wins_required is generated from best_of, never independently supplied.
-- Publication of the TOURNAMENT freezes every stage's format/best_of/selection
-- rules, including the final before its entrants have been selected.
--
-- A root stage draws from this league's drafted teams. A later stage selects
-- qualifier_count entrants from source_stage_id's certified qualifier places.
-- standings_finalized_at/by on the source stage record final publication: only
-- places 1 and 2 are certified. Final-stage champion follows its official result;
-- there is no separate complete-ranking or final-stage certification operation.
-- The initial publisher accepts only the two-stage pipeline described above;
-- a configurable qualifier_count alone does not implement a larger bracket.
CREATE TABLE dl.tournament_stage (
    id integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    tournament_id integer NOT NULL REFERENCES dl.tournament(league_id),
    position integer NOT NULL CHECK (position > 0),
    name text NOT NULL CHECK (btrim(name) <> ''),
    format text NOT NULL CHECK (format IN ('round_robin', 'single_elimination')),
    best_of integer NOT NULL CHECK (best_of > 0 AND best_of % 2 = 1),
    wins_required integer GENERATED ALWAYS AS (best_of / 2 + 1) STORED NOT NULL,
    source_stage_id integer REFERENCES dl.tournament_stage(id),
    qualifier_count integer CHECK (qualifier_count > 0),
    standings_finalized_at timestamptz,
    standings_finalized_by integer REFERENCES dl.app_user(id),
    UNIQUE (tournament_id, position),
    CHECK (source_stage_id IS NULL OR source_stage_id <> id),
    CHECK ((source_stage_id IS NULL) = (qualifier_count IS NULL)),
    CHECK ((standings_finalized_at IS NULL) = (standings_finalized_by IS NULL))
);

-- Membership/seeding is an independent fact, not inferred from current results.
-- final_place certifies only the source stage's two qualifiers (1 and 2).
-- Nonqualifiers remain NULL. It freezes when the final is published, even if
-- later corrections change live records. Final-stage entries use seeds 1 and 2;
-- their final_place remains NULL because the final result identifies the winner.
-- Live wins/losses remain derived from current official results.
-- Once published, the final retains exactly its two original entries and fixture.
-- A finalist withdrawal causes a forfeit, never a replacement admission.
CREATE TABLE dl.stage_entry (
    stage_id integer NOT NULL REFERENCES dl.tournament_stage(id),
    team_id integer NOT NULL REFERENCES dl.team(id),
    seed integer CHECK (seed > 0),
    final_place integer CHECK (final_place IN (1, 2)),
    entered_by integer NOT NULL REFERENCES dl.app_user(id),
    entered_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (stage_id, team_id),
    UNIQUE (stage_id, seed),
    UNIQUE (stage_id, final_place)
);
CREATE INDEX stage_entry_team_idx ON dl.stage_entry (team_id);

-- A logical round has a dated playing window; it is not intrinsically a week.
-- The initial schedule allocates one regular-season fixture per team per week
-- (or a bye), but later makeups retain the original round assignment.
-- Normal rounds contribute to their stage's record. A tiebreak round is one
-- qualification attempt, with every selected pair playing once in its window;
-- a team may therefore play multiple fixtures in that round. Another unresolved
-- tie creates another round, never an overwrite or a custom SQL intervention.
-- Tiebreaks reuse the stage's entrants and best_of. Their results are read per
-- round and never added to the normal season totals, even across repeat attempts.
CREATE TABLE dl.tournament_round (
    id integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    stage_id integer NOT NULL REFERENCES dl.tournament_stage(id),
    number integer NOT NULL CHECK (number > 0),
    purpose text NOT NULL DEFAULT 'normal' CHECK (purpose IN ('normal', 'tiebreak')),
    tiebreak_reason text CHECK (btrim(tiebreak_reason) <> ''),
    created_by integer NOT NULL REFERENCES dl.app_user(id),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    opens_at timestamptz NOT NULL CHECK (isfinite(opens_at)),
    closes_at timestamptz NOT NULL CHECK (isfinite(closes_at)),
    UNIQUE (stage_id, number),
    CHECK ((purpose = 'tiebreak') = (tiebreak_reason IS NOT NULL)),
    CHECK (opens_at < closes_at)
);

-- Opponent columns guarantee exactly two distinct teams on every row.
-- Do not add per-column (round_id, team_a_id)/(round_id, team_b_id) UNIQUE
-- constraints: they miss a team appearing on different sides in two fixtures.
-- Do not add a league-wide unique opponent pair: finals and tiebreaks are rematches.
-- Same-tournament/stage membership and format-specific pair uniqueness are
-- validated by the schedule writer, following the normal FK paths through joins.
CREATE TABLE dl.match (
    id integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    round_id integer NOT NULL REFERENCES dl.tournament_round(id),
    team_a_id integer NOT NULL REFERENCES dl.team(id),
    team_b_id integer NOT NULL REFERENCES dl.team(id),
    revision integer NOT NULL DEFAULT 0 CHECK (revision >= 0),
    vod_url text CHECK (btrim(vod_url) <> ''),
    team_a_submission_id integer,
    team_b_submission_id integer,
    current_decision_id integer,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    CHECK (team_a_id <> team_b_id),
    CHECK (team_a_submission_id IS NULL OR team_b_submission_id IS NULL
           OR team_a_submission_id <> team_b_submission_id)
);
CREATE INDEX match_round_idx ON dl.match (round_id);
CREATE INDEX match_team_a_idx ON dl.match (team_a_id);
CREATE INDEX match_team_b_idx ON dl.match (team_b_id);

-- Managers are fixed, so a team is an unambiguous availability owner. Its user
-- and league follow through team; no duplicated user_id/league_id is needed.
-- This header gives a whole availability edit its own revision, including edits
-- that remove the final window. time_zone is the owner's input/display zone.
-- display_step_minutes is a painting-grid preference, not a tournament rule.
-- Neither changing the zone nor the grid reinterprets already stored instants.
CREATE TABLE dl.team_availability (
    team_id integer PRIMARY KEY REFERENCES dl.team(id),
    time_zone text NOT NULL CHECK (btrim(time_zone) <> ''),
    display_step_minutes integer NOT NULL DEFAULT 60 CHECK (display_step_minutes > 0),
    revision integer NOT NULL DEFAULT 0 CHECK (revision >= 0)
);

-- Dated, half-open intervals [starts_at, ends_at). Adjacent/overlapping windows
-- mean their union; overlap is not a contradictory state. Writers may coalesce
-- them. Exact duplicate rows are prohibited.
-- Hour granularity is an input policy, NOT a UTC timestamp alignment constraint:
-- a local whole hour can map to :30 or :45 UTC. No recurrence semantics here.
-- There is no match/round reference or booking state. The interface fetches both
-- owners' windows for a displayed date range, converts them to the viewer's zone,
-- and overlays them. Common free time is the intersection of each owner's union
-- of ranges; it is computed when reading, never saved as an agreement.
CREATE TABLE dl.availability_window (
    id integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    team_id integer NOT NULL REFERENCES dl.team_availability(team_id),
    starts_at timestamptz NOT NULL CHECK (isfinite(starts_at)),
    ends_at timestamptz NOT NULL CHECK (isfinite(ends_at)),
    UNIQUE (team_id, starts_at, ends_at),
    CHECK (starts_at < ends_at)
);

-- One immutable report, including its author and score. No separate outcome
-- payload table and no copying into a second official-score schema.
-- reporting_side NULL means a commissioner-authored result, never a participant
-- vote even when the commissioner manages a team. Each match holds one current
-- submission pointer per side. A player can replace their own pointer with a new
-- immutable report until official; neither timestamps nor counters choose latest.
-- Forfeits have an awarded winner and no invented series score. Partial games
-- before a forfeit are outside the initial aggregate-score model.
-- winner_side is generated for every valid assertion. Only a forfeit requires
-- a separately supplied winner; a played outcome derives it from the scores.
CREATE TABLE dl.result_submission (
    id integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    match_id integer NOT NULL REFERENCES dl.match(id),
    reporting_side text CHECK (reporting_side IN ('a', 'b')),
    submitted_by integer NOT NULL REFERENCES dl.app_user(id),
    submitted_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    outcome_kind text NOT NULL CHECK (outcome_kind IN ('played', 'forfeit')),
    team_a_game_wins integer CHECK (team_a_game_wins >= 0),
    team_b_game_wins integer CHECK (team_b_game_wins >= 0),
    awarded_winner_side text CHECK (awarded_winner_side IN ('a', 'b')),
    winner_side text GENERATED ALWAYS AS (
        CASE WHEN outcome_kind = 'forfeit' THEN awarded_winner_side
             WHEN team_a_game_wins > team_b_game_wins THEN 'a'::text
             WHEN team_b_game_wins > team_a_game_wins THEN 'b'::text
        END
    ) STORED NOT NULL,
    UNIQUE (match_id, id),
    CHECK (
        (outcome_kind = 'played'
         AND team_a_game_wins IS NOT NULL AND team_b_game_wins IS NOT NULL
         AND team_a_game_wins <> team_b_game_wins
         AND awarded_winner_side IS NULL)
        OR
        (outcome_kind = 'forfeit'
         AND team_a_game_wins IS NULL AND team_b_game_wins IS NULL
         AND awarded_winner_side IS NOT NULL)
    )
);
ALTER TABLE dl.match ADD CONSTRAINT match_team_a_submission_fk
    FOREIGN KEY (id, team_a_submission_id)
    REFERENCES dl.result_submission (match_id, id);
ALTER TABLE dl.match ADD CONSTRAINT match_team_b_submission_fk
    FOREIGN KEY (id, team_b_submission_id)
    REFERENCES dl.result_submission (match_id, id);

-- An immutable official decision selects a report; it does not rewrite it.
-- Agreement cites both current reports: select side A's and support with side B's.
-- The writer verifies their sides, authors, score validity and matching payloads.
-- Both reports carry attribution; an automatic agreement has no separate decider.
-- A commissioner can select either current report or create a new submission and
-- decision atomically. Every commissioner decision requires an actor and reason.
CREATE TABLE dl.match_decision (
    id integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    match_id integer NOT NULL REFERENCES dl.match(id),
    selected_submission_id integer NOT NULL,
    supporting_submission_id integer,
    authority text NOT NULL CHECK (authority IN ('agreement', 'commissioner')),
    decided_by integer REFERENCES dl.app_user(id),
    decided_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    reason text CHECK (btrim(reason) <> ''),
    UNIQUE (match_id, id),
    FOREIGN KEY (match_id, selected_submission_id)
        REFERENCES dl.result_submission (match_id, id),
    FOREIGN KEY (match_id, supporting_submission_id)
        REFERENCES dl.result_submission (match_id, id),
    CHECK (
        (authority = 'agreement'
         AND supporting_submission_id IS NOT NULL
         AND supporting_submission_id <> selected_submission_id
         AND decided_by IS NULL AND reason IS NULL)
        OR
        (authority = 'commissioner' AND supporting_submission_id IS NULL
         AND decided_by IS NOT NULL AND reason IS NOT NULL)
    )
);
ALTER TABLE dl.match ADD CONSTRAINT match_current_decision_fk
    FOREIGN KEY (id, current_decision_id)
    REFERENCES dl.match_decision (match_id, id);

-- The composite pointer FK buys concrete validation: the selected official
-- decision cannot accidentally belong to another match.
-- current_decision_id alone determines the official result. Do not also infer
-- it from max(id) or maintain an independently editable is_current flag.
-- Current participant pointers remain as history and freeze once official.
-- Commissioner corrections change only the official pointer, never those reports.
-- Mismatching reports need review only while the fixture is active and has no
-- official decision. No flag table, player appeal queue or review watermark.

-- A fixed withdrawal consequence, immutable through the API. No row means the
-- fixture is active and its official result counts. There are no standalone
-- cancel/exclude/restore/reopen controls. Withdrawal excludes fixtures with an
-- official result or either participant report, and cancels fixtures with none.
-- Excluding unresolved reports preserves evidence without claiming it was unplayed.
-- The presence of this row removes the fixture from its round's record; there
-- is no independently editable inclusion flag. The official-result view derives
-- counts_in_standings from row absence. Round purpose remains unchanged.
CREATE TABLE dl.match_disposition (
    match_id integer PRIMARY KEY REFERENCES dl.match(id),
    cancelled boolean NOT NULL,
    decided_by integer NOT NULL REFERENCES dl.app_user(id),
    decided_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    reason text NOT NULL CHECK (btrim(reason) <> '')
);

-- One terminal withdrawal event for this team's tournament. Its tournament is
-- reached through team.league_id; writers require it to exist. No row means
-- eligible. The application offers no reinstatement or independent status edit.
-- Before final publication, the SAME operation applies the fixed dispositions
-- to all this team's first-stage fixtures, including every tiebreak attempt.
-- After final publication, source-stage results/qualification remain unchanged;
-- a finalist withdrawal awards the other finalist a forfeit on the same fixture.
CREATE TABLE dl.tournament_withdrawal (
    team_id integer PRIMARY KEY REFERENCES dl.team(id),
    decided_by integer NOT NULL REFERENCES dl.app_user(id),
    decided_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    reason text NOT NULL CHECK (btrim(reason) <> '')
);

-- Small read projections, not a final HTTP/API surface.
CREATE VIEW dl.match_participant WITH (security_invoker = true) AS
SELECT id AS match_id, 'a'::text AS side, team_a_id AS team_id FROM dl.match
UNION ALL
SELECT id AS match_id, 'b'::text AS side, team_b_id AS team_id FROM dl.match;

CREATE VIEW dl.match_official_result WITH (security_invoker = true) AS
SELECT m.id AS match_id, d.id AS decision_id, d.authority, d.decided_by,
       d.decided_at, d.reason,
       s.id AS submission_id, s.outcome_kind,
       s.team_a_game_wins, s.team_b_game_wins, s.winner_side,
       coalesce(a.cancelled, false) AS cancelled,
       (a.match_id IS NULL) AS counts_in_standings
FROM dl.match m
JOIN dl.match_decision d ON d.id = m.current_decision_id
JOIN dl.result_submission s ON s.id = d.selected_submission_id
LEFT JOIN dl.match_disposition a ON a.match_id = m.id;

-- Unranked live records from NORMAL rounds only. Tiebreak results are separate
-- qualification evidence, not extra season wins. Byes/cancellations create no wins.
-- A forfeit contributes a series win/loss but no fabricated game wins/losses.
-- Withdrawal writes its required dispositions atomically; the view follows them.
-- This deliberately does not silently choose a tiebreak order or qualify teams.
CREATE VIEW dl.stage_record WITH (security_invoker = true) AS
WITH counted AS (
    SELECT r.stage_id, p.team_id, p.side, o.winner_side, o.outcome_kind,
           CASE WHEN p.side = 'a' THEN o.team_a_game_wins
                ELSE o.team_b_game_wins END AS game_wins,
           CASE WHEN p.side = 'a' THEN o.team_b_game_wins
                ELSE o.team_a_game_wins END AS game_losses
    FROM dl.match_official_result o
    JOIN dl.match m ON m.id = o.match_id
    JOIN dl.tournament_round r ON r.id = m.round_id
    JOIN dl.match_participant p ON p.match_id = m.id
    WHERE r.purpose = 'normal' AND o.counts_in_standings AND NOT o.cancelled
)
SELECT e.stage_id, e.team_id,
       (w.team_id IS NOT NULL) AS withdrawn,
       count(c.team_id) AS series_completed,
       count(c.team_id) FILTER (WHERE c.side = c.winner_side) AS series_wins,
       count(c.team_id) FILTER (WHERE c.side <> c.winner_side) AS series_losses,
       count(c.team_id) FILTER (WHERE c.outcome_kind = 'played') AS played_series,
       coalesce(sum(c.game_wins), 0::bigint) AS recorded_game_wins,
       coalesce(sum(c.game_losses), 0::bigint) AS recorded_game_losses
FROM dl.stage_entry e
LEFT JOIN counted c ON c.stage_id = e.stage_id AND c.team_id = e.team_id
LEFT JOIN dl.tournament_withdrawal w ON w.team_id = e.team_id
GROUP BY e.stage_id, e.team_id, w.team_id;

-- Per-round records for scheduled participants, including a separate table for
-- every tiebreak attempt. Do not sum attempts: a later attempt may contain only
-- the still-tied subset. Zero totals do not establish that an attempt is over;
-- the app also reads its fixtures, dispositions and unresolved participant reports.
CREATE VIEW dl.round_record WITH (security_invoker = true) AS
SELECT r.id AS round_id, r.stage_id, r.purpose, p.team_id,
       (w.team_id IS NOT NULL) AS withdrawn,
       count(o.match_id) AS series_completed,
       count(o.match_id) FILTER (WHERE p.side = o.winner_side) AS series_wins,
       count(o.match_id) FILTER (WHERE p.side <> o.winner_side) AS series_losses,
       count(o.match_id) FILTER (WHERE o.outcome_kind = 'played') AS played_series,
       coalesce(sum(CASE WHEN p.side = 'a' THEN o.team_a_game_wins
                         ELSE o.team_b_game_wins END), 0::bigint) AS recorded_game_wins,
       coalesce(sum(CASE WHEN p.side = 'a' THEN o.team_b_game_wins
                         ELSE o.team_a_game_wins END), 0::bigint) AS recorded_game_losses
FROM dl.tournament_round r
JOIN dl.match m ON m.round_id = r.id
JOIN dl.match_participant p ON p.match_id = m.id
LEFT JOIN dl.match_official_result o
    ON o.match_id = m.id AND o.counts_in_standings AND NOT o.cancelled
LEFT JOIN dl.tournament_withdrawal w ON w.team_id = p.team_id
GROUP BY r.id, r.stage_id, r.purpose, p.team_id, w.team_id;

ALTER TABLE dl.tournament ENABLE ROW LEVEL SECURITY;
ALTER TABLE dl.tournament_stage ENABLE ROW LEVEL SECURITY;
ALTER TABLE dl.stage_entry ENABLE ROW LEVEL SECURITY;
ALTER TABLE dl.tournament_round ENABLE ROW LEVEL SECURITY;
ALTER TABLE dl.match ENABLE ROW LEVEL SECURITY;
ALTER TABLE dl.team_availability ENABLE ROW LEVEL SECURITY;
ALTER TABLE dl.availability_window ENABLE ROW LEVEL SECURITY;
ALTER TABLE dl.result_submission ENABLE ROW LEVEL SECURITY;
ALTER TABLE dl.match_decision ENABLE ROW LEVEL SECURITY;
ALTER TABLE dl.match_disposition ENABLE ROW LEVEL SECURITY;
ALTER TABLE dl.tournament_withdrawal ENABLE ROW LEVEL SECURITY;

REVOKE ALL ON TABLE
    dl.tournament, dl.tournament_stage, dl.stage_entry, dl.tournament_round,
    dl.match, dl.team_availability, dl.availability_window,
    dl.result_submission, dl.match_decision,
    dl.match_disposition, dl.tournament_withdrawal,
    dl.match_participant,
    dl.match_official_result, dl.stage_record, dl.round_record
FROM PUBLIC;

-- CONTROLLED-WRITE CONTRACTS (implemented by the companion proposal SQL files)
--
-- Common transaction pattern:
-- * Keep require_writer(), SERIALIZABLE transactions and whole-call retries.
--   Authenticate/authorize, check the operation's freshness token, advance the
--   relevant revisions and apply the complete operation atomically. All validation
--   reads belong inside the retried transaction. Preserve the client's original
--   expected revision or own-report ID on retry; stale edits/previews return PT409.
-- * Expected revisions protect decisions made from an earlier screen. SERIALIZABLE
--   protects concurrent database operations. Neither replaces business checks.
--   Use ordinary revision UPDATEs, without an additional SELECT FOR UPDATE or
--   advisory-lock protocol. Updates already acquire the necessary row locks.
-- * Tournament configuration, withdrawal, tiebreak creation and final publication
--   check the expected tournament revision. Commissioner result operations check
--   the expected match revision. Player submissions instead check their own current
--   report ID as described below; an opponent's report is not a stale player edit.
--   All result operations bump both tournament and match revisions, so competitive
--   changes invalidate commissioner previews even without a player revision check.
-- * Update tournament before matches (matches in ascending id order for a batch).
--   Availability updates only its own header/revision. VOD-only edits update only
--   the match revision. Keep a consistent UPDATE order to limit deadlocks.
--   The shared tournament UPDATE still causes contention; accept that simple
--   freshness mechanism at this scale rather than adding more version machinery.
-- * Allocate a new stage round number after the tournament revision UPDATE.
--   Keep UNIQUE(stage_id, number). There are no report/status/disposition counters;
--   pointers select reports/decisions, and withdrawals/dispositions are single
--   immutable events. Do not blanket-retry unique-constraint violations.
-- * Resolve league/manager scope through joins. Validate stage entries, opponents,
--   source stages and actors against their competition. Authenticate all authors.
-- * Submissions, decisions, dispositions and withdrawals are append-only
--   through the API. Keep the FK/CHECK/UNIQUE constraints as local integrity rules.
-- * Emit existing account-scoped wake-ups on successful mutations. Durable rows,
--   not NOTIFY deliveries, drive the UI. No custom SQL is an operational workflow.
--
-- configure_tournament / configure_stage / publish_tournament:
-- * Check/bump tournament revision for configuration and publication. Freeze
--   stage rules, first-stage membership/seeds and existing fixture assignments.
--   Only publish_final admits the final's two entries and creates its normal
--   fixture after publication.
-- * Require a completed draft. Add a publication guard to existing draft undo,
--   and read draft completion during publication in the same SERIALIZABLE call.
--   Both operations must be correct in either serial order; a lock cannot supply
--   the missing rule. If publication also updates league.revision, update league
--   before tournament consistently. Do not remove/reorder drafted teams.
-- * Validate named time zones and the supported two-stage pipeline: round_robin
--   followed by single_elimination with two qualifiers from that round robin;
--   both stages belong to this tournament, with source position strictly earlier.
--   Freeze format/best_of/source/qualifier settings for BOTH stages at publication.
-- * Require at least two drafted entrants. Generate complete first-stage entries
--   and a single round robin: every unordered pair once across NORMAL rounds,
--   each team at most once per normal round. Even N has N-1 rounds; odd N has N
--   rounds with one bye per team. No persisted bye fixture or automatic bye win.
-- * Use weekly local windows in the tournament zone, respecting DST. Publish only
--   normal first-stage rounds here; final entries/fixture await publish_final.
--
-- availability / deadlines / VOD:
-- * Availability is advisory. Owner edits check/bump team_availability.revision;
--   team.manager is the owner. Accept finite actual instants/ranges, and validate
--   the named zone. The display step controls painting, not UTC alignment.
-- * Changing zone preserves stored instants; copying a week preserves local wall
--   times. League members read availability. Intersect each owner's merged ranges
--   in the requested display window; no booking, acceptance or result dependency.
-- * The commissioner can change a round's playing window using the tournament
--   revision. Extensions apply to that whole round; no per-match deadline override.
--   Makeups retain their original round assignment. Published stage/round/purpose
--   and opponent assignments never change. No replacement-fixture operation.
-- * VOD editing is separate from reporting/agreement; validate the URL in its
--   authorized writer. Spectator observations cannot change competitive results.
--
-- submit_result (both participants, including replacement of their own report):
-- * Require a published, active fixture with no official decision. An active
--   fixture has no match_disposition; removed fixtures are read-only to players.
--   Resolve the authenticated player's side through the match's team managers.
--   Availability has no bearing on reporting. There is no first-reporter role.
-- * Accept expected_previous_submission_id for that player's OWN current pointer,
--   NULL for their first report. Require a NULL-safe match to the stored pointer;
--   a stale same-player edit fails. Do not require expected match/tournament revision
--   for this operation, and never compare freshness against the opponent's pointer.
-- * Advance tournament then match revision before inserting the report; on a
--   serialization failure retry the whole call with the same own-report ID and
--   payload. Re-read and revalidate current reports, disposition and official status.
--   Concurrent first reports can both succeed after retry: the opponent's new report
--   leaves the caller's own expected NULL valid. A same-side change or a newly
--   official/removed fixture still rejects a stale edit. Bounded retries can exhaust.
-- * Append an immutable submission and change only the caller's side pointer.
--   Preserve the other side's pointer and all historical reports. Either player
--   may correct their own report while unresolved, including during disagreement.
-- * Validate a played score against the stage: one side equals wins_required and
--   the other is below it. Forfeits name a winner with NULL game counts. Resolve
--   stage rules through joins; do not duplicate best_of on every submission.
-- * Compare the two CURRENT reports after each submission. Validate the pointers
--   refer to this match and reporting_side a/b respectively; commissioner-authored
--   reports cannot serve as participant votes. Compare outcome_kind, canonical A/B
--   game-win counts and awarded_winner_side with NULL-safe equality; VOD is irrelevant.
--   Matching valid reports create an agreement decision citing both exact IDs and
--   set current_decision_id in the same transaction. No separate confirmation call.
-- * One missing report means awaiting a report; two unequal reports mean disagreement
--   in the commissioner queue. Derive this state only for active fixtures with no
--   official decision. No stored dispute flag, timer victory or player handoff state.
--   A player's correction can produce agreement without commissioner intervention.
-- * Once official, close participant submissions and freeze both participant
--   pointers. Only the commissioner can correct the official result. Different
--   retained reports after adjudication do not reopen a dispute or an appeal queue.
-- * Participants and commissioner can read current participant reports. This is
--   independent authorship, not a blind-submission policy. Controlled writers must
--   see both reports; filtered evidence must not appear missing or create agreement.
--   Official-result payloads are visible to league members; raw history stays scoped.
--
-- record_result / correct_result (commissioner):
-- * With an expected match revision and reason, resolve an active fixture by
--   selecting either current participant report or inserting a commissioner submission
--   (reporting_side NULL). Insert a commissioner decision, move the official
--   pointer atomically, and retain/freeze the participant pointers as evidence.
-- * Correct an official result by appending a commissioner submission and decision
--   with a reason. Keep all previous reports/decisions and the same fixture identity.
--   An excluded historical official result may be corrected, but stays excluded.
--   Do not create a first result for a removed fixture or reopen it to participants.
-- * A request that results in no correction leaves the database unchanged. This
--   version does not record informal post-result requests or their rejection.
-- * Corrections update live records but never change certified qualifiers, final
--   entrants or existing tiebreak opponents. No rollback/reseeding workflow.
--
-- withdraw_team (commissioner, one complete operation):
-- * Require a published tournament, expected tournament revision, a member team
--   with no withdrawal row, and a reason. Withdrawal is terminal for this tournament;
--   no reinstatement action. Preserve team/draft/entry history and manager read access.
-- * Before final publication, insert the withdrawal and remove ALL this team's
--   first-stage fixtures, across normal and tiebreak rounds. For each fixture with
--   an official result or either participant report, insert an exclusion (cancelled=false).
--   With none, insert a cancellation (cancelled=true). Either row removes the
--   fixture from standings; inclusion is derived rather than separately editable.
--   Preserve an existing disposition if the other team already withdrew. Bump
--   affected match revisions. These consequences are automatic, never checkboxes.
-- * Removed fixtures retain submissions but require no further reports or
--   commissioner resolution. Their dispositions satisfy completion checks. No new
--   tiebreak attempt can admit a withdrawn team. Standings derive from the remaining
--   counted fixtures, with no forged scores or cached-total adjustments.
-- * Once the final is published, source-stage results and qualification stay as
--   recorded. If the withdrawing team is a finalist and the final has no official
--   result, insert a commissioner forfeit awarding the other finalist, with the
--   withdrawal reason, retain both participant pointers, and keep both entries/fixture.
--   The official decision closes any unresolved disagreement. No substitute team.
-- * An already official final is unchanged by a later withdrawal. A correction to
--   its factual result uses correct_result, never reopens finalist selection.
--
-- create_tiebreak_round / repeat_tiebreak (same commissioner operation):
-- * Select at least two still-tied eligible entrants, a playing window and a reason;
--   preview all pairs. Check expected tournament revision, publication, round_robin
--   format, and that source-stage qualification has not been finalized.
-- * Before EVERY attempt, require all normal fixtures and earlier attempts to have
--   official decisions or withdrawal dispositions. Active unresolved reports
--   must be resolved first. Read current results/eligibility in the same transaction.
-- * After advancing tournament revision, allocate the next round number and insert
--   one tiebreak round plus every unordered selected pair exactly once. Reuse the
--   stage best_of and ordinary submission/agreement/commissioner result writers.
--   Multiple fixtures for a team in this round are intentional; prior encounters
--   are allowed. Do not add tiebreak wins to normal-season totals.
-- * Show normal stage_record and each attempt's round_record separately. Apply the
--   announced criteria; if a cycle survives, repeat with the still-tied subset.
--   No fixed attempt limit, automatic timer winner, or summing different attempts.
-- * Later corrections leave earlier rounds intact. Reassess qualification from
--   corrected evidence; if needed create a fresh attempt. There is no separate
--   cancel/exclude/replace-tiebreak workflow. Withdrawal uses its fixed operation.
-- * The exact secondary criteria, evidence used and forfeit treatment must be
--   announced before launch. Commissioner operation avoids a configurable ranking
--   engine, but does not substitute for a clear competition rule.
--   The writer accepts the commissioner's selection as certification that these
--   entrants remain tied; it validates eligibility/completion, not a ranking formula.
--
-- publish_final (commissioner, one complete operation):
-- * Check expected tournament revision and an unpublished final. Require every
--   first-stage fixture, including all tiebreaks, to be official or removed by
--   withdrawal. Resolve all active unresolved reports and qualification ties.
-- * Certify ONLY two eligible qualifiers as final_place 1 and 2 in the source
--   stage; every other entry stays NULL. Do not resolve unrelated lower-place ties.
--   The ordered qualifier argument is the commissioner's certification that the
--   announced criteria were applied and all ties affecting these places resolved.
--   Set source standings_finalized_at/by, insert exactly two final entries with
--   seeds 1/2, and create the final's normal round and one fixture atomically.
-- * That commit is final publication and freezes selection. No separate provisional
--   certification state, started-final marker, full ranking, or replacement action.
--   Final-stage final_place/finalization metadata stay NULL; its official result
--   identifies the champion. Historical score corrections cannot promote a third team.
-- * If fewer than two eligible qualifiers remain, reject publication; never create
--   an invented opponent or infer a champion. A no-champion closure policy remains
--   a launch decision outside this two-entrant final operation.
