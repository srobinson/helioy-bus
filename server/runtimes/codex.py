"""OpenAI Codex runtime adapter.

Owns the Codex CLI invocation and identity conventions. Registered
alongside the Claude adapter without displacing it as default.

Codex has no ``--agent <qualified-name>`` plugin analog, so role-mode and
repo-mode spawn the same base command. Role selection for Codex panes is
expressed through the pane title, not a CLI flag.
"""

from __future__ import annotations

from pathlib import Path

from server.runtimes.base import register


class CodexRuntimeAdapter:
    runtime_id = "codex"
    self_pid_env = "HELIOY_BUS_CODEX_PID"

    _BASE_CMD = "codex --dangerously-bypass-approvals-and-sandbox"

    def build_launch_command(self, *, qualified_name: str | None) -> str:
        # Codex has no persona CLI flag; qualified_name is carried via the
        # pane title only.
        return self._BASE_CMD

    def agents_cache_dir(self) -> Path:
        return Path.home() / ".codex" / "skills"


CODEX = CodexRuntimeAdapter()
register(CODEX)
