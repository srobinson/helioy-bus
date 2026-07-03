"""Tests for warroom_kill, warroom_add, warroom_remove, status cross-reference, and token-usage surface.

Tests run against a temporary BUS_DIR via the shared isolated_bus fixture in conftest.py.
"""

from __future__ import annotations

# Tests patch the tmux gateway singleton directly. warroom_server no
# longer re-exports `gateway` since the service extraction.
from server._tmux import gateway

from tests.conftest import _insert_member


def _stub_deferred_launch(monkeypatch, targets: dict[str, str]) -> None:
    """Make warroom_add's deferred launch path deterministic in unit tests."""
    monkeypatch.setattr(gateway, "target_for_pane", lambda pane_id: targets[pane_id], raising=False)
    monkeypatch.setattr(gateway, "set_pane_title", lambda pane_id, title: None, raising=False)
    monkeypatch.setattr(
        gateway,
        "launch_pane",
        lambda **kw: {
            "agent_type": kw["agent_type"],
            "qualified_name": kw["qualified_name"],
            "tmux_target": kw["tmux_target"],
            "pane_id": kw["pane_id"],
            "runtime": kw["runtime"],
        },
        raising=False,
    )


# ── Warroom: warroom_kill ────────────────────────────────────────────────────


def test_warroom_kill_removes_from_db(monkeypatch):
    """Kill removes warroom and members from the database."""
    from server._db import _now, db

    import server.warroom_server as wm

    # Insert a warroom directly
    now = _now()
    with db() as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(
            "INSERT INTO warrooms (warroom_id, tmux_session, tmux_window, cwd, created_at, status) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("test-wr", "main", "test-wr", "/tmp", now, "active"),
        )
        _insert_member(
            conn, warroom_id="test-wr", role="helioy-tools:backend-engineer",
            tmux_target="main:1.0", pane_id="%10", now=now,
        )

    result = wm.warroom_kill(name="test-wr")
    assert "test-wr" in result["killed"]

    with db() as conn:
        assert conn.execute(
            "SELECT * FROM warrooms WHERE warroom_id = 'test-wr'"
        ).fetchone() is None
        assert conn.execute(
            "SELECT * FROM warroom_members WHERE warroom_id = 'test-wr'"
        ).fetchall() == []


def test_warroom_kill_requires_name_or_all():
    """Kill requires either a name or kill_all flag."""
    import server.warroom_server as wm

    result = wm.warroom_kill()
    assert "error" in result


# ── Warroom: warroom_status ──────────────────────────────────────────────────


def test_warroom_status_cross_references_agents(monkeypatch):
    """Status cross-references warroom members with registered agents."""
    from server._db import _now, db

    import server.warroom_server as wm

    now = _now()
    with db() as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(
            "INSERT INTO warrooms (warroom_id, tmux_session, tmux_window, cwd, created_at, status) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("status-wr", "main", "status-wr", "/tmp", now, "active"),
        )
        member_id = _insert_member(
            conn, warroom_id="status-wr", role="helioy-tools:backend-engineer",
            tmux_target="main:2.0", pane_id="%20", now=now,
        )
        conn.execute(
            "INSERT INTO agents "
            "(agent_id, cwd, tmux_target, pid, registered_at, last_seen) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("project:helioy-tools:backend-engineer:main:2.0", "/tmp", "main:2.0", 1234, now, now),
        )

    monkeypatch.setattr(gateway, "pane_alive", lambda t: True)

    statuses = wm.warroom_status(name="status-wr")
    assert len(statuses) == 1
    wr = statuses[0]
    assert wr["warroom_id"] == "status-wr"
    assert len(wr["members"]) == 1
    member = wr["members"][0]
    assert member["registered"] is True
    assert member["pane_alive"] is True
    assert member["agent_instance_id"] == "project:helioy-tools:backend-engineer:main:2.0"
    assert member["warroom_member_id"] == member_id
    assert member["desired_role"] == "helioy-tools:backend-engineer"
    assert member["desired_runtime"] == "claude"
    assert member["desired_repo"] is None
    # Reconciler runs before status and promotes pending→active when the
    # inserted agent row matches the member's tmux_target.
    assert member["state"] == "active"
    assert member["spawn_order"] == 0


def test_warroom_status_reconciles_restarted_member_identity(monkeypatch):
    """A restarted pane updates the persisted agent_instance_id."""
    from server._db import _now, db

    import server.warroom_server as wm

    now = _now()
    old_agent_id = "project:helioy-tools:backend-engineer:main:2.0:old"
    new_agent_id = "project:helioy-tools:backend-engineer:main:2.0:new"

    with db() as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(
            "INSERT INTO warrooms (warroom_id, tmux_session, tmux_window, cwd, created_at, status) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("restart-wr", "main", "restart-wr", "/tmp", now, "active"),
        )
        member_id = _insert_member(
            conn,
            warroom_id="restart-wr",
            role="helioy-tools:backend-engineer",
            tmux_target="main:2.0",
            pane_id="%20",
            now=now,
            state="active",
            agent_instance_id=old_agent_id,
        )
        conn.execute(
            "INSERT INTO agents "
            "(agent_id, cwd, tmux_target, pid, registered_at, last_seen) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (new_agent_id, "/tmp", "main:2.0", 1234, now, now),
        )

    monkeypatch.setattr(gateway, "pane_alive", lambda t: True)

    statuses = wm.warroom_status(name="restart-wr")
    member = statuses[0]["members"][0]
    assert member["warroom_member_id"] == member_id
    assert member["registered"] is True
    assert member["state"] == "active"
    assert member["agent_instance_id"] == new_agent_id

    with db() as conn:
        row = conn.execute(
            "SELECT state, agent_instance_id FROM warroom_members "
            "WHERE warroom_member_id = ?",
            (member_id,),
        ).fetchone()
        assert row["state"] == "active"
        assert row["agent_instance_id"] == new_agent_id


def test_warroom_status_reconciles_unregistered_member_state(monkeypatch):
    """Losing registration clears stale persisted identity and active state."""
    from server._db import _now, db

    import server.warroom_server as wm

    now = _now()
    stale_agent_id = "project:helioy-tools:backend-engineer:main:2.0"

    with db() as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(
            "INSERT INTO warrooms (warroom_id, tmux_session, tmux_window, cwd, created_at, status) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("unregister-wr", "main", "unregister-wr", "/tmp", now, "active"),
        )
        member_id = _insert_member(
            conn,
            warroom_id="unregister-wr",
            role="helioy-tools:backend-engineer",
            tmux_target="main:2.0",
            pane_id="%20",
            now=now,
            state="active",
            agent_instance_id=stale_agent_id,
        )

    monkeypatch.setattr(gateway, "pane_alive", lambda t: True)

    statuses = wm.warroom_status(name="unregister-wr")
    member = statuses[0]["members"][0]
    assert member["warroom_member_id"] == member_id
    assert member["registered"] is False
    assert member["state"] == "pending"
    assert member["agent_instance_id"] is None

    with db() as conn:
        row = conn.execute(
            "SELECT state, agent_instance_id FROM warroom_members "
            "WHERE warroom_member_id = ?",
            (member_id,),
        ).fetchone()
        assert row["state"] == "pending"
        assert row["agent_instance_id"] is None


def test_warroom_status_treats_dead_pane_as_unregistered(monkeypatch):
    """A stale agents row does not keep a dead pane marked active."""
    from server._db import _now, db

    import server.warroom_server as wm

    now = _now()
    agent_id = "project:helioy-tools:backend-engineer:main:2.0"

    with db() as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(
            "INSERT INTO warrooms (warroom_id, tmux_session, tmux_window, cwd, created_at, status) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("dead-pane-wr", "main", "dead-pane-wr", "/tmp", now, "active"),
        )
        member_id = _insert_member(
            conn,
            warroom_id="dead-pane-wr",
            role="helioy-tools:backend-engineer",
            tmux_target="main:2.0",
            pane_id="%20",
            now=now,
            state="active",
            agent_instance_id=agent_id,
        )
        conn.execute(
            "INSERT INTO agents "
            "(agent_id, cwd, tmux_target, pid, registered_at, last_seen) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (agent_id, "/tmp", "main:2.0", 1234, now, now),
        )

    monkeypatch.setattr(gateway, "pane_alive", lambda t: False)

    statuses = wm.warroom_status(name="dead-pane-wr")
    member = statuses[0]["members"][0]
    assert member["warroom_member_id"] == member_id
    assert member["registered"] is False
    assert member["pane_alive"] is False
    assert member["state"] == "pending"
    assert member["agent_instance_id"] is None
    assert member["token_usage"] is None

    with db() as conn:
        row = conn.execute(
            "SELECT state, agent_instance_id FROM warroom_members "
            "WHERE warroom_member_id = ?",
            (member_id,),
        ).fetchone()
        assert row["state"] == "pending"
        assert row["agent_instance_id"] is None


# ── Warroom: warroom_add ─────────────────────────────────────────────────────


def test_warroom_add_to_existing(fake_plugins, monkeypatch):
    """Add an agent to an existing warroom."""
    from server._db import _now, db

    import server.warroom_server as wm

    now = _now()
    # Create warroom with one member
    with db() as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(
            "INSERT INTO warrooms (warroom_id, tmux_session, tmux_window, cwd, created_at, status) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("add-test", "main", "add-test", "/tmp/project", now, "active"),
        )
        _insert_member(
            conn, warroom_id="add-test", role="helioy-tools:backend-engineer",
            tmux_target="main:1.0", pane_id="%10", now=now,
        )

    monkeypatch.setattr(gateway, "spawn_pane", lambda **kw: {
        "agent_type": kw["agent_type"],
        "qualified_name": kw["qualified_name"],
        "tmux_target": "main:1.1",
        "pane_id": "%11",
    })
    _stub_deferred_launch(monkeypatch, {"%10": "main:1.0", "%11": "main:1.1"})

    result = wm.warroom_add(name="add-test", agent="frontend-engineer")
    assert result["warroom_id"] == "add-test"
    assert result["added"]["qualified_name"] == "helioy-tools:frontend-engineer"
    assert result["added"]["desired_role"] == "helioy-tools:frontend-engineer"
    assert "warroom_member_id" in result["added"]
    assert result["added"]["spawn_order"] == 1
    assert result["member_count"] == 2


def test_warroom_add_general_agent(fake_plugins, monkeypatch):
    """Adding 'general' launches a raw pane without catalogue resolution."""
    from server._db import _now, db

    import server.warroom_server as wm

    now = _now()
    with db() as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(
            "INSERT INTO warrooms (warroom_id, tmux_session, tmux_window, cwd, created_at, status) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("add-raw", "main", "add-raw", "/tmp/project", now, "active"),
        )
        _insert_member(
            conn, warroom_id="add-raw", role="helioy-tools:backend-engineer",
            tmux_target="main:1.0", pane_id="%10", now=now,
        )

    monkeypatch.setattr(gateway, "spawn_pane", lambda **kw: {
        "agent_type": kw["agent_type"],
        "qualified_name": kw["qualified_name"],
        "tmux_target": "main:1.1",
        "pane_id": "%11",
    })
    _stub_deferred_launch(monkeypatch, {"%10": "main:1.0", "%11": "main:1.1"})

    result = wm.warroom_add(name="add-raw", agent="general")
    assert "error" not in result
    assert result["added"]["qualified_name"] is None
    assert result["added"]["agent_type"] == "general"
    assert result["added"]["desired_role"] == "general"
    assert result["member_count"] == 2

    with db() as conn:
        row = conn.execute(
            "SELECT desired_role FROM warroom_members "
            "WHERE warroom_id = 'add-raw' AND pane_id = '%11'"
        ).fetchone()
    assert row["desired_role"] == "general"


def test_warroom_add_preserves_stored_layout(fake_plugins, monkeypatch):
    """Add uses the persisted warroom layout instead of forcing tiled."""
    from server._db import _now, db

    import server.warroom_server as wm

    now = _now()
    seen_layouts: list[str] = []

    with db() as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(
            "INSERT INTO warrooms (warroom_id, tmux_session, tmux_window, cwd, layout, created_at, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("layout-add", "main", "layout-add", "/tmp/project", "even-horizontal", now, "active"),
        )
        _insert_member(
            conn, warroom_id="layout-add", role="helioy-tools:backend-engineer",
            tmux_target="main:1.0", pane_id="%10", now=now,
        )

    def mock_spawn_pane(**kw):
        seen_layouts.append(kw["layout"])
        return {
            "agent_type": kw["agent_type"],
            "qualified_name": kw["qualified_name"],
            "tmux_target": "main:1.1",
            "pane_id": "%11",
        }

    monkeypatch.setattr(gateway, "spawn_pane", mock_spawn_pane)
    _stub_deferred_launch(monkeypatch, {"%10": "main:1.0", "%11": "main:1.1"})

    result = wm.warroom_add(name="layout-add", agent="frontend-engineer")
    assert "error" not in result
    assert seen_layouts == ["even-horizontal"]


def test_warroom_add_rekeys_existing_members_before_new_runtime_registers(
    fake_plugins, isolated_bus, monkeypatch
):
    """Existing pane indexes can shift when tmux reflows after a split."""
    from server._db import _now, db

    import server.warroom_server as wm

    now = _now()
    old_agent_id = "project:helioy-tools:backend-engineer:main:2.2"
    new_agent_id = "project:helioy-tools:backend-engineer:main:2.3"
    pids = isolated_bus / "pids"
    pids.mkdir()
    (pids / "4242").write_text(old_agent_id)

    with db() as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(
            "INSERT INTO warrooms "
            "(warroom_id, tmux_session, tmux_window, cwd, layout, created_at, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("rekey-add", "main", "rekey-add", "/tmp/project", "tiled", now, "active"),
        )
        member_id = _insert_member(
            conn,
            warroom_id="rekey-add",
            role="helioy-tools:backend-engineer",
            tmux_target="main:2.2",
            pane_id="%640",
            now=now,
            state="active",
            agent_instance_id=old_agent_id,
        )
        conn.execute(
            "INSERT INTO agents "
            "(agent_id, cwd, tmux_target, pid, session_id, agent_type, runtime, "
            " registered_at, last_seen) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                old_agent_id,
                "/tmp/project",
                "main:2.2",
                4242,
                "session-a",
                "helioy-tools:backend-engineer",
                "claude",
                now,
                now,
            ),
        )

    spawn_calls = []
    launch_calls = []
    titles = []

    def mock_spawn_pane(**kw):
        spawn_calls.append(kw)
        return {
            "agent_type": kw["agent_type"],
            "qualified_name": kw["qualified_name"],
            "tmux_target": "main:2.2",
            "pane_id": "%641",
            "runtime": "claude",
        }

    monkeypatch.setattr(gateway, "spawn_pane", mock_spawn_pane)
    monkeypatch.setattr(gateway, "target_for_pane", lambda pane_id: {
        "%640": "main:2.3",
        "%641": "main:2.2",
    }[pane_id], raising=False)
    monkeypatch.setattr(
        gateway,
        "set_pane_title",
        lambda pane_id, title: titles.append((pane_id, title)),
        raising=False,
    )

    def mock_launch_pane(**kw):
        launch_calls.append(kw)
        return {
            "agent_type": kw["agent_type"],
            "qualified_name": kw["qualified_name"],
            "tmux_target": kw["tmux_target"],
            "pane_id": kw["pane_id"],
            "runtime": "claude",
        }

    monkeypatch.setattr(gateway, "launch_pane", mock_launch_pane, raising=False)

    result = wm.warroom_add(name="rekey-add", agent="frontend-engineer")

    assert "error" not in result
    assert spawn_calls[0]["launch"] is False
    assert launch_calls[0]["pane_id"] == "%641"
    assert launch_calls[0]["tmux_target"] == "main:2.2"
    assert ("%640", new_agent_id) in titles

    with db() as conn:
        existing = conn.execute(
            "SELECT tmux_target, agent_instance_id, state FROM warroom_members "
            "WHERE warroom_member_id = ?",
            (member_id,),
        ).fetchone()
        assert existing["tmux_target"] == "main:2.3"
        assert existing["agent_instance_id"] == new_agent_id
        assert existing["state"] == "active"

        old_row = conn.execute(
            "SELECT * FROM agents WHERE agent_id = ?",
            (old_agent_id,),
        ).fetchone()
        assert old_row is None

        new_row = conn.execute(
            "SELECT agent_id, tmux_target, pid, session_id, agent_type FROM agents "
            "WHERE agent_id = ?",
            (new_agent_id,),
        ).fetchone()
        assert dict(new_row) == {
            "agent_id": new_agent_id,
            "tmux_target": "main:2.3",
            "pid": 4242,
            "session_id": "session-a",
            "agent_type": "helioy-tools:backend-engineer",
        }

    assert (pids / "4242").read_text() == new_agent_id


def test_warroom_add_rekeys_duplicate_role_members_without_losing_registry_rows(
    fake_plugins, isolated_bus, monkeypatch
):
    """Same-role source/destination ids can overlap during tmux reflow."""
    from server._db import _now, db

    import server.warroom_server as wm

    now = _now()
    role = "helioy-tools:backend-engineer"
    pids = isolated_bus / "pids"
    pids.mkdir()
    (pids / "1111").write_text(f"project:{role}:main:1.1")
    (pids / "2222").write_text(f"project:{role}:main:1.2")

    with db() as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(
            "INSERT INTO warrooms "
            "(warroom_id, tmux_session, tmux_window, cwd, layout, created_at, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("dup-rekey", "main", "dup-rekey", "/tmp/project", "tiled", now, "active"),
        )
        first_id = _insert_member(
            conn,
            warroom_id="dup-rekey",
            role=role,
            tmux_target="main:1.1",
            pane_id="%1",
            now=now,
            spawn_order=0,
            state="active",
            agent_instance_id=f"project:{role}:main:1.1",
        )
        second_id = _insert_member(
            conn,
            warroom_id="dup-rekey",
            role=role,
            tmux_target="main:1.2",
            pane_id="%2",
            now=now,
            spawn_order=1,
            state="active",
            agent_instance_id=f"project:{role}:main:1.2",
        )
        for pid, target in [(1111, "main:1.1"), (2222, "main:1.2")]:
            conn.execute(
                "INSERT INTO agents "
                "(agent_id, cwd, tmux_target, pid, session_id, agent_type, runtime, "
                " registered_at, last_seen) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    f"project:{role}:{target}",
                    "/tmp/project",
                    target,
                    pid,
                    f"session-{pid}",
                    role,
                    "claude",
                    now,
                    now,
                ),
            )

    monkeypatch.setattr(gateway, "spawn_pane", lambda **kw: {
        "agent_type": kw["agent_type"],
        "qualified_name": kw["qualified_name"],
        "tmux_target": "main:1.3",
        "pane_id": "%3",
        "runtime": "claude",
    })
    _stub_deferred_launch(
        monkeypatch,
        {
            "%1": "main:1.2",
            "%2": "main:1.3",
            "%3": "main:1.1",
        },
    )

    result = wm.warroom_add(name="dup-rekey", agent="frontend-engineer")

    assert "error" not in result
    with db() as conn:
        members = conn.execute(
            "SELECT warroom_member_id, tmux_target, agent_instance_id, state "
            "FROM warroom_members WHERE warroom_member_id IN (?, ?) "
            "ORDER BY spawn_order",
            (first_id, second_id),
        ).fetchall()
        assert [m["tmux_target"] for m in members] == ["main:1.2", "main:1.3"]
        assert [m["agent_instance_id"] for m in members] == [
            f"project:{role}:main:1.2",
            f"project:{role}:main:1.3",
        ]
        assert [m["state"] for m in members] == ["active", "active"]

        agents = conn.execute(
            "SELECT agent_id, tmux_target, pid, session_id FROM agents "
            "WHERE agent_type = ? ORDER BY tmux_target",
            (role,),
        ).fetchall()
        assert [dict(a) for a in agents] == [
            {
                "agent_id": f"project:{role}:main:1.2",
                "tmux_target": "main:1.2",
                "pid": 1111,
                "session_id": "session-1111",
            },
            {
                "agent_id": f"project:{role}:main:1.3",
                "tmux_target": "main:1.3",
                "pid": 2222,
                "session_id": "session-2222",
            },
        ]

    assert (pids / "1111").read_text() == f"project:{role}:main:1.2"
    assert (pids / "2222").read_text() == f"project:{role}:main:1.3"


def test_warroom_add_migrates_inboxes_when_member_ids_change(
    fake_plugins, isolated_bus, monkeypatch
):
    """Unread and archived mail follow canonical agent id rekeys."""
    from server._db import _now, db

    import server.warroom_server as wm

    now = _now()
    role = "helioy-tools:backend-engineer"
    inbox_root = isolated_bus / "inbox"
    old_agent_id = f"project:{role}:main:1.1"
    new_agent_id = f"project:{role}:main:1.2"
    old_inbox = inbox_root / old_agent_id
    old_archive = old_inbox / "archive"
    old_archive.mkdir(parents=True)
    (old_inbox / "unread.json").write_text('{"content": "unread"}')
    (old_archive / "archived.json").write_text('{"content": "archived"}')

    existing_new_inbox = inbox_root / new_agent_id
    existing_new_inbox.mkdir(parents=True)
    (existing_new_inbox / "existing.json").write_text('{"content": "existing"}')

    with db() as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(
            "INSERT INTO warrooms "
            "(warroom_id, tmux_session, tmux_window, cwd, layout, created_at, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("inbox-rekey", "main", "inbox-rekey", "/tmp/project", "tiled", now, "active"),
        )
        _insert_member(
            conn,
            warroom_id="inbox-rekey",
            role=role,
            tmux_target="main:1.1",
            pane_id="%1",
            now=now,
            state="active",
            agent_instance_id=old_agent_id,
        )
        conn.execute(
            "INSERT INTO agents "
            "(agent_id, cwd, tmux_target, pid, session_id, agent_type, runtime, "
            " registered_at, last_seen) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                old_agent_id,
                "/tmp/project",
                "main:1.1",
                1111,
                "session-1111",
                role,
                "claude",
                now,
                now,
            ),
        )

    monkeypatch.setattr(gateway, "spawn_pane", lambda **kw: {
        "agent_type": kw["agent_type"],
        "qualified_name": kw["qualified_name"],
        "tmux_target": "main:1.1",
        "pane_id": "%2",
        "runtime": "claude",
    })
    _stub_deferred_launch(monkeypatch, {"%1": "main:1.2", "%2": "main:1.1"})

    result = wm.warroom_add(name="inbox-rekey", agent="frontend-engineer")

    assert "error" not in result
    assert not old_inbox.exists()
    assert (existing_new_inbox / "existing.json").exists()
    assert (existing_new_inbox / "unread.json").read_text() == '{"content": "unread"}'
    assert (existing_new_inbox / "archive" / "archived.json").read_text() == (
        '{"content": "archived"}'
    )


def test_warroom_add_migrates_chained_duplicate_role_inboxes(
    fake_plugins, isolated_bus, monkeypatch
):
    """Inbox staging prevents A->B, B->C rekeys from mixing mail."""
    from server._db import _now, db

    import server.warroom_server as wm

    now = _now()
    role = "helioy-tools:backend-engineer"
    first_old = f"project:{role}:main:1.1"
    first_new = f"project:{role}:main:1.2"
    second_old = first_new
    second_new = f"project:{role}:main:1.3"

    for agent_id, label in [(first_old, "first"), (second_old, "second")]:
        inbox = isolated_bus / "inbox" / agent_id
        archive = inbox / "archive"
        archive.mkdir(parents=True)
        (inbox / f"{label}-unread.json").write_text(f'{{"content": "{label}-unread"}}')
        (archive / f"{label}-archived.json").write_text(
            f'{{"content": "{label}-archived"}}'
        )

    with db() as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(
            "INSERT INTO warrooms "
            "(warroom_id, tmux_session, tmux_window, cwd, layout, created_at, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("chain-inbox-rekey", "main", "chain-inbox-rekey", "/tmp/project", "tiled", now, "active"),
        )
        for order, pane_id, target in [(0, "%1", "main:1.1"), (1, "%2", "main:1.2")]:
            _insert_member(
                conn,
                warroom_id="chain-inbox-rekey",
                role=role,
                tmux_target=target,
                pane_id=pane_id,
                now=now,
                spawn_order=order,
                state="active",
                agent_instance_id=f"project:{role}:{target}",
            )
            conn.execute(
                "INSERT INTO agents "
                "(agent_id, cwd, tmux_target, pid, session_id, agent_type, runtime, "
                " registered_at, last_seen) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    f"project:{role}:{target}",
                    "/tmp/project",
                    target,
                    1100 + order,
                    f"session-{order}",
                    role,
                    "claude",
                    now,
                    now,
                ),
            )

    monkeypatch.setattr(gateway, "spawn_pane", lambda **kw: {
        "agent_type": kw["agent_type"],
        "qualified_name": kw["qualified_name"],
        "tmux_target": "main:1.1",
        "pane_id": "%3",
        "runtime": "claude",
    })
    _stub_deferred_launch(
        monkeypatch,
        {
            "%1": "main:1.2",
            "%2": "main:1.3",
            "%3": "main:1.1",
        },
    )

    result = wm.warroom_add(name="chain-inbox-rekey", agent="frontend-engineer")

    assert "error" not in result
    first_final = isolated_bus / "inbox" / first_new
    second_final = isolated_bus / "inbox" / second_new
    assert (first_final / "first-unread.json").exists()
    assert (first_final / "archive" / "first-archived.json").exists()
    assert not (first_final / "second-unread.json").exists()
    assert (second_final / "second-unread.json").exists()
    assert (second_final / "archive" / "second-archived.json").exists()
    assert not (isolated_bus / "inbox" / first_old).exists()


def test_warroom_add_marks_rekeyed_member_pending_without_registry_row(
    fake_plugins, monkeypatch
):
    """Persisted agent_instance_id is not proof of a live registration."""
    from server._db import _now, db

    import server.warroom_server as wm

    now = _now()
    role = "helioy-tools:backend-engineer"
    with db() as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(
            "INSERT INTO warrooms "
            "(warroom_id, tmux_session, tmux_window, cwd, layout, created_at, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("missing-registry", "main", "missing-registry", "/tmp/project", "tiled", now, "active"),
        )
        member_id = _insert_member(
            conn,
            warroom_id="missing-registry",
            role=role,
            tmux_target="main:1.1",
            pane_id="%1",
            now=now,
            state="active",
            agent_instance_id=f"project:{role}:main:1.1",
        )

    monkeypatch.setattr(gateway, "spawn_pane", lambda **kw: {
        "agent_type": kw["agent_type"],
        "qualified_name": kw["qualified_name"],
        "tmux_target": "main:1.1",
        "pane_id": "%2",
        "runtime": "claude",
    })
    _stub_deferred_launch(monkeypatch, {"%1": "main:1.2", "%2": "main:1.1"})

    result = wm.warroom_add(name="missing-registry", agent="frontend-engineer")

    assert "error" not in result
    with db() as conn:
        row = conn.execute(
            "SELECT tmux_target, agent_instance_id, state FROM warroom_members "
            "WHERE warroom_member_id = ?",
            (member_id,),
        ).fetchone()
        assert row["tmux_target"] == "main:1.2"
        assert row["agent_instance_id"] is None
        assert row["state"] == "pending"


def test_warroom_add_allows_duplicate_role(fake_plugins, monkeypatch):
    """Adding the same role twice creates a second distinct member."""
    from server._db import _now, db

    import server.warroom_server as wm

    now = _now()
    with db() as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(
            "INSERT INTO warrooms (warroom_id, tmux_session, tmux_window, cwd, created_at, status) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("dup-test", "main", "dup-test", "/tmp/project", now, "active"),
        )
        first_id = _insert_member(
            conn, warroom_id="dup-test", role="helioy-tools:backend-engineer",
            tmux_target="main:1.0", pane_id="%10", now=now, spawn_order=0,
        )

    pane_counter = [1]

    def mock_spawn_pane(**kw):
        idx = pane_counter[0]
        pane_counter[0] += 1
        return {
            "agent_type": kw["agent_type"],
            "qualified_name": kw["qualified_name"],
            "tmux_target": f"main:1.{idx}",
            "pane_id": f"%1{idx}",
        }

    monkeypatch.setattr(gateway, "spawn_pane", mock_spawn_pane)
    _stub_deferred_launch(monkeypatch, {"%10": "main:1.0", "%11": "main:1.1"})

    result = wm.warroom_add(name="dup-test", agent="backend-engineer")
    assert "error" not in result
    assert result["member_count"] == 2
    assert result["added"]["warroom_member_id"] != first_id
    assert result["added"]["spawn_order"] == 1

    with db() as conn:
        roles = conn.execute(
            "SELECT desired_role, spawn_order FROM warroom_members "
            "WHERE warroom_id = ? ORDER BY spawn_order",
            ("dup-test",),
        ).fetchall()
        assert len(roles) == 2
        assert roles[0]["desired_role"] == roles[1]["desired_role"]
        assert roles[0]["spawn_order"] == 0
        assert roles[1]["spawn_order"] == 1


def test_warroom_add_nonexistent_warroom(fake_plugins):
    """Adding to a non-existent warroom returns an error."""
    import server.warroom_server as wm

    result = wm.warroom_add(name="ghost-room", agent="backend-engineer")
    assert "error" in result
    assert "ghost-room" in result["error"]


def test_warroom_add_unknown_agent_type(fake_plugins, monkeypatch):
    """Adding an unknown agent type returns error with suggestions."""
    from server._db import _now, db

    import server.warroom_server as wm

    now = _now()
    with db() as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(
            "INSERT INTO warrooms (warroom_id, tmux_session, tmux_window, cwd, created_at, status) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("type-test", "main", "type-test", "/tmp", now, "active"),
        )

    result = wm.warroom_add(name="type-test", agent="nonexistent-xyz")
    assert "error" in result
    assert result["error"] == "Unknown agent type"


# ── Warroom: warroom_remove ──────────────────────────────────────────────────


def test_warroom_remove_agent(fake_plugins, monkeypatch):
    """Remove an agent from a warroom by role name."""
    from server._db import _now, db

    import server.warroom_server as wm

    now = _now()
    with db() as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(
            "INSERT INTO warrooms (warroom_id, tmux_session, tmux_window, cwd, created_at, status) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("rm-test", "main", "rm-test", "/tmp", now, "active"),
        )
        be_id = _insert_member(
            conn, warroom_id="rm-test", role="helioy-tools:backend-engineer",
            tmux_target="main:1.0", pane_id="%10", now=now, spawn_order=0,
        )
        _insert_member(
            conn, warroom_id="rm-test", role="helioy-tools:frontend-engineer",
            tmux_target="main:1.1", pane_id="%11", now=now, spawn_order=1,
        )

    result = wm.warroom_remove(name="rm-test", agent="backend-engineer")
    assert result["warroom_id"] == "rm-test"
    assert result["removed"]["desired_role"] == "helioy-tools:backend-engineer"
    assert result["removed"]["warroom_member_id"] == be_id
    assert result["remaining_members"] == 1
    assert result["warroom_killed"] is False


def test_warroom_remove_short_name_scoped_to_target_warroom(
    fake_plugins, fake_codex_instructions, monkeypatch
):
    """Short-name removal ignores collisions outside the target warroom."""
    from server._db import _now, db

    import server.warroom_server as wm

    (fake_codex_instructions / "backend-engineer.md").write_text(
        '---\nname: backend-engineer\ndescription: "Codex backend engineer"\n---\n'
    )

    now = _now()
    with db() as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(
            "INSERT INTO warrooms (warroom_id, tmux_session, tmux_window, cwd, created_at, status) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("mix-rm", "main", "mix-rm", "/tmp", now, "active"),
        )
        codex_id = _insert_member(
            conn,
            warroom_id="mix-rm",
            role="codex:backend-engineer",
            tmux_target="main:1.0",
            pane_id="%10",
            now=now,
            runtime="codex",
        )
        _insert_member(
            conn,
            warroom_id="mix-rm",
            role="helioy-tools:frontend-engineer",
            tmux_target="main:1.1",
            pane_id="%11",
            now=now,
            runtime="claude",
        )

    monkeypatch.setattr(gateway, "kill_pane", lambda pane_id: True)
    monkeypatch.setattr(gateway, "select_layout", lambda *args, **kwargs: True)

    result = wm.warroom_remove(name="mix-rm", agent="backend-engineer")
    assert "error" not in result
    assert result["removed"]["warroom_member_id"] == codex_id
    assert result["removed"]["desired_role"] == "codex:backend-engineer"


def test_warroom_remove_reapplies_stored_layout(fake_plugins, monkeypatch):
    """Remove reflows with the persisted warroom layout."""
    from server._db import _now, db

    import server.warroom_server as wm

    now = _now()
    select_layout_calls: list[tuple[str, str, str]] = []

    with db() as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(
            "INSERT INTO warrooms (warroom_id, tmux_session, tmux_window, cwd, layout, created_at, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("layout-rm", "main", "layout-rm", "/tmp", "main-horizontal", now, "active"),
        )
        _insert_member(
            conn, warroom_id="layout-rm", role="helioy-tools:backend-engineer",
            tmux_target="main:1.0", pane_id="%10", now=now, spawn_order=0,
        )
        _insert_member(
            conn, warroom_id="layout-rm", role="helioy-tools:frontend-engineer",
            tmux_target="main:1.1", pane_id="%11", now=now, spawn_order=1,
        )

    monkeypatch.setattr(gateway, "kill_pane", lambda pane_id: True)
    monkeypatch.setattr(
        gateway,
        "select_layout",
        lambda session, window, layout="tiled": select_layout_calls.append(
            (session, window, layout)
        ) or True,
    )

    result = wm.warroom_remove(name="layout-rm", agent="backend-engineer")
    assert "error" not in result
    assert result["remaining_members"] == 1
    assert select_layout_calls == [("main", "layout-rm", "main-horizontal")]


def test_warroom_remove_short_name_ambiguous_within_same_warroom(
    fake_plugins, fake_codex_instructions, monkeypatch
):
    """Short-name removal stays ambiguous when the target warroom has both matches."""
    from server._db import _now, db

    import server.warroom_server as wm

    (fake_codex_instructions / "backend-engineer.md").write_text(
        '---\nname: backend-engineer\ndescription: "Codex backend engineer"\n---\n'
    )

    now = _now()
    with db() as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(
            "INSERT INTO warrooms (warroom_id, tmux_session, tmux_window, cwd, created_at, status) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("mix-amb", "main", "mix-amb", "/tmp", now, "active"),
        )
        claude_id = _insert_member(
            conn,
            warroom_id="mix-amb",
            role="helioy-tools:backend-engineer",
            tmux_target="main:1.0",
            pane_id="%10",
            now=now,
            runtime="claude",
        )
        codex_id = _insert_member(
            conn,
            warroom_id="mix-amb",
            role="codex:backend-engineer",
            tmux_target="main:1.1",
            pane_id="%11",
            now=now,
            runtime="codex",
        )

    result = wm.warroom_remove(name="mix-amb", agent="backend-engineer")
    assert "error" in result
    assert "ambiguous" in result["error"].lower()
    candidate_ids = {c["warroom_member_id"] for c in result["candidates"]}
    assert candidate_ids == {claude_id, codex_id}


def test_warroom_remove_by_member_id(fake_plugins, monkeypatch):
    """Remove targets the exact stable member_id when provided."""
    from server._db import _now, db

    import server.warroom_server as wm

    now = _now()
    with db() as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(
            "INSERT INTO warrooms (warroom_id, tmux_session, tmux_window, cwd, created_at, status) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("rm-id", "main", "rm-id", "/tmp", now, "active"),
        )
        first_id = _insert_member(
            conn, warroom_id="rm-id", role="helioy-tools:backend-engineer",
            tmux_target="main:1.0", pane_id="%10", now=now, spawn_order=0,
        )
        second_id = _insert_member(
            conn, warroom_id="rm-id", role="helioy-tools:backend-engineer",
            tmux_target="main:1.1", pane_id="%11", now=now, spawn_order=1,
        )

    result = wm.warroom_remove(name="rm-id", member_id=second_id)
    assert "error" not in result
    assert result["removed"]["warroom_member_id"] == second_id
    assert result["remaining_members"] == 1

    with db() as conn:
        rows = conn.execute(
            "SELECT warroom_member_id FROM warroom_members WHERE warroom_id = 'rm-id'"
        ).fetchall()
        assert [r["warroom_member_id"] for r in rows] == [first_id]


def test_warroom_remove_ambiguous_role_returns_candidates(fake_plugins, monkeypatch):
    """Removing by role when multiple members share it returns candidate ids."""
    from server._db import _now, db

    import server.warroom_server as wm

    now = _now()
    with db() as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(
            "INSERT INTO warrooms (warroom_id, tmux_session, tmux_window, cwd, created_at, status) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("amb", "main", "amb", "/tmp", now, "active"),
        )
        a_id = _insert_member(
            conn, warroom_id="amb", role="helioy-tools:backend-engineer",
            tmux_target="main:1.0", pane_id="%10", now=now, spawn_order=0,
        )
        b_id = _insert_member(
            conn, warroom_id="amb", role="helioy-tools:backend-engineer",
            tmux_target="main:1.1", pane_id="%11", now=now, spawn_order=1,
        )

    result = wm.warroom_remove(name="amb", agent="backend-engineer")
    assert "error" in result
    assert "ambiguous" in result["error"].lower()
    candidate_ids = {c["warroom_member_id"] for c in result["candidates"]}
    assert candidate_ids == {a_id, b_id}


def test_warroom_remove_last_agent_kills_warroom(fake_plugins, monkeypatch):
    """Removing the last agent marks warroom as killed."""
    from server._db import _now, db

    import server.warroom_server as wm

    now = _now()
    with db() as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(
            "INSERT INTO warrooms (warroom_id, tmux_session, tmux_window, cwd, created_at, status) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("last-test", "main", "last-test", "/tmp", now, "active"),
        )
        _insert_member(
            conn, warroom_id="last-test", role="helioy-tools:backend-engineer",
            tmux_target="main:1.0", pane_id="%10", now=now,
        )

    result = wm.warroom_remove(name="last-test", agent="backend-engineer")
    assert result["remaining_members"] == 0
    assert result["warroom_killed"] is True

    # Verify DB state
    with db() as conn:
        wr = conn.execute("SELECT status FROM warrooms WHERE warroom_id = 'last-test'").fetchone()
        assert wr["status"] == "killed"


def test_warroom_remove_nonexistent_agent(fake_plugins):
    """Removing an agent not in the warroom returns an error."""
    from server._db import _now, db

    import server.warroom_server as wm

    now = _now()
    with db() as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(
            "INSERT INTO warrooms (warroom_id, tmux_session, tmux_window, cwd, created_at, status) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("no-agent", "main", "no-agent", "/tmp", now, "active"),
        )

    result = wm.warroom_remove(name="no-agent", agent="backend-engineer")
    assert "error" in result


# ── Token tracking: warroom_status includes token_usage ──────────────────────


def test_warroom_status_includes_token_usage(monkeypatch):
    """warroom_status includes token_usage in member dicts (simplified format)."""
    from server._db import _now, db

    import server.warroom_server as wm

    now = _now()
    token_data = '{"tokens": 85000, "updated": "2026-03-17T08:17:51Z"}'

    with db() as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(
            "INSERT INTO warrooms (warroom_id, tmux_session, tmux_window, cwd, created_at, status) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("token-wr", "main", "token-wr", "/tmp", now, "active"),
        )
        _insert_member(
            conn, warroom_id="token-wr", role="helioy-tools:backend-engineer",
            tmux_target="main:3.0", pane_id="%30", now=now,
        )
        conn.execute(
            "INSERT INTO agents "
            "(agent_id, cwd, tmux_target, pid, registered_at, last_seen, token_usage) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("proj:be:main:3.0", "/tmp", "main:3.0", 1234, now, now, token_data),
        )

    monkeypatch.setattr(gateway, "pane_alive", lambda t: True)

    statuses = wm.warroom_status(name="token-wr")
    member = statuses[0]["members"][0]
    assert isinstance(member["token_usage"], dict)
    assert member["token_usage"]["tokens"] == 85000
    assert member["token_usage"]["updated"] == "2026-03-17T08:17:51Z"
