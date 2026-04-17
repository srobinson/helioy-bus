"""Claude Code runtime adapter.

Owns every Claude Code specific assumption that used to live inline in the
core bus: the ``claude`` CLI invocation, the ``HELIOY_BUS_CLAUDE_PID`` env
var, and the ``~/.claude/plugins/cache`` lookup path.
"""

from __future__ import annotations

import re
from pathlib import Path

from server.runtimes._frontmatter import _parse_frontmatter
from server.runtimes.base import LifecycleIntegration, register

_PLUGIN_HOOKS = Path(__file__).resolve().parent.parent.parent / "plugin" / "hooks"

# Mirrors the ``grep -oE '[0-9]+ tokens'`` pipeline in
# plugin/hooks/token-capture.sh. Kept module-level so contract tests can
# assert the two extraction paths stay in sync.
_CLAUDE_TOKENS_RE = re.compile(r"(\d+) tokens")


class ClaudeRuntimeAdapter:
    runtime_id = "claude"
    self_pid_env = "HELIOY_BUS_CLAUDE_PID"

    # Claude enacts a specialist persona via ``--agent <qualified-name>``,
    # so the session really is bound to that role from launch onward.
    supports_specialist_roles = True

    _BASE_CMD = "claude --dangerously-skip-permissions --model opus --effort max"

    def build_launch_command(self, *, qualified_name: str | None) -> str:
        if qualified_name is None:
            return self._BASE_CMD
        return f"{self._BASE_CMD} --agent {qualified_name}"

    def agents_cache_dir(self) -> Path:
        return Path.home() / ".claude" / "plugins" / "cache"

    def discover_agent_types(self) -> list[dict]:
        """Walk the Claude plugin cache: ``cache/{org}/{plugin}/{version}/agents/*.md``.

        Namespace is the plugin directory (second path component). Multiple
        installed versions of the same plugin are deduplicated by keeping
        the newest mtime.
        """
        cache = self.agents_cache_dir()
        if not cache.is_dir():
            return []

        agents: dict[str, dict] = {}
        for md_path in cache.rglob("agents/*.md"):
            fm = _parse_frontmatter(md_path)
            if not fm or "name" not in fm:
                continue
            rel = md_path.relative_to(cache)
            parts = rel.parts
            # Pattern: {org}/{plugin}/{version}/agents/{file}.md
            if len(parts) < 4:
                continue
            namespace = parts[1]
            short_name = fm["name"]
            qualified = f"{namespace}:{short_name}"
            mtime = md_path.stat().st_mtime

            if qualified in agents and agents[qualified].get("_mtime", 0) >= mtime:
                continue

            summary = fm.get("description", "")
            if len(summary) > 200:
                summary = summary[:197] + "..."

            agents[qualified] = {
                "qualified_name": qualified,
                "name": short_name,
                "namespace": namespace,
                "summary": summary,
                "model": fm.get("model", ""),
                "runtime": self.runtime_id,
                "_mtime": mtime,
            }

        return [
            {k: v for k, v in entry.items() if not k.startswith("_")}
            for entry in sorted(agents.values(), key=lambda e: e["qualified_name"])
        ]

    def lifecycle_integration(self) -> LifecycleIntegration:
        # Claude Code's plugin system invokes SessionStart/SessionEnd
        # directly against these scripts; no wrapper needed.
        return LifecycleIntegration(
            startup_script=_PLUGIN_HOOKS / "bus-register.sh",
            shutdown_script=_PLUGIN_HOOKS / "bus-unregister.sh",
            usage_capture_script=_PLUGIN_HOOKS / "token-capture.sh",
            registration_kind="hook",
        )

    def capture_usage(self, pane_content: str) -> dict | None:
        # Claude's status bar renders ``<n> tokens``; token-capture.sh
        # greps the same pattern off the tail of the pane. This method
        # is the canonical extractor and is pinned to the shell pipeline
        # by test_adapter_lifecycle.py.
        matches = _CLAUDE_TOKENS_RE.findall(pane_content)
        if not matches:
            return None
        return {"tokens": int(matches[-1])}


CLAUDE = ClaudeRuntimeAdapter()
register(CLAUDE, default=True)
