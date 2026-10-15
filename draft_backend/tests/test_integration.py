"""Application workflows and SQL permissions against the runner-owned database."""

import json
from uuid import uuid4

import psycopg
import pytest

pytestmark = pytest.mark.integration


def login(client, username):
    response = client.post("/auth/test", json={"username": username}, headers={"X-Test-Login": "1"})
    assert response.status_code == 200, response.text
    session = response.json()
    client.headers["X-CSRF-Token"] = session["csrf_token"]
    return session["user"]


def rpc(client, name, *, expected=200, **arguments):
    response = client.post("/api/rpc/" + name, json=arguments)
    assert response.status_code == expected, response.text
    return response.json()["result"] if expected == 200 else response.json()


def snapshot(client, league_id):
    response = client.get(f"/api/leagues/{league_id}")
    assert response.status_code == 200, response.text
    return response.json()


def configure_league(client, alice, bob, carol):
    name = "Integration " + uuid4().hex
    league_id = rpc(client, "create_league", p_name=name, p_roster_size=2)
    state = snapshot(client, league_id)
    assert state["revision"] == 0
    assert state["state"]["status"] == "setup"
    assert state["league"]["pick_seconds"] == 60
    assert any(row["id"] == league_id for row in client.get("/api/leagues").json())

    # Configure through every setup RPC, passing the precise viewed revision.
    assert (
        rpc(
            client,
            "update_league",
            p_league=league_id,
            p_revision=0,
            p_name=name + " updated",
            p_roster_size=2,
            p_pick_seconds=90,
        )
        is None
    )
    team_a = rpc(
        client,
        "add_team",
        p_league=league_id,
        p_revision=1,
        p_name="Alice's team",
        p_manager=alice["id"],
        p_point_budget=100,
    )
    team_b = rpc(
        client,
        "add_team",
        p_league=league_id,
        p_revision=2,
        p_name="Bob's team",
        p_manager=bob["id"],
        p_point_budget=100,
    )
    temporary_team = rpc(
        client,
        "add_team",
        p_league=league_id,
        p_revision=3,
        p_name="Temporary",
        p_manager=carol["id"],
        p_point_budget=100,
    )
    rpc(
        client,
        "update_team",
        p_league=league_id,
        p_revision=4,
        p_team=team_b,
        p_name="Bob renamed",
        p_manager=bob["id"],
        p_point_budget=100,
    )
    rpc(
        client,
        "remove_team",
        p_league=league_id,
        p_revision=5,
        p_team=temporary_team,
    )
    rpc(
        client,
        "set_draft_order",
        expected=400,
        p_league=league_id,
        p_revision=6,
        p_teams=[team_a, team_a],
    )
    assert snapshot(client, league_id)["revision"] == 6
    rpc(
        client,
        "set_draft_order",
        p_league=league_id,
        p_revision=6,
        p_teams=[team_a, team_b],
    )
    assert snapshot(client, league_id)["state"]["can_start"] is True

    return league_id, team_a, team_b


def test_draft_workflow_and_database_enforced_access(client):
    assert client.get("/api/leagues").status_code == 401
    alice = login(client, "alice")
    users_response = client.get("/api/users")
    assert users_response.status_code == 200
    users = users_response.json()
    assert all(set(user) == {"id", "display_name"} for user in users)
    bob = next(user for user in users if user["display_name"] == "Bob")
    carol = next(user for user in users if user["display_name"] == "Carol")
    catalog = client.get("/api/draftables").json()
    assert len(catalog) >= 4
    players = [row["id"] for row in catalog[:4]]

    league_id, team_a, team_b = configure_league(client, alice, bob, carol)

    # A removed manager loses visibility and cannot invoke a mutation either.
    login(client, "carol")
    assert client.get(f"/api/leagues/{league_id}").status_code == 404
    assert all(row["id"] != league_id for row in client.get("/api/leagues").json())
    rpc(
        client,
        "draft_action",
        expected=404,
        p_league=league_id,
        p_revision=7,
        p_kind="start",
    )

    login(client, "alice")
    rpc(client, "draft_action", p_league=league_id, p_revision=7, p_kind="start")
    state = snapshot(client, league_id)
    assert state["state"]["status"] == "drafting"
    assert state["state"]["next_team_id"] == team_a
    assert state["state"]["deadline"] is not None

    login(client, "bob")
    rpc(
        client,
        "draft_action",
        expected=409,
        p_league=league_id,
        p_revision=8,
        p_kind="pick",
        p_draftable=players[0],
    )
    login(client, "alice")
    first_pick = rpc(
        client,
        "draft_action",
        p_league=league_id,
        p_revision=8,
        p_kind="pick",
        p_draftable=players[0],
    )
    rpc(
        client,
        "draft_action",
        expected=409,
        p_league=league_id,
        p_revision=8,
        p_kind="pick",
        p_draftable=players[1],
    )
    state = snapshot(client, league_id)
    assert state["revision"] == 9
    assert state["state"]["next_team_id"] == team_b
    assert state["rosters"][0]["pick_id"] == first_pick

    login(client, "bob")
    rpc(
        client,
        "draft_action",
        expected=409,
        p_league=league_id,
        p_revision=9,
        p_kind="pick",
        p_draftable=players[0],
    )
    assert snapshot(client, league_id)["revision"] == 9
    second_pick = rpc(
        client,
        "draft_action",
        p_league=league_id,
        p_revision=9,
        p_kind="pick",
        p_draftable=players[1],
    )
    # Snake order gives the second team two consecutive turns.
    assert snapshot(client, league_id)["state"]["next_team_id"] == team_b
    rpc(
        client,
        "draft_action",
        expected=409,
        p_league=league_id,
        p_revision=10,
        p_kind="undo",
    )
    login(client, "alice")
    assert (
        rpc(client, "draft_action", p_league=league_id, p_revision=10, p_kind="undo") == second_pick
    )
    state = snapshot(client, league_id)
    assert len(state["rosters"]) == 1
    undone = next(row for row in state["history"] if row["id"] == second_pick)
    assert undone["undone_by"] == alice["id"]
    assert undone["undone_at"] is not None

    # An undone draftable becomes available again. The commissioner may pick
    # on behalf of any team; PostgreSQL assigns the snake-order roster.
    replacement = rpc(
        client,
        "draft_action",
        p_league=league_id,
        p_revision=11,
        p_kind="pick",
        p_draftable=players[1],
    )
    assert replacement != second_pick
    for revision, player in [(12, players[2]), (13, players[3])]:
        rpc(
            client,
            "draft_action",
            p_league=league_id,
            p_revision=revision,
            p_kind="pick",
            p_draftable=player,
        )
    state = snapshot(client, league_id)
    assert state["state"]["status"] == "completed"
    assert state["state"]["next_team_id"] is None
    assert state["state"]["deadline"] is None
    assert [row["team_id"] for row in state["rosters"]] == [team_a, team_b, team_b, team_a]
    assert len(state["history"]) == 5


def test_oidc_registration_is_idempotent_and_updates_display_name(client):
    subject = "integration-oidc-" + uuid4().hex
    first = client.portal.call(
        client.app.state.db.register_identity, "https://id.example", subject, "First name"
    )
    second = client.portal.call(
        client.app.state.db.register_identity, "https://id.example", subject, "Updated name"
    )
    assert second == {"id": first["id"], "display_name": "Updated name"}
    other = client.portal.call(
        client.app.state.db.register_identity, "https://other.example", subject, "Other user"
    )
    assert other["id"] != first["id"]
    login(client, "alice")
    users = client.get("/api/users").json()
    matches = [user for user in users if user["id"] == first["id"]]
    assert matches == [second]


@pytest.mark.parametrize(
    "query",
    [
        "SELECT issuer, subject FROM dl.app_user",
        "UPDATE dl.league SET revision = revision + 1 WHERE false",
        "INSERT INTO dl.draftable (name) VALUES ('forbidden')",
        "SELECT dl.require_writer()",
        "SELECT dl.bump(1)",
        "SELECT dl.admit(1, 0, 'configure')",
    ],
)
def test_api_role_cannot_bypass_rpc_boundary(database_url, query):
    with psycopg.connect(database_url, autocommit=True) as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            with conn.transaction():
                conn.execute("SET TRANSACTION ISOLATION LEVEL SERIALIZABLE")
                conn.execute("SET LOCAL ROLE dl_api")
                conn.execute(
                    "SELECT set_config('request.jwt.claims', %s, true)",
                    (json.dumps({"user_id": 1}),),
                )
                conn.execute(query)
        # Transaction-local role and claims are restored after this failed transaction.
        assert conn.execute("SELECT current_user").fetchone()[0] == "dl_gateway"
        claims = conn.execute("SELECT current_setting('request.jwt.claims', true)").fetchone()[0]
        assert claims in (None, "")


def create_two_team_league(client, *, name, roster_size, budgets):
    alice = login(client, "alice")
    bob = next(u for u in client.get("/api/users").json() if u["display_name"] == "Bob")
    league = rpc(client, "create_league", p_name=name, p_roster_size=roster_size)
    teams = [
        rpc(
            client,
            "add_team",
            p_league=league,
            p_revision=revision,
            p_name=user["display_name"],
            p_manager=user["id"],
            p_point_budget=budget,
        )
        for revision, (user, budget) in enumerate(zip((alice, bob), budgets, strict=True))
    ]
    return league, *teams


def test_point_budgets_exact_limit_rejection_commissioner_and_undo(client):
    league, a, b = create_two_team_league(client, name="Points", roster_size=2, budgets=(0, 1))
    bob = next(u for u in client.get("/api/users").json() if u["display_name"] == "Bob")
    catalog = client.get("/api/draftables").json()
    by_cost = {d["point_cost"]: d for d in catalog}
    free, cheap, costly = (by_cost[cost] for cost in (0, 1, 2))
    rpc(
        client,
        "update_team",
        p_league=league,
        p_revision=2,
        p_team=b,
        p_name="B",
        p_manager=bob["id"],
        p_point_budget=2,
    )
    rpc(client, "set_draft_order", p_league=league, p_revision=3, p_teams=[a, b])
    rpc(client, "draft_action", p_league=league, p_revision=4, p_kind="start")

    def act(revision, kind="pick", expected=200, **args):
        return rpc(
            client,
            "draft_action",
            expected=expected,
            p_league=league,
            p_revision=revision,
            p_kind=kind,
            **args,
        )

    def balances():
        return {
            t["id"]: (t["point_budget"], t["points_spent"], t["points_remaining"])
            for t in snapshot(client, league)["teams"]
        }

    assert balances() == {a: (0, 0, 0), b: (2, 0, 2)}
    before = snapshot(client, league)
    assert act(5, p_draftable=cheap["id"], expected=409) == {"detail": "point_budget_exceeded"}
    after = snapshot(client, league)
    for key in ["revision", "league", "state", "history", "teams"]:
        assert after[key] == before[key]
    act(5, p_draftable=free["id"])
    # Alice is commissioner: it is Bob's two-point budget that must be charged.
    picked = act(6, p_draftable=costly["id"])
    assert balances() == {a: (0, 0, 0), b: (2, 2, 0)}
    row = next(r for r in snapshot(client, league)["rosters"] if r["pick_id"] == picked)
    assert row["team_id"] == b and row["point_cost"] == 2
    login(client, "bob")
    assert act(7, p_draftable=cheap["id"], expected=409)["detail"] == "point_budget_exceeded"
    login(client, "alice")
    # Setup budgets freeze at start, even for the commissioner.
    assert rpc(
        client,
        "update_team",
        expected=409,
        p_league=league,
        p_revision=7,
        p_team=b,
        p_name="B",
        p_manager=bob["id"],
        p_point_budget=100,
    )["detail"] == ("action_not_permitted")
    assert act(7, kind="undo") == picked
    assert balances()[b] == (2, 0, 2)
    assert (
        next(h for h in snapshot(client, league)["history"] if h["id"] == picked)["point_cost"] == 2
    )
    replacement = act(8, p_draftable=costly["id"])
    assert replacement != picked
    assert balances()[b] == (2, 2, 0)


def test_only_commissioner_can_make_free_picks(client):
    league, a, b = create_two_team_league(client, name="Free picks", roster_size=1, budgets=(0, 0))
    players = [d["id"] for d in client.get("/api/draftables").json() if d["point_cost"] > 0]
    rpc(client, "set_draft_order", p_league=league, p_revision=2, p_teams=[a, b])

    def act(revision, kind="pick", expected=200, **args):
        return rpc(
            client,
            "draft_action",
            expected=expected,
            p_league=league,
            p_revision=revision,
            p_kind=kind,
            **args,
        )

    assert act(3, kind="start", p_charge_points=False, expected=400)["detail"] == (
        "invalid_draft_action"
    )
    act(3, kind="start")
    assert act(4, p_draftable=players[0], expected=409)["detail"] == "point_budget_exceeded"
    # Commissioner can waive a charge for their own team too.
    act(4, p_draftable=players[0], p_charge_points=False)
    login(client, "bob")
    assert act(5, p_draftable=players[1], p_charge_points=False, expected=409)["detail"] == (
        "action_not_permitted"
    )
    assert snapshot(client, league)["revision"] == 5
    login(client, "alice")
    assert act(4, p_draftable=players[1], p_charge_points=False, expected=409)["detail"] == (
        "stale_revision"
    )
    assert act(5, p_draftable=players[0], p_charge_points=False, expected=409)["detail"] == (
        "already_exists_or_claimed"
    )
    picked = act(5, p_draftable=players[1], p_charge_points=False)
    state = snapshot(client, league)
    assert state["state"]["status"] == "completed"
    assert all(t["points_spent"] == t["points_remaining"] == 0 for t in state["teams"])
    assert all(r["point_cost"] == 0 for r in state["rosters"])
    assert act(6, kind="undo", p_charge_points=False, expected=400)["detail"] == (
        "invalid_draft_action"
    )
    assert act(6, kind="undo") == picked
    assert act(7, p_draftable=players[1], expected=409)["detail"] == "point_budget_exceeded"
    state = snapshot(client, league)
    assert state["history"][0]["point_cost"] == 0
    assert all(t["points_spent"] == 0 for t in state["teams"])
