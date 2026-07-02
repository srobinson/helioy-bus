"""Regression tests for teardown-time registration reaping.

Covers the three-layer leak found in the grok-runtime smoke test
(2026-07-02): tmux kill-window delivers SIGHUP which bash does not route
through an EXIT-only trap; kill_warrooms never reaped the agents table;
and the lazy prune backstop keyed liveness on the reusable tmux_target,
so a re-indexed window could make a dead agent look alive forever.

Kept separate from test_shell_hooks.py / test_warroom_members.py to keep
each file under the repo's 700-line threshold.
"""

from __future__ import annotations

import os
import signal
import sqlite3
import subprocess
import time

from tests.test_shell_hooks import REGISTER_HOOK, RUNTIME_LAUNCH, _hook_env

from server import _db
from server._tmux import gateway
from server.services import agent_registry, reconciliation
from server.services import warroom as warroom_service
from server.services.warroom import kill_warrooms


def _agent_ids(bus_dir) -> set[str]:
    conn = sqlite3.connect(bus_dir / "registry.db")
    try:
        return {r[0] for r in conn.execute("SELECT agent_id FROM agents").fetchall()}
    except sqlite3.OperationalError:
        # Registry not bootstrapped yet (the register hook creates it).
        return set()
    finally:
        conn.close()


# ── runtime-launch.sh: SIGHUP unregisters ────────────────────────────────────


def test_runtime_launch_wrapper_unregisters_on_sighup(isolated_bus):
    """tmux kill-window HUPs the pane process group; the wrapper's trap
    must fire on HUP, not only on clean EXIT, or the registration leaks."""
    proc = subprocess.Popen(
        ["bash", *[str(RUNTIME_LAUNCH), "grok", "HELIOY_BUS_GROK_PID", "sleep", "30"]],
        env=_hook_env(isolated_bus),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        deadline = time.time() + 5.0
        while time.time() < deadline and not _agent_ids(isolated_bus):
            time.sleep(0.05)
        assert _agent_ids(isolated_bus), "wrapper never registered the agent"

        # Mirror tmux kill-window: SIGHUP to the whole process group.
        os.killpg(proc.pid, signal.SIGHUP)
        proc.wait(timeout=5)
    finally:
        if proc.poll() is None:
            proc.kill()

    assert _agent_ids(isolated_bus) == set(), "HUP did not unregister the agent"


# ── bus-register.sh: pane_id recorded ────────────────────────────────────────


def test_register_hook_records_pane_id(isolated_bus):
    """The register hook stores $TMUX_PANE as the stable pane id."""
    result = subprocess.run(
        ["bash", str(REGISTER_HOOK)],
        input="{}",
        env=_hook_env(
            isolated_bus,
            TMUX="/tmp/fake-tmux-socket,1234,0",
            TMUX_PANE="%42",
            HELIOY_BUS_TMUX="7:1.1",
        ),
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr

    conn = sqlite3.connect(isolated_bus / "registry.db")
    try:
        row = conn.execute("SELECT tmux_target, pane_id FROM agents").fetchone()
    finally:
        conn.close()
    assert row is not None
    assert row[0] == "7:1.1"
    assert row[1] == "%42"


# ── prune_dead_agents: pane_id-first liveness ────────────────────────────────


def test_prune_prefers_pane_id_over_reused_tmux_target(monkeypatch):
    """The reuse hazard: after a kill, window re-indexing hands a dead
    agent's tmux_target to an unrelated live pane. The stale row must
    still be pruned because its stable pane id is gone."""
    agent_registry.register(
        pwd="/tmp/stale-repo",
        tmux_target="6:3.1",
        agent_id="stale-repo:general:6:3.1",
        session_id="",
        agent_type="general",
        pane_id="%99",
        profile=None,
    )
    # target alive (reused by an unrelated pane), original pane dead
    monkeypatch.setattr(gateway, "pane_alive", lambda target: target == "6:3.1")

    evicted = reconciliation.prune_dead_agents()

    assert evicted == {"stale-repo:general:6:3.1"}


def test_prune_falls_back_to_tmux_target_for_legacy_rows(monkeypatch):
    """Rows without pane_id (pre-migration) keep tmux_target liveness."""
    agent_registry.register(
        pwd="/tmp/legacy-repo",
        tmux_target="6:4.1",
        agent_id="legacy-repo:general:6:4.1",
        session_id="",
        agent_type="general",
        profile=None,
    )
    monkeypatch.setattr(gateway, "pane_alive", lambda target: target == "6:4.1")

    assert reconciliation.prune_dead_agents() == set()


# ── kill_warrooms: agents rows reaped with the warroom ──────────────────────


def test_kill_warrooms_reaps_member_agent_registrations(monkeypatch):
    """warroom_kill must remove members' bus registrations, keyed on the
    stable pane_id / reconciled agent id, never the reusable tmux_target."""
    monkeypatch.setattr(gateway, "kill_window", lambda session, window: True)

    now = _db._now()
    # One member matched by pane_id (re-registered under a new agent_id),
    # one matched by agent_instance_id (legacy row without pane_id).
    agent_registry.register(
        pwd="/tmp/wr-repo",
        tmux_target="6:9.1",
        agent_id="wr-repo:general:6:9.1",
        session_id="",
        agent_type="general",
        pane_id="%71",
        profile=None,
    )
    agent_registry.register(
        pwd="/tmp/wr-repo",
        tmux_target="6:9.2",
        agent_id="wr-repo:backend-engineer:6:9.2",
        session_id="",
        agent_type="backend-engineer",
        profile=None,
    )
    agent_registry.register(
        pwd="/tmp/other-repo",
        tmux_target="6:1.1",
        agent_id="other-repo:general:6:1.1",
        session_id="",
        agent_type="general",
        pane_id="%5",
        profile=None,
    )

    with _db.db() as conn:
        conn.execute(
            "INSERT INTO warrooms (warroom_id, tmux_session, tmux_window, cwd, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("wr-test", "6", "wr-test", "/tmp/wr-repo", now),
        )
        conn.executemany(
            "INSERT INTO warroom_members "
            "(warroom_member_id, warroom_id, desired_runtime, desired_role, state, "
            " agent_instance_id, spawn_order, tmux_target, pane_id, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                ("m1", "wr-test", "grok", "general", "active", "wr-repo:stale-id", 0, "6:9.1", "%71", now, now),
                ("m2", "wr-test", "claude", "backend-engineer", "active", "wr-repo:backend-engineer:6:9.2", 1, "6:9.2", "", now, now),
            ],
        )

        killed = kill_warrooms(conn, "wr-test", False)

    assert killed == ["wr-test"]
    with _db.db() as conn:
        remaining = {r["agent_id"] for r in conn.execute("SELECT agent_id FROM agents")}
        assert remaining == {"other-repo:general:6:1.1"}
        assert conn.execute("SELECT COUNT(*) FROM warroom_members").fetchone()[0] == 0


# ── warroom status: pane_id-first member liveness ────────────────────────────


def test_status_reports_dead_member_despite_reused_tmux_target(monkeypatch):
    """A dead member whose freed tmux_target was reused by an unrelated
    live pane must not be reported pane_alive/registered in status."""
    now = _db._now()
    with _db.db() as conn:
        conn.execute(
            "INSERT INTO warrooms (warroom_id, tmux_session, tmux_window, cwd, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("wr-status", "6", "wr-status", "/tmp/wr-repo", now),
        )
        conn.execute(
            "INSERT INTO warroom_members "
            "(warroom_member_id, warroom_id, desired_runtime, desired_role, state, "
            " agent_instance_id, spawn_order, tmux_target, pane_id, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("m1", "wr-status", "grok", "general", "active", "wr-repo:general:6:2.1", 0, "6:2.1", "%528", now, now),
        )
    # The unrelated pane that inherited the member's freed target.
    agent_registry.register(
        pwd="/tmp/other-repo",
        tmux_target="6:2.1",
        agent_id="other-repo:general:6:2.1",
        session_id="",
        agent_type="general",
        pane_id="%518",
        profile=None,
    )
    # Target alive (reused), the member's actual pane dead.
    monkeypatch.setattr(gateway, "pane_alive", lambda target: target == "6:2.1")

    (wr,) = warroom_service.status(name="wr-status")
    (member,) = wr["members"]

    assert member["pane_alive"] is False
    assert member["registered"] is False
    assert member["agent_instance_id"] is None


# ── sync_pane_addresses: tmux_target follows the pane, not the other way ─────


def _register_synced_agent(agent_id: str, tmux_target: str, pane_id: str) -> None:
    agent_registry.register(
        pwd=f"/tmp/{agent_id.split(':', 1)[0]}",
        tmux_target=tmux_target,
        agent_id=agent_id,
        session_id="",
        agent_type="general",
        pane_id=pane_id,
        profile=None,
    )


def test_sync_refreshes_drifted_agent_and_member_targets(monkeypatch):
    """After a window kill re-indexes survivors, the snapshot maps each
    stable pane_id to its NEW address; sync rewrites the stale rows."""
    _register_synced_agent("repo-a:general:6:3.1", "6:3.1", "%70")
    _register_synced_agent("repo-b:general:6:4.1", "6:4.1", "%80")
    now = _db._now()
    with _db.db() as conn:
        conn.execute(
            "INSERT INTO warrooms (warroom_id, tmux_session, tmux_window, cwd, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("wr-sync", "6", "wr-sync", "/tmp/repo-a", now),
        )
        conn.execute(
            "INSERT INTO warroom_members "
            "(warroom_member_id, warroom_id, desired_runtime, desired_role, state, "
            " agent_instance_id, spawn_order, tmux_target, pane_id, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("ms1", "wr-sync", "grok", "general", "active", "repo-a:general:6:3.1", 0, "6:3.1", "%70", now, now),
        )
    # Window 2 was killed: %70 moved from 6:3.1 to 6:2.1; %80 unchanged.
    monkeypatch.setattr(
        gateway, "pane_snapshot", lambda: {"%70": "6:2.1", "%80": "6:4.1"}
    )

    updated = reconciliation.sync_pane_addresses()

    assert updated == 2  # the agent row and the member row for %70
    with _db.db() as conn:
        agent = conn.execute(
            "SELECT tmux_target FROM agents WHERE agent_id = 'repo-a:general:6:3.1'"
        ).fetchone()
        assert agent["tmux_target"] == "6:2.1"
        untouched = conn.execute(
            "SELECT tmux_target FROM agents WHERE agent_id = 'repo-b:general:6:4.1'"
        ).fetchone()
        assert untouched["tmux_target"] == "6:4.1"
        member = conn.execute(
            "SELECT tmux_target FROM warroom_members WHERE warroom_member_id = 'ms1'"
        ).fetchone()
        assert member["tmux_target"] == "6:2.1"


def test_sync_leaves_dead_and_legacy_rows_to_prune(monkeypatch):
    """A pane_id absent from the snapshot means the pane is gone: sync
    must not touch the row (eviction is prune's job), and rows without
    pane_id are never rewritten."""
    _register_synced_agent("repo-dead:general:6:5.1", "6:5.1", "%90")
    agent_registry.register(
        pwd="/tmp/repo-legacy",
        tmux_target="6:6.1",
        agent_id="repo-legacy:general:6:6.1",
        session_id="",
        agent_type="general",
        profile=None,
    )
    monkeypatch.setattr(gateway, "pane_snapshot", lambda: {"%999": "6:6.1"})

    assert reconciliation.sync_pane_addresses() == 0
    with _db.db() as conn:
        rows = {
            r["agent_id"]: r["tmux_target"]
            for r in conn.execute("SELECT agent_id, tmux_target FROM agents")
        }
    assert rows["repo-dead:general:6:5.1"] == "6:5.1"
    assert rows["repo-legacy:general:6:6.1"] == "6:6.1"


def test_sync_skips_when_tmux_unreachable(monkeypatch):
    _register_synced_agent("repo-x:general:6:7.1", "6:7.1", "%95")
    monkeypatch.setattr(gateway, "pane_snapshot", lambda: None)

    assert reconciliation.sync_pane_addresses() == 0
