# Tournaments

Tournament storage, rules, projections, and permissions are installed by the first packaged migration, [0001_tournament.sql](../src/draft_backend/sql_migrations/0001_tournament.sql). The HTTP gateway exposes the controlled operations and public reads described in the [backend API contract](../README.md#tournaments).

The implemented workflow is one round robin followed by a two-team final, with repeatable qualification tiebreaks when needed. League size is variable. Each league has its own tournament and champion; one league is one draft and one tournament. The tables retain general stage, entry, round, and match relationships. Another format would need its scheduling/progression operations when actually introduced, without changing the report/decision model. There is no supported-format metadata registry or configurable tournament engine.

The current publisher creates the arrangement it knows how to schedule. It accepts meaningful choices, not a caller-supplied stage graph that must reproduce fixed constants. Setup choices can remain in the UI until publication; there is no separately configured, unpublished tournament in storage.

All data is publicly readable, including availability, participant reports, official decisions, and their history. Write authorization remains explicit: commissioners manage the tournament, managers report for their own teams and edit their availability. Read access never depends on league membership.

## Installation

Bootstrap installs the fixed schema and grants baseline. Application startup then applies the packaged tournament migration as `dl_owner` in one transaction, including its permissions and migration-history entry. The integration test database follows the same path. Existing leagues, accounts, draft picks, and catalog data are retained. Restarting after a successful migration validates its checksum and skips reapplying it.

The migration is the sole executable tournament SQL source. The earlier five proposal files have been consolidated into it; do not install a proposal separately. An existing review database that had those proposal files applied manually is not a supported migration baseline: use a fresh disposable database for testing.

The existing identity, transaction retry, notification, and public-read mechanisms are reused. All mutations authenticate and require a `SERIALIZABLE` transaction. Retry the entire transaction, including a commit failure, on `40001` or `40P01` with the original arguments. The backend's `Database.write` already does this. `PT409` requires refreshing the view; it is not an automatic retry with a new token.

## Operations

Arguments below omit the SQL `p_` prefix except where distinguishing revision types matters. Date hints are calendar dates, not timestamps. Optional final/tiebreak dates default to `NULL`.

| SQL call | Input and freshness contract | Returns |
|---|---|---|
| `publish_tournament(league, league_revision, first_suggested_date, season_best_of=3, final_best_of=5)` | Commissioner; completed draft and expected **league** revision; no existing tournament | Void |
| `set_round_suggested_date(round, revision, suggested_start_date)` | Commissioner; tournament revision; `NULL` clears the hint | Void |
| `set_team_availability(team, revision, zone, display_step_minutes, windows)` | Manager; own availability revision, `NULL` on creation; array of actual timestamp ranges | Void |
| `copy_team_availability_week(team, revision, source_date, target_date)` | Manager; own availability revision; copy local wall times into another week | Void |
| `submit_result(match, expected_previous_submission_id, outcome_kind, a_wins, b_wins, awarded_winner_side)` | Participant; own current report ID, `NULL` for first report; scores use fixture A/B orientation | Submission ID |
| `record_result(match, revision, reason, selected_submission, outcome_kind, a_wins, b_wins, awarded_winner_side)` | Commissioner; match revision; select a current report **or** supply an outcome | Decision ID |
| `correct_result(match, revision, reason, outcome_kind, a_wins, b_wins, awarded_winner_side)` | Commissioner; match revision; identical outcome is a no-op | Decision ID |
| `set_match_vod(match, revision, url)` | Participant or commissioner; match revision; HTTP(S) URL, `NULL` to clear | Void |
| `withdraw_team(league, revision, team, reason)` | Commissioner; tournament revision; terminal withdrawal | Void |
| `create_tiebreak_round(stage, revision, teams, reason, suggested_start_date=NULL)` | Commissioner; tournament revision; distinct eligible tied entrants; earlier fixtures resolved or removed | Round ID |
| `publish_final(league, revision, qualifiers, suggested_start_date=NULL)` | Commissioner; tournament revision; two distinct eligible qualifiers, in either order | Match ID |

`configure_tournament`, `configure_stage`, and `set_round_window` are gone. Publication creates the tournament at revision **0**, both stages, the source-stage entries, and the complete round-robin schedule atomically. It records `published_at/by`; the mere presence of a tournament means publication happened. A failed call leaves none of those rows behind. The league revision is checked, not repurposed as the tournament revision.

The stage names are initially Season and Final. Best-of values must be positive odd integers and freeze at publication. The final's best-of is established before anyone qualifies. Future stage generators can use the same tables; there is no schema-level allowlist of supported format labels.

## Suggested weeks and availability

A round has one nullable `suggested_start_date`. The first regular round uses the publication input; subsequent regular rounds are seven calendar days apart. The UI can display “Week of October 19” and open its availability widget there. No tournament timezone, opening timestamp, closing timestamp, duration, or daylight-saving boundary calculation is needed for these date labels.

The suggestion never opens or closes reporting. Players can look at other weeks, play early or late, and report their result normally. A round-three match played during round five remains a round-three match, with its original suggested date and standings contribution. No extension, replacement fixture, appointment, or administrator action is needed. A commissioner can edit or clear a round's suggested date if the overall schedule changes; that changes only the hint. Publishing a final or tiebreak does not require choosing a date.

Later normal rounds do not wait for earlier ones. Qualification waits for the results it depends on: unresolved active source fixtures must be resolved before adding a qualification tiebreak or publishing the final. This is a dependency on competitive evidence, not on the calendar.

Availability remains a lightweight independent aid. Each team's manager can replace its ranges, paint a preferred display grid, or copy a week. `[]` clears the ranges while retaining the revisioned header. There is no added phase gate or setup recovery workflow. As accepted in the discussion, availability saved during setup can obstruct team removal through its foreign key or remain attached after a manager change; this minor setup edge is left alone.

Actual availability ranges still use explicit-offset timestamps. Changing the display zone preserves the instants. Copying a week preserves local wall times and rejects nonexistent target boundaries; ambiguous fall-back times use PostgreSQL's standard-time interpretation. Overlapping ranges mean their union and exact duplicates collapse. The display step is a painting preference, not a UTC-alignment rule.

For example, if A is free 01:00–03:00Z and H is free 02:00–04:00Z, their common availability is 02:00–03:00Z. The UI displays it in the viewer's zone. It does not write a match appointment or affect result eligibility. Availability timezone handling serves this overlap display and does not affect round date hints.

## Example: eight entrants, from publication to a champion

Suppose North contains A–H and its draft is complete. The commissioner publishes with a first suggested date of October 19, season best-of-three, and final best-of-five. One transaction creates:

| Rows | Immediately after publication | After final publication |
|---|---:|---:|
| `tournament` | 1 | 1 |
| `tournament_stage` | 2 | 2 |
| `stage_entry` | 8 | 10 |
| `tournament_round` | 7 | 8 |
| `match` | 28 | 29 |

The final stage initially has no entries or fixtures. A possible season schedule is:

| Round | Suggested date | Fixtures |
|---:|---|---|
| 1 | 2026-10-19 | A–H, B–G, C–F, D–E |
| 2 | 2026-10-26 | A–G, H–F, B–E, C–D |
| 3 | 2026-11-02 | A–F, G–E, H–D, B–C |
| 4 | 2026-11-09 | A–E, F–D, G–C, H–B |
| 5 | 2026-11-16 | A–D, E–C, F–B, G–H |
| 6 | 2026-11-23 | A–C, D–B, E–H, F–G |
| 7 | 2026-11-30 | A–B, C–H, D–G, E–F |

These are suggested weeks. Moving across daylight-saving time does not change the seven-day calendar spacing or require a timestamp interpretation. Another league publishes and progresses independently; it does not feed a shared championship.

If A beats H 2–1, A submits a report in the fixture's A/B orientation. H independently submits the same score. Each report checks only that player's previous report ID; both first reports use `NULL`, even if the opponent has already reported. The second matching report creates an agreement decision citing both current submissions. Side A's submission is always selected and side B's is supporting evidence, regardless of arrival order.

| State | A report | H report | Official result |
|---|---|---|---|
| Before reporting | None | None | None |
| A reports | 2–1 | None | None |
| H agrees | 2–1 | 2–1 | A wins 2–1 |

There is no second official-score payload. The official decision points to a submission, and `stage_record` derives wins/losses and game counts from current official results. A forfeit has an awarded winner and no fabricated game counts. Byes and removed fixtures create no wins.

Once all source fixtures are resolved or removed, the commissioner selects two qualifiers. Suppose A and C advance. `publish_final` inserts their two final-stage entries, its round and fixture, and marks the source stage's `qualification_finalized_at/by` together. The final entries are the sole qualifier record. There are no source `final_place` values or final seeds to keep synchronized. `[A,C]` and `[C,A]` select the same field; team-ID order determines display sides without advantage.

If A and C are tied but both advance, no tiebreak is required merely to label one first and the other second. The final's official best-of-five result identifies the champion. No separate certification of a final ranking is required. A later season-score correction updates live records without replacing A or C.

## Odd league sizes and qualification tiebreaks

The round-robin generator handles odd counts with a temporary bye placeholder, never a persisted team or fixture. For five teams J–N:

| Round | Fixtures | Bye |
|---:|---|---|
| 1 | K–N, L–M | J |
| 2 | J–N, K–L | M |
| 3 | J–M, N–L | K |
| 4 | J–L, M–K | N |
| 5 | J–K, M–N | L |

There are `N × (N − 1) / 2` fixtures: `N − 1` rounds for even N, N rounds for odd N. Each pair meets once and each team appears at most once per normal round. Publication requires at least two entrants.

If three teams remain tied for two qualifying places under the announced criteria, the commissioner can add a tiebreak round with those three teams and a reason. It creates every pair once, using the same stage best-of and result operations. Multiple fixtures per team in this one round are intentional. Its suggested date can be absent.

If A beats B, B beats C, and C beats A, another attempt may be needed. The same operation adds a fresh round once the first is resolved. It may include only the still-tied subset. Each attempt has its own `round_record`; attempts are not summed or added to `stage_record`, since their fields and fixture counts may differ. Existing attempts are retained when later corrections change the evidence.

The commissioner certifies the selected tie field and final qualifiers using the competition's announced criteria. SQL checks eligibility, completion, authorization, and freshness; it does not implement a configurable ranking formula. There is no need to resolve ties that do not affect who qualifies.

## Corrections and withdrawals

Before an official result exists, either player may replace their own report by appending a new submission and advancing their own pointer. A missing report means pending; different reports mean disagreement. A replacement may create agreement without commissioner intervention. All evidence is public, so reporting is independent authorship, not blind voting.

The commissioner can resolve an active fixture by selecting a current participant report or supplying an outcome with a reason. Official results freeze the participant pointers. A correction appends new evidence and a decision, moves the official pointer, and retains history. An identical outcome returns the existing decision without revisions or notifications. Corrections can change live records but never regenerate fixtures or change frozen qualification.

Before final publication, withdrawing H removes all H's source-stage fixtures, across normal and tiebreak rounds, from counted records. One withdrawal stores the actor, timestamp, and reason. Each affected `match_disposition` contains only `match_id` and `withdrawal_team_id`. If an opponent already withdrew, the original disposition retains that earlier cause.

| Affected fixture | Retained evidence | Display interpretation | Counts in standings |
|---|---|---|---|
| Already official | Reports and official decision | Excluded result | No |
| Reported but unresolved | Existing participant reports | Excluded evidence | No |
| No reports or result | None | Cancelled fixture | No |

The last distinction is derived from whether any report or official-decision pointer exists, not stored as a `cancelled` flag. Removed fixtures cannot gain reports or an initial official result. Existing official results remain correctable factual history but stay excluded. All dispositions link to the reason that caused removal; there is no separate restoration or per-match exclusion operation.

After final publication, withdrawal records the event and leaves source results, final membership, and the final fixture unchanged. It does not award a forfeit automatically. Existing reports remain evidence; ordinary result operations can still record an actual result or an explicit commissioner award. If the final cannot be played, it may remain unresolved indefinitely. If both finalists withdraw, neither becomes champion merely because of withdrawal order. An already official final is also unchanged.

If fewer than two eligible teams remain before final publication, no final can be published. The tournament can simply remain incomplete. There is no additional closure state or launch decision about how to manufacture a champion.

## Freshness and concurrency

| Operation | Freshness token |
|---|---|
| Initial publication | Existing league revision from the completed draft |
| Date-hint edit, withdrawal, tiebreak, final publication | Tournament revision |
| Commissioner result/correction and VOD | Match revision |
| Participant report/replacement | That participant's own current submission ID |
| Availability replacement/copy | Team availability revision |

Competitive result writes bump tournament then match revisions, so changed results invalidate qualification previews. Availability edits only its header revision; VOD edits only match revision. Batch match writes run in ascending ID order. Tournament revisions deliberately create a shared write point: unrelated matches can contend and require whole-transaction retries. There is no additional locking protocol or stored dispute state.

Draft undo's permitted-action branch checks for an existing tournament. Publication reads draft completion in the same serializable transaction that creates it. Undo and publication cannot both commit from incompatible snapshots; each operation remains valid in whichever serial order succeeds. Concurrent publication creates one tournament; the losing caller receives a conflict after any required serialization retry.

Player reports use their own pointer token so opposing first submissions can both succeed after retry. Same-side replacements, an already official match, or a removed fixture reject stale submissions. The commissioner must refresh qualification/withdrawal previews after competitive changes. Notifications are account wake-ups delivered after commit, not a durable event log; the app reloads authoritative state.

## Validation

[test_tournament.py](../tests/test_tournament.py) exercises publication, scheduling, reports, decisions, corrections, withdrawals, tiebreaks, finals, permissions, concurrency, availability, and notifications against the normally migrated database. HTTP coverage in [test_tournament_api.py](../tests/test_tournament_api.py) verifies the gateway contract, JSONB availability, dates, public reads, authorization, and freshness. [test_tournament_migration.py](../tests/test_tournament_migration.py) verifies upgrading a populated baseline and repeat startup.

Run the backend suite with `python -m pytest --integration --pg-bin=<PostgreSQL binaries>`. Tests own a fresh local cluster and retain its files and logs for inspection.
