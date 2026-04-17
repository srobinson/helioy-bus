"""Contract tests for the lifecycle + usage-capture adapter surface.

Kept separate from ``test_runtime_adapters.py`` to keep each file under
the repo's 700-line threshold. This module pins:

* every adapter returns a ``LifecycleIntegration`` whose scripts actually
  exist on disk (catches ``plugin/hooks/`` rename drift)
* Codex explicitly declares no usage-capture script and ``capture_usage``
  returns ``None``; the two signals agree by contract
* Claude's ``capture_usage`` extractor stays in lockstep with the grep
  pipeline in ``plugin/hooks/token-capture.sh``
"""

from __future__ import annotations

import subprocess
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from server.runtimes import LifecycleIntegration, RuntimeAdapter
from server.runtimes.claude import CLAUDE
from server.runtimes.codex import CODEX


# ── LifecycleIntegration dataclass ───────────────────────────────────────────


def test_lifecycle_integration_is_frozen():
    """frozen=True prevents callers from mutating an adapter's declaration."""
    integration = LifecycleIntegration(
        startup_script=Path("/a"),
        shutdown_script=Path("/b"),
        usage_capture_script=None,
        registration_kind="hook",
    )
    with pytest.raises(FrozenInstanceError):
        integration.registration_kind = "wrapper"  # type: ignore[misc]


# ── Claude adapter: lifecycle integration ────────────────────────────────────


def test_claude_lifecycle_integration_declares_hook_kind():
    assert CLAUDE.lifecycle_integration().registration_kind == "hook"


def test_claude_startup_script_is_bus_register_sh():
    startup = CLAUDE.lifecycle_integration().startup_script
    assert startup.name == "bus-register.sh"
    assert startup.exists(), f"startup script missing: {startup}"


def test_claude_shutdown_script_is_bus_unregister_sh():
    shutdown = CLAUDE.lifecycle_integration().shutdown_script
    assert shutdown.name == "bus-unregister.sh"
    assert shutdown.exists(), f"shutdown script missing: {shutdown}"


def test_claude_declares_token_capture_as_usage_script():
    usage = CLAUDE.lifecycle_integration().usage_capture_script
    assert usage is not None
    assert usage.name == "token-capture.sh"
    assert usage.exists(), f"usage script missing: {usage}"


# ── Codex adapter: lifecycle integration ─────────────────────────────────────


def test_codex_lifecycle_integration_declares_wrapper_kind():
    """Codex has no plugin hook system; registration is wrapper-driven."""
    assert CODEX.lifecycle_integration().registration_kind == "wrapper"


def test_codex_startup_script_is_launch_wrapper():
    """Codex bootstraps registration from codex-launch.sh, not a hook."""
    startup = CODEX.lifecycle_integration().startup_script
    assert startup.name == "codex-launch.sh"
    assert startup.exists(), f"launch wrapper missing: {startup}"


def test_codex_shutdown_reuses_shared_unregister_script():
    """Wrapper's EXIT trap invokes the runtime-neutral unregister script."""
    shutdown = CODEX.lifecycle_integration().shutdown_script
    assert shutdown.name == "bus-unregister.sh"
    assert shutdown.exists()


def test_codex_declares_no_usage_capture_script():
    """Codex has no tmux-visible token counter; usage capture is None, not
    a borrowed Claude script.
    """
    assert CODEX.lifecycle_integration().usage_capture_script is None


# ── Cross-adapter invariants ─────────────────────────────────────────────────


def test_adapters_own_distinct_startup_paths():
    """Each adapter declares its own startup path; no accidental aliasing."""
    assert (
        CLAUDE.lifecycle_integration().startup_script
        != CODEX.lifecycle_integration().startup_script
    )


def test_usage_capture_script_agrees_with_capture_usage_method():
    """Invariant: ``usage_capture_script is None`` iff ``capture_usage``
    has no mechanism to sample. The shell presence and the Python
    implementation must not disagree about whether the runtime samples.
    """
    for adapter in (CLAUDE, CODEX):
        has_script = adapter.lifecycle_integration().usage_capture_script is not None
        sample = "⠐ 42 tokens · esc\n"
        method_returns_value = adapter.capture_usage(sample) is not None
        assert has_script == method_returns_value, (
            f"{adapter.runtime_id}: script={has_script} but "
            f"capture_usage returned {adapter.capture_usage(sample)!r}"
        )


# ── capture_usage: Claude ────────────────────────────────────────────────────


def test_claude_capture_usage_extracts_token_count():
    sample = (
        "─ Claude Code ─\n"
        "some unrelated text\n"
        "⠐ thinking... 1234 tokens · esc to interrupt\n"
    )
    assert CLAUDE.capture_usage(sample) == {"tokens": 1234}


def test_claude_capture_usage_prefers_most_recent_match():
    """When scrollback contains multiple samples, the latest one wins.

    Matches ``tail -1`` behavior in token-capture.sh.
    """
    sample = "200 tokens\nsomething else\n500 tokens\n"
    assert CLAUDE.capture_usage(sample) == {"tokens": 500}


def test_claude_capture_usage_returns_none_when_no_pattern():
    assert CLAUDE.capture_usage("no pattern here") is None


def test_claude_capture_usage_returns_none_on_empty_input():
    assert CLAUDE.capture_usage("") is None


def test_claude_capture_usage_matches_shell_hook_pipeline():
    """Pin the Python extractor to ``grep -oE '[0-9]+ tokens' | tail -1``.

    If either path drifts (hook edited without adapter, or vice versa),
    this test fires before production divergence ships.
    """
    sample = "⠐ 42 tokens\n⠒ 7 tokens · esc\n"
    result = subprocess.run(
        ["grep", "-oE", "[0-9]+ tokens"],
        input=sample,
        capture_output=True,
        text=True,
        check=False,
    )
    shell_last = result.stdout.strip().splitlines()[-1]
    shell_tokens = int(shell_last.split()[0])

    assert CLAUDE.capture_usage(sample) == {"tokens": shell_tokens}


# ── capture_usage: Codex ─────────────────────────────────────────────────────


def test_codex_capture_usage_always_returns_none():
    """Codex has no capture mechanism: method returns None regardless of
    input. When Codex grows a token-visible surface, both this test and
    the adapter body change together.
    """
    assert CODEX.capture_usage("⠐ 42 tokens\n") is None
    assert CODEX.capture_usage("") is None


# ── Extended protocol shape ──────────────────────────────────────────────────


def test_claude_satisfies_extended_runtime_adapter_protocol():
    assert isinstance(CLAUDE, RuntimeAdapter)
    assert callable(CLAUDE.lifecycle_integration)
    assert callable(CLAUDE.capture_usage)


def test_codex_satisfies_extended_runtime_adapter_protocol():
    assert isinstance(CODEX, RuntimeAdapter)
    assert callable(CODEX.lifecycle_integration)
    assert callable(CODEX.capture_usage)
