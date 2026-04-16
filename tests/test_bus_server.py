"""Tests for helioy-bus messaging and registry tools.

Tests run against a temporary BUS_DIR via the shared isolated_bus fixture
in conftest.py.
"""

from __future__ import annotations

import json
import time
from unittest.mock import patch

import pytest

# Tests patch the tmux gateway singleton and the messaging service's nudge
# throttle directly. Handlers no longer re-export these symbols since the
# ALP-1789 service extraction.
from server._tmux import gateway
from server.services import message as message_svc


# ── Registry ──────────────────────────────────────────────────────────────────


def test_register_agent_basic():
    """Auto-derivation without tmux produces the 2-segment canonical form."""
    import server.bus_server as bm

    result = bm.register_agent(pwd="/tmp/myproject")
    assert result["agent_id"] == "myproject:general"
    assert "registered_at" in result


def test_register_agent_with_tmux_target_uses_compound_id():
    """Auto-derivation with tmux produces the full 4-segment canonical form
    including agent_type — never {basename}:{tmux_target} alone."""
    import server.bus_server as bm

    result = bm.register_agent(pwd="/tmp/myproject", tmux_target="7:1.2")
    assert result["agent_id"] == "myproject:general:7:1.2"


def test_register_agent_with_tmux_target_and_type():
    """Auto-derivation honors agent_type in the canonical form."""
    import server.bus_server as bm

    result = bm.register_agent(
        pwd="/tmp/myproject",
        tmux_target="7:1.2",
        agent_type="backend-engineer",
    )
    assert result["agent_id"] == "myproject:backend-engineer:7:1.2"


def test_register_agent_explicit_id():
    import server.bus_server as bm

    result = bm.register_agent(pwd="/tmp/myproject", agent_id="custom-id")
    assert result["agent_id"] == "custom-id"


def test_register_creates_inbox(tmp_path):
    import server._db as _db_mod
    import server.bus_server as bm

    bm.register_agent(pwd="/tmp/myproject")
    inbox = _db_mod.INBOX_DIR / "myproject:general"
    assert inbox.is_dir()


def test_register_evicts_prior_pane_occupant():
    """A tmux pane hosts at most one Claude process. Registering a new
    agent at a tmux_target already claimed by another row must evict
    the prior occupant, since pane ownership is exclusive by definition."""
    import server.bus_server as bm

    # First occupant of pane 8:1.1
    bm.register_agent(
        pwd="/tmp/helioy-plugins",
        tmux_target="8:1.1",
        agent_id="helioy-plugins:general:8:1.1",
    )
    # Second occupant of the same pane (different CWD → different agent_id)
    bm.register_agent(
        pwd="/tmp/helioy",
        tmux_target="8:1.1",
        agent_id="helioy:general:8:1.1",
    )

    agents = bm.list_agents()
    ids = [a["agent_id"] for a in agents]
    assert ids == ["helioy:general:8:1.1"], (
        f"Expected only the new occupant, got {ids}"
    )


def test_register_does_not_evict_different_pane():
    """Agents on different tmux_targets coexist — eviction is pane-scoped."""
    import server.bus_server as bm

    bm.register_agent(pwd="/tmp/a", tmux_target="8:1.1", agent_id="a:8:1.1")
    bm.register_agent(pwd="/tmp/b", tmux_target="8:1.2", agent_id="b:8:1.2")

    ids = sorted(a["agent_id"] for a in bm.list_agents())
    assert ids == ["a:8:1.1", "b:8:1.2"]


def test_register_does_not_evict_empty_tmux_target():
    """Non-tmux agents (tmux_target='') must be allowed to coexist —
    the empty string is not a pane identity and eviction must not fire."""
    import server.bus_server as bm

    bm.register_agent(pwd="/tmp/a", agent_id="a")
    bm.register_agent(pwd="/tmp/b", agent_id="b")

    ids = sorted(agent["agent_id"] for agent in bm.list_agents())
    assert ids == ["a", "b"]


def test_list_agents_empty():
    import server.bus_server as bm

    agents = bm.list_agents()
    assert agents == []


def test_list_agents_after_register():
    import server.bus_server as bm

    bm.register_agent(pwd="/tmp/alpha")
    bm.register_agent(pwd="/tmp/beta")

    agents = bm.list_agents()
    ids = [a["agent_id"] for a in agents]
    assert "alpha:general" in ids
    assert "beta:general" in ids


def test_list_agents_filter_by_session():
    """tmux_filter='session' returns only agents in that session."""
    import server.bus_server as bm

    bm.register_agent(pwd="/tmp/a", tmux_target="work:0.0", agent_id="a:work:0.0")
    bm.register_agent(pwd="/tmp/b", tmux_target="work:1.0", agent_id="b:work:1.0")
    bm.register_agent(pwd="/tmp/c", tmux_target="other:0.0", agent_id="c:other:0.0")

    with patch.object(gateway, "pane_alive", return_value=True):
        agents = bm.list_agents(tmux_filter="work")

    ids = [a["agent_id"] for a in agents]
    assert "a:work:0.0" in ids
    assert "b:work:1.0" in ids
    assert "c:other:0.0" not in ids


def test_list_agents_filter_by_session_and_window():
    """tmux_filter='session:window' narrows to a specific window."""
    import server.bus_server as bm

    bm.register_agent(pwd="/tmp/a", tmux_target="work:0.0", agent_id="a:work:0.0")
    bm.register_agent(pwd="/tmp/b", tmux_target="work:0.1", agent_id="b:work:0.1")
    bm.register_agent(pwd="/tmp/c", tmux_target="work:1.0", agent_id="c:work:1.0")

    with patch.object(gateway, "pane_alive", return_value=True):
        agents = bm.list_agents(tmux_filter="work:0")

    ids = [a["agent_id"] for a in agents]
    assert "a:work:0.0" in ids
    assert "b:work:0.1" in ids
    assert "c:work:1.0" not in ids


def test_unregister_agent():
    import server.bus_server as bm

    bm.register_agent(pwd="/tmp/myproject")
    result = bm.unregister_agent("myproject:general")
    assert result["unregistered"] == "myproject:general"
    assert bm.list_agents() == []


def test_heartbeat_updates_last_seen():
    import server.bus_server as bm

    bm.register_agent(pwd="/tmp/myproject")
    time.sleep(0.01)
    result = bm.heartbeat("myproject:general")
    assert result["agent_id"] == "myproject:general"
    assert "last_seen" in result


# ── Mailbox ───────────────────────────────────────────────────────────────────


def test_send_message_to_registered_agent(set_sender):
    import server.bus_server as bm

    bm.register_agent(pwd="/tmp/beta")
    set_sender("alpha")
    result = bm.send_message(to="beta:general", content="hello", nudge=False)
    assert result["delivered"] is True
    assert "beta:general" in result["recipients"]


def test_send_message_writes_json_file(set_sender):
    import server._db as _db_mod
    import server.bus_server as bm

    bm.register_agent(pwd="/tmp/target")
    set_sender("src")
    bm.send_message(to="target:general", content="payload", nudge=False)

    inbox = _db_mod.INBOX_DIR / "target:general"
    files = list(inbox.glob("*.json"))
    assert len(files) == 1
    data = json.loads(files[0].read_text())
    assert data["content"] == "payload"
    assert data["from"] == "src"


def test_send_message_atomic_write(set_sender):
    """No .tmp files left after a successful send."""
    import server._db as _db_mod
    import server.bus_server as bm

    bm.register_agent(pwd="/tmp/target")
    set_sender("y")
    bm.send_message(to="target:general", content="x", nudge=False)

    inbox = _db_mod.INBOX_DIR / "target:general"
    tmp_files = list(inbox.glob("*.tmp"))
    assert tmp_files == []


def test_send_message_recipient_not_found(set_sender):
    import server.bus_server as bm

    set_sender("me")
    result = bm.send_message(to="ghost", content="hi", nudge=False)
    assert result["delivered"] is False
    assert "error" in result


def test_send_message_broadcast(set_sender):
    import server.bus_server as bm

    bm.register_agent(pwd="/tmp/a")
    bm.register_agent(pwd="/tmp/b")
    set_sender("sender")
    result = bm.send_message(to="*", content="hello all", nudge=False)
    assert set(result["recipients"]) == {"a:general", "b:general"}


def test_get_messages_returns_and_archives(set_sender):
    import server._db as _db_mod
    import server.bus_server as bm

    bm.register_agent(pwd="/tmp/reader")
    set_sender("w")
    bm.send_message(to="reader:general", content="msg1", nudge=False)
    bm.send_message(to="reader:general", content="msg2", nudge=False)

    messages = bm.get_messages("reader:general")
    assert len(messages) == 2
    assert messages[0]["content"] == "msg1"
    assert messages[1]["content"] == "msg2"

    # Messages archived
    inbox = _db_mod.INBOX_DIR / "reader:general"
    assert list(inbox.glob("*.json")) == []
    assert len(list((inbox / "archive").glob("*.json"))) == 2


def test_get_messages_empty_inbox():
    import server.bus_server as bm

    bm.register_agent(pwd="/tmp/empty")
    messages = bm.get_messages("empty:general")
    assert messages == []


def test_get_messages_idempotent(set_sender):
    import server.bus_server as bm

    bm.register_agent(pwd="/tmp/reader")
    set_sender("w")
    bm.send_message(to="reader:general", content="once", nudge=False)

    first = bm.get_messages("reader:general")
    assert len(first) == 1

    second = bm.get_messages("reader:general")
    assert second == []


# ── Liveness pruning ──────────────────────────────────────────────────────────


def test_list_agents_prunes_dead_tmux_targets():
    import server.bus_server as bm

    bm.register_agent(pwd="/tmp/alive", tmux_target="main:0.0", agent_id="alive:main:0.0")
    bm.register_agent(pwd="/tmp/dead", tmux_target="main:0.1", agent_id="dead:main:0.1")

    with patch.object(
        gateway, "pane_alive", side_effect=lambda t: t == "main:0.0"
    ):
        agents = bm.list_agents()

    ids = [a["agent_id"] for a in agents]
    assert "alive:main:0.0" in ids
    assert "dead:main:0.1" not in ids


def test_list_agents_keeps_agents_without_tmux_target():
    import server.bus_server as bm

    bm.register_agent(pwd="/tmp/notmux", agent_id="notmux")
    agents = bm.list_agents()
    assert len(agents) == 1
    assert agents[0]["agent_id"] == "notmux"


# ── Nudge behavior ───────────────────────────────────────────────────────────


def test_send_message_nudge_skips_dead_pane(set_sender):
    import server.bus_server as bm

    bm.register_agent(pwd="/tmp/dead", tmux_target="main:9.9", agent_id="dead:main:9.9")
    set_sender("nudger")
    with patch.object(gateway, "pane_alive", return_value=False):
        result = bm.send_message(to="dead:main:9.9", content="wake up")
    assert result["delivered"] is True
    assert result["nudged"] is False


def test_send_message_nudge_suppressed_with_flag(set_sender):
    import server.bus_server as bm

    bm.register_agent(pwd="/tmp/quiet", tmux_target="main:0.0", agent_id="quiet:main:0.0")
    set_sender("sender")
    with (
        patch.object(gateway, "pane_alive", return_value=True),
        patch.object(gateway, "nudge", return_value=True) as mock_nudge,
    ):
        result = bm.send_message(to="quiet:main:0.0", content="shh", nudge=False)
    assert result["nudged"] is False
    mock_nudge.assert_not_called()


def test_send_message_nudges_live_pane(set_sender):
    import server.bus_server as bm

    bm.register_agent(pwd="/tmp/live", tmux_target="main:0.0", agent_id="live:main:0.0")
    set_sender("sender")
    with (
        patch.object(gateway, "pane_alive", return_value=True),
        patch.object(gateway, "nudge", return_value=True),
        patch.object(message_svc, "_nudge_allowed", return_value=True),
    ):
        result = bm.send_message(to="live:main:0.0", content="ping")
    assert result["nudged"] is True


# ── Agent types & identity ────────────────────────────────────────────────────


def test_register_agent_stores_agent_type():
    import server.bus_server as bm

    bm.register_agent(pwd="/tmp/be", agent_id="be", agent_type="backend-engineer")
    agents = bm.list_agents()
    agent = next(a for a in agents if a["agent_id"] == "be")
    assert agent["agent_type"] == "backend-engineer"


def test_register_agent_default_type_is_general():
    import server.bus_server as bm

    bm.register_agent(pwd="/tmp/gen", agent_id="gen")
    agents = bm.list_agents()
    agent = next(a for a in agents if a["agent_id"] == "gen")
    assert agent["agent_type"] == "general"


# ── Canonical identity contract (ALP-1786) ────────────────────────────────────


def test_canonical_agent_id_shape_without_tmux():
    """canonical_agent_id() returns {repo}:{agent_type} when no tmux_target."""
    from server._identity import canonical_agent_id

    assert canonical_agent_id("/tmp/myproject") == "myproject:general"
    assert (
        canonical_agent_id("/tmp/myproject", "backend-engineer")
        == "myproject:backend-engineer"
    )


def test_canonical_agent_id_shape_with_tmux():
    """canonical_agent_id() returns the 4-segment form when tmux_target is set."""
    from server._identity import canonical_agent_id

    assert (
        canonical_agent_id("/tmp/myproject", "general", "7:1.2")
        == "myproject:general:7:1.2"
    )
    assert (
        canonical_agent_id("/tmp/myproject", "backend-engineer", "7:1.2")
        == "myproject:backend-engineer:7:1.2"
    )


def test_canonical_agent_id_normalizes_trailing_slash():
    from server._identity import canonical_agent_id

    assert canonical_agent_id("/tmp/myproject/") == "myproject:general"


def test_canonical_agent_id_empty_cwd_becomes_unknown():
    from server._identity import canonical_agent_id

    assert canonical_agent_id("") == "unknown:general"
    assert canonical_agent_id("/") == "unknown:general"


def test_canonical_agent_id_empty_type_defaults_to_general():
    """Empty agent_type defaults to 'general' — never produce bare repo."""
    from server._identity import canonical_agent_id

    assert canonical_agent_id("/tmp/myproject", "") == "myproject:general"


def test_register_agent_auto_derivation_matches_canonical_helper():
    """register_agent() auto-derivation must produce the exact output of
    canonical_agent_id() for the same inputs. This is the core invariant
    that prevents MCP-registered rows from diverging from hook-registered
    rows for the same live process."""
    import server.bus_server as bm
    from server._identity import canonical_agent_id

    for pwd, agent_type, tmux_target in [
        ("/tmp/proj", "general", ""),
        ("/tmp/proj", "backend-engineer", ""),
        ("/tmp/proj", "general", "7:1.2"),
        ("/tmp/proj", "backend-engineer", "main:0.0"),
        ("/tmp/proj", "voltagent-lang:rust-engineer", "9:3.4"),
    ]:
        expected = canonical_agent_id(pwd, agent_type, tmux_target)
        result = bm.register_agent(
            pwd=pwd, agent_type=agent_type, tmux_target=tmux_target
        )
        assert result["agent_id"] == expected, (
            f"auto-derive for ({pwd}, {agent_type}, {tmux_target}) "
            f"produced {result['agent_id']!r}, expected {expected!r}"
        )
        bm.unregister_agent(expected)


def test_self_agent_id_reads_pid_file(monkeypatch):
    """Fast path: _self_agent_id() reads the PID file written by the
    SessionStart hook and returns its contents verbatim. This is how
    hook-registered identity propagates to MCP self-resolution."""
    import server._db as _db_mod
    from server._identity import _self_agent_id

    agent_id = "myproject:backend-engineer:7:1.2"
    pids_dir = _db_mod.BUS_DIR / "pids"
    pids_dir.mkdir(parents=True, exist_ok=True)
    pid = "99999"
    (pids_dir / pid).write_text(agent_id)
    monkeypatch.setenv("HELIOY_BUS_CLAUDE_PID", pid)

    assert _self_agent_id() == agent_id


def test_self_agent_id_last_resort_uses_canonical_form(monkeypatch, tmp_path):
    """Last resort: with no PID file and no shell resolver available,
    _self_agent_id() still produces the canonical 2-segment shape rather
    than the legacy bare-basename form."""
    import server._identity as _id_mod

    cwd = tmp_path / "fakeproj"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    monkeypatch.delenv("HELIOY_BUS_CLAUDE_PID", raising=False)
    monkeypatch.delenv("HELIOY_BUS_TMUX", raising=False)
    monkeypatch.delenv("HELIOY_AGENT_TYPE", raising=False)
    monkeypatch.delenv("HELIOY_BUS_AGENT_TYPE", raising=False)
    # Disable the shell resolver so we exercise the Python fallback
    monkeypatch.setattr(
        _id_mod, "_RESOLVE_IDENTITY_SH", tmp_path / "does-not-exist.sh"
    )

    assert _id_mod._self_agent_id() == "fakeproj:general"


def test_self_agent_id_last_resort_honors_agent_type_env(monkeypatch, tmp_path):
    """Last resort honors HELIOY_AGENT_TYPE (hook-exported) so a late-booted
    MCP server still agrees with the hook-written identity."""
    import server._identity as _id_mod

    cwd = tmp_path / "myrepo"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    monkeypatch.delenv("HELIOY_BUS_CLAUDE_PID", raising=False)
    monkeypatch.delenv("HELIOY_BUS_TMUX", raising=False)
    monkeypatch.setenv("HELIOY_AGENT_TYPE", "backend-engineer")
    monkeypatch.setattr(
        _id_mod, "_RESOLVE_IDENTITY_SH", tmp_path / "does-not-exist.sh"
    )

    assert _id_mod._self_agent_id() == "myrepo:backend-engineer"


# ── Role-based messaging ─────────────────────────────────────────────────────


def test_send_message_role_addressing_delivers_to_matching_agents(set_sender):
    import server.bus_server as bm

    bm.register_agent(pwd="/tmp/be1", agent_id="be1", agent_type="backend-engineer")
    bm.register_agent(pwd="/tmp/be2", agent_id="be2", agent_type="backend-engineer")
    bm.register_agent(pwd="/tmp/fe", agent_id="fe", agent_type="frontend-engineer")

    set_sender("orch")
    result = bm.send_message(
        to="role:backend-engineer", content="build it", nudge=False
    )
    assert result["delivered"] is True
    assert set(result["recipients"]) == {"be1", "be2"}


def test_send_message_role_addressing_excludes_sender(set_sender):
    import server.bus_server as bm

    bm.register_agent(pwd="/tmp/be", agent_id="be", agent_type="backend-engineer")
    set_sender("be")
    result = bm.send_message(
        to="role:backend-engineer", content="self", nudge=False
    )
    assert result["delivered"] is False


def test_send_message_role_not_found_returns_error(set_sender):
    import server.bus_server as bm

    bm.register_agent(pwd="/tmp/x", agent_id="x", agent_type="general")
    set_sender("y")
    result = bm.send_message(
        to="role:nonexistent", content="x", nudge=False
    )
    assert result["delivered"] is False
    assert "error" in result


def test_send_message_role_creates_inbox_files(set_sender):
    import server._db as _db_mod
    import server.bus_server as bm

    bm.register_agent(pwd="/tmp/be", agent_id="be", agent_type="backend-engineer")
    bm.register_agent(pwd="/tmp/fe", agent_id="fe", agent_type="frontend-engineer")

    set_sender("orch")
    bm.send_message(
        to="role:backend-engineer", content="task", nudge=False
    )

    be_inbox = _db_mod.INBOX_DIR / "be"
    fe_inbox = _db_mod.INBOX_DIR / "fe"
    assert len(list(be_inbox.glob("*.json"))) == 1
    # frontend-engineer should not receive the message
    fe_files = list(fe_inbox.glob("*.json")) if fe_inbox.exists() else []
    assert fe_files == []


# ── Lifecycle integration tests ──────────────────────────────────────────────


def test_repo_mode_lifecycle(set_sender):
    """Simulates the repo-mode lifecycle: register multiple general agents,
    send between them, receive, then unregister."""
    import server.bus_server as bm

    # Register two "warroom" agents with pane-title-style IDs
    bm.register_agent(
        pwd="/tmp/fmm", agent_id="fmm:general:7:2.1", agent_type="general", tmux_target="7:2.1"
    )
    bm.register_agent(
        pwd="/tmp/helioy-bus",
        agent_id="helioy-bus:general:7:2.2",
        agent_type="general",
        tmux_target="7:2.2",
    )

    with patch.object(gateway, "pane_alive", return_value=True):
        agents = bm.list_agents()
    assert len(agents) == 2
    assert all(a["agent_type"] == "general" for a in agents)

    # Direct message between two repo-mode agents
    set_sender("fmm:general:7:2.1")
    result = bm.send_message(
        to="helioy-bus:general:7:2.2",
        content="hi from fmm",
        nudge=False,
    )
    assert result["delivered"] is True
    assert "helioy-bus:general:7:2.2" in result["recipients"]

    messages = bm.get_messages("helioy-bus:general:7:2.2")
    assert len(messages) == 1
    assert messages[0]["content"] == "hi from fmm"

    # Unregister
    bm.unregister_agent("fmm:general:7:2.1")
    bm.unregister_agent("helioy-bus:general:7:2.2")
    with patch.object(gateway, "pane_alive", return_value=True):
        assert bm.list_agents() == []


def test_role_mode_lifecycle(set_sender):
    """Simulates the crew/role-mode lifecycle: register specialist agents,
    send via role addressing, receive, verify isolation."""
    import server.bus_server as bm

    # Register agents as warroom.sh would: pane-title-style IDs with agent_type
    bm.register_agent(
        pwd="/tmp/helioy-bus",
        agent_id="helioy-bus:backend-engineer:7:3.1",
        agent_type="backend-engineer",
        tmux_target="7:3.1",
    )
    bm.register_agent(
        pwd="/tmp/helioy-bus",
        agent_id="helioy-bus:frontend-engineer:7:3.2",
        agent_type="frontend-engineer",
        tmux_target="7:3.2",
    )
    bm.register_agent(
        pwd="/tmp/helioy-bus",
        agent_id="helioy-bus:general:7:3.3",
        agent_type="general",
        tmux_target="7:3.3",
    )

    # Role-based send: only backend-engineer should receive
    set_sender("helioy-bus:general:7:3.3")
    result = bm.send_message(
        to="role:backend-engineer",
        content="implement the auth endpoint",
        nudge=False,
    )
    assert result["delivered"] is True
    assert result["recipients"] == ["helioy-bus:backend-engineer:7:3.1"]

    # frontend-engineer and general should not have received anything
    msgs_fe = bm.get_messages("helioy-bus:frontend-engineer:7:3.2")
    msgs_gen = bm.get_messages("helioy-bus:general:7:3.3")
    assert msgs_fe == []
    assert msgs_gen == []

    # Backend agent reads its message
    msgs_be = bm.get_messages("helioy-bus:backend-engineer:7:3.1")
    assert len(msgs_be) == 1
    assert msgs_be[0]["content"] == "implement the auth endpoint"


def test_coexistence_of_both_modes(set_sender):
    """Both warroom (general) and crew (specialist) agents coexist and can
    message each other directly or via broadcast."""
    import server.bus_server as bm

    # Warroom agents (repo-mode, general)
    bm.register_agent(
        pwd="/tmp/fmm", agent_id="fmm:general:7:2.1", agent_type="general", tmux_target="7:2.1"
    )
    bm.register_agent(
        pwd="/tmp/helioy-bus",
        agent_id="helioy-bus:general:7:2.2",
        agent_type="general",
        tmux_target="7:2.2",
    )

    # Crew agents (role-mode, specialist)
    bm.register_agent(
        pwd="/tmp/helioy-bus",
        agent_id="helioy-bus:backend-engineer:7:3.1",
        agent_type="backend-engineer",
        tmux_target="7:3.1",
    )
    bm.register_agent(
        pwd="/tmp/helioy-bus",
        agent_id="helioy-bus:frontend-engineer:7:3.2",
        agent_type="frontend-engineer",
        tmux_target="7:3.2",
    )

    with patch.object(gateway, "pane_alive", return_value=True):
        agents = bm.list_agents()
    assert len(agents) == 4

    # Broadcast from a non-registered sender reaches all four agents
    set_sender("external-orchestrator")
    result = bm.send_message(to="*", content="standup time", nudge=False)
    assert set(result["recipients"]) == {
        "fmm:general:7:2.1",
        "helioy-bus:general:7:2.2",
        "helioy-bus:backend-engineer:7:3.1",
        "helioy-bus:frontend-engineer:7:3.2",
    }

    # Role-based send from a registered agent reaches only matching specialists
    set_sender("fmm:general:7:2.1")
    result2 = bm.send_message(
        to="role:backend-engineer",
        content="deploy the API",
        nudge=False,
    )
    assert result2["recipients"] == ["helioy-bus:backend-engineer:7:3.1"]


def test_adhoc_session_fallback_identity(set_sender):
    """An ad-hoc claude session (no warroom, no tmux) registers with the
    canonical 2-segment identity: {repo}:{agent_type}. Bare-basename is a
    legacy shape rejected by the canonical contract (ALP-1786)."""
    import server.bus_server as bm

    # Simulate ad-hoc registration as bus-register.sh would derive it
    bm.register_agent(pwd="/tmp/myproject", agent_type="general")

    agents = bm.list_agents()
    assert len(agents) == 1
    agent = agents[0]
    assert agent["agent_id"] == "myproject:general"
    assert agent["agent_type"] == "general"

    # Can receive direct messages
    set_sender("other")
    result = bm.send_message(
        to="myproject:general", content="hello from peer", nudge=False
    )
    assert result["delivered"] is True

    bm.unregister_agent("myproject:general")
    assert bm.list_agents() == []


def test_profile_migration_from_shell_hook_created_db(tmp_path, monkeypatch):
    """register_agent succeeds even when the DB was first created by bus-register.sh
    (which doesn't include the profile column).

    Reproduces the missing-migration bug: if the shell hook runs first and
    creates the agents table without the profile column, the MCP server must
    add it via ALTER TABLE before attempting the INSERT OR REPLACE.
    """
    import sqlite3

    import server._db as _db_mod
    import server.bus_server as bm

    bus_dir = tmp_path / "bus_legacy"
    bus_dir.mkdir()
    monkeypatch.setattr(_db_mod, "BUS_DIR", bus_dir)
    monkeypatch.setattr(_db_mod, "REGISTRY_DB", bus_dir / "registry.db")
    monkeypatch.setattr(_db_mod, "INBOX_DIR", bus_dir / "inbox")

    # Simulate the DB as bus-register.sh creates it: no profile column.
    conn = sqlite3.connect(str(bus_dir / "registry.db"))
    conn.executescript("""
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS agents (
            agent_id      TEXT PRIMARY KEY,
            cwd           TEXT NOT NULL,
            tmux_target   TEXT NOT NULL DEFAULT '',
            pid           INTEGER,
            session_id    TEXT NOT NULL DEFAULT '',
            agent_type    TEXT NOT NULL DEFAULT 'general',
            registered_at TEXT NOT NULL,
            last_seen     TEXT NOT NULL
        );
    """)
    conn.commit()
    conn.close()

    # MCP register_agent must succeed -- migration adds profile column.
    result = bm.register_agent(
        pwd="/tmp/myproject",
        agent_id="myproject",
        agent_type="general",
        profile={"owns": ["myproject"]},
    )
    assert result["agent_id"] == "myproject"

    agents = bm.list_agents()
    assert len(agents) == 1
    assert agents[0].get("profile") == {"owns": ["myproject"]}


# ── Token tracking: schema migration ─────────────────────────────────────────


def test_token_usage_column_exists():
    """token_usage column exists in agents table after db init."""
    from server._db import db

    with db() as conn:
        # Insert a row and verify token_usage defaults to '{}'
        conn.execute(
            "INSERT INTO agents (agent_id, cwd, registered_at, last_seen) VALUES (?, ?, ?, ?)",
            ("test-token", "/tmp", "2026-01-01", "2026-01-01"),
        )
        row = conn.execute(
            "SELECT token_usage FROM agents WHERE agent_id = 'test-token'"
        ).fetchone()
        assert row["token_usage"] == "{}"


def test_token_usage_migration_from_older_schema(tmp_path, monkeypatch):
    """token_usage column is added via migration to older databases."""
    import sqlite3

    import server._db as _db_mod

    bus_dir = tmp_path / "bus_old"
    bus_dir.mkdir()
    monkeypatch.setattr(_db_mod, "BUS_DIR", bus_dir)
    monkeypatch.setattr(_db_mod, "REGISTRY_DB", bus_dir / "registry.db")

    # Create DB without token_usage column
    conn = sqlite3.connect(str(bus_dir / "registry.db"))
    conn.executescript("""
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS agents (
            agent_id      TEXT PRIMARY KEY,
            cwd           TEXT NOT NULL,
            tmux_target   TEXT NOT NULL DEFAULT '',
            pid           INTEGER,
            session_id    TEXT NOT NULL DEFAULT '',
            agent_type    TEXT NOT NULL DEFAULT 'general',
            profile       TEXT,
            registered_at TEXT NOT NULL,
            last_seen     TEXT NOT NULL
        );
    """)
    conn.execute(
        "INSERT INTO agents (agent_id, cwd, registered_at, last_seen) VALUES (?, ?, ?, ?)",
        ("old-agent", "/tmp", "2026-01-01", "2026-01-01"),
    )
    conn.commit()
    conn.close()

    # Open with _init_db migration
    with _db_mod.db() as conn:
        row = conn.execute(
            "SELECT token_usage FROM agents WHERE agent_id = 'old-agent'"
        ).fetchone()
        assert row["token_usage"] == "{}"


# ── Token tracking: list_agents includes token_usage ─────────────────────────


def test_list_agents_includes_token_usage():
    """list_agents returns parsed token_usage JSON (simplified format)."""
    from server._db import db

    import server.bus_server as bm

    token_data = '{"tokens": 81751, "updated": "2026-03-17T08:17:51Z"}'
    bm.register_agent(pwd="/tmp/tracked", agent_id="tracked")
    with db() as conn:
        conn.execute(
            "UPDATE agents SET token_usage = ? WHERE agent_id = 'tracked'",
            (token_data,),
        )

    agents = bm.list_agents()
    agent = next(a for a in agents if a["agent_id"] == "tracked")
    assert isinstance(agent["token_usage"], dict)
    assert agent["token_usage"]["tokens"] == 81751
    assert agent["token_usage"]["updated"] == "2026-03-17T08:17:51Z"


def test_list_agents_empty_token_usage():
    """list_agents handles empty token_usage gracefully."""
    import server.bus_server as bm

    bm.register_agent(pwd="/tmp/fresh", agent_id="fresh")
    agents = bm.list_agents()
    agent = next(a for a in agents if a["agent_id"] == "fresh")
    # Empty '{}' string should remain as-is (not parsed into dict since it's falsy-ish)
    assert agent["token_usage"] in ("{}", {})


# ── Token tracking: whoami includes token_usage ──────────────────────────────


def test_whoami_includes_token_usage(monkeypatch):
    """whoami returns parsed token_usage (simplified format)."""
    from server._db import db

    import server.bus_server as bm

    token_data = '{"tokens": 20000, "updated": "2026-03-17T08:17:51Z"}'
    bm.register_agent(pwd="/tmp/myproj", agent_id="myproj")
    with db() as conn:
        conn.execute(
            "UPDATE agents SET token_usage = ? WHERE agent_id = 'myproj'",
            (token_data,),
        )

    monkeypatch.setattr(bm, "_self_agent_id", lambda: "myproj")
    result = bm.whoami()
    assert isinstance(result["token_usage"], dict)
    assert result["token_usage"]["tokens"] == 20000


# ── DB hygiene: init-once ─────────────────────────────────────────────────────


def test_init_db_idempotent():
    """Calling db() multiple times does not error; schema is bootstrapped once."""
    from server._db import db

    # First call creates the schema
    with db() as conn:
        conn.execute("SELECT 1 FROM agents")

    # Second call must work without re-running _init_db on the fresh temp db
    with db() as conn:
        conn.execute("SELECT 1 FROM agents")
        conn.execute("SELECT 1 FROM warrooms")
