"""Contract tests for the Grok runtime adapters (``grok``, ``grok-fast``).

Kept separate from ``test_runtime_adapters.py`` to keep each file under
the repo's 700-line threshold. This module pins:

* one registered adapter instance per selectable grok model, with Claude
  remaining the default runtime
* launch commands ride the shared ``runtime-launch.sh`` wrapper (grok
  discovers Claude plugin hooks but does not execute them)
* discovery delegates to the Claude plugin catalogue with the runtime
  field remapped
"""

from __future__ import annotations

from pathlib import Path

import pytest

from server.runtimes import RuntimeAdapter, default_adapter, for_id
from server.runtimes.claude import CLAUDE
from server.runtimes.grok import GROK, GROK_FAST

_ADAPTERS = pytest.mark.parametrize("adapter", [GROK, GROK_FAST], ids=lambda a: a.runtime_id)


# ── Identity metadata ────────────────────────────────────────────────────────


def test_grok_runtime_ids_map_one_instance_per_model():
    assert GROK.runtime_id == "grok"
    assert GROK.model == "grok-build"
    assert GROK_FAST.runtime_id == "grok-fast"
    assert GROK_FAST.model == "grok-composer-2.5-fast"


def test_grok_registration_does_not_evict_claude_as_default():
    assert for_id("grok") is GROK
    assert for_id("grok-fast") is GROK_FAST
    assert default_adapter() is CLAUDE


@_ADAPTERS
def test_grok_adapter_satisfies_runtime_adapter_protocol(adapter):
    assert isinstance(adapter, RuntimeAdapter)


@_ADAPTERS
def test_grok_self_pid_env_is_shared_across_models(adapter):
    """Both grok runtime ids share one env name: the wrapper keys the PID
    file on its own $$ per pane, so identity resolution only needs the
    name to find it."""
    assert adapter.self_pid_env == "HELIOY_BUS_GROK_PID"


@_ADAPTERS
def test_grok_message_suffix_is_empty(adapter):
    """Grok runs with --always-approve and acts on incoming prompts without
    human intermediation; no authorization preamble."""
    assert adapter.message_suffix == ""


@_ADAPTERS
def test_grok_supports_specialist_roles(adapter):
    """Grok binds a persona at launch via --agent, like Claude."""
    assert adapter.supports_specialist_roles is True


# ── Launch command ───────────────────────────────────────────────────────────


@_ADAPTERS
def test_grok_launch_command_repo_mode_rides_shared_wrapper(adapter):
    cmd = adapter.build_launch_command(qualified_name=None)
    parts = cmd.split()
    wrapper = Path(parts[0])
    assert wrapper.name == "runtime-launch.sh"
    assert wrapper.exists(), f"wrapper missing: {wrapper}"
    assert parts[1:3] == [adapter.runtime_id, "HELIOY_BUS_GROK_PID"]
    assert parts[3:5] == ["grok", "--always-approve"]
    assert f"-m {adapter.model}" in cmd
    assert "--agent" not in cmd
    assert "HELIOY_BUS_AGENT_TYPE" not in cmd


def test_grok_launch_command_role_mode_pins_agent_flag_and_env():
    """Grok clobbers the pane title after start (like Codex), so specialist
    launches carry the qualified role in the pane environment for fallback
    identity resolution alongside the --agent binding."""
    cmd = GROK.build_launch_command(qualified_name="helioy-tools:backend-engineer")
    assert cmd.startswith("HELIOY_BUS_AGENT_TYPE=helioy-tools:backend-engineer ")
    assert "--agent helioy-tools:backend-engineer" in cmd
    assert "-m grok-build" in cmd


# ── Discovery ────────────────────────────────────────────────────────────────


def test_grok_agents_cache_dir_is_claude_plugin_cache():
    """Grok reads the Claude plugin catalogue directly."""
    assert GROK.agents_cache_dir() == CLAUDE.agents_cache_dir()


@_ADAPTERS
def test_grok_discovery_delegates_to_claude_with_own_runtime(adapter, monkeypatch):
    monkeypatch.setattr(
        CLAUDE,
        "discover_agent_types",
        lambda: [
            {
                "qualified_name": "helioy-tools:backend-engineer",
                "name": "backend-engineer",
                "namespace": "helioy-tools",
                "summary": "Backend work",
                "model": "",
                "runtime": "claude",
            }
        ],
    )
    discovered = adapter.discover_agent_types()
    assert len(discovered) == 1
    assert discovered[0]["qualified_name"] == "helioy-tools:backend-engineer"
    assert discovered[0]["runtime"] == adapter.runtime_id


# ── Lifecycle + usage capture ────────────────────────────────────────────────


@_ADAPTERS
def test_grok_lifecycle_integration_declares_wrapper_kind(adapter):
    """Grok discovers Claude plugin hooks but does not execute them
    (validated live), so registration is wrapper-driven like Codex."""
    integration = adapter.lifecycle_integration()
    assert integration.registration_kind == "wrapper"
    assert integration.startup_script.name == "runtime-launch.sh"
    assert integration.startup_script.exists()
    assert integration.shutdown_script.name == "bus-unregister.sh"
    assert integration.shutdown_script.exists()
    assert integration.usage_capture_script is None


@_ADAPTERS
def test_grok_capture_usage_always_returns_none(adapter):
    """Grok's status bar shows no token counter; the None agrees with the
    absent usage_capture_script by contract."""
    assert adapter.capture_usage("⠐ 42 tokens · esc\n") is None
