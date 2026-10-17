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
-- Publication defaults to best_of = 3 and 5; callers may choose other odd values.
-- No manager replacement, individual game records, recurring calendars,
-- appointment booking, generic metadata, rule-version infrastructure, or new
-- triggers. Availability is advisory and independent of match/result lifecycle.
--
-- All tournament data, including reports and decision history, is public.
-- The companion grants expose controlled SERIALIZABLE writers.
--
-- INVARIANT OWNERSHIP
-- CHECK/UNIQUE/FK constraints below enforce local shapes and selected ownership
-- relationships. The companion writers enforce publication immutability, actor
-- authorization, complete schedules, agreement and scores against stage.best_of.
-- The write contracts are specified at the end; qualification choices are
-- commissioner-certified against announced criteria, not automatically ranked.

-- One-to-one extension of a league, independently revisioned from the draft.
-- Created atomically at publication; no separately configured/unpublished row.
CREATE TABLE dl.tournament (
    league_id integer PRIMARY KEY REFERENCES dl.league(id),
    revision integer NOT NULL DEFAULT 0 CHECK (revision >= 0),
    published_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    published_by integer NOT NULL REFERENCES dl.app_user(id)
);

-- Common typed rules. No rule payload is copied to individual matches.
-- wins_required is generated from best_of, never independently supplied.
-- Publication of the TOURNAMENT freezes every stage's format/best_of/selection
-- rules, including the final before its entrants have been selected.
--
-- A root stage draws from this league's drafted teams. A later stage selects
-- qualifier_count entrants from source_stage_id. Membership in the later stage
-- is the qualification record; there is no duplicated placing on the source.
-- qualification_finalized_at/by freeze that selection, not the live standings.
-- These tables describe stages generally. The current publisher constructs the
-- round-robin/two-team-final arrangement; it does not accept a stage graph or
-- consult a supported-format registry. Other generators can be added when needed.
-- Format is a label supplied by that generator, not a schema-level allowlist.
CREATE TABLE dl.tournament_stage (
    id integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    tournament_id integer NOT NULL REFERENCES dl.tournament(league_id),
    position integer NOT NULL CHECK (position > 0),
    name text NOT NULL CHECK (btrim(name) <> ''),
    format text NOT NULL CHECK (btrim(format) <> ''),
    best_of integer NOT NULL CHECK (best_of > 0 AND best_of % 2 = 1),
    wins_required integer GENERATED ALWAYS AS (best_of / 2 + 1) STORED NOT NULL,
    source_stage_id integer REFERENCES dl.tournament_stage(id),
    qualifier_count integer CHECK (qualifier_count > 0),
    qualification_finalized_at timestamptz,
    qualification_finalized_by integer REFERENCES dl.app_user(id),
    UNIQUE (tournament_id, position),
    CHECK (source_stage_id IS NULL OR source_stage_id <> id),
    CHECK ((source_stage_id IS NULL) = (qualifier_count IS NULL)),
    CHECK ((qualification_finalized_at IS NULL) = (qualification_finalized_by IS NULL))
);

-- Membership is an independent fact, not inferred from current results. The
-- final's two entries are the sole qualification record, with no first/second
-- ordering or seeding. A/B orientation has no competitive meaning. Corrections
-- and withdrawals never replace the finalists. Live records remain derived.
CREATE TABLE dl.stage_entry (
    stage_id integer NOT NULL REFERENCES dl.tournament_stage(id),
    team_id integer NOT NULL REFERENCES dl.team(id),
    entered_by integer NOT NULL REFERENCES dl.app_user(id),
    entered_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (stage_id, team_id)
);
CREATE INDEX stage_entry_team_idx ON dl.stage_entry (team_id);

-- A suggested calendar date opens the availability widget to that week. It is
-- optional, advisory, and never gates reporting or progression. The publisher
-- suggests dates seven days apart. Late matches keep their original round/date;
-- players may browse availability in any week without an extension or reschedule.
-- Normal rounds contribute to their stage's record. A tiebreak round is one
-- qualification attempt, with every selected pair playing once;
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
    suggested_start_date date CHECK (isfinite(suggested_start_date)),
    UNIQUE (stage_id, number),
    CHECK ((purpose = 'tiebreak') = (tiebreak_reason IS NOT NULL))
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

-- Availability belongs to a team. Managers are fixed after draft start. Its user
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

-- One terminal withdrawal event. Its tournament follows team.league_id.
-- Before final publication, withdrawal removes this team's source-stage fixtures.
-- After final publication it only records withdrawal: no automatic forfeit,
-- replacement finalist, or closure. An unplayable final may remain unresolved.
CREATE TABLE dl.tournament_withdrawal (
    team_id integer PRIMARY KEY REFERENCES dl.team(id),
    decided_by integer NOT NULL REFERENCES dl.app_user(id),
    decided_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    reason text NOT NULL CHECK (btrim(reason) <> '')
);

-- Withdrawal is the only fixture-removal operation. Record its cause once;
-- attribution/reason/time come from the referenced withdrawal. If both opponents
-- withdraw, the first disposition remains. No independent cancel/restore control.
-- Row absence means the fixture counts. For display, a removed fixture with no
-- participant reports or official decision is a cancellation; otherwise it is
-- excluded evidence. Those pointers cannot become populated after removal, so
-- this distinction needs no stored flag. Existing official results can be corrected.
CREATE TABLE dl.match_disposition (
    match_id integer PRIMARY KEY REFERENCES dl.match(id),
    withdrawal_team_id integer NOT NULL REFERENCES dl.tournament_withdrawal(team_id)
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
    WHERE r.purpose = 'normal' AND o.counts_in_standings
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
    ON o.match_id = m.id AND o.counts_in_standings
LEFT JOIN dl.tournament_withdrawal w ON w.team_id = p.team_id
GROUP BY r.id, r.stage_id, r.purpose, p.team_id, w.team_id;


-- CONTROLLED-WRITE CONTRACTS (companion proposal SQL files)
--
-- All writes authenticate, authorize, and run in SERIALIZABLE transactions.
-- Retry complete transactions on 40001/40P01 with original arguments; PT409
-- requires a fresh view. Read current evidence inside the retried transaction.
-- No extra advisory-lock or SELECT FOR UPDATE protocol is required.
--
-- publish_tournament checks the LEAGUE revision and completed draft, then creates
-- the tournament, both stages, source entries and weekly suggested fixture dates
-- together. Tournament revision starts at zero. No configuration calls, pending
-- tournament state, arbitrary stage graph, or supported-format metadata table.
-- Draft undo is blocked once a tournament exists. SERIALIZABLE protects the
-- publication/undo race. Stage rules and fixture assignments stay fixed afterward.
--
-- Other tournament-wide operations check/bump tournament revision. Result writes
-- bump tournament then match revisions; batch match updates use ascending IDs.
-- Commissioner results check match revision. Player reports instead check their
-- OWN current submission ID, so an opponent's first report is not a stale edit.
-- Availability uses its own header revision; VOD-only edits use match revision.
-- Successful changes notify league accounts after commit through existing helpers.
-- Notifications are wake-ups; current database state drives the interface.
--
-- suggested_start_date is a calendar label, never an opening time or deadline.
-- An edit changes that hint only. Late matches stay in their original round;
-- later normal rounds can proceed while earlier matches remain unresolved.
-- Finals/tiebreaks can be published without a suggested date. Qualification still
-- waits for required results, independent of the suggested calendar dates.
--
-- Availability is a lightweight aid owned by team.manager_user_id. Store actual
-- instants, accept overlapping ranges as their union, and permit [] to clear them.
-- Changing the display zone preserves instants; copying a week preserves local
-- wall times. DST handling belongs to availability, not tournament scheduling.
-- There is no booking/acceptance state, phase gate, or added setup recovery workflow.
--
-- submit_result appends immutable evidence and moves only the caller's pointer.
-- Scores use fixture A/B orientation and must finish the stage's best-of series.
-- Forfeits have an awarded side and no invented game counts. Matching current
-- reports from opposite participants produce an agreement decision atomically.
-- Missing/different reports mean pending/disagreement only while the fixture is
-- active and unofficial. Official results freeze player pointers. All data is public.
--
-- record_result resolves an active fixture by selecting a current player report
-- or creating commissioner evidence. correct_result appends a new official
-- decision with a reason; an unchanged payload is a no-op. Corrections preserve
-- fixture identity and all history, and never change frozen qualification.
-- Removed fixtures cannot gain new reports or an initial result; corrections
-- to an existing official result remain possible but never restore its inclusion.
-- VOD editing is independent of reporting and competitive results.
--
-- withdraw_team records a terminal withdrawal. Before qualification is finalized,
-- it links all affected source-stage fixtures (normal and tiebreak) to that event,
-- preserving an existing disposition from an earlier opponent withdrawal. Removed
-- fixtures no longer count and need no resolution. After final publication, leave
-- source results and the final untouched. No automatic forfeit or replacement.
-- Normal result operations remain available for the final if a result is supplied.
--
-- create_tiebreak_round requires earlier source fixtures to be official or removed,
-- and at least two distinct eligible entrants. It adds every selected pair once.
-- The commissioner certifies a tie affecting WHO qualifies; SQL does not rank.
-- Repeated attempts remain separate round records, never extra season wins.
--
-- publish_final requires all source fixtures resolved/removed and two distinct
-- eligible teams. Their input order is irrelevant: canonical team-ID order assigns
-- A/B without advantage. Insert final entries, round and fixture, and mark source
-- qualification_finalized_at/by atomically. Final membership is the sole qualifier
-- record; there are no places/seeds and no need to break a tie between two teams
-- that both advance. Later corrections can change live records, not that selection.
-- An official final result identifies the champion. If a final cannot be created
-- or played, the tournament may remain incomplete indefinitely; no closure action.
