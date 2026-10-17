"""Tournament transport contracts and real database workflows through HTTP."""

from datetime import date, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from psycopg.types.json import Jsonb

from draft_backend import app as module
from draft_backend.config import Settings


def login(client, username="alice"):
    response = client.post(
        "/auth/test", json={"username": username}, headers={"X-Test-Login": "1"}
    )
    assert response.status_code == 200, response.text
    client.headers["X-CSRF-Token"] = response.json()["csrf_token"]
    return response.json()["user"]


def rpc(client, name, *, expected=200, **arguments):
    response = client.post("/api/rpc/" + name, json=arguments)
    assert response.status_code == expected, response.text
    return response.json()["result"] if expected == 200 else response.json()


def read(client, path):
    response = client.get(path)
    assert response.status_code == 200, response.text
    return response.json()


@pytest.fixture
def application(monkeypatch):
    db = SimpleNamespace(
        open=AsyncMock(), close=AsyncMock(), is_test_database=AsyncMock(return_value=True),
        test_user=AsyncMock(return_value={"id": 1, "display_name": "Alice"}),
        user=AsyncMock(return_value={"id": 1, "display_name": "Alice"}),
        write=AsyncMock(return_value=[{"result": 17}]),
        tournament=AsyncMock(return_value={"league_id": 7}),
        match=AsyncMock(return_value={"id": 7}),
        team_availability=AsyncMock(return_value={"team_id": 7}),
    )
    monkeypatch.setattr(module, "Database", lambda _: db)
    monkeypatch.setattr(module, "migrate", lambda *args, **kwargs: [])
    settings = Settings(
        database_url="postgresql://unused",
        migration_database_url="postgresql://unused",
        session_secret="x" * 40,
        public_base_url="http://testserver",
        app_env="test", test_login_enabled=True, cookie_secure=False,
    )
    return module.create_app(settings), db


@pytest.mark.parametrize(
    "function, body, parameters, result",
    [
        ("publish_tournament",
         {"p_league": 2, "p_league_revision": 3, "p_first_suggested_date": "2026-10-19"},
         (2, 3, date(2026, 10, 19), 3, 5), None),
        ("set_round_suggested_date",
         {"p_round": 2, "p_revision": 3, "p_suggested_start_date": None},
         (2, 3, None), None),
        ("copy_team_availability_week",
         {"p_team": 2, "p_revision": 3, "p_source": "2026-10-19", "p_target": "2026-10-26"},
         (2, 3, date(2026, 10, 19), date(2026, 10, 26)), None),
        ("submit_result",
         {"p_match": 2, "p_expected_previous_submission_id": None, "p_outcome_kind": "played",
          "p_team_a_game_wins": 2, "p_team_b_game_wins": 1},
         (2, None, "played", 2, 1, None), 17),
        ("record_result",
         {"p_match": 2, "p_revision": 3, "p_reason": "Reviewed", "p_selected_submission": 4},
         (2, 3, "Reviewed", 4, None, None, None, None), 17),
        ("correct_result",
         {"p_match": 2, "p_revision": 3, "p_reason": "Reviewed", "p_outcome_kind": "forfeit",
          "p_awarded_winner_side": "b"},
         (2, 3, "Reviewed", "forfeit", None, None, "b"), 17),
        ("set_match_vod", {"p_match": 2, "p_revision": 3, "p_vod_url": None},
         (2, 3, None), None),
        ("withdraw_team", {"p_league": 2, "p_revision": 3, "p_team": 4, "p_reason": "Left"},
         (2, 3, 4, "Left"), None),
        ("create_tiebreak_round",
         {"p_stage": 2, "p_revision": 3, "p_teams": [4, 5], "p_reason": "Tie for qualification"},
         (2, 3, [4, 5], "Tie for qualification", None), 17),
        ("publish_final", {"p_league": 2, "p_revision": 3, "p_qualifiers": [4, 5]},
         (2, 3, [4, 5], None), 17),
    ],
)
def test_tournament_rpc_contracts_and_session_boundary(
    application, function, body, parameters, result,
):
    app, db = application
    with TestClient(app) as client:
        path = "/api/rpc/" + function
        assert client.post(path, json=body).status_code == 401
        login(client)
        assert client.post(path, json=body, headers={"X-CSRF-Token": "bad"}).status_code == 403
        response = client.post(path, json=body, headers={"Origin": "https://elsewhere"})
        assert response.status_code == 403
        assert client.post(path, json={**body, "actor_user_id": 99}).status_code == 422
        db.write.assert_not_awaited()
        response = client.post(path, json=body)
        assert response.status_code == 200
        assert response.json() == {"result": result}
        assert response.headers["cache-control"] == "no-store"
        placeholders = ", ".join(["%s"] * len(parameters))
        db.write.assert_awaited_once_with(
            1, f"SELECT dl.{function}({placeholders}) AS result", parameters,
        )
        assert client.post("/api/rpc/tournament_bump", json={"p_league": 2}).status_code == 404


def test_availability_jsonb_preserves_instants_and_rejects_ambiguous_input(application):
    app, db = application
    body = {
        "p_team": 2, "p_revision": None, "p_time_zone": "America/Los_Angeles",
        "p_display_step_minutes": 30,
        "p_windows": [{"starts_at": "2026-10-19T09:00:00-07:00",
                       "ends_at": "2026-10-19T17:00:00Z"}],
    }
    with TestClient(app) as client:
        assert client.post("/api/rpc/set_team_availability", json=body).status_code == 401
        login(client)
        for invalid in ["2026-10-19T09:00:00", "2026-10-19", 1792425600,
                        "2026-10-19T09:00:00-0700", "2026-13-19T09:00:00Z"]:
            bad = {**body, "p_windows": [{**body["p_windows"][0], "starts_at": invalid}]}
            assert client.post("/api/rpc/set_team_availability", json=bad).status_code == 422
        bad = {**body, "p_windows": [{**body["p_windows"][0], "extra": "ignored?"}]}
        assert client.post("/api/rpc/set_team_availability", json=bad).status_code == 422
        db.write.assert_not_awaited()
        assert rpc(client, "set_team_availability", **body) is None
        args = db.write.await_args.args
        assert args[:2] == (1, "SELECT dl.set_team_availability(%s, %s, %s, %s, %s) AS result")
        assert args[2][:4] == (2, None, "America/Los_Angeles", 30)
        assert isinstance(args[2][4], Jsonb)
        assert args[2][4].obj == body["p_windows"]
        cleared = {**body, "p_revision": 0, "p_windows": []}
        assert rpc(client, "set_team_availability", **cleared) is None
        assert db.write.await_args.args[2][4].obj == []


@pytest.mark.parametrize("invalid", ["2026-10-19T00:00:00Z", "2026-10-19T00:00:00",
                                    1792368000, "2026-02-30", "20261019", None])
def test_round_dates_are_calendar_dates(application, invalid):
    app, db = application
    with TestClient(app) as client:
        login(client)
        response = client.post("/api/rpc/publish_tournament", json={
            "p_league": 2, "p_league_revision": 3, "p_first_suggested_date": invalid,
        })
        assert response.status_code == 422
        db.write.assert_not_awaited()


@pytest.mark.parametrize("path, method, detail", [
    ("/api/leagues/7/tournament", "tournament", "tournament_not_found"),
    ("/api/matches/7", "match", "match_not_found"),
    ("/api/teams/7/availability", "team_availability", "team_not_found"),
])
def test_tournament_reads_are_public_and_missing_resources_return_404(
    application, path, method, detail,
):
    app, db = application
    operation = getattr(db, method)
    with TestClient(app) as client:
        assert client.get(path).status_code == 200
        operation.assert_awaited_with(None, 7)
        login(client)
        assert client.get(path).status_code == 200
        operation.assert_awaited_with(1, 7)
        operation.return_value = None
        response = client.get(path)
        assert response.status_code == 404
        assert response.json() == {"detail": detail}


def published_tournament(client):
    """Create the entire draft and tournament through the ordinary gateway."""
    login(client)
    users = {u["display_name"].lower(): u["id"] for u in read(client, "/api/users")}
    league = rpc(client, "create_league", p_name="Tournament HTTP " + uuid4().hex, p_roster_size=1)
    teams = {}
    revision = 0
    for name in ["alice", "bob", "carol"]:
        teams[rpc(client, "add_team", p_league=league, p_revision=revision,
                  p_name=name, p_manager=users[name], p_point_budget=100)] = name
        revision += 1
    rpc(client, "set_draft_order", p_league=league, p_revision=revision, p_teams=list(teams))
    revision += 1
    rpc(client, "draft_action", p_league=league, p_revision=revision, p_kind="start")
    revision += 1
    for draftable in read(client, "/api/draftables")[:3]:
        rpc(client, "draft_action", p_league=league, p_revision=revision,
            p_kind="pick", p_draftable=draftable["id"])
        revision += 1
    assert read(client, f"/api/leagues/{league}")["state"]["status"] == "completed"
    assert client.get(f"/api/leagues/{league}/tournament").status_code == 404
    assert rpc(client, "publish_tournament", p_league=league, p_league_revision=revision,
               p_first_suggested_date="2026-10-19") is None
    return league, teams


@pytest.mark.integration
def test_tournament_http_workflow_history_and_public_reads(client):
    league, teams = published_tournament(client)
    path = f"/api/leagues/{league}/tournament"
    state = read(client, path)
    assert state["revision"] == 0
    assert [s["best_of"] for s in state["stages"]] == [3, 5]
    assert len(state["entries"]) == len(state["matches"]) == 3
    assert [r["suggested_start_date"] for r in state["rounds"]] == [
        "2026-10-19", "2026-10-26", "2026-11-02",
    ]
    assert state["current_submissions"] == state["official_results"] == []
    assert len(state["stage_records"]) == 3
    assert rpc(client, "set_round_suggested_date", p_round=state["rounds"][0]["id"],
               p_revision=0, p_suggested_start_date=None) is None
    assert rpc(client, "publish_final", expected=409, p_league=league, p_revision=1,
               p_qualifiers=list(teams)[:2])["detail"] == "stage_matches_unresolved"

    fixture = state["matches"][0]
    match_id = fixture["id"]
    match_path = f"/api/matches/{match_id}"
    for side in ["a", "b"]:
        login(client, teams[fixture[f"team_{side}_id"]])
        submission = rpc(client, "submit_result", p_match=match_id,
                         p_expected_previous_submission_id=None, p_outcome_kind="played",
                         p_team_a_game_wins=2, p_team_b_game_wins=1)
        assert submission > 0
    match = read(client, match_path)
    assert len(match["submissions"]) == 2
    assert match["official_result"]["authority"] == "agreement"
    assert match["official_result"]["winner_side"] == "a"
    assert len(read(client, path)["current_submissions"]) == 2

    login(client)
    rpc(client, "set_match_vod", p_match=match_id, p_revision=match["revision"],
        p_vod_url="https://example.com/replay")
    match = read(client, match_path)
    assert match["vod_url"] == "https://example.com/replay"
    rpc(client, "correct_result", p_match=match_id, p_revision=match["revision"],
        p_reason="Score transcribed backward", p_outcome_kind="played",
        p_team_a_game_wins=1, p_team_b_game_wins=2)
    match = read(client, match_path)
    assert len(match["submissions"]) == 3
    assert len(match["decisions"]) == 2
    assert match["official_result"]["winner_side"] == "b"
    assert match["team_a_submission_id"] == match["submissions"][0]["id"]

    for fixture in state["matches"][1:]:
        rpc(client, "record_result", p_match=fixture["id"], p_revision=0,
            p_reason="Reviewed replay", p_outcome_kind="played",
            p_team_a_game_wins=2, p_team_b_game_wins=0)
    state = read(client, path)
    round_id = rpc(client, "create_tiebreak_round", p_stage=state["stages"][0]["id"],
                   p_revision=state["revision"], p_teams=list(teams)[:2],
                   p_reason="Qualification tie")
    state = read(client, path)
    tiebreak = next(m for m in state["matches"] if m["round_id"] == round_id)
    rpc(client, "record_result", p_match=tiebreak["id"], p_revision=0,
        p_reason="Reviewed tiebreak", p_outcome_kind="forfeit", p_awarded_winner_side="a")
    state = read(client, path)
    assert len(state["round_records"]) == 8
    assert sum(r["series_completed"] for r in state["stage_records"]) == 6
    final_id = rpc(client, "publish_final", p_league=league, p_revision=state["revision"],
                   p_qualifiers=list(teams)[:2])
    state = read(client, path)
    assert len(state["entries"]) == 5
    assert len(state["matches"]) == 5
    assert next(r for r in state["rounds"] if r["stage_id"] == state["stages"][1]["id"])[
        "suggested_start_date"
    ] is None
    rpc(client, "withdraw_team", p_league=league, p_revision=state["revision"],
        p_team=list(teams)[0], p_reason="Unable to continue")
    rpc(client, "record_result", p_match=final_id, p_revision=0,
        p_reason="Final forfeit", p_outcome_kind="forfeit", p_awarded_winner_side="b")

    client.cookies.clear()
    state = read(client, path)
    assert len(state["withdrawals"]) == 1
    assert state["dispositions"] == []  # Final publication froze source results.
    assert len(read(client, match_path)["decisions"]) == 2
    assert read(client, f"/api/matches/{final_id}")["official_result"]["winner_side"] == "b"


@pytest.mark.integration
def test_availability_http_jsonb_dates_authorization_and_empty_state(client):
    league, teams = published_tournament(client)
    team = next(iter(teams))
    path = f"/api/teams/{team}/availability"
    assert read(client, path) == {"team_id": team, "availability": None, "windows": []}
    body = {
        "p_team": team, "p_revision": None, "p_time_zone": "America/Los_Angeles",
        "p_display_step_minutes": 30,
        "p_windows": [{"starts_at": "2026-10-19T09:00:00-07:00",
                       "ends_at": "2026-10-19T10:00:00-07:00"}],
    }
    login(client, "bob")
    assert rpc(client, "set_team_availability", expected=409, **body)[
        "detail"
    ] == "action_not_permitted"
    login(client)
    assert rpc(client, "set_team_availability", **body) is None
    state = read(client, path)
    assert state["availability"]["revision"] == 0
    assert datetime.fromisoformat(state["windows"][0]["starts_at"]) == datetime.fromisoformat(
        "2026-10-19T16:00:00+00:00"
    )
    assert rpc(client, "set_team_availability", expected=409, **body)["detail"] == "stale_revision"
    assert rpc(client, "copy_team_availability_week", p_team=team, p_revision=0,
               p_source="2026-10-19", p_target="2026-11-02") is None
    state = read(client, path)
    assert state["availability"]["revision"] == 1
    assert len(state["windows"]) == 2
    assert datetime.fromisoformat(state["windows"][1]["starts_at"]) == datetime.fromisoformat(
        "2026-11-02T17:00:00+00:00"
    )
    assert read(client, f"/api/leagues/{league}/tournament")["revision"] == 0
    client.cookies.clear()
    assert read(client, path) == state
    login(client)
    rpc(client, "set_team_availability", **{**body, "p_revision": 1, "p_windows": []})
    assert read(client, path)["windows"] == []
    assert read(client, path)["availability"]["revision"] == 2

