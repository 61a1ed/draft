"""Account wake-ups are determined transactionally by SQL, including management changes."""

import json
from contextlib import contextmanager
from types import SimpleNamespace
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql

pytestmark = pytest.mark.integration


@pytest.fixture
def notifications(test_database):
    with (
        psycopg.connect(test_database.admin, autocommit=True) as admin,
        psycopg.connect(test_database.gateway, autocommit=True) as writer,
        psycopg.connect(test_database.gateway, autocommit=True) as listener,
    ):
        accounts = [
            admin.execute(
                "INSERT INTO dl.app_user (issuer, subject, display_name) "
                "VALUES ('notification-test', %s, %s) RETURNING id",
                (uuid4().hex, name),
            ).fetchone()[0]
            for name in ("Commissioner", "Manager", "Other manager", "Replacement", "Outsider")
        ]
        for user_id in accounts:
            listener.execute(sql.SQL("LISTEN {}").format(sql.Identifier(f"dl_user_{user_id}")))
        # An obsolete global notification must not survive alongside the new scheme.
        listener.execute("LISTEN dl_changed")
        yield SimpleNamespace(admin=admin, writer=writer, listener=listener, accounts=accounts)


@contextmanager
def actor(conn, user_id):
    with conn.transaction():
        conn.execute("SET TRANSACTION ISOLATION LEVEL SERIALIZABLE")
        conn.execute("SET LOCAL ROLE dl_api")
        conn.execute(
            "SELECT set_config('request.jwt.claims', %s, true)",
            (json.dumps({"user_id": user_id}),),
        )
        yield conn


def call(state, query, arguments=()):
    with actor(state.writer, state.accounts[0]) as conn:
        return conn.execute(query, arguments).fetchone()[0]


def assert_wakes(state, *accounts):
    messages = list(state.listener.notifies(timeout=0.1))
    assert all(message.payload == "" for message in messages)
    assert sorted(message.channel for message in messages) == sorted(
        f"dl_user_{user_id}" for user_id in accounts
    )


def test_sql_rpc_membership_and_draft_changes_notify_exact_accounts(notifications):
    state = notifications
    commissioner, manager, other, replacement, _outsider = state.accounts
    league = call(state, "SELECT dl.create_league('Notification draft', 1)")
    assert_wakes(state, commissioner)
    first = call(state, "SELECT dl.add_team(%s, 0, 'First', %s, 100)", (league, manager))
    assert_wakes(state, commissioner, manager)
    second = call(state, "SELECT dl.add_team(%s, 1, 'Second', %s, 100)", (league, other))
    assert_wakes(state, commissioner, manager, other)
    call(state, "SELECT dl.update_league(%s, 2, 'Renamed', 1, 30)", (league,))
    assert_wakes(state, commissioner, manager, other)

    call(
        state,
        "SELECT dl.update_team(%s, 3, %s, 'Replacement', %s, 100)",
        (league, first, replacement),
    )
    # Both sides of reassignment refresh, after the old manager stops managing the team.
    assert_wakes(state, commissioner, manager, other, replacement)
    call(state, "SELECT dl.remove_team(%s, 4, %s)", (league, first))
    assert_wakes(state, commissioner, other, replacement)
    first = call(state, "SELECT dl.add_team(%s, 5, 'Rejoined', %s, 100)", (league, manager))
    assert_wakes(state, commissioner, manager, other)
    call(state, "SELECT dl.set_draft_order(%s, 6, %s)", (league, [first, second]))
    assert_wakes(state, commissioner, manager, other)
    # Supply fixture colors without shifting the draft revision assertions.
    state.admin.execute("UPDATE dl.team SET color_rgb = 0 WHERE league_id = %s", (league,))
    assert_wakes(state, commissioner, manager, other)
    call(state, "SELECT dl.draft_action(%s, 7, 'start')", (league,))
    assert_wakes(state, commissioner, manager, other)
    draftable = state.admin.execute("SELECT min(id) FROM dl.draftable").fetchone()[0]
    call(state, "SELECT dl.draft_action(%s, 8, 'pick', %s)", (league, draftable))
    assert_wakes(state, commissioner, manager, other)
    call(state, "SELECT dl.draft_action(%s, 9, 'undo')", (league,))
    assert_wakes(state, commissioner, manager, other)
    assert (
        state.admin.execute("SELECT revision FROM dl.league WHERE id = %s", (league,)).fetchone()[0]
        == 10
    )
    with pytest.raises(psycopg.Error) as error:
        call(state, "SELECT dl.draft_action(%s, 8, 'pick', %s)", (league, draftable))
    assert error.value.sqlstate == "PT409"
    assert_wakes(state)


def test_notifications_wait_for_commit_and_disappear_on_rollback(notifications):
    state = notifications
    commissioner, manager, *_ = state.accounts
    with actor(state.writer, commissioner) as conn:
        league = conn.execute("SELECT dl.create_league('Transactional', 1)").fetchone()[0]
        assert_wakes(state)
    assert_wakes(state, commissioner)
    with actor(state.writer, commissioner) as conn:
        conn.execute("SELECT dl.add_team(%s, 0, 'Rolled back', %s, 100)", (league, manager))
        assert_wakes(state)
        raise psycopg.Rollback
    assert_wakes(state)
    assert (
        state.admin.execute("SELECT revision FROM dl.league WHERE id = %s", (league,)).fetchone()[0]
        == 0
    )


def test_table_triggers_include_departing_commissioner_and_moved_team(notifications):
    state = notifications
    commissioner, manager, other, replacement, _outsider = state.accounts
    first = call(state, "SELECT dl.create_league('First league', 1)")
    assert_wakes(state, commissioner)
    second = state.admin.execute(
        "INSERT INTO dl.league (name, commissioner_user_id, roster_size) "
        "VALUES ('Second league', %s, 1) RETURNING id",
        (other,),
    ).fetchone()[0]
    assert_wakes(state, other)
    team = state.admin.execute(
        "INSERT INTO dl.team (league_id, name, manager_user_id, point_budget) "
        "VALUES (%s, 'SQL team', %s, 100) RETURNING id",
        (first, manager),
    ).fetchone()[0]
    assert_wakes(state, commissioner, manager)
    state.admin.execute(
        "UPDATE dl.league SET commissioner_user_id = %s WHERE id = %s", (replacement, first)
    )
    assert_wakes(state, commissioner, replacement, manager)
    state.admin.execute("UPDATE dl.team SET league_id = %s WHERE id = %s", (second, team))
    assert_wakes(state, replacement, other, manager)
    state.admin.execute("DELETE FROM dl.team WHERE id = %s", (team,))
    assert_wakes(state, other, manager)
    state.admin.execute("DELETE FROM dl.league WHERE id = %s", (second,))
    assert_wakes(state, other)


@pytest.mark.parametrize(
    "query",
    [
        "SELECT dl.notify_account(1)",
        "SELECT dl.notify_league_accounts(1)",
        "SELECT dl.notify_league()",
        "SELECT dl.notify_team()",
    ],
)
def test_notification_helpers_are_private(notifications, query):
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        call(notifications, query)
    assert_wakes(notifications)
