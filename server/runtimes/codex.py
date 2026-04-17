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
from server.runtimes.base import LifecycleIntegration, register

_PLUGIN_HOOKS = Path(__file__).resolve().parent.parent.parent / "plugin" / "hooks"
_LAUNCH_WRAPPER = _PLUGIN_HOOKS / "codex-launch.sh"


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

    def shared_skills_dir(self) -> Path:
        return Path.home() / ".agents" / "skills"

    def skill_roots(self) -> list[Path]:
        """Return every skill tree that participates in Codex discovery.

        Codex uses its own ``~/.codex/skills`` tree, including nested
        ``.system/*/SKILL.md`` entries, and this environment also exposes
        shared skills under ``~/.agents/skills``.
        """
        return [self.agents_cache_dir(), self.shared_skills_dir()]

    def discover_agent_types(self) -> list[dict]:
        """Walk Codex skill trees and return discovered skill definitions.

        Supported layouts:

        * ``~/.codex/skills/{skill}/SKILL.md``
        * ``~/.codex/skills/.system/{skill}/SKILL.md``
        * ``~/.agents/skills/{skill}/SKILL.md``

        All discovered skills share the ``codex`` namespace until Codex
        grows a plugin/version layer. When the same skill name appears in
        multiple trees, the first root wins so local Codex skills shadow
        shared ones.
        """
        result_by_name: dict[str, dict] = {}
        for root in self.skill_roots():
            if not root.is_dir():
                continue
            manifests = sorted(root.glob("*/SKILL.md"))
            manifests.extend(sorted(root.glob(".system/*/SKILL.md")))
            for skill_md in manifests:
                fm = _parse_frontmatter(skill_md)
                if not fm or "name" not in fm:
                    continue

                short_name = fm["name"]
                qualified = f"{self._NAMESPACE}:{short_name}"
                if qualified in result_by_name:
                    continue

                summary = fm.get("description", "")
                if len(summary) > 200:
                    summary = summary[:197] + "..."

                result_by_name[qualified] = {
                    "qualified_name": qualified,
                    "name": short_name,
                    "namespace": self._NAMESPACE,
                    "summary": summary,
                    "model": fm.get("model", ""),
                    "runtime": self.runtime_id,
                }

        return [result_by_name[name] for name in sorted(result_by_name)]

    def lifecycle_integration(self) -> LifecycleIntegration:
        # Codex has no SessionStart/SessionEnd hook mechanism, so the
        # adapter-owned launch wrapper runs bus-register.sh up front and
        # installs EXIT/INT/TERM traps around bus-unregister.sh. The
        # wrapper is what build_launch_command returns.
        return LifecycleIntegration(
            startup_script=_LAUNCH_WRAPPER,
            shutdown_script=_PLUGIN_HOOKS / "bus-unregister.sh",
            usage_capture_script=None,
            registration_kind="wrapper",
        )

    def capture_usage(self, pane_content: str) -> dict | None:
        # Codex exposes no tmux-visible token counter: there is no
        # equivalent of Claude's ``\d+ tokens`` pattern in the status
        # bar, so there is nothing for a capture hook to sample. Explicit
        # None instead of inheriting Claude's regex keeps the fact
        # visible on the adapter surface. When Codex grows such a
        # surface, implement the extraction here and add a companion
        # usage_capture_script on lifecycle_integration().
        return None


CODEX = CodexRuntimeAdapter()
register(CODEX)
