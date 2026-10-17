-- Tournament integration. Applied atomically as dl_owner by the migration runner.
-- Keep sql/schema.sql and sql/grants.sql as the fixed installation baseline.

-- Storage and public projections

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
-- Explicit grants below expose controlled SERIALIZABLE writers.
--
-- INVARIANT OWNERSHIP
-- CHECK/UNIQUE/FK constraints below enforce local shapes and selected ownership
-- relationships. The writers below enforce publication immutability, actor
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


-- CONTROLLED-WRITE CONTRACTS
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

-- Publication, availability, and draft undo guard

-- Private shared helpers. Entry points authenticate even when an edit is a no-op.
CREATE FUNCTION dl.tournament_commissioner(p_league integer) RETURNS integer
LANGUAGE plpgsql SET search_path = pg_catalog, dl, pg_temp AS $$
DECLARE u integer := require_writer();
BEGIN
    IF NOT EXISTS (SELECT FROM league WHERE id = p_league) THEN
        RAISE SQLSTATE 'PT404' USING MESSAGE = 'league_not_found';
    END IF;
    IF NOT EXISTS (SELECT FROM league WHERE id = p_league AND commissioner_user_id = u) THEN
        RAISE SQLSTATE 'PT409' USING MESSAGE = 'action_not_permitted';
    END IF;
    RETURN u;
END $$;

CREATE FUNCTION dl.tournament_check_revision(p_league integer, p_revision integer)
RETURNS void LANGUAGE plpgsql SET search_path = pg_catalog, dl, pg_temp AS $$
DECLARE rev integer;
BEGIN
    SELECT revision INTO rev FROM tournament WHERE league_id = p_league;
    IF NOT FOUND THEN RAISE SQLSTATE 'PT404' USING MESSAGE = 'tournament_not_found'; END IF;
    IF rev IS DISTINCT FROM p_revision THEN
        RAISE SQLSTATE 'PT409' USING MESSAGE = 'stale_revision';
    END IF;
END $$;

CREATE FUNCTION dl.tournament_bump(p_league integer) RETURNS void
LANGUAGE sql SET search_path = pg_catalog, dl, pg_temp AS $$
    UPDATE tournament SET revision = revision + 1 WHERE league_id = p_league
$$;

CREATE FUNCTION dl.tournament_validate_zone(p_zone text) RETURNS void
LANGUAGE plpgsql SET search_path = pg_catalog, dl, pg_temp AS $$
BEGIN
    IF p_zone IS NULL OR NOT EXISTS (SELECT FROM pg_timezone_names WHERE name = p_zone) THEN
        RAISE SQLSTATE 'PT400' USING MESSAGE = 'invalid_time_zone';
    END IF;
END $$;

CREATE FUNCTION dl.tournament_validate_availability_window(p_starts_at timestamptz, p_ends_at timestamptz)
RETURNS void LANGUAGE plpgsql SET search_path = pg_catalog, dl, pg_temp AS $$
BEGIN
    IF p_starts_at IS NULL OR p_ends_at IS NULL OR NOT isfinite(p_starts_at)
       OR NOT isfinite(p_ends_at) OR p_starts_at >= p_ends_at THEN
        RAISE SQLSTATE 'PT400' USING MESSAGE = 'invalid_availability_window';
    END IF;
END $$;

-- One atomic constructor for the implemented round-robin/two-team-final format.
-- The tables remain reusable; this operation does not accept a configurable graph.
-- Freshness is the completed draft's LEAGUE revision. Later operations use the
-- independently revisioned tournament created here, initially at revision zero.
CREATE FUNCTION dl.publish_tournament(p_league integer, p_league_revision integer,
    p_first_suggested_date date, p_season_best_of integer DEFAULT 3,
    p_final_best_of integer DEFAULT 5)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, dl, pg_temp AS $$
DECLARE u integer := tournament_commissioner(p_league); rev integer;
        first_stage integer;
        ring integer[]; n integer; round_no integer; pair_no integer; round_id integer;
BEGIN
    SELECT revision INTO rev FROM league WHERE id = p_league;
    IF rev IS DISTINCT FROM p_league_revision THEN
        RAISE SQLSTATE 'PT409' USING MESSAGE = 'stale_revision';
    END IF;
    IF EXISTS (SELECT FROM tournament WHERE league_id = p_league) THEN
        RAISE SQLSTATE 'PT409' USING MESSAGE = 'tournament_already_published';
    END IF;
    IF NOT EXISTS (SELECT FROM draft_state WHERE league_id = p_league AND status = 'completed'
                   AND team_count >= 2) THEN
        RAISE SQLSTATE 'PT409' USING MESSAGE = 'draft_not_completed';
    END IF;
    IF p_first_suggested_date IS NULL OR NOT isfinite(p_first_suggested_date) THEN
        RAISE SQLSTATE 'PT400' USING MESSAGE = 'invalid_suggested_date';
    END IF;
    IF p_season_best_of IS NULL OR p_season_best_of < 1 OR p_season_best_of % 2 <> 1
       OR p_final_best_of IS NULL OR p_final_best_of < 1 OR p_final_best_of % 2 <> 1 THEN
        RAISE SQLSTATE 'PT400' USING MESSAGE = 'invalid_best_of';
    END IF;
    -- Concurrent publication either serializes or retries into the already
    -- published check; never return a raw uniqueness error for the losing caller.
    INSERT INTO tournament (league_id, published_by) VALUES (p_league, u)
    ON CONFLICT (league_id) DO NOTHING;
    IF NOT FOUND THEN
        RAISE SQLSTATE 'PT409' USING MESSAGE = 'tournament_already_published';
    END IF;
    INSERT INTO tournament_stage (tournament_id, position, name, format, best_of)
    VALUES (p_league, 1, 'Season', 'round_robin', p_season_best_of)
    RETURNING id INTO first_stage;
    INSERT INTO tournament_stage (tournament_id, position, name, format, best_of,
                                  source_stage_id, qualifier_count)
    VALUES (p_league, 2, 'Final', 'single_elimination', p_final_best_of, first_stage, 2);
    INSERT INTO stage_entry (stage_id, team_id, entered_by)
    SELECT first_stage, id, u FROM team WHERE league_id = p_league;
    SELECT array_agg(id ORDER BY draft_position, id) INTO ring FROM team WHERE league_id = p_league;
    n := cardinality(ring);
    IF n % 2 = 1 THEN ring := array_append(ring, NULL::integer); n := n + 1; END IF;
    FOR round_no IN 1..n - 1 LOOP
        INSERT INTO tournament_round (stage_id, number, created_by, suggested_start_date)
        VALUES (first_stage, round_no, u, p_first_suggested_date + (round_no - 1) * 7)
        RETURNING id INTO round_id;
        FOR pair_no IN 1..n / 2 LOOP
            IF ring[pair_no] IS NOT NULL AND ring[n - pair_no + 1] IS NOT NULL THEN
                INSERT INTO match (round_id, team_a_id, team_b_id)
                VALUES (round_id, ring[pair_no], ring[n - pair_no + 1]);
            END IF;
        END LOOP;
        ring := ARRAY[ring[1], ring[n]] || ring[2:n - 1];
    END LOOP;
    PERFORM notify_league_accounts(p_league);
END $$;

-- A date edit changes the UI hint only; NULL clears it. Reports never consult it.
CREATE FUNCTION dl.set_round_suggested_date(p_round integer, p_revision integer,
    p_suggested_start_date date)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, dl, pg_temp AS $$
DECLARE league_id integer;
BEGIN
    PERFORM require_writer();
    SELECT s.tournament_id INTO league_id FROM tournament_round r
    JOIN tournament_stage s ON s.id = r.stage_id WHERE r.id = p_round;
    PERFORM tournament_commissioner(league_id);
    PERFORM tournament_check_revision(league_id, p_revision);
    IF p_suggested_start_date IS NOT NULL AND NOT isfinite(p_suggested_start_date) THEN
        RAISE SQLSTATE 'PT400' USING MESSAGE = 'invalid_suggested_date';
    END IF;
    PERFORM tournament_bump(league_id);
    UPDATE tournament_round SET suggested_start_date = p_suggested_start_date WHERE id = p_round;
    PERFORM notify_league_accounts(league_id);
END $$;

CREATE FUNCTION dl.tournament_availability_owner(p_team integer) RETURNS integer
LANGUAGE plpgsql SET search_path = pg_catalog, dl, pg_temp AS $$
DECLARE u integer := require_writer(); t dl.team;
BEGIN
    SELECT * INTO t FROM team WHERE id = p_team;
    IF NOT FOUND THEN
        RAISE SQLSTATE 'PT404' USING MESSAGE = 'team_not_found';
    END IF;
    IF t.manager_user_id <> u THEN
        RAISE SQLSTATE 'PT409' USING MESSAGE = 'action_not_permitted';
    END IF;
    RETURN t.league_id;
END $$;

-- Atomic full replacement. [] clears windows but keeps a revisioned header.
-- JSON endpoints must be ISO timestamps with an explicit Z or +/-HH:MM offset;
-- they are actual instants, never reinterpreted when the display zone changes.
CREATE FUNCTION dl.set_team_availability(p_team integer, p_revision integer, p_time_zone text,
    p_display_step_minutes integer, p_windows jsonb)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, dl, pg_temp AS $$
DECLARE league_id integer := tournament_availability_owner(p_team); rev integer;
        window_value jsonb; starts timestamptz; ends timestamptz;
BEGIN
    PERFORM tournament_validate_zone(p_time_zone);
    IF p_display_step_minutes IS NULL OR p_display_step_minutes <= 0
       OR jsonb_typeof(p_windows) IS DISTINCT FROM 'array' THEN
        RAISE SQLSTATE 'PT400' USING MESSAGE = 'invalid_availability';
    END IF;
    SELECT revision INTO rev FROM team_availability WHERE team_id = p_team;
    IF rev IS DISTINCT FROM p_revision THEN
        RAISE SQLSTATE 'PT409' USING MESSAGE = 'stale_revision';
    END IF;
    IF rev IS NULL THEN
        INSERT INTO team_availability (team_id, time_zone, display_step_minutes)
        VALUES (p_team, p_time_zone, p_display_step_minutes)
        ON CONFLICT (team_id) DO NOTHING;
        IF NOT FOUND THEN RAISE SQLSTATE 'PT409' USING MESSAGE = 'stale_revision'; END IF;
    ELSE
        UPDATE team_availability SET time_zone = p_time_zone,
            display_step_minutes = p_display_step_minutes, revision = revision + 1 WHERE team_id = p_team;
    END IF;
    DELETE FROM availability_window WHERE team_id = p_team;
    FOR window_value IN SELECT value FROM jsonb_array_elements(p_windows) LOOP
        IF jsonb_typeof(window_value) IS DISTINCT FROM 'object' THEN
            RAISE SQLSTATE 'PT400' USING MESSAGE = 'invalid_availability';
        END IF;
        IF (SELECT count(*) FROM jsonb_object_keys(window_value)) <> 2
           OR jsonb_typeof(window_value -> 'starts_at') IS DISTINCT FROM 'string'
           OR jsonb_typeof(window_value -> 'ends_at') IS DISTINCT FROM 'string'
           OR (window_value ->> 'starts_at') !~* '^[0-9]{4}-[0-9]{2}-[0-9]{2}[T ][0-9]{2}:[0-9]{2}(:[0-9]{2}(\.[0-9]+)?)?(Z|[+-][0-9]{2}:[0-9]{2})$'
           OR (window_value ->> 'ends_at') !~* '^[0-9]{4}-[0-9]{2}-[0-9]{2}[T ][0-9]{2}:[0-9]{2}(:[0-9]{2}(\.[0-9]+)?)?(Z|[+-][0-9]{2}:[0-9]{2})$' THEN
            RAISE SQLSTATE 'PT400' USING MESSAGE = 'invalid_availability';
        END IF;
        BEGIN
            starts := (window_value ->> 'starts_at')::timestamptz;
            ends := (window_value ->> 'ends_at')::timestamptz;
        EXCEPTION WHEN invalid_datetime_format OR datetime_field_overflow THEN
            RAISE SQLSTATE 'PT400' USING MESSAGE = 'invalid_availability';
        END;
        PERFORM tournament_validate_availability_window(starts, ends);
        INSERT INTO availability_window (team_id, starts_at, ends_at)
        VALUES (p_team, starts, ends) ON CONFLICT (team_id, starts_at, ends_at) DO NOTHING;
    END LOOP;
    PERFORM notify_league_accounts(league_id);
END $$;

-- Copy the intersection with [source local midnight, source+7 days) into the
-- target week, retaining each local wall time and adding to existing windows.
-- Gaps are rejected atomically; overlaps are allowed and exact duplicates fold.
CREATE FUNCTION dl.copy_team_availability_week(p_team integer, p_revision integer,
    p_source date, p_target date)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, dl, pg_temp AS $$
DECLARE league_id integer := tournament_availability_owner(p_team); a dl.team_availability;
        source_start timestamptz; source_end timestamptz; w record;
        local_start timestamp; local_end timestamp; starts timestamptz; ends timestamptz;
BEGIN
    SELECT * INTO a FROM team_availability WHERE team_id = p_team;
    IF NOT FOUND THEN RAISE SQLSTATE 'PT404' USING MESSAGE = 'availability_not_found'; END IF;
    IF a.revision IS DISTINCT FROM p_revision THEN
        RAISE SQLSTATE 'PT409' USING MESSAGE = 'stale_revision';
    END IF;
    IF p_source IS NULL OR p_target IS NULL OR NOT isfinite(p_source) OR NOT isfinite(p_target) THEN
        RAISE SQLSTATE 'PT400' USING MESSAGE = 'invalid_availability_week';
    END IF;
    source_start := p_source::timestamp AT TIME ZONE a.time_zone;
    source_end := (p_source + 7)::timestamp AT TIME ZONE a.time_zone;
    UPDATE team_availability SET revision = revision + 1 WHERE team_id = p_team;
    FOR w IN SELECT greatest(starts_at, source_start) AS starts_at,
                    least(ends_at, source_end) AS ends_at
             FROM availability_window WHERE team_id = p_team
             AND starts_at < source_end AND ends_at > source_start LOOP
        local_start := (w.starts_at AT TIME ZONE a.time_zone) + (p_target - p_source) * interval '1 day';
        local_end := (w.ends_at AT TIME ZONE a.time_zone) + (p_target - p_source) * interval '1 day';
        starts := local_start AT TIME ZONE a.time_zone;
        ends := local_end AT TIME ZONE a.time_zone;
        IF starts AT TIME ZONE a.time_zone <> local_start OR ends AT TIME ZONE a.time_zone <> local_end THEN
            RAISE SQLSTATE 'PT400' USING MESSAGE = 'nonexistent_local_boundary';
        END IF;
        PERFORM tournament_validate_availability_window(starts, ends);
        INSERT INTO availability_window (team_id, starts_at, ends_at)
        VALUES (p_team, starts, ends) ON CONFLICT (team_id, starts_at, ends_at) DO NOTHING;
    END LOOP;
    PERFORM notify_league_accounts(league_id);
END $$;

-- The draft action view also prevents undo after tournament publication.
-- Its predicate read and publication run in SERIALIZABLE transactions.
CREATE OR REPLACE VIEW dl.permitted_action WITH (security_invoker = true) AS
SELECT s.league_id, s.revision, l.commissioner_user_id AS actor_user_id,
       'configure'::text AS kind, NULL::integer AS pick_number, NULL::integer AS target_pick_id
FROM dl.draft_state s JOIN dl.league l ON l.id = s.league_id
WHERE s.status = 'setup'
UNION ALL
SELECT s.league_id, s.revision, l.commissioner_user_id, 'start', NULL::integer, NULL::integer
FROM dl.draft_state s JOIN dl.league l ON l.id = s.league_id WHERE s.can_start
UNION ALL
SELECT DISTINCT s.league_id, s.revision, a.actor, 'pick', s.next_pick_number, NULL::integer
FROM dl.draft_state s JOIN dl.league l ON l.id = s.league_id
JOIN dl.team t ON t.id = s.next_team_id
CROSS JOIN LATERAL (VALUES (l.commissioner_user_id), (t.manager_user_id)) a(actor)
WHERE s.status = 'drafting'
UNION ALL
SELECT s.league_id, s.revision, l.commissioner_user_id, 'undo', NULL::integer, s.last_pick_id
FROM dl.draft_state s JOIN dl.league l ON l.id = s.league_id
WHERE s.status <> 'setup' AND s.last_pick_id IS NOT NULL AND NOT EXISTS (
    SELECT FROM dl.tournament WHERE league_id = s.league_id
);

-- Owner default privileges keep new functions private until explicitly granted.

-- Reports, official decisions, corrections, and VODs

CREATE FUNCTION dl.tournament_result_context(p_match integer)
RETURNS TABLE (match_row dl.match, league_id integer, wins_required integer,
               manager_a integer, manager_b integer)
LANGUAGE sql STABLE
SET search_path = pg_catalog, dl, pg_temp AS $$
    SELECT m, t.league_id, s.wins_required,
           a.manager_user_id, b.manager_user_id
    FROM match m
    JOIN tournament_round r ON r.id = m.round_id
    JOIN tournament_stage s ON s.id = r.stage_id
    JOIN tournament t ON t.league_id = s.tournament_id
    JOIN team a ON a.id = m.team_a_id AND a.league_id = t.league_id
    JOIN team b ON b.id = m.team_b_id AND b.league_id = t.league_id
    JOIN stage_entry ea ON ea.stage_id = s.id AND ea.team_id = a.id
    JOIN stage_entry eb ON eb.stage_id = s.id AND eb.team_id = b.id
    WHERE m.id = p_match
$$;

CREATE FUNCTION dl.tournament_validate_result(p_wins_required integer,
    p_outcome_kind text, p_team_a_game_wins integer, p_team_b_game_wins integer,
    p_awarded_winner_side text)
RETURNS void LANGUAGE plpgsql IMMUTABLE
SET search_path = pg_catalog, dl, pg_temp AS $$
BEGIN
    IF ((p_outcome_kind = 'played' AND p_awarded_winner_side IS NULL
         AND p_team_a_game_wins >= 0 AND p_team_b_game_wins >= 0
         AND ((p_team_a_game_wins = p_wins_required
               AND p_team_b_game_wins < p_wins_required)
              OR (p_team_b_game_wins = p_wins_required
                  AND p_team_a_game_wins < p_wins_required)))
        OR (p_outcome_kind = 'forfeit'
            AND p_team_a_game_wins IS NULL AND p_team_b_game_wins IS NULL
            AND p_awarded_winner_side IN ('a', 'b'))) IS NOT TRUE THEN
        RAISE SQLSTATE 'PT400' USING MESSAGE = 'invalid_result';
    END IF;
END $$;

-- A current vote must belong to this match, side and its fixed manager. An
-- administrator-authored assertion never counts as a participant's vote.
CREATE FUNCTION dl.tournament_current_report(p_match integer, p_submission integer,
    p_side text, p_author integer, p_wins_required integer)
RETURNS dl.result_submission LANGUAGE plpgsql STABLE
SET search_path = pg_catalog, dl, pg_temp AS $$
DECLARE report dl.result_submission;
BEGIN
    SELECT * INTO report FROM result_submission
    WHERE id = p_submission AND match_id = p_match;
    IF NOT FOUND OR report.reporting_side IS DISTINCT FROM p_side
       OR report.submitted_by IS DISTINCT FROM p_author THEN
        RAISE SQLSTATE 'PT409' USING MESSAGE = 'invalid_current_submission';
    END IF;
    PERFORM tournament_validate_result(p_wins_required, report.outcome_kind,
        report.team_a_game_wins, report.team_b_game_wins, report.awarded_winner_side);
    RETURN report;
END $$;

-- Shared by record/correct. Callers
-- authorize and validate first, then advance tournament and match revisions.
-- This helper only appends history and moves the official pointer. It does not
-- change revisions, notify, alter player pointers or decide whether play is open.
CREATE FUNCTION dl.tournament_append_commissioner_result(p_match integer,
    p_actor integer, p_reason text, p_outcome_kind text,
    p_team_a_game_wins integer, p_team_b_game_wins integer,
    p_awarded_winner_side text, p_selected_submission integer DEFAULT NULL)
RETURNS integer LANGUAGE plpgsql
SET search_path = pg_catalog, dl, pg_temp AS $$
DECLARE submission integer := p_selected_submission; decision integer;
BEGIN
    IF submission IS NULL THEN
        INSERT INTO result_submission (match_id, reporting_side, submitted_by,
            outcome_kind, team_a_game_wins, team_b_game_wins, awarded_winner_side)
        VALUES (p_match, NULL, p_actor, p_outcome_kind,
            p_team_a_game_wins, p_team_b_game_wins, p_awarded_winner_side)
        RETURNING id INTO submission;
    END IF;
    INSERT INTO match_decision (match_id, selected_submission_id,
        authority, decided_by, reason)
    VALUES (p_match, submission, 'commissioner', p_actor, p_reason)
    RETURNING id INTO decision;
    UPDATE match SET current_decision_id = decision WHERE id = p_match;
    RETURN decision;
END $$;

CREATE FUNCTION dl.submit_result(p_match integer,
    p_expected_previous_submission_id integer, p_outcome_kind text,
    p_team_a_game_wins integer DEFAULT NULL, p_team_b_game_wins integer DEFAULT NULL,
    p_awarded_winner_side text DEFAULT NULL)
RETURNS integer LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, dl, pg_temp AS $$
DECLARE u integer := require_writer(); ctx record; m dl.match; side text;
        previous integer; submission integer; decision integer;
        a dl.result_submission; b dl.result_submission;
BEGIN
    SELECT * INTO ctx FROM tournament_result_context(p_match);
    IF NOT FOUND THEN
        RAISE SQLSTATE 'PT404' USING MESSAGE = 'match_not_found';
    END IF;
    m := ctx.match_row;
    side := CASE WHEN u = ctx.manager_a THEN 'a' WHEN u = ctx.manager_b THEN 'b' END;
    IF side IS NULL THEN
        RAISE SQLSTATE 'PT409' USING MESSAGE = 'action_not_permitted';
    END IF;
    IF m.current_decision_id IS NOT NULL
       OR EXISTS (SELECT FROM match_disposition WHERE match_id = p_match) THEN
        RAISE SQLSTATE 'PT409' USING MESSAGE = 'match_not_open';
    END IF;
    previous := CASE WHEN side = 'a' THEN m.team_a_submission_id
                     ELSE m.team_b_submission_id END;
    IF previous IS DISTINCT FROM p_expected_previous_submission_id THEN
        RAISE SQLSTATE 'PT409' USING MESSAGE = 'stale_submission';
    END IF;
    PERFORM tournament_validate_result(ctx.wins_required, p_outcome_kind,
        p_team_a_game_wins, p_team_b_game_wins, p_awarded_winner_side);

    -- The tournament row is the shared competitive write point. Keep this order
    -- on every retry, preserving the caller's original own-report expectation.
    PERFORM tournament_bump(ctx.league_id);
    UPDATE match SET revision = revision + 1 WHERE id = p_match;
    INSERT INTO result_submission (match_id, reporting_side, submitted_by,
        outcome_kind, team_a_game_wins, team_b_game_wins, awarded_winner_side)
    VALUES (p_match, side, u, p_outcome_kind,
        p_team_a_game_wins, p_team_b_game_wins, p_awarded_winner_side)
    RETURNING id INTO submission;
    IF side = 'a' THEN
        UPDATE match SET team_a_submission_id = submission WHERE id = p_match;
        m.team_a_submission_id := submission;
    ELSE
        UPDATE match SET team_b_submission_id = submission WHERE id = p_match;
        m.team_b_submission_id := submission;
    END IF;

    IF m.team_a_submission_id IS NOT NULL THEN
        a := tournament_current_report(p_match, m.team_a_submission_id,
            'a', ctx.manager_a, ctx.wins_required);
    END IF;
    IF m.team_b_submission_id IS NOT NULL THEN
        b := tournament_current_report(p_match, m.team_b_submission_id,
            'b', ctx.manager_b, ctx.wins_required);
    END IF;
    IF m.team_a_submission_id IS NOT NULL AND m.team_b_submission_id IS NOT NULL
       AND ROW(a.outcome_kind, a.team_a_game_wins, a.team_b_game_wins, a.awarded_winner_side)
           IS NOT DISTINCT FROM
           ROW(b.outcome_kind, b.team_a_game_wins, b.team_b_game_wins, b.awarded_winner_side) THEN
        INSERT INTO match_decision (match_id, selected_submission_id,
            supporting_submission_id, authority)
        VALUES (p_match, a.id, b.id, 'agreement') RETURNING id INTO decision;
        UPDATE match SET current_decision_id = decision WHERE id = p_match;
    END IF;
    PERFORM notify_league_accounts(ctx.league_id);
    RETURN submission;
END $$;

CREATE FUNCTION dl.record_result(p_match integer, p_revision integer, p_reason text,
    p_selected_submission integer DEFAULT NULL, p_outcome_kind text DEFAULT NULL,
    p_team_a_game_wins integer DEFAULT NULL, p_team_b_game_wins integer DEFAULT NULL,
    p_awarded_winner_side text DEFAULT NULL)
RETURNS integer LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, dl, pg_temp AS $$
DECLARE u integer := require_writer(); ctx record; m dl.match;
        report dl.result_submission; decision integer;
BEGIN
    SELECT * INTO ctx FROM tournament_result_context(p_match);
    IF NOT FOUND THEN
        RAISE SQLSTATE 'PT404' USING MESSAGE = 'match_not_found';
    END IF;
    m := ctx.match_row;
    u := tournament_commissioner(ctx.league_id);
    IF m.revision IS DISTINCT FROM p_revision THEN
        RAISE SQLSTATE 'PT409' USING MESSAGE = 'stale_revision';
    END IF;
    IF m.current_decision_id IS NOT NULL
       OR EXISTS (SELECT FROM match_disposition WHERE match_id = p_match) THEN
        RAISE SQLSTATE 'PT409' USING MESSAGE = 'match_not_open';
    END IF;
    IF p_reason IS NULL OR btrim(p_reason) = '' THEN
        RAISE SQLSTATE 'PT400' USING MESSAGE = 'reason_required';
    END IF;
    IF p_selected_submission IS NOT NULL THEN
        IF p_outcome_kind IS NOT NULL OR p_team_a_game_wins IS NOT NULL
           OR p_team_b_game_wins IS NOT NULL OR p_awarded_winner_side IS NOT NULL THEN
            RAISE SQLSTATE 'PT400' USING MESSAGE = 'select_report_or_supply_result';
        END IF;
        IF p_selected_submission = m.team_a_submission_id THEN
            report := tournament_current_report(p_match, p_selected_submission,
                'a', ctx.manager_a, ctx.wins_required);
        ELSIF p_selected_submission = m.team_b_submission_id THEN
            report := tournament_current_report(p_match, p_selected_submission,
                'b', ctx.manager_b, ctx.wins_required);
        ELSE
            RAISE SQLSTATE 'PT409' USING MESSAGE = 'submission_not_current';
        END IF;
    ELSE
        PERFORM tournament_validate_result(ctx.wins_required, p_outcome_kind,
            p_team_a_game_wins, p_team_b_game_wins, p_awarded_winner_side);
    END IF;
    PERFORM tournament_bump(ctx.league_id);
    UPDATE match SET revision = revision + 1 WHERE id = p_match;
    decision := tournament_append_commissioner_result(p_match, u, p_reason,
        p_outcome_kind, p_team_a_game_wins, p_team_b_game_wins,
        p_awarded_winner_side, p_selected_submission);
    PERFORM notify_league_accounts(ctx.league_id);
    RETURN decision;
END $$;

CREATE FUNCTION dl.correct_result(p_match integer, p_revision integer, p_reason text,
    p_outcome_kind text, p_team_a_game_wins integer DEFAULT NULL,
    p_team_b_game_wins integer DEFAULT NULL, p_awarded_winner_side text DEFAULT NULL)
RETURNS integer LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, dl, pg_temp AS $$
DECLARE u integer := require_writer(); ctx record; m dl.match;
        previous dl.result_submission; decision integer;
BEGIN
    SELECT * INTO ctx FROM tournament_result_context(p_match);
    IF NOT FOUND THEN
        RAISE SQLSTATE 'PT404' USING MESSAGE = 'match_not_found';
    END IF;
    m := ctx.match_row;
    u := tournament_commissioner(ctx.league_id);
    IF m.revision IS DISTINCT FROM p_revision THEN
        RAISE SQLSTATE 'PT409' USING MESSAGE = 'stale_revision';
    END IF;
    IF m.current_decision_id IS NULL THEN
        RAISE SQLSTATE 'PT409' USING MESSAGE = 'official_result_required';
    END IF;
    IF p_reason IS NULL OR btrim(p_reason) = '' THEN
        RAISE SQLSTATE 'PT400' USING MESSAGE = 'reason_required';
    END IF;
    PERFORM tournament_validate_result(ctx.wins_required, p_outcome_kind,
        p_team_a_game_wins, p_team_b_game_wins, p_awarded_winner_side);
    SELECT s.* INTO STRICT previous FROM match_decision d
    JOIN result_submission s ON s.id = d.selected_submission_id AND s.match_id = d.match_id
    WHERE d.id = m.current_decision_id AND d.match_id = p_match;
    IF ROW(previous.outcome_kind, previous.team_a_game_wins,
           previous.team_b_game_wins, previous.awarded_winner_side)
       IS NOT DISTINCT FROM ROW(p_outcome_kind, p_team_a_game_wins,
                                p_team_b_game_wins, p_awarded_winner_side) THEN
        RETURN m.current_decision_id;
    END IF;
    -- Excluded official results remain correctable factual history. No operation
    -- here edits their disposition, certified qualifiers or frozen participants.
    PERFORM tournament_bump(ctx.league_id);
    UPDATE match SET revision = revision + 1 WHERE id = p_match;
    decision := tournament_append_commissioner_result(p_match, u, p_reason,
        p_outcome_kind, p_team_a_game_wins, p_team_b_game_wins, p_awarded_winner_side);
    PERFORM notify_league_accounts(ctx.league_id);
    RETURN decision;
END $$;

CREATE FUNCTION dl.set_match_vod(p_match integer, p_revision integer, p_vod_url text)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, dl, pg_temp AS $$
DECLARE u integer := require_writer(); ctx record; m dl.match;
BEGIN
    SELECT * INTO ctx FROM tournament_result_context(p_match);
    IF NOT FOUND THEN
        RAISE SQLSTATE 'PT404' USING MESSAGE = 'match_not_found';
    END IF;
    m := ctx.match_row;
    IF u <> ctx.manager_a AND u <> ctx.manager_b
       AND NOT EXISTS (SELECT FROM league
                       WHERE id = ctx.league_id AND commissioner_user_id = u) THEN
        RAISE SQLSTATE 'PT409' USING MESSAGE = 'action_not_permitted';
    END IF;
    IF m.revision IS DISTINCT FROM p_revision THEN
        RAISE SQLSTATE 'PT409' USING MESSAGE = 'stale_revision';
    END IF;
    -- NULL removes the link. Require an absolute HTTP(S) link with a host;
    -- reject credentials, whitespace/control characters and browser backslashes.
    IF p_vod_url IS NOT NULL AND (length(p_vod_url) > 4096
       OR p_vod_url !~* '^https?://([a-z0-9]([a-z0-9.-]*[a-z0-9])?|\[[0-9a-f:.]+\])(:[0-9]{1,5})?([/?#][^[:space:][:cntrl:]]*)?$'
       OR strpos(p_vod_url, chr(92)) > 0) THEN
        RAISE SQLSTATE 'PT400' USING MESSAGE = 'invalid_vod_url';
    END IF;
    IF m.vod_url IS NOT DISTINCT FROM p_vod_url THEN RETURN; END IF;
    UPDATE match SET vod_url = p_vod_url, revision = revision + 1 WHERE id = p_match;
    PERFORM notify_league_accounts(ctx.league_id);
END $$;

REVOKE ALL ON FUNCTION
    dl.tournament_result_context(integer),
    dl.tournament_validate_result(integer,text,integer,integer,text),
    dl.tournament_current_report(integer,integer,text,integer,integer),
    dl.tournament_append_commissioner_result(integer,integer,text,text,integer,integer,text,integer),
    dl.submit_result(integer,integer,text,integer,integer,text),
    dl.record_result(integer,integer,text,integer,text,integer,integer,text),
    dl.correct_result(integer,integer,text,text,integer,integer,text),
    dl.set_match_vod(integer,integer,text)
FROM PUBLIC;

-- Withdrawals, tiebreaks, and final publication

-- Qualification is commissioner-certified. The commissioner selects the teams
-- using the announced competition criteria and the current normal-round and
-- individual tiebreak-round evidence. These writers validate eligibility,
-- completeness and freshness; they do not implement an automatic ranking engine.
-- Selecting a tiebreak field certifies that those entrants remain tied under the
-- announced criteria. Publishing the final field certifies its two members;
-- their order has no meaning and ties between two advancing teams need no ruling.

CREATE FUNCTION dl.withdraw_team(p_league integer, p_revision integer,
    p_team integer, p_reason text)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, dl, pg_temp AS $$
DECLARE
    u integer := dl.tournament_commissioner(p_league);
    source_stage integer;
    finalized_at timestamptz;
    fixture dl.match;
BEGIN
    PERFORM dl.tournament_check_revision(p_league, p_revision);
    IF p_reason IS NULL OR btrim(p_reason) = '' THEN
        RAISE SQLSTATE 'PT400' USING MESSAGE = 'reason_required';
    END IF;
    SELECT s.id, s.qualification_finalized_at INTO source_stage, finalized_at
    FROM tournament_stage s
    WHERE s.tournament_id = p_league AND s.source_stage_id IS NULL;
    IF NOT FOUND THEN
        RAISE SQLSTATE 'PT409' USING MESSAGE = 'stage_not_found';
    END IF;
    IF NOT EXISTS (
        SELECT FROM team t JOIN stage_entry e ON e.team_id = t.id
        WHERE t.id = p_team AND t.league_id = p_league
          AND e.stage_id = source_stage
    ) THEN
        RAISE SQLSTATE 'PT404' USING MESSAGE = 'team_not_found';
    END IF;
    IF EXISTS (SELECT FROM tournament_withdrawal WHERE team_id = p_team) THEN
        RAISE SQLSTATE 'PT409' USING MESSAGE = 'team_already_withdrawn';
    END IF;

    PERFORM dl.tournament_bump(p_league);
    INSERT INTO tournament_withdrawal (team_id, decided_by, reason)
    VALUES (p_team, u, btrim(p_reason));

    IF finalized_at IS NULL THEN
        -- Only newly removed fixtures change. A disposition already recorded for
        -- an opponent's withdrawal, including its original attribution, survives.
        FOR fixture IN
            SELECT m.* FROM match m
            JOIN tournament_round r ON r.id = m.round_id
            WHERE r.stage_id = source_stage
              AND (m.team_a_id = p_team OR m.team_b_id = p_team)
              AND NOT EXISTS (SELECT FROM match_disposition d WHERE d.match_id = m.id)
            ORDER BY m.id
        LOOP
            UPDATE match SET revision = revision + 1 WHERE id = fixture.id;
            INSERT INTO match_disposition (match_id, withdrawal_team_id)
            VALUES (fixture.id, p_team);
        END LOOP;
    END IF;
    -- After qualification, record withdrawal only. Leave the final and all
    -- source results intact, with no automatic forfeit, replacement or closure.
    PERFORM dl.notify_league_accounts(p_league);
END $$;

CREATE FUNCTION dl.create_tiebreak_round(p_stage integer, p_revision integer,
    p_teams integer[], p_reason text, p_suggested_start_date date DEFAULT NULL)
RETURNS integer LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, dl, pg_temp AS $$
DECLARE
    u integer := dl.require_writer();
    stage tournament_stage;
    result integer;
    round_number integer;
BEGIN
    SELECT * INTO stage FROM tournament_stage WHERE id = p_stage;
    IF NOT FOUND THEN
        RAISE SQLSTATE 'PT404' USING MESSAGE = 'stage_not_found';
    END IF;
    u := dl.tournament_commissioner(stage.tournament_id);
    PERFORM dl.tournament_check_revision(stage.tournament_id, p_revision);
    IF stage.format <> 'round_robin' OR stage.source_stage_id IS NOT NULL THEN
        RAISE SQLSTATE 'PT409' USING MESSAGE = 'tiebreak_requires_source_round_robin';
    END IF;
    IF stage.qualification_finalized_at IS NOT NULL THEN
        RAISE SQLSTATE 'PT409' USING MESSAGE = 'qualification_already_finalized';
    END IF;
    IF p_reason IS NULL OR btrim(p_reason) = '' THEN
        RAISE SQLSTATE 'PT400' USING MESSAGE = 'reason_required';
    END IF;
    IF p_suggested_start_date IS NOT NULL AND NOT isfinite(p_suggested_start_date) THEN
        RAISE SQLSTATE 'PT400' USING MESSAGE = 'invalid_suggested_date';
    END IF;
    IF p_teams IS NULL OR cardinality(p_teams) < 2
       OR array_ndims(p_teams) IS DISTINCT FROM 1 THEN
        RAISE SQLSTATE 'PT400' USING MESSAGE = 'at_least_two_teams_required';
    END IF;
    IF EXISTS (SELECT FROM unnest(p_teams) AS selected(team_id) WHERE team_id IS NULL)
       OR (SELECT count(DISTINCT team_id) FROM unnest(p_teams) AS selected(team_id))
          <> cardinality(p_teams) THEN
        RAISE SQLSTATE 'PT400' USING MESSAGE = 'distinct_teams_required';
    END IF;
    IF EXISTS (
        SELECT FROM unnest(p_teams) AS selected(team_id)
        LEFT JOIN stage_entry e ON e.stage_id = p_stage AND e.team_id = selected.team_id
        LEFT JOIN team t ON t.id = e.team_id AND t.league_id = stage.tournament_id
        WHERE t.id IS NULL OR EXISTS (
            SELECT FROM tournament_withdrawal w WHERE w.team_id = selected.team_id
        )
    ) THEN
        RAISE SQLSTATE 'PT409' USING MESSAGE = 'ineligible_tiebreak_team';
    END IF;
    -- Covers all normal fixtures and every earlier tiebreak attempt. A removed
    -- fixture is complete for qualification even if its retained reports differ.
    IF EXISTS (
        SELECT FROM match m JOIN tournament_round r ON r.id = m.round_id
        WHERE r.stage_id = p_stage AND m.current_decision_id IS NULL
          AND NOT EXISTS (SELECT FROM match_disposition d WHERE d.match_id = m.id)
    ) THEN
        RAISE SQLSTATE 'PT409' USING MESSAGE = 'stage_matches_unresolved';
    END IF;

    PERFORM dl.tournament_bump(stage.tournament_id);
    SELECT coalesce(max(number), 0) + 1 INTO round_number
    FROM tournament_round WHERE stage_id = p_stage;
    INSERT INTO tournament_round
        (stage_id, number, purpose, tiebreak_reason, created_by, suggested_start_date)
    VALUES (p_stage, round_number, 'tiebreak', btrim(p_reason), u, p_suggested_start_date)
    RETURNING id INTO result;
    -- Canonical pair orientation makes the complete fixture preview deterministic.
    INSERT INTO match (round_id, team_a_id, team_b_id)
    SELECT result, a.team_id, b.team_id
    FROM unnest(p_teams) AS a(team_id) CROSS JOIN unnest(p_teams) AS b(team_id)
    WHERE a.team_id < b.team_id ORDER BY a.team_id, b.team_id;
    PERFORM dl.notify_league_accounts(stage.tournament_id);
    RETURN result;
END $$;

CREATE FUNCTION dl.publish_final(p_league integer, p_revision integer,
    p_qualifiers integer[], p_suggested_start_date date DEFAULT NULL)
RETURNS integer LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, dl, pg_temp AS $$
DECLARE
    u integer := dl.tournament_commissioner(p_league);
    source_stage tournament_stage;
    final_stage tournament_stage;
    qualifiers integer[];
    final_round integer;
    result integer;
BEGIN
    PERFORM dl.tournament_check_revision(p_league, p_revision);
    SELECT * INTO source_stage FROM tournament_stage
    WHERE tournament_id = p_league AND source_stage_id IS NULL;
    IF NOT FOUND THEN
        RAISE SQLSTATE 'PT409' USING MESSAGE = 'stage_not_found';
    END IF;
    SELECT * INTO final_stage FROM tournament_stage
    WHERE tournament_id = p_league AND source_stage_id = source_stage.id;
    IF NOT FOUND THEN
        RAISE SQLSTATE 'PT409' USING MESSAGE = 'stage_not_found';
    END IF;
    IF source_stage.qualification_finalized_at IS NOT NULL THEN
        RAISE SQLSTATE 'PT409' USING MESSAGE = 'final_already_published';
    END IF;
    IF p_qualifiers IS NULL OR cardinality(p_qualifiers) <> 2
       OR array_ndims(p_qualifiers) IS DISTINCT FROM 1 THEN
        RAISE SQLSTATE 'PT400' USING MESSAGE = 'exactly_two_qualifiers_required';
    END IF;
    IF EXISTS (SELECT FROM unnest(p_qualifiers) AS selected(team_id) WHERE team_id IS NULL)
       OR (SELECT count(DISTINCT team_id) FROM unnest(p_qualifiers) AS selected(team_id)) <> 2 THEN
        RAISE SQLSTATE 'PT400' USING MESSAGE = 'distinct_qualifiers_required';
    END IF;
    -- Input order is irrelevant. Canonical A/B orientation has no seed/rank meaning.
    SELECT array_agg(team_id ORDER BY team_id) INTO qualifiers
    FROM unnest(p_qualifiers) AS selected(team_id);
    IF EXISTS (
        SELECT FROM unnest(qualifiers) AS selected(team_id)
        LEFT JOIN stage_entry e ON e.stage_id = source_stage.id AND e.team_id = selected.team_id
        LEFT JOIN team t ON t.id = e.team_id AND t.league_id = p_league
        WHERE t.id IS NULL OR EXISTS (
            SELECT FROM tournament_withdrawal w WHERE w.team_id = selected.team_id
        )
    ) THEN
        RAISE SQLSTATE 'PT409' USING MESSAGE = 'ineligible_qualifier';
    END IF;
    IF p_suggested_start_date IS NOT NULL AND NOT isfinite(p_suggested_start_date) THEN
        RAISE SQLSTATE 'PT400' USING MESSAGE = 'invalid_suggested_date';
    END IF;
    IF EXISTS (
        SELECT FROM match m JOIN tournament_round r ON r.id = m.round_id
        WHERE r.stage_id = source_stage.id AND m.current_decision_id IS NULL
          AND NOT EXISTS (SELECT FROM match_disposition d WHERE d.match_id = m.id)
    ) THEN
        RAISE SQLSTATE 'PT409' USING MESSAGE = 'stage_matches_unresolved';
    END IF;

    PERFORM dl.tournament_bump(p_league);
    UPDATE tournament_stage
    SET qualification_finalized_at = clock_timestamp(), qualification_finalized_by = u
    WHERE id = source_stage.id;
    INSERT INTO stage_entry (stage_id, team_id, entered_by)
    SELECT final_stage.id, team_id, u FROM unnest(qualifiers) AS selected(team_id);
    INSERT INTO tournament_round (stage_id, number, purpose, created_by, suggested_start_date)
    VALUES (final_stage.id, 1, 'normal', u, p_suggested_start_date)
    RETURNING id INTO final_round;
    INSERT INTO match (round_id, team_a_id, team_b_id)
    VALUES (final_round, qualifiers[1], qualifiers[2]) RETURNING id INTO result;
    PERFORM dl.notify_league_accounts(p_league);
    RETURN result;
END $$;

COMMENT ON FUNCTION dl.create_tiebreak_round(integer, integer, integer[], text, date)
IS 'Commissioner certifies selected eligible entrants remain tied under announced external criteria. Validates completion and freshness; does not compute rankings.';
COMMENT ON FUNCTION dl.publish_final(integer, integer, integer[], date)
IS 'Commissioner certifies two eligible qualifiers, without ordering them. Atomically freezes final membership and publishes its fixture; does not compute rankings.';

REVOKE ALL ON FUNCTION
    dl.withdraw_team(integer, integer, integer, text),
    dl.create_tiebreak_round(integer, integer, integer[], text, date),
    dl.publish_final(integer, integer, integer[], date)
FROM PUBLIC;

-- Controlled write permissions

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
