"""Claude Code runtime adapter.

Owns every Claude Code specific assumption that used to live inline in the
core bus: the ``claude`` CLI invocation, the ``HELIOY_BUS_CLAUDE_PID`` env
var, and the ``~/.claude/plugins/cache`` lookup path.
"""

from __future__ import annotations

from pathlib import Path

from server.runtimes.base import register


class ClaudeRuntimeAdapter:
    runtime_id = "claude"
    self_pid_env = "HELIOY_BUS_CLAUDE_PID"

    _BASE_CMD = "claude --dangerously-skip-permissions --model opus --effort max"

    def build_launch_command(self, *, qualified_name: str | None) -> str:
        if qualified_name is None:
            return self._BASE_CMD
        return f"{self._BASE_CMD} --agent {qualified_name}"

    def agents_cache_dir(self) -> Path:
        return Path.home() / ".claude" / "plugins" / "cache"


CLAUDE = ClaudeRuntimeAdapter()
register(CLAUDE, default=True)
