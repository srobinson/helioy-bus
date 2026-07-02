"""xAI Grok CLI runtime adapter.

Grok mirrors Claude Code's CLI surface (``--agent <qualified-name>``,
``-m/--model``, plugin discovery from the Claude config dir) but does not
execute plugin hooks (validated live: no SessionStart registration fired
across a full turn), so registration rides the shared launch wrapper like
Codex.

One adapter class, one registered instance per selectable model. No
runtime takes a model parameter at spawn time (Claude pins claude-fable-5
the same way), so each grok model is its own runtime id rather than
plumbing model selection through the warroom:

  * ``grok``       -> grok-build (the CLI default model)
  * ``grok-fast``  -> grok-composer-2.5-fast
"""

from __future__ import annotations

import shlex
from pathlib import Path

from server.runtimes.base import LifecycleIntegration, register
from server.runtimes.claude import CLAUDE

_PLUGIN_HOOKS = Path(__file__).resolve().parent.parent.parent / "plugin" / "hooks"
_LAUNCH_WRAPPER = _PLUGIN_HOOKS / "runtime-launch.sh"


class GrokRuntimeAdapter:
    """One registered instance per selectable model; see module docstring."""

    # Both grok runtime ids share one env name: the wrapper keys the PID
    # file on its own $$ per pane, so the value is unique per pane and
    # identity resolution only needs the name to find it.
    self_pid_env = "HELIOY_BUS_GROK_PID"

    # Grok launches with --always-approve and acted on an incoming prompt
    # without human intermediation in live validation; no authorization
    # preamble needed.
    message_suffix = ""

    # Grok binds a specialist persona via ``--agent <qualified-name>``,
    # reading the same plugin catalogue as Claude.
    supports_specialist_roles = True

    def __init__(self, runtime_id: str, model: str) -> None:
        self.runtime_id = runtime_id
        self.model = model

    def build_launch_command(self, *, qualified_name: str | None) -> str:
        # -m is honored (grok 0.2.81, verified via self-ID + transcript
        # model_id), but two TUI quirks matter to callers: the footer and
        # /model picker display ~/.grok/config.toml [models].default until
        # the FIRST response, so a fresh pane's footer is not evidence of
        # its model; and the first turn persists this flag's model as the
        # user's new global default in config.toml (a cross-pane side
        # effect of spawning grok panes).
        wrapper = shlex.quote(str(_LAUNCH_WRAPPER))
        cmd = (
            f"{wrapper} {self.runtime_id} {self.self_pid_env} grok --always-approve -m {self.model}"
        )
        if qualified_name is None:
            return cmd
        # Grok clobbers the pane title shortly after start (like Codex),
        # so pin the qualified role in the pane environment for fallback
        # identity resolution. resolve-identity.sh consumes it in its
        # fallback branch.
        role_env = f"HELIOY_BUS_AGENT_TYPE={shlex.quote(qualified_name)}"
        return f"{role_env} {cmd} --agent {shlex.quote(qualified_name)}"

    def agents_cache_dir(self) -> Path:
        # Grok reads the Claude plugin catalogue directly (`grok inspect`
        # lists the same plugin agents), so the cache dir is Claude's.
        return CLAUDE.agents_cache_dir()

    def discover_agent_types(self) -> list[dict]:
        return [{**agent, "runtime": self.runtime_id} for agent in CLAUDE.discover_agent_types()]

    def lifecycle_integration(self) -> LifecycleIntegration:
        # Grok discovers Claude plugin hooks but does not execute them, so
        # the shared launch wrapper drives register/unregister exactly as
        # it does for Codex.
        return LifecycleIntegration(
            startup_script=_LAUNCH_WRAPPER,
            shutdown_script=_PLUGIN_HOOKS / "bus-unregister.sh",
            usage_capture_script=None,
            registration_kind="wrapper",
        )

    def capture_usage(self, pane_content: str) -> dict | None:
        # Grok's status bar shows no token counter (nothing like Claude's
        # ``<n> tokens`` pattern), so there is nothing to sample.
        return None


GROK = GrokRuntimeAdapter("grok", "grok-build")
GROK_FAST = GrokRuntimeAdapter("grok-fast", "grok-composer-2.5-fast")
register(GROK)
register(GROK_FAST)
