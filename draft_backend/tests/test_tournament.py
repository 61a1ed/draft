"""Opt-in PostgreSQL tests of the tournament proposal, outside the live bootstrap."""

import json
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from threading import Barrier
from uuid import uuid4
from zoneinfo import ZoneInfo

import psycopg
import pytest
from psycopg import sql
from psycopg.types.json import Jsonb

pytestmark = pytest.mark.integration

UTC = timezone.utc
START = datetime(2026, 10, 25, 9)
OPENS = datetime(2027, 1, 1, 9, tzinfo=UTC)
CLOSES = OPENS + timedelta(days=7)


@pytest.fixture(scope="session")
def tournament_database(test_database):
    directory = Path(__file__).resolve().parents[1] / "sql"
    with psycopg.connect(test_database.admin) as conn:
        conn.execute("SET LOCAL ROLE dl_owner")
        for name in (
            "tournament.proposal.sql",
            "tournament.mutators.proposal.sql",
            "tournament.results.proposal.sql",
            "tournament.lifecycle.proposal.sql",
            "tournament.grants.proposal.sql",
        ):
            conn.execute((directory / name).read_text(encoding="utf-8"))
    return test_database


@contextmanager
def rejected(code="PT409"):
    with pytest.raises(psycopg.Error) as caught:
        yield
    assert caught.value.sqlstate == code, str(caught.value)


class Tournament:
    def __init__(self, database, count=4, *, publish=True, configure=True):
        self.database = database
        with psycopg.connect(database.admin) as conn:
            self.users = [
                conn.execute(
                    "INSERT INTO dl.app_user (issuer, subject, display_name) "
                    "VALUES ('urn:tournament:test', %s, %s) RETURNING id",
                    (uuid4().hex, f"Tournament manager {i}"),
                ).fetchone()[0]
                for i in range(count + 1)
            ]
            self.owner, self.outsider = self.users[0], self.users[-1]
            self.league = conn.execute(
                "INSERT INTO dl.league "
                "(name, commissioner_user_id, roster_size, started_at, turn_started_at) "
                "VALUES ('Tournament test', %s, 1, now(), now()) RETURNING id",
                (self.owner,),
            ).fetchone()[0]
            self.teams = [
                conn.execute(
                    "INSERT INTO dl.team "
                    "(league_id, name, manager_user_id, draft_position, point_budget) "
                    "VALUES (%s, %s, %s, %s, 100) RETURNING id",
                    (self.league, f"Team {i}", user, i + 1),
                ).fetchone()[0]
                for i, user in enumerate(self.users[:-1])
            ]
            conn.execute(
                "INSERT INTO dl.pick "
                "(league_id, pick_number, draftable_id, selected_by, point_cost) "
                "SELECT %s, row_number() OVER (ORDER BY id), id, %s, 0 "
                "FROM dl.draftable ORDER BY id LIMIT %s",
                (self.league, self.owner, count),
            )
        self.managers = dict(zip(self.teams, self.users, strict=False))
        if not configure:
            return
        assert (
            self.call(
                "configure_tournament",
                p_league=self.league,
                p_revision=None,
                p_time_zone="America/Los_Angeles",
            )
            == self.league
        )
        assert self.revision() == 0
        self.stage = self.call(
            "configure_stage",
            p_league=self.league,
            p_revision=0,
            p_position=1,
            p_name="Season",
            p_format="round_robin",
            p_best_of=3,
        )
        self.final_stage = self.call(
            "configure_stage",
            p_league=self.league,
            p_revision=1,
            p_position=2,
            p_name="Final",
            p_format="single_elimination",
            p_best_of=5,
            p_source_stage=self.stage,
            p_qualifier_count=2,
        )
        if publish:
            self.publish()

    def read(self, query, parameters=()):
        with psycopg.connect(self.database.admin) as conn:
            return conn.execute(query, parameters or None).fetchall()

    def scalar(self, query, parameters=()):
        return self.read(query, parameters)[0][0]

    def revision(self):
        return self.scalar("SELECT revision FROM dl.tournament WHERE league_id=%s", (self.league,))

    def match_revision(self, match):
        return self.scalar("SELECT revision FROM dl.match WHERE id=%s", (match,))

    def call(self, name, *, user=None, barrier=None, **arguments):
        query = sql.SQL("SELECT dl.{}({})").format(
            sql.Identifier(name),
            sql.SQL(", ").join(
                sql.SQL("{} => %s").format(sql.Identifier(key)) for key in arguments
            ),
        )
        # Retry the whole transaction with the ORIGINAL tokens and payload.
        for attempt in range(5):
            try:
                with psycopg.connect(self.database.gateway) as conn:
                    conn.execute("SET TRANSACTION ISOLATION LEVEL SERIALIZABLE")
                    conn.execute("SET LOCAL ROLE dl_api")
                    conn.execute(
                        "SELECT set_config('request.jwt.claims', %s, true)",
                        (json.dumps({"user_id": self.owner if user is None else user}),),
                    )
                    if barrier is not None and attempt == 0:
                        # A shared snapshot before either writer begins makes the race reliable.
                        conn.execute("SELECT revision FROM dl.league WHERE id=%s", (self.league,))
                        barrier.wait(timeout=15)
                    result = conn.execute(query, tuple(arguments.values())).fetchone()[0]
                return result
            except psycopg.errors.SerializationFailure:
                if attempt == 4:
                    raise

    def publish(self, **kwargs):
        return self.call(
            "publish_tournament",
            p_league=self.league,
            p_revision=self.revision(),
            p_starts_at=START,
            **kwargs,
        )

    def matches(self, stage=None):
        return self.read(
            "SELECT m.id, m.team_a_id, m.team_b_id FROM dl.match m "
            "JOIN dl.tournament_round r ON r.id=m.round_id "
            "WHERE r.stage_id=%s ORDER BY m.id",
            (self.stage if stage is None else stage,),
        )

    def report(self, match, user, *, previous=None, a=2, b=0, **kwargs):
        return self.call(
            "submit_result",
            user=user,
            p_match=match,
            p_expected_previous_submission_id=previous,
            p_outcome_kind="played",
            p_team_a_game_wins=a,
            p_team_b_game_wins=b,
            **kwargs,
        )

    def record(self, match, *, a=2, b=0):
        return self.call(
            "record_result",
            p_match=match,
            p_revision=self.match_revision(match),
            p_reason="Commissioner certified score",
            p_outcome_kind="played",
            p_team_a_game_wins=a,
            p_team_b_game_wins=b,
        )

    def complete(self, stage=None):
        for match, _, _ in self.matches(stage):
            if (
                self.scalar("SELECT current_decision_id FROM dl.match WHERE id=%s", (match,))
                is None
            ):
                self.record(match)

    def final(self, qualifiers=None):
        return self.call(
            "publish_final",
            p_league=self.league,
            p_revision=self.revision(),
            p_qualifiers=self.teams[:2] if qualifiers is None else qualifiers,
            p_opens_at=OPENS,
            p_closes_at=CLOSES,
        )


@pytest.mark.parametrize("count", [2, 3, 4, 5, 6])
def test_complete_round_robin_and_local_weekly_windows(tournament_database, count):
    t = Tournament(tournament_database, count)
    rows = t.read(
        "SELECT r.number, r.opens_at, r.closes_at, m.team_a_id, m.team_b_id "
        "FROM dl.tournament_round r JOIN dl.match m ON m.round_id=r.id "
        "WHERE r.stage_id=%s ORDER BY r.number, m.id",
        (t.stage,),
    )
    expected_rounds = count if count % 2 else count - 1
    assert len(rows) == count * (count - 1) // 2
    assert len({tuple(sorted(row[3:])) for row in rows}) == len(rows)
    assert {row[0] for row in rows} == set(range(1, expected_rounds + 1))
    for number in range(1, expected_rounds + 1):
        fixtures = [row for row in rows if row[0] == number]
        teams = [team for row in fixtures for team in row[3:]]
        assert len(teams) == len(set(teams)) == count - count % 2
        opening, closing = fixtures[0][1:3]
        local = ZoneInfo("America/Los_Angeles")
        assert opening.astimezone(local).replace(tzinfo=None) == START + timedelta(weeks=number - 1)
        assert closing.astimezone(local).replace(tzinfo=None) == START + timedelta(weeks=number)
    # First week crosses the DST fall-back and therefore lasts 169 actual hours.
    assert rows[0][2].astimezone(UTC) - rows[0][1].astimezone(UTC) == timedelta(hours=169)
    assert t.read(
        "SELECT team_id, seed FROM dl.stage_entry WHERE stage_id=%s ORDER BY team_id", (t.stage,)
    ) == [(team, None) for team in t.teams]
    assert t.matches(t.final_stage) == []


def test_publication_freezes_setup_and_blocks_draft_undo(tournament_database):
    t = Tournament(tournament_database, publish=False)
    with rejected("PT400"):
        t.call("configure_tournament", p_league=t.league, p_revision=2, p_time_zone="Not/AZone")
    with rejected():
        t.call("configure_tournament", p_league=t.league, p_revision=None, p_time_zone="UTC")
    with rejected():
        t.call(
            "configure_stage",
            user=t.users[1],
            p_league=t.league,
            p_revision=2,
            p_position=1,
            p_name="Season",
            p_format="round_robin",
            p_best_of=5,
        )
    assert t.revision() == 2
    t.publish()
    for position, source in [(1, None), (2, t.stage)]:
        with rejected():
            t.call(
                "configure_stage",
                p_league=t.league,
                p_revision=t.revision(),
                p_position=position,
                p_name="Changed",
                p_format="round_robin",
                p_best_of=7,
                p_source_stage=source,
                p_qualifier_count=2 if source else None,
            )
    league_revision = t.scalar("SELECT revision FROM dl.league WHERE id=%s", (t.league,))
    with rejected():
        t.call("draft_action", p_league=t.league, p_revision=league_revision, p_kind="undo")
    with rejected():
        t.publish()
    round_id = t.scalar("SELECT id FROM dl.tournament_round WHERE stage_id=%s LIMIT 1", (t.stage,))
    before = t.matches()
    revision = t.revision()
    t.call(
        "set_round_window",
        p_round=round_id,
        p_revision=revision,
        p_opens_at=OPENS,
        p_closes_at=CLOSES,
    )
    with rejected():
        t.call(
            "set_round_window",
            p_round=round_id,
            p_revision=revision,
            p_opens_at=OPENS,
            p_closes_at=CLOSES,
        )
    assert t.matches() == before


@pytest.mark.parametrize("a,b", [(0, 0), (1, 0), (2, 2), (3, 0), (-1, 2)])
def test_invalid_series_scores_leave_no_evidence(tournament_database, a, b):
    t = Tournament(tournament_database, 2)
    match, team_a, _ = t.matches()[0]
    revision = t.revision()
    with rejected("PT400"):
        t.report(match, t.managers[team_a], a=a, b=b)
    assert t.revision() == revision
    assert t.scalar("SELECT count(*) FROM dl.result_submission WHERE match_id=%s", (match,)) == 0


def test_independent_reports_replacement_agreement_and_correction(tournament_database):
    t = Tournament(tournament_database)
    match, team_a, team_b = t.matches()[0]
    a, b = t.managers[team_a], t.managers[team_b]
    first = t.report(match, a)
    second = t.report(match, b, a=1, b=2)
    with rejected():
        t.report(match, a, previous=None, a=1, b=2)
    assert t.scalar("SELECT current_decision_id FROM dl.match WHERE id=%s", (match,)) is None
    assert t.scalar("SELECT count(*) FROM dl.result_submission WHERE match_id=%s", (match,)) == 2
    replacement = t.report(match, a, previous=first, a=1, b=2)
    assert t.read(
        "SELECT m.team_a_submission_id, m.team_b_submission_id, d.authority, "
        "d.selected_submission_id, d.supporting_submission_id "
        "FROM dl.match m JOIN dl.match_decision d ON d.id=m.current_decision_id WHERE m.id=%s",
        (match,),
    ) == [(replacement, second, "agreement", replacement, second)]
    with rejected():
        t.report(match, b, previous=second)
    before = (t.revision(), t.match_revision(match))
    current = t.scalar("SELECT current_decision_id FROM dl.match WHERE id=%s", (match,))
    assert (
        t.call(
            "correct_result",
            p_match=match,
            p_revision=before[1],
            p_reason="Checked unchanged",
            p_outcome_kind="played",
            p_team_a_game_wins=1,
            p_team_b_game_wins=2,
        )
        == current
    )
    assert (t.revision(), t.match_revision(match)) == before
    corrected = t.call(
        "correct_result",
        p_match=match,
        p_revision=before[1],
        p_reason="Video confirms score",
        p_outcome_kind="played",
        p_team_a_game_wins=2,
        p_team_b_game_wins=1,
    )
    assert corrected != current
    assert t.scalar("SELECT count(*) FROM dl.result_submission WHERE match_id=%s", (match,)) == 4
    assert t.scalar("SELECT count(*) FROM dl.match_decision WHERE match_id=%s", (match,)) == 2
    assert t.read(
        "SELECT team_a_submission_id, team_b_submission_id FROM dl.match WHERE id=%s", (match,)
    ) == [(replacement, second)]
    assert t.read(
        "SELECT winner_side, authority FROM dl.match_official_result WHERE match_id=%s", (match,)
    ) == [("a", "commissioner")]


def test_commissioner_can_select_current_report_and_forfeit_without_scores(tournament_database):
    t = Tournament(tournament_database)
    match, a, b = t.matches()[0]
    report = t.report(match, t.managers[a])
    t.report(match, t.managers[b], a=0, b=2)
    t.call(
        "record_result",
        p_match=match,
        p_revision=t.match_revision(match),
        p_reason="Verified current report",
        p_selected_submission=report,
    )
    assert t.scalar("SELECT count(*) FROM dl.result_submission WHERE match_id=%s", (match,)) == 2
    other = t.matches()[1][0]
    with rejected():
        t.call(
            "record_result",
            p_match=other,
            p_revision=0,
            p_reason="Cross-match report",
            p_selected_submission=report,
        )
    t.call(
        "record_result",
        p_match=other,
        p_revision=0,
        p_reason="Opponent conceded",
        p_outcome_kind="forfeit",
        p_awarded_winner_side="b",
    )
    assert t.read(
        "SELECT outcome_kind, winner_side, team_a_game_wins, team_b_game_wins "
        "FROM dl.match_official_result WHERE match_id=%s",
        (other,),
    ) == [("forfeit", "b", None, None)]


def test_withdrawal_preserves_evidence_and_removes_all_source_fixtures(tournament_database):
    t = Tournament(tournament_database)
    withdrawn = t.teams[0]
    affected = [row for row in t.matches() if withdrawn in row[1:]]
    official, reported, untouched = affected
    t.record(official[0])
    report = t.report(reported[0], t.managers[reported[1]])
    t.call(
        "withdraw_team",
        p_league=t.league,
        p_revision=t.revision(),
        p_team=withdrawn,
        p_reason="Unavailable for remaining season",
    )
    assert dict(
        t.read(
            "SELECT match_id, cancelled FROM dl.match_disposition WHERE match_id=ANY(%s)",
            ([row[0] for row in affected],),
        )
    ) == {official[0]: False, reported[0]: False, untouched[0]: True}
    assert (
        t.scalar(
            "SELECT counts_in_standings FROM dl.match_official_result WHERE match_id=%s",
            (official[0],),
        )
        is False
    )
    assert (
        t.scalar("SELECT team_a_submission_id FROM dl.match WHERE id=%s", (reported[0],)) == report
    )
    with rejected():
        t.report(reported[0], t.managers[reported[2]])
    with rejected():
        t.record(untouched[0])
    t.call(
        "correct_result",
        p_match=official[0],
        p_revision=t.match_revision(official[0]),
        p_reason="Correct historical evidence",
        p_outcome_kind="played",
        p_team_a_game_wins=0,
        p_team_b_game_wins=2,
    )
    assert (
        t.scalar(
            "SELECT counts_in_standings FROM dl.match_official_result WHERE match_id=%s",
            (official[0],),
        )
        is False
    )
    for match, a, b in t.matches():
        if withdrawn not in (a, b):
            t.record(match)
    with rejected():
        t.final([withdrawn, t.teams[1]])
    final = t.final(t.teams[1:3])
    assert final is not None


def test_tiebreak_completion_certified_choices_and_frozen_finalists(tournament_database):
    t = Tournament(tournament_database)

    def tiebreak(teams):
        return t.call(
            "create_tiebreak_round",
            p_stage=t.stage,
            p_revision=t.revision(),
            p_teams=teams,
            p_opens_at=OPENS,
            p_closes_at=CLOSES,
            p_reason="Commissioner-certified qualification tie",
        )

    with rejected():
        t.final()
    with rejected():
        tiebreak(t.teams[:3])
    t.complete()
    baseline = t.read(
        "SELECT team_id, series_wins FROM dl.stage_record WHERE stage_id=%s ORDER BY team_id",
        (t.stage,),
    )
    first = tiebreak(t.teams[:3])
    assert t.scalar("SELECT count(*) FROM dl.match WHERE round_id=%s", (first,)) == 3
    with rejected():
        tiebreak(t.teams[:2])
    with rejected():
        t.final()
    t.complete()
    second = tiebreak(t.teams[:2])
    assert second != first
    t.complete()
    assert (
        t.read(
            "SELECT team_id, series_wins FROM dl.stage_record WHERE stage_id=%s ORDER BY team_id",
            (t.stage,),
        )
        == baseline
    )
    # Selection is deliberately certified by the commissioner, not inferred from rank.
    chosen = t.teams[-2:][::-1]
    final = t.final(chosen)
    assert t.read(
        "SELECT team_id, final_place FROM dl.stage_entry "
        "WHERE stage_id=%s AND final_place IS NOT NULL ORDER BY final_place",
        (t.stage,),
    ) == list(zip(chosen, [1, 2], strict=True))
    assert t.read(
        "SELECT team_id, seed, final_place FROM dl.stage_entry WHERE stage_id=%s ORDER BY seed",
        (t.final_stage,),
    ) == [
        (chosen[0], 1, None),
        (chosen[1], 2, None),
    ]
    prior = t.matches(t.final_stage)
    with rejected():
        t.final()
    with rejected():
        tiebreak(t.teams[:2])
    original = t.matches()[0][0]
    t.call(
        "correct_result",
        p_match=original,
        p_revision=t.match_revision(original),
        p_reason="Late factual correction",
        p_outcome_kind="played",
        p_team_a_game_wins=0,
        p_team_b_game_wins=2,
    )
    assert t.matches(t.final_stage) == prior
    with rejected("PT400"):
        t.report(final, t.managers[prior[0][1]], a=2, b=0)
    t.record(final, a=3, b=1)


def test_finalist_withdrawal_awards_forfeit_without_reseeding(tournament_database):
    t = Tournament(tournament_database)
    t.complete()
    final = t.final()
    _, a, b = t.matches(t.final_stage)[0]
    first = t.report(final, t.managers[a], a=3, b=1)
    second = t.report(final, t.managers[b], a=1, b=3)
    source = t.read(
        "SELECT id, revision, current_decision_id FROM dl.match WHERE id<>%s "
        "AND round_id IN (SELECT id FROM dl.tournament_round WHERE stage_id=%s) "
        "ORDER BY id",
        (final, t.stage),
    )
    t.call(
        "withdraw_team",
        p_league=t.league,
        p_revision=t.revision(),
        p_team=a,
        p_reason="Finalist unavailable",
    )
    decision = t.scalar("SELECT current_decision_id FROM dl.match WHERE id=%s", (final,))
    assert t.read(
        "SELECT outcome_kind, winner_side FROM dl.match_official_result WHERE match_id=%s", (final,)
    ) == [("forfeit", "b")]
    assert t.read(
        "SELECT team_a_submission_id, team_b_submission_id FROM dl.match WHERE id=%s", (final,)
    ) == [(first, second)]
    assert (
        t.read(
            "SELECT id, revision, current_decision_id FROM dl.match WHERE id<>%s "
            "AND round_id IN (SELECT id FROM dl.tournament_round WHERE stage_id=%s) "
            "ORDER BY id",
            (final, t.stage),
        )
        == source
    )
    t.call(
        "withdraw_team",
        p_league=t.league,
        p_revision=t.revision(),
        p_team=b,
        p_reason="Later withdrawal",
    )
    assert t.scalar("SELECT current_decision_id FROM dl.match WHERE id=%s", (final,)) == decision
    assert t.matches(t.final_stage) == [(final, a, b)]


def test_availability_owner_revision_fractional_offset_and_dst_copy(tournament_database):
    t = Tournament(tournament_database, 2)
    team, owner = t.teams[1], t.users[1]
    windows = [{"starts_at": "2026-10-26T16:00:00Z", "ends_at": "2026-10-26T18:00:00Z"}]
    arguments = dict(
        p_team=team,
        p_revision=None,
        p_time_zone="America/Los_Angeles",
        p_display_step_minutes=60,
        p_windows=Jsonb(windows),
    )
    with rejected():
        t.call("set_team_availability", **arguments)
    t.call("set_team_availability", user=owner, **arguments)
    before = t.revision()
    t.call(
        "copy_team_availability_week",
        user=owner,
        p_team=team,
        p_revision=0,
        p_source=date(2026, 10, 26),
        p_target=date(2026, 11, 2),
    )
    assert t.read(
        "SELECT starts_at, ends_at FROM dl.availability_window WHERE team_id=%s ORDER BY starts_at",
        (team,),
    ) == [
        (datetime(2026, 10, 26, 16, tzinfo=UTC), datetime(2026, 10, 26, 18, tzinfo=UTC)),
        (datetime(2026, 11, 2, 17, tzinfo=UTC), datetime(2026, 11, 2, 19, tzinfo=UTC)),
    ]
    with rejected():
        t.call(
            "copy_team_availability_week",
            user=owner,
            p_team=team,
            p_revision=0,
            p_source=date(2026, 10, 26),
            p_target=date(2026, 11, 9),
        )
    fractional = [{"starts_at": "2026-11-03T03:15:00Z", "ends_at": "2026-11-03T04:15:00Z"}]
    t.call(
        "set_team_availability",
        user=owner,
        p_team=team,
        p_revision=1,
        p_time_zone="Asia/Kathmandu",
        p_display_step_minutes=60,
        p_windows=Jsonb(fractional),
    )
    assert t.scalar("SELECT starts_at FROM dl.availability_window WHERE team_id=%s", (team,)) == (
        datetime(2026, 11, 3, 3, 15, tzinfo=UTC)
    )
    t.call(
        "set_team_availability",
        user=owner,
        p_team=team,
        p_revision=2,
        p_time_zone="UTC",
        p_display_step_minutes=30,
        p_windows=Jsonb(fractional),
    )
    assert t.scalar("SELECT starts_at FROM dl.availability_window WHERE team_id=%s", (team,)) == (
        datetime(2026, 11, 3, 3, 15, tzinfo=UTC)
    )
    assert t.revision() == before


def test_vod_is_separate_from_competitive_results(tournament_database):
    t = Tournament(tournament_database)
    match, a, b = t.matches()[0]
    revision = t.revision()
    t.call(
        "set_match_vod",
        user=t.managers[a],
        p_match=match,
        p_revision=0,
        p_vod_url="https://example.com/watch?v=123",
    )
    assert t.revision() == revision
    assert t.match_revision(match) == 1
    assert t.scalar("SELECT current_decision_id FROM dl.match WHERE id=%s", (match,)) is None
    with rejected():
        t.call("set_match_vod", user=t.managers[b], p_match=match, p_revision=0, p_vod_url=None)
    with rejected("PT400"):
        t.call("set_match_vod", p_match=match, p_revision=1, p_vod_url="javascript:alert(1)")
    t.call("set_match_vod", user=t.managers[b], p_match=match, p_revision=1, p_vod_url=None)
    assert t.scalar("SELECT vod_url FROM dl.match WHERE id=%s", (match,)) is None


def test_rpc_authentication_scope_and_private_helpers(tournament_database):
    t = Tournament(tournament_database)
    match, _, _ = t.matches()[0]
    with rejected("PT404"):
        t.report(match, t.outsider)
    with rejected("PT401"):
        t.report(match, -1)
    with rejected():
        t.call(
            "record_result",
            user=t.users[1],
            p_match=match,
            p_revision=0,
            p_reason="Unauthorized",
            p_outcome_kind="played",
            p_team_a_game_wins=2,
            p_team_b_game_wins=0,
        )
    helpers = t.read(
        "SELECT p.oid::regprocedure::text, has_function_privilege('dl_api', p.oid, 'EXECUTE') "
        "FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace "
        "WHERE n.nspname='dl' AND p.proname LIKE 'tournament_%' "
        "AND p.proname <> 'tournament_draft_undo_allowed'"
    )
    assert helpers and not any(allowed for _, allowed in helpers)
    with psycopg.connect(t.database.gateway) as conn:
        conn.execute("SET LOCAL ROLE dl_api")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("UPDATE dl.match SET revision=revision+1 WHERE id=%s", (match,))
    with psycopg.connect(t.database.gateway) as conn:
        conn.execute("SET LOCAL ROLE dl_api")
        conn.execute(
            "SELECT set_config('request.jwt.claims', %s, true)", (json.dumps({"user_id": t.owner}),)
        )
        with pytest.raises(psycopg.Error, match="SERIALIZABLE"):
            conn.execute("SELECT dl.set_match_vod(%s, 0, NULL)", (match,))


def test_opposing_reports_survive_serializable_retry_with_original_null_tokens(tournament_database):
    t = Tournament(tournament_database, 2)
    match, a, b = t.matches()[0]
    barrier = Barrier(2)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(t.report, match, t.managers[team], barrier=barrier) for team in (a, b)
        ]
        reports = [future.result(timeout=30) for future in futures]
    assert len(set(reports)) == 2
    assert t.scalar("SELECT count(*) FROM dl.result_submission WHERE match_id=%s", (match,)) == 2
    assert t.scalar("SELECT count(*) FROM dl.match_decision WHERE match_id=%s", (match,)) == 1
    assert (
        t.scalar("SELECT authority FROM dl.match_official_result WHERE match_id=%s", (match,))
        == "agreement"
    )


def test_draft_undo_and_publication_cannot_both_commit(tournament_database):
    t = Tournament(tournament_database, 2, publish=False)
    barrier = Barrier(2)
    league_revision = t.scalar("SELECT revision FROM dl.league WHERE id=%s", (t.league,))

    def race(name, **arguments):
        try:
            t.call(name, barrier=barrier, **arguments)
            return "committed"
        except psycopg.Error as error:
            return error.sqlstate

    with ThreadPoolExecutor(max_workers=2) as pool:
        published = pool.submit(
            race,
            "publish_tournament",
            p_league=t.league,
            p_revision=t.revision(),
            p_starts_at=START,
        )
        undone = pool.submit(
            race, "draft_action", p_league=t.league, p_revision=league_revision, p_kind="undo"
        )
        outcomes = [published.result(timeout=30), undone.result(timeout=30)]
    assert sorted(outcomes) == ["PT409", "committed"]
    published_at = t.scalar(
        "SELECT published_at FROM dl.tournament WHERE league_id=%s", (t.league,)
    )
    status = t.scalar("SELECT status FROM dl.draft_state WHERE league_id=%s", (t.league,))
    assert (published_at is not None, status) in {(True, "completed"), (False, "drafting")}


@pytest.mark.parametrize("resource", ["tournament", "availability"])
def test_concurrent_first_creation_preserves_expected_absence(tournament_database, resource):
    t = Tournament(tournament_database, 2, configure=False)
    barrier = Barrier(2)
    if resource == "tournament":
        name = "configure_tournament"
        arguments = dict(p_league=t.league, p_revision=None, p_time_zone="UTC")
    else:
        name = "set_team_availability"
        arguments = dict(
            p_team=t.teams[0],
            p_revision=None,
            p_time_zone="UTC",
            p_display_step_minutes=60,
            p_windows=Jsonb([]),
        )

    def create():
        try:
            t.call(name, barrier=barrier, **arguments)
            return "committed"
        except psycopg.Error as error:
            return error.sqlstate

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(create) for _ in range(2)]
        assert sorted(future.result(timeout=30) for future in futures) == ["PT409", "committed"]
    if resource == "tournament":
        assert t.revision() == 0
    else:
        assert (
            t.scalar("SELECT revision FROM dl.team_availability WHERE team_id=%s", (t.teams[0],))
            == 0
        )


def test_invalid_availability_endpoints_roll_back_the_entire_edit(tournament_database):
    t = Tournament(tournament_database, 2)
    team = t.teams[0]
    valid = {"starts_at": "2026-10-26T16:00:00Z", "ends_at": "2026-10-26T18:00:00Z"}
    for invalid in (
        {"starts_at": "tomorrow", "ends_at": valid["ends_at"]},
        {"starts_at": "2026-10-26T16:00:00", "ends_at": valid["ends_at"]},
        {"starts_at": "10/26/2026 16:00+00", "ends_at": valid["ends_at"]},
        {"starts_at": "infinity", "ends_at": valid["ends_at"]},
        {"starts_at": valid["ends_at"], "ends_at": valid["starts_at"]},
        {"starts_at": "2026-02-30T16:00:00Z", "ends_at": valid["ends_at"]},
    ):
        with rejected("PT400"):
            t.call(
                "set_team_availability",
                p_team=team,
                p_revision=None,
                p_time_zone="UTC",
                p_display_step_minutes=60,
                p_windows=Jsonb([valid, invalid]),
            )
        assert t.scalar("SELECT count(*) FROM dl.team_availability WHERE team_id=%s", (team,)) == 0
        assert (
            t.scalar("SELECT count(*) FROM dl.availability_window WHERE team_id=%s", (team,)) == 0
        )


def test_every_writer_notifies_members_only_on_commit_and_noop_is_quiet(tournament_database):
    t = Tournament(tournament_database, 2, configure=False)
    channels = {f"dl_user_{user}" for user in t.users[:-1]}
    with psycopg.connect(t.database.admin, autocommit=True) as listener:
        for user in t.users:
            listener.execute(sql.SQL("LISTEN {}").format(sql.Identifier(f"dl_user_{user}")))

        def changed(function, *args, **kwargs):
            value = function(*args, **kwargs)
            notifications = list(listener.notifies(timeout=1, stop_after=2))
            assert {item.channel for item in notifications} == channels
            assert all(item.payload == "" for item in notifications)
            assert list(listener.notifies(timeout=0.05)) == []
            return value

        changed(
            t.call, "configure_tournament", p_league=t.league, p_revision=None, p_time_zone="UTC"
        )
        changed(
            t.call,
            "configure_tournament",
            p_league=t.league,
            p_revision=0,
            p_time_zone="America/Los_Angeles",
        )
        assert t.revision() == 1
        t.stage = changed(
            t.call,
            "configure_stage",
            p_league=t.league,
            p_revision=1,
            p_position=1,
            p_name="Season",
            p_format="round_robin",
            p_best_of=3,
        )
        assert (
            changed(
                t.call,
                "configure_stage",
                p_league=t.league,
                p_revision=2,
                p_position=1,
                p_name="Renamed season",
                p_format="round_robin",
                p_best_of=3,
            )
            == t.stage
        )
        t.final_stage = changed(
            t.call,
            "configure_stage",
            p_league=t.league,
            p_revision=3,
            p_position=2,
            p_name="Final",
            p_format="single_elimination",
            p_best_of=5,
            p_source_stage=t.stage,
            p_qualifier_count=2,
        )
        changed(t.publish)
        match, a, b = t.matches()[0]
        round_id = t.scalar("SELECT round_id FROM dl.match WHERE id=%s", (match,))
        changed(
            t.call,
            "set_round_window",
            p_round=round_id,
            p_revision=t.revision(),
            p_opens_at=OPENS,
            p_closes_at=CLOSES,
        )
        changed(
            t.call,
            "set_match_vod",
            p_match=match,
            p_revision=0,
            p_vod_url="https://example.com/video",
        )
        changed(
            t.call,
            "set_team_availability",
            p_team=a,
            user=t.managers[a],
            p_revision=None,
            p_time_zone="UTC",
            p_display_step_minutes=60,
            p_windows=Jsonb(
                [{"starts_at": "2026-10-26T16:00:00Z", "ends_at": "2026-10-26T18:00:00Z"}]
            ),
        )
        changed(
            t.call,
            "copy_team_availability_week",
            p_team=a,
            user=t.managers[a],
            p_revision=0,
            p_source=date(2026, 10, 26),
            p_target=date(2026, 11, 2),
        )
        changed(t.report, match, t.managers[a])
        changed(t.report, match, t.managers[b])
        changed(
            t.call,
            "correct_result",
            p_match=match,
            p_revision=t.match_revision(match),
            p_reason="Corrected evidence",
            p_outcome_kind="played",
            p_team_a_game_wins=0,
            p_team_b_game_wins=2,
        )
        t.call(
            "correct_result",
            p_match=match,
            p_revision=t.match_revision(match),
            p_reason="No factual change",
            p_outcome_kind="played",
            p_team_a_game_wins=0,
            p_team_b_game_wins=2,
        )
        assert list(listener.notifies(timeout=0.1)) == []
        with psycopg.connect(t.database.gateway) as writer:
            writer.execute("SET TRANSACTION ISOLATION LEVEL SERIALIZABLE")
            writer.execute("SET LOCAL ROLE dl_api")
            writer.execute(
                "SELECT set_config('request.jwt.claims', %s, true)",
                (json.dumps({"user_id": t.owner}),),
            )
            writer.execute(
                "SELECT dl.set_match_vod(%s, %s, NULL)", (match, t.match_revision(match))
            )
            assert list(listener.notifies(timeout=0.1)) == []
            writer.rollback()
        assert list(listener.notifies(timeout=0.1)) == []
        assert t.scalar("SELECT vod_url FROM dl.match WHERE id=%s", (match,)) is not None
        round_id = changed(
            t.call,
            "create_tiebreak_round",
            p_stage=t.stage,
            p_revision=t.revision(),
            p_teams=t.teams,
            p_opens_at=OPENS,
            p_closes_at=CLOSES,
            p_reason="Certified qualification tie",
        )
        extra = t.scalar("SELECT id FROM dl.match WHERE round_id=%s", (round_id,))
        changed(t.record, extra)
        changed(t.final)
        changed(
            t.call,
            "withdraw_team",
            p_league=t.league,
            p_revision=t.revision(),
            p_team=t.teams[0],
            p_reason="Finalist withdrawal",
        )
