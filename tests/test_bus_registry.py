"""Registry tests: register/unregister/heartbeat/list, pane eviction, tmux liveness filtering."""

from __future__ import annotations

import time
from unittest.mock import patch

from server._tmux import gateway


# ── Registry ──────────────────────────────────────────────────────────────────


def test_register_agent_basic():
    """Auto-derivation without tmux produces the 2-segment canonical form."""
    import server.bus_server as bm

    result = bm.register_agent(pwd="/tmp/myproject")
    assert result["agent_id"] == "myproject:general"
    assert "registered_at" in result


def test_register_agent_with_tmux_target_uses_compound_id():
    """Auto-derivation with tmux produces the full 4-segment canonical form
    including agent_type, never {basename}:{tmux_target} alone."""
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
    """Agents on different tmux_targets coexist; eviction is pane-scoped."""
    import server.bus_server as bm

    bm.register_agent(pwd="/tmp/a", tmux_target="8:1.1", agent_id="a:8:1.1")
    bm.register_agent(pwd="/tmp/b", tmux_target="8:1.2", agent_id="b:8:1.2")

    ids = sorted(a["agent_id"] for a in bm.list_agents())
    assert ids == ["a:8:1.1", "b:8:1.2"]


def test_register_does_not_evict_empty_tmux_target():
    """Non-tmux agents (tmux_target='') must be allowed to coexist;
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


# ── Agent types ──────────────────────────────────────────────────────────────


def test_register_agent_stores_agent_type():
    import server.bus_server as bm

    bm.register_agent(pwd="/tmp/be", agent_id="be", agent_type="backend-engineer")
    agents = bm.list_agents()
    agent = next(a for a in agents if a["agent_id"] == "be")
    assert agent["agent_type"] == "backend-engineer"


def test_register_agent_stores_runtime(monkeypatch):
    import server.bus_server as bm

    monkeypatch.setenv("HELIOY_RUNTIME", "codex")
    bm.register_agent(pwd="/tmp/codex", agent_id="codex")
    agents = bm.list_agents()
    agent = next(a for a in agents if a["agent_id"] == "codex")
    assert agent["runtime"] == "codex"


def test_register_agent_explicit_runtime_overrides_env(monkeypatch):
    import server.bus_server as bm

    monkeypatch.setenv("HELIOY_RUNTIME", "claude")
    bm.register_agent(pwd="/tmp/codex", agent_id="codex-explicit", runtime="codex")
    agents = bm.list_agents()
    agent = next(a for a in agents if a["agent_id"] == "codex-explicit")
    assert agent["runtime"] == "codex"


def test_register_agent_default_type_is_general():
    import server.bus_server as bm

    bm.register_agent(pwd="/tmp/gen", agent_id="gen")
    agents = bm.list_agents()
    agent = next(a for a in agents if a["agent_id"] == "gen")
    assert agent["agent_type"] == "general"
