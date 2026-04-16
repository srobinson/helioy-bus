"""OpenAI Codex runtime adapter.

Owns the Codex CLI invocation and identity conventions. Registered
alongside the Claude adapter without displacing it as default.

Codex has no ``--agent <qualified-name>`` plugin analog, so role-mode and
repo-mode spawn the same base command. Role selection for Codex panes is
expressed through the pane title, not a CLI flag. Codex also has no
SessionStart/SessionEnd hook mechanism, so the adapter launches codex
through ``plugin/hooks/codex-launch.sh`` which bootstraps registration
on the bus and tears it down on exit.
"""

from __future__ import annotations

from pathlib import Path

from server.runtimes.base import register

_LAUNCH_WRAPPER = (
    Path(__file__).parent.parent.parent / "plugin" / "hooks" / "codex-launch.sh"
)


class CodexRuntimeAdapter:
    runtime_id = "codex"
    self_pid_env = "HELIOY_BUS_CODEX_PID"

    def build_launch_command(self, *, qualified_name: str | None) -> str:
        # Codex has no persona CLI flag; qualified_name is carried via the
        # pane title only. The wrapper internally invokes codex with the
        # bypass flag and drives register/unregister around it.
        return str(_LAUNCH_WRAPPER)

    def agents_cache_dir(self) -> Path:
        return Path.home() / ".codex" / "skills"


CODEX = CodexRuntimeAdapter()
register(CODEX)
