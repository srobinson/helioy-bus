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

from server.runtimes._frontmatter import _parse_frontmatter
from server.runtimes.base import register

_LAUNCH_WRAPPER = (
    Path(__file__).parent.parent.parent / "plugin" / "hooks" / "codex-launch.sh"
)


class CodexRuntimeAdapter:
    runtime_id = "codex"
    self_pid_env = "HELIOY_BUS_CODEX_PID"

    # Codex has no persona CLI flag; skills are activated per-turn via
    # slash commands, not bound to the session at launch. A "specialist
    # role" in a warroom would persist state the runtime never enacts,
    # so we reject such spawns at the service layer. Codex is usable in
    # general/repo mode via warroom_spawn_repos.
    supports_specialist_roles = False

    # Codex does not layer a plugin/version directory under its skills
    # cache, so all discovered skills share a single namespace.
    _NAMESPACE = "codex"

    def build_launch_command(self, *, qualified_name: str | None) -> str:
        # Codex has no persona CLI flag; qualified_name is carried via the
        # pane title only. The wrapper internally invokes codex with the
        # bypass flag and drives register/unregister around it.
        return str(_LAUNCH_WRAPPER)

    def agents_cache_dir(self) -> Path:
        return Path.home() / ".codex" / "skills"

    def discover_agent_types(self) -> list[dict]:
        """Walk ``~/.codex/skills/{skill}/SKILL.md`` and return skill definitions.

        Codex skills live one directory deep under the cache root; each
        folder's ``SKILL.md`` carries the ``name``/``description``
        frontmatter. All skills share the ``codex`` namespace until Codex
        grows a plugin/version layer.
        """
        cache = self.agents_cache_dir()
        if not cache.is_dir():
            return []

        result: list[dict] = []
        for skill_dir in sorted(p for p in cache.iterdir() if p.is_dir()):
            skill_md = skill_dir / "SKILL.md"
            if not skill_md.is_file():
                continue
            fm = _parse_frontmatter(skill_md)
            if not fm or "name" not in fm:
                continue

            short_name = fm["name"]
            qualified = f"{self._NAMESPACE}:{short_name}"

            summary = fm.get("description", "")
            if len(summary) > 200:
                summary = summary[:197] + "..."

            result.append({
                "qualified_name": qualified,
                "name": short_name,
                "namespace": self._NAMESPACE,
                "summary": summary,
                "model": fm.get("model", ""),
                "runtime": self.runtime_id,
            })

        result.sort(key=lambda e: e["qualified_name"])
        return result


CODEX = CodexRuntimeAdapter()
register(CODEX)
