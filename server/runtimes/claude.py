"""Claude Code runtime adapter.

Owns every Claude Code specific assumption that used to live inline in the
core bus: the ``claude`` CLI invocation, the ``HELIOY_BUS_CLAUDE_PID`` env
var, and the ``~/.claude/plugins/cache`` lookup path.

One adapter class, one registered instance per selectable model (same
pattern as grok): no runtime takes a model parameter at spawn time, so
each Claude model is its own runtime id:

  * ``claude``       -> claude-fable-5[1m] (the default runtime)
  * ``claude-opus``  -> claude-opus-5[1m]

The ``[1m]`` suffix pins the 1M context tier. Without it the CLI falls
back to whatever ``~/.claude/settings.json`` currently defaults to, which
any ``/model`` press inside a pane silently rewrites; warroom panes must
not inherit that drift.
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
    """One registered instance per selectable model; see module docstring."""

    # Both claude runtime ids share one env name: the SessionStart hook
    # keys the PID file per pane, so identity resolution only needs the
    # name to find it.
    self_pid_env = "HELIOY_BUS_CLAUDE_PID"

    # Claude acts on bus messages without human intermediation; no
    # authorization preamble is needed.
    message_suffix = ""

    # Claude enacts a specialist persona via ``--agent <qualified-name>``,
    # so the session really is bound to that role from launch onward.
    supports_specialist_roles = True

    def __init__(self, runtime_id: str, model: str) -> None:
        self.runtime_id = runtime_id
        self.model = model

    def build_launch_command(self, *, qualified_name: str | None) -> str:
        # HELIOY_RUNTIME pins the exact runtime id for the SessionStart
        # hook; without it resolve_runtime would record every
        # claude-family pane as plain "claude".
        cmd = (
            f"HELIOY_RUNTIME={self.runtime_id} claude "
            f'--dangerously-skip-permissions --model "{self.model}" --effort high'
        )
        if qualified_name is None:
            return cmd
        return f"{cmd} --agent {qualified_name}"

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


CLAUDE = ClaudeRuntimeAdapter("claude", "claude-fable-5[1m]")
CLAUDE_OPUS = ClaudeRuntimeAdapter("claude-opus", "claude-opus-5[1m]")
register(CLAUDE, default=True)
register(CLAUDE_OPUS)
