The proposal supports arbitrary league sizes within one competition format: a round robin followed by a two-team final, with repeatable qualification tiebreaks when needed. The write design uses a small set of complete operations: submit or replace your report, commissioner decision or correction, withdraw team, add tiebreak round, and publish final. Withdrawal consequences and the qualification cutoff are fixed policies rather than independent administrative controls.

This walkthrough describes the [tournament storage proposal](/E:/bz/draft_backend/sql/tournament.proposal.sql) alongside the existing [league/team schema](/E:/bz/draft_backend/sql/schema.sql). Controlled SQL writers are implemented in the companion files below. The HTTP routes, tournament read policies, and commissioner interface remain separate work; the normal bootstrap does not install this proposal.

The earlier SQLite-adapted constraint checks are now supplemented by opt-in PostgreSQL integration tests in [test_tournament.py](/E:/bz/draft_backend/tests/test_tournament.py). They execute the controlled writers as the unprivileged API role, including serializable concurrency cases, and inspect durable state as administrator.

Validation: all 25 tournament cases passed on PostgreSQL, with 36 passing tests when combined with the existing notification and database-retry checks. Ruff passed for the new test file. From `draft_backend`, run `uv run pytest tests/test_tournament.py tests/test_notifications.py tests/test_db.py --integration --pg-bin "<PostgreSQL binary directory>"` against a fresh runner-owned cluster. The runner stops PostgreSQL and retains the database and logs for inspection.

Install the following files in order as `dl_owner`, in one transaction, after the existing backend schema and grants in a disposable database:

1. [Storage and projections](/E:/bz/draft_backend/sql/tournament.proposal.sql).
2. [Configuration, scheduling, availability, and draft-undo guard](/E:/bz/draft_backend/sql/tournament.mutators.proposal.sql).
3. [Reports, decisions, corrections, and VOD links](/E:/bz/draft_backend/sql/tournament.results.proposal.sql).
4. [Withdrawals, tiebreaks, and final publication](/E:/bz/draft_backend/sql/tournament.lifecycle.proposal.sql).
5. [Explicit callable-function grants](/E:/bz/draft_backend/sql/tournament.grants.proposal.sql).

All mutations require authenticated `request.jwt.claims` and a `SERIALIZABLE` transaction. Retry the entire transaction, including commit failures, on `40001` or `40P01` with unchanged arguments; the existing backend `Database.write` provides this behavior. `PT409` is a conflict that requires a fresh screen, not an automatic retry. The grant file exposes no table writes or raw report history. It also exposes the scoped undo-eligibility helper used by the existing draft action view.

| SQL call | Freshness and input contract | Returns |
|---|---|---|
| `configure_tournament(league, revision, zone)` | Commissioner; `NULL` revision creates a missing tournament at revision 0 | League ID |
| `configure_stage(league, revision, position, name, format, best_of, source_stage, qualifier_count)` | Commissioner; upserts position 1 or 2 before publication; root uses `NULL` selection fields, final selects two from root | Stage ID |
| `publish_tournament(league, revision, starts_at)` | Completed draft; `starts_at` is a local timestamp without timezone in the tournament zone | Void |
| `set_round_window(round, revision, opens_at, closes_at)` | Commissioner; tournament revision; finite actual instants | Void |
| `set_team_availability(team, revision, zone, display_step_minutes, windows)` | Manager; own header revision, `NULL` on creation; JSON array of `starts_at`/`ends_at` with explicit offsets | Void |
| `copy_team_availability_week(team, revision, source_date, target_date)` | Manager; copies the source week's intersection to the target week, preserving local wall times and adding to existing windows | Void |
| `submit_result(match, expected_previous_submission_id, outcome_kind, a_wins, b_wins, awarded_winner_side)` | Participant; own current report ID, `NULL` for first report; scores use fixture A/B orientation | Submission ID |
| `record_result(match, revision, reason, selected_submission, outcome_kind, a_wins, b_wins, awarded_winner_side)` | Commissioner; match revision; select one current participant report **or** supply an outcome | Decision ID |
| `correct_result(match, revision, reason, outcome_kind, a_wins, b_wins, awarded_winner_side)` | Commissioner; match revision; same payload returns existing decision with no write or wake-up | Decision ID |
| `set_match_vod(match, revision, url)` | Participant or commissioner; match revision only; HTTP(S) URL, `NULL` to clear | Void |
| `withdraw_team(league, revision, team, reason)` | Commissioner; tournament revision; fixed consequences | Void |
| `create_tiebreak_round(stage, revision, teams, opens_at, closes_at, reason)` | Commissioner; tournament revision; certified tied eligible entrants; all earlier fixtures resolved or removed | Round ID |
| `publish_final(league, revision, qualifiers, opens_at, closes_at)` | Commissioner; tournament revision; exactly two certified eligible teams in first/second order | Match ID |

Played results require exactly one side to reach the stage's generated `wins_required`; forfeits require an awarded side and `NULL` scores. Availability replacement accepts `[]` to clear all windows while retaining its revision. Exact duplicate windows collapse, and overlapping windows represent their union. Changing display zone does not reinterpret supplied instants. Copying weeks and generating round boundaries reject nonexistent local times in spring-forward gaps; ambiguous fall-back boundaries use PostgreSQL's standard-time interpretation. A display step is a positive painting preference, not a UTC-alignment constraint.

Qualification is commissioner-certified for this version. Selecting a tiebreak field certifies that its members remain tied under announced criteria; selecting ordered final qualifiers certifies places 1 and 2 and resolution of relevant ties. SQL validates authorization, eligibility, completion, and freshness without choosing a ranking formula.

In these examples, a “player” is a manager controlling a drafted `team`. A match is an entire best-of series; a 2–1 result means two games won by one team and one by the other. Letter names stand in for integer team IDs. Tables show relevant columns, omitting routine authors, timestamps, and revisions unless they matter to the example. `NULL` means no stored value.

Each league has its own tournament and champion. Two leagues are not two groups feeding a shared championship. A league represents one draft and one tournament; reusing the same league row for another season is outside this model.

**Scenario 1: two leagues of eight, from publication to a champion.** Suppose North contains A–H, and South contains P–W. Both drafts are complete. The commissioner configures and publishes each tournament.

The tournament rows are simply:

| `tournament.league_id` | League name, via `league` | `time_zone` | `published_at` / `published_by` |
|---:|---|---|---|
| 10 | North | America/Los_Angeles | Set when published |
| 20 | South | America/Los_Angeles | Set when published |

There is no stored “two leagues” setting. Each league has its own row and publication lifecycle. North can progress while South is still drafting.

The stage rows establish the rules before anyone qualifies for a final:

| `id` | `tournament_id` | `position` | `format` | `best_of` | Generated `wins_required` | `source_stage_id` | `qualifier_count` |
|---:|---:|---:|---|---:|---:|---:|---:|
| 101 | 10 | 1 | round_robin | 3 | 2 | NULL | NULL |
| 102 | 10 | 2 | single_elimination | 5 | 3 | 101 | 2 |
| 201 | 20 | 1 | round_robin | 3 | 2 | NULL | NULL |
| 202 | 20 | 2 | single_elimination | 5 | 3 | 201 | 2 |

Eight entries are added to each regular-season stage. The final stages initially have no entries. The draft positions can supply a deterministic scheduling order, but the proposal does not require treating draft position as competitive seeding; regular-season `seed` can remain `NULL`.

| `stage_entry.stage_id` | `team_id` | `seed` | `final_place` |
|---:|---|---:|---:|
| 101 | A | NULL | NULL |
| 101 | B | NULL | NULL |
| 101 | C through H, one row each | NULL | NULL |
| 201 | P through W, one row each | NULL | NULL |

Publication produces a single round robin from those entries. One valid North schedule is below. Each fixture becomes one `match` row pointing to the corresponding `tournament_round` row.

| Round | Fixture 1 | Fixture 2 | Fixture 3 | Fixture 4 |
|---:|---|---|---|---|
| 1 | A–H | B–G | C–F | D–E |
| 2 | A–G | H–F | B–E | C–D |
| 3 | A–F | G–E | H–D | B–C |
| 4 | A–E | F–D | G–C | H–B |
| 5 | A–D | E–C | F–B | G–H |
| 6 | A–C | D–B | E–H | F–G |
| 7 | A–B | C–H | D–G | E–F |

For N entrants, the regular season contains `N × (N − 1) / 2` fixtures. With even N, there are `N − 1` rounds and `N / 2` fixtures per round. Thus eight entrants produce 28 fixtures across seven rounds. Those numbers are derived, not settings or constants in the scheduler. South gets its own schedule by the same procedure.

Publishing the final later creates its two entries, round, and fixture together with the qualification record.

| Stored rows across both leagues | Just after tournament publication | After both finals are published |
|---|---:|---:|
| `tournament` | 2 | 2 |
| `tournament_stage` | 4 | 4 |
| `stage_entry` | 16 | 20 |
| `tournament_round` | 14 | 16 |
| `match` | 56 | 58 |

The rounds contain playing windows, not appointments. A and H can paint availability independently. For example, the stored windows might be:

| `availability_window.team_id` | `starts_at` | `ends_at` |
|---|---|---|
| A | 2026-10-22 01:00Z | 2026-10-22 03:00Z |
| H | 2026-10-22 02:00Z | 2026-10-22 04:00Z |

The UI derives an overlap of 02:00–03:00Z and displays it in the viewer's time zone. Nothing is inserted into `match`, and no appointment or agreement is created. If A later removes the window, that changes availability only. The app helps people find a time without implementing a booking system.

Now A beats H by two games to one. Their fixture has `id = 1001`, `team_a_id = A`, and `team_b_id = H`. A reports the result:

| `result_submission.id` | `match_id` | `reporting_side` | `outcome_kind` | A game wins | H game wins | Generated `winner_side` |
|---:|---:|---|---|---:|---:|---|
| 2001 | 1001 | a | played | 2 | 1 | a |

The writer stores `match.team_a_submission_id = 2001`. Each player independently submits their own result, with both score columns in the fixture's A/H orientation. H also reports A 2, H 1:

| `result_submission.id` | `match_id` | `reporting_side` | `outcome_kind` | A game wins | H game wins | Generated `winner_side` |
|---:|---:|---|---|---:|---:|---|
| 2002 | 1001 | b | played | 2 | 1 | a |

H's request supplies `expected_previous_submission_id = NULL`, because H has not reported before. The writer checks H's own report pointer, writes `match.team_b_submission_id = 2002`, and compares the two current reports. Matching means equal outcome kind, canonical A/B game counts, and awarded winner, treating two `NULL` values as equal. It validates that these are the latest reports from opposite sides and appends an agreement decision atomically:

| `match_decision.id` | `selected_submission_id` | `supporting_submission_id` | `authority` | `decided_by` | `reason` |
|---:|---:|---:|---|---|---|
| 3001 | 2001 | 2002 | agreement | NULL | NULL |

| Moment | `match.team_a_submission_id` | `match.team_b_submission_id` | `match.current_decision_id` | Official result | A's derived record from this fixture |
|---|---:|---:|---:|---|---|
| Before reporting | NULL | NULL | NULL | None | 0–0 |
| A reports | 2001 | NULL | NULL | None | 0–0 |
| H reports the same result | 2001 | 2002 | 3001 | A wins 2–1 | 1–0, games 2–1 |

Both report pointers remain as evidence and stop changing once the result is official. Agreement always selects side A's report and retains side B's report as supporting evidence, regardless of which arrived first. Participants and the commissioner can see both current reports; independent authorship does not introduce a hidden-report phase or a separate confirmation action.

No mutable standings totals are updated. `stage_record` derives them by following the current decision to its selected submission. The series score exists in the submission; there is no second official-score payload to keep synchronized. The opposing entrant gets the complementary 0–1 series record and 1–2 game record.

After all 28 North fixtures are resolved, suppose the records look like this. A qualifies outright. C and B tie at the qualification boundary, and an announced secondary criterion selects C. For illustration, C beat B and that result settles their tie under the chosen competition rules; the proposal does not implement an automatic tiebreak ranking rule. F and E are also tied, but that tie does not affect qualification and needs no ruling.

| Team | Derived series wins | Derived series losses | `stage_entry.final_place` before final publication | After final publication |
|---|---:|---:|---:|---:|
| A | 6 | 1 | NULL | 1 |
| C | 5 | 2 | NULL | 2 |
| B | 5 | 2 | NULL | NULL |
| D | 4 | 3 | NULL | NULL |
| F | 3 | 4 | NULL | NULL |
| E | 3 | 4 | NULL | NULL |
| G | 2 | 5 | NULL | NULL |
| H | 0 | 7 | NULL | NULL |

The 28 wins and 28 losses account for all 28 series. **Publish final** verifies that the required active fixtures are resolved, no active fixture needs commissioner review, and the qualification ties are settled. In one transaction it records only places 1 and 2, sets the source stage's `standings_finalized_at` / `standings_finalized_by`, admits those two teams to the final, and creates its round and fixture. There is no separate full-ranking certification or final-generation step.

| `stage_entry.stage_id` | `team_id` | `seed` | `final_place` |
|---:|---|---:|---:|
| 102 | A | 1 | NULL |
| 102 | C | 2 | NULL |

A new fixture, say `1100`, pairs A and C in the final round. Their earlier regular-season match remains a different fixture. The new match inherits best-of-five through its round and stage: 3–2 is a valid completed score, while 2–1 is not. The writer must validate those rules; the score table's checks alone do not enforce the stage's best-of value.

If A wins the final 3–2, the two-report process repeats. A is champion because A won the final's official result; no separate final-stage certification is required. The final stage's `final_place` values and finalization metadata stay `NULL`. A's regular-season record stays 6–1, and its final-stage record is separately 1–0. South produces its own champion in the same way. There is no North-versus-South championship fixture in the proposed format.

**Scenario 2: one league has five players instead of eight.** This tests league-size flexibility without introducing another competition format. Suppose a later league contains J–N. The stage rules stay the same, but there are five regular-season entries.

A valid schedule is:

| Round | Fixture 1 | Fixture 2 | Bye |
|---:|---|---|---|
| 1 | K–N | L–M | J |
| 2 | J–N | K–L | M |
| 3 | J–M | N–L | K |
| 4 | J–L | M–K | N |
| 5 | J–K | M–N | L |

The database contains five round rows and ten match rows. **A bye is the absence of a fixture for that entrant in that round.** It creates no fake opponent, submission, decision, or automatic win. The schedule generator can use a temporary bye placeholder internally without persisting it as a team.

| Entrants | Regular-season rounds | Fixtures per round | Regular-season fixtures | Fixtures including a normal final |
|---:|---:|---:|---:|---:|
| 5 | 5 | 2, plus one bye | 10 | 11 |
| 8 | 7 | 4 | 28 | 29 |
| 9 | 9 | 4, plus one bye | 36 | 37 |

For odd N, there are N rounds, `(N − 1) / 2` fixtures per round, and one bye per entrant over the season. Each entrant still faces every other entrant exactly once.

Nothing requires two leagues to have equal sizes or to finish on the same date. An eight-player league and a five-player league would contain 40 fixtures including their two finals. Tournament publication requires at least two entrants. If withdrawals later leave fewer than two eligible qualifiers, final publication is rejected; it does not invent an opponent or automatically declare a champion.

The flexibility limit is the **format**, not N. Setting `qualifier_count = 4` does not create semifinals. The publisher rejects that setup. The fixture totals above assume no extra qualification tiebreaks.

**A qualification tie can require another attempt, entirely through the app.** Suppose A, B, and C finish 5–2 and remain tied after the announced secondary criteria. They are competing for two final places. The commissioner uses **Add tiebreak round**, selects those three existing stage entrants and a playing window, supplies a reason, reviews the generated fixtures, and confirms.

The app appends round 8 to the regular-season stage with `purpose = 'tiebreak'`. This round is one complete qualification attempt: A–B, A–C, and B–C. Each player has two series within its window. The regular-season rule of at most one fixture per team per round applies only to normal rounds. The same stage supplies the best-of-three rule; no new stage or stage entries are needed.

Suppose the results are cyclic:

| Round | Match | Result |
|---:|---|---|
| 8 | A–B | A wins 2–1 |
| 8 | B–C | B wins 2–1 |
| 8 | C–A | C wins 2–1 |

| Player | Normal-season record, still `stage_record` | Round 8 series record, via `round_record` | Round 8 games |
|---|---|---|---|
| A | 5–2 | 1–1 | 3–3 |
| B | 5–2 | 1–1 | 3–3 |
| C | 5–2 | 1–1 | 3–3 |

If the secondary criteria still do not separate them, the commissioner uses **Repeat tiebreak**. This invokes the same creation operation and appends round 9 with fresh fixtures, retaining all round 8 history. Suppose the new results are A over B 2–1, A over C 2–0, and B over C 2–0:

| Player | Normal-season record | Round 8 series record | Round 9 series record | Round 9 games | Qualification |
|---|---|---|---|---|---|
| A | 5–2 | 1–1 | 2–0 | 4–1 | Qualifies |
| B | 5–2 | 1–1 | 1–1 | 3–2 | Qualifies |
| C | 5–2 | 1–1 | 0–2 | 0–4 | Eliminated |

The commissioner publishes the final using that outcome, recording source-stage places 1 and 2 and creating A–B in the existing final stage atomically. If a later attempt is still tied, another attempt remains available before final publication. If only a subset remains tied across the qualification cutoff, only that subset enters the next attempt. Attempts are displayed separately rather than added together, since their participant sets and number of matches may differ.

Creating an attempt, reporting its results, correcting unresolved reports, commissioner resolution, reviewing secondary criteria, and repeating an unresolved attempt must all be available in the application. **No custom SQL is an acceptable operational step.** The exact secondary-criterion order, which results each criterion examines, and how forfeits affect it remain competition rules to settle before launch.

The storage uses a round purpose, creation attribution and tiebreak reason, an exclusion of tiebreak rounds from normal stage totals, and a per-round record view. The ordinary independent-report and commissioner-decision operations are reused. The tiebreak writer checks the tournament revision, entrant eligibility, and completion/review state of earlier active fixtures. Competitive mutations advance that revision so a commissioner cannot confirm an outdated preview.

A commissioner correction never regenerates an earlier attempt or changes its participants. The correction updates the official evidence through a new decision; a later attempt uses the corrected evidence when selecting the remaining tied entrants. Only the fixed withdrawal operation removes affected fixtures. There is no general “cancel this attempt and restore another” workflow.

**Scenario 3: correcting mismatched reports and correcting an official result.** These are alternative branches from the A–H fixture. They show the boundaries of player and commissioner actions.

Suppose A accidentally reports A 2, H 0 as submission 2001, while H reports A 2, H 1 as submission 2002. The current reports disagree, so there is no official result. The commissioner review queue is derived from two unequal current reports on an active fixture with no official decision. A single report means awaiting the opponent, not a dispute. Neither a stored flag nor a separate dispute operation is needed.

Either player may replace only their own report while the fixture remains unresolved, including while it appears in that queue. A notices the typo and submits the correct score with `expected_previous_submission_id = 2001`. The writer preserves the old row, adds submission 2003, and moves only A's pointer:

| Submission | Author | A game wins | H game wins | State after A corrects the typo |
|---:|---|---:|---:|---|
| 2001 | A's manager | 2 | 0 | Preserved history |
| 2002 | H's manager | 2 | 1 | Current side B report |
| 2003 | A's manager | 2 | 1 | Current side A report |

The current reports now match. The same transaction creates decision 3001 with `authority = 'agreement'`, `selected_submission_id = 2003`, and `supporting_submission_id = 2002`. No commissioner intervention is needed. Both report pointers are retained, and the official result removes the fixture from the review queue.

In an alternative branch, A's current report says A 2, H 0, but H's current report says A 1, H 2. The commissioner reviews the evidence and may select either current report if it is correct, or create a submission with `reporting_side = NULL` containing the official score. Suppose H's current report is submission 2002 and the commissioner selects it:

| Decision | Selected submission | Supporting submission | `authority` | `decided_by` | `reason` |
|---:|---:|---:|---|---|---|
| 3001 | 2002 | NULL | commissioner | Commissioner | Reviewed match evidence; H won 2–1 |

The transaction sets the current-decision pointer and retains both players' reports. They still disagree as historical evidence, but this fixture is no longer in the review queue because it now has an official result. A commissioner can also record an initial official result directly when needed; two reports or a prior disagreement are not prerequisites.

After any result becomes official, players cannot submit corrections or create an in-app appeal. They can contact the commissioner outside this workflow. If a correction is warranted, **Correct result** appends a new commissioner-authored submission and a new commissioner decision with a reason, then changes `match.current_decision_id`. It does not edit the old scores, decisions, or player-report pointers. Declining an outside-app request needs no new database row or review boundary.

For example, in a separate branch both players originally reported A 2–1 H, producing agreement decision 3001. The commissioner later verifies that H actually won:

| Decision | Selected submission | Supporting submission | Authority | Official score at that decision | State |
|---:|---|---|---|---|---|
| 3001 | 2001, side A report | 2002, side B report | agreement | A 2–1 H | Preserved history |
| 3002 | 2004, a new commissioner submission | NULL | commissioner | A 1–2 H | Current |

Derived standings now give H the series win. The original agreement preserves both reports as evidence. No submission sequence number or “reviewed through” counter is needed.

**Final publication freezes qualification.** Suppose C's recorded victory over G is corrected after the A–C final has been published:

| Team | Live record before correction | Live record after correction | Recorded source-stage `final_place` | Final membership |
|---|---|---|---:|---|
| C | 5–2 | 4–3 | 2 | Retained |
| B | 5–2 | 5–2 | NULL | Still absent |
| G | 2–5 | 3–4 | NULL | Still absent |

The correction changes historical results, but it cannot change the two finalists, even by commissioner exception. The UI distinguishes current results from the qualification recorded at publication. It does not offer reopening, reseeding, or replacement actions. Corrections to a final's official score can correct its winner, but do not change its participants.

**Scenario 4: withdrawal has one complete, irreversible consequence.** These are alternative branches from the ordinary season, not additional events layered onto the previous correction examples.

Suppose H withdraws after rounds 1 and 2. H has lost to A and F, and has five fixtures remaining. **Withdraw team** records one immutable event and applies the fixed consequences in the same transaction:

| `tournament_withdrawal.team_id` | `decided_by` | `reason` |
|---|---|---|
| H | Commissioner | Withdrawn from the season |

Before final publication, the operation removes H's first-stage results from counted records and removes all remaining first-stage fixtures involving H from active play. This includes any qualification tiebreak fixtures. A fixture with either player's report or an official result is excluded with its evidence intact; a fixture with neither report nor an official result is cancelled. Each affected fixture gets at most one immutable `match_disposition` row. The presence of that row removes the fixture from counted records; there is no stored, independently editable inclusion flag. An already removed fixture needs no second disposition.

| Matches affected in this example | Number of disposition rows | `cancelled` | Consequence |
|---|---:|---|---|
| Two completed fixtures against A and F | 2 | false | Official evidence retained; excluded from records |
| Five remaining fixtures, assuming no reports yet | 5 | true | Cancelled; excluded from records |

There is no visible intermediate state in which H is withdrawn but the commissioner still needs to choose what happens to H's fixtures. Withdrawal, exclusions, cancellations, and revisions commit together. The application does not expose separate per-fixture exclusion, restoration, or cancellation controls.

| Item | Before withdrawal | After withdrawal commits |
|---|---|---|
| H's eligibility | Eligible | Permanently withdrawn |
| A–H and H–F results | Count | Preserved as official results, excluded from standings |
| H's five remaining fixtures | Active | Removed from active play |
| H's stage entry and drafted team | Present | Still present |
| H's manager's league read access | Present | Still present |

The completed scores remain official factual results. In the eventual normal-season records, every remaining entrant has six counted opponents rather than seven. This is the chosen competition rule; retaining completed results and awarding future forfeits is not a second supported withdrawal mode.

| Contribution from H's two completed fixtures | Before withdrawal | After withdrawal |
|---|---|---|
| A | One series win | No counted result |
| F | One series win | No counted result |
| H | Two series losses | No counted results; withdrawn |

If one of H's unfinished fixtures already has either or both players' reports, its disposition is `cancelled = false`; row presence is enough to exclude it. All reports remain as evidence; removal does not manufacture an official score. The players can no longer act on the fixture, and any disagreement creates no outstanding review obligation because the fixture is no longer active. This lets final publication proceed once the other required active fixtures and qualification ties are resolved.

`stage_record.series_completed` counts included official results, so excluding a result reduces it even though the series physically occurred. `match_official_result.counts_in_standings` is derived from the absence of a disposition row. A cancelled unplayed fixture has no row in that view, because it has no decision. The schedule reads fixtures and their dispositions directly to show removed games.

Now consider another branch: the season finishes normally, A and C qualify, and C withdraws before the final has an official result. Final publication has already frozen qualification. The same withdrawal operation records C's withdrawal and awards A a forfeit in the existing final fixture, with a commissioner-authored submission and decision explaining the withdrawal. It does not exclude C's source-stage results, add a finalist, or create another fixture.

| Final-stage entry | `seed` before withdrawal | `seed` after withdrawal |
|---|---:|---:|
| A | 1 | 1 |
| C | 2 | 2 |

| Final fixture | Opponents | Before withdrawal | After withdrawal |
|---:|---|---|---|
| 1100 | A–C | Active; no official result | Official forfeit to A |

The forfeit has an awarded winner and `NULL` game scores. It adds a series win/loss without inventing a played 3–0 result. Any player reports remain historical evidence, and the commissioner decision resolves the fixture. The final continues to have exactly two entries and one fixture.

A withdrawal after the final already has an official result leaves that result and champion unchanged. A withdrawal by a nonfinalist after final publication likewise does not exclude source-stage results or reopen qualification. There is no reinstatement or general restoration operation.

**Scenario 5: concurrent reports do not compete for first-reporter ownership.** Every competitive mutation authenticates the actor, updates the relevant revisions, reads and validates the required state, applies its changes, and commits inside a serializable transaction. The application retries the entire call on the supported transaction-retry errors, including errors at commit. It preserves the client's original expected revision or own-report reference across those retries.

Player `submit_result` operations require `expected_previous_submission_id` for the caller's own side, with `NULL` meaning their first report. They do not require an expected match or tournament revision. Each accepted report still advances the tournament revision and then the match revision, so commissioner previews become stale when the underlying reports change. Commissioner decisions require the expected match revision. Tournament-wide actions such as adding a tiebreak, withdrawal, and publishing the final require the expected tournament revision.

Suppose both players open empty report forms just after their match and submit A 2, H 1 at the same time. Both requests carry `expected_previous_submission_id = NULL`. If A commits first and H's transaction needs a retry, H's original input stays `NULL`. The retry sees H's own pointer still empty, accepts H's report, and makes the matching reports official in that transaction. H is not sent back to review a changed match merely because A reported first. If their reports differ, both are accepted and remain unresolved. If the match became official through another operation or was removed while H's transaction retried, H's report is rejected after re-reading that state.

The own-report reference still protects against stale edits. If two tabs for A both edit submission 2001, the first accepted replacement moves A's pointer. The second request still expects 2001 and is rejected, even if the operations were sequential. Serializable isolation alone cannot detect what A's second tab was editing.

| Competing actions | Required behavior |
|---|---|
| A and H submit their first reports together | Each checks its own empty pointer; transaction retries accept the opponent's concurrent report, then matching current reports become official |
| Two tabs for A replace the same report | Only one can replace the expected own-report ID; the other must refresh |
| A corrects a report while H submits or corrects H's report | Each changes only its own pointer; retries re-read both current reports and produce agreement if they match, unless the fixture already became official or was removed |
| A player reports while a commissioner decides the result | A prior report invalidates the commissioner's expected match revision; a committed official decision rejects later player reporting |
| A result changes after a commissioner previews qualification | The tournament revision changes; final publication from the old preview is rejected |
| An entrant withdraws while the commissioner publishes the final | One action makes the other's tournament revision stale. After refreshing, withdrawal before publication affects eligibility; withdrawal after publication follows the final-forfeit policy |
| Two commissioners append a tiebreak from the same preview | Only one can use that expected tournament revision; the other must refresh |
| A team withdraws while a player reports one of its fixtures | A report committed first invalidates the withdrawal preview; a committed withdrawal preserves earlier evidence and rejects later reporting |

There is no explicit pre-lock protocol for tournaments, matches, or teams. Ordinary writes acquire the database locks they need. Writers update the tournament before affected matches, using ascending match IDs for a batch. For a new round, the tournament update occurs before reading the next `MAX(number) + 1` for its stage. Reports and decisions use generated IDs and explicit pointers; withdrawals and dispositions use their team's or fixture's ID as the key for a single immutable event. No custom per-parent history counters remain.

The tournament revision serves two purposes: stale-preview detection and a shared write point for competitive changes. This deliberately means unrelated matches in one tournament can contend on the same row and may need transaction retries. Removing explicit lock statements does not remove that tradeoff. Revisions and serializable transactions also do not replace business validation: the writer still checks actors, fixture status, current opposite-side reports and their agreement, scores, and qualification rules.

The supported first version therefore keeps these boundaries:

| Area | Adopted rule |
|---|---|
| Competition | Independent leagues; one round robin followed by a two-team final |
| Reporting | One current report per side; each player may replace only their own until the result is official |
| Agreement and disagreement | Matching current reports become official atomically; mismatches allow player corrections or commissioner resolution |
| Official corrections | Commissioner-only, with an immutable new submission, decision, and reason |
| Withdrawal before final publication | Void all of that entrant's counted first-stage results and remove its remaining fixtures atomically |
| Withdrawal after final publication | Preserve source-stage results and finalists; award an unfinished final as a forfeit when a finalist withdraws |
| Qualification | Record only the two qualifiers; freeze them at final publication |
| Championship | Derive the champion from the final's official result |
| Tiebreaks | Repeatable attempts using ordinary fixtures and result handling |
| Availability | Advisory overlap, without appointments or booking |

Two competition rules still need an explicit launch decision: the order and scope of secondary tiebreak criteria, and how to close a tournament when fewer than two eligible qualifiers remain. Final publication rejects that case. Deadline extensions apply to the whole round in this version.

The companion writers enforce cross-league membership checks, score validity against best-of, complete schedules, publication freezing, matching current participant reports, and prevention of draft undo after publication. Report and decision history stays append-only through the callable functions. Read policies and HTTP integration remain unimplemented, so these proposal files are for explicit review and testing rather than deployment.
