"""OpenAI Codex runtime adapter.

Owns the Codex CLI invocation and identity conventions. Registered
alongside the Claude adapter without displacing it as default.

Codex has no ``--agent <qualified-name>`` plugin analog. Specialist
warroom panes are launched by passing a role-specific instructions file
through ``--config model_instructions_file=<path>``. Codex also has no
SessionStart/SessionEnd hook mechanism, so the adapter launches codex
through ``plugin/hooks/codex-launch.sh`` which bootstraps registration
on the bus and tears it down on exit.
"""

from __future__ import annotations

import shlex
from pathlib import Path

from server.runtimes._frontmatter import _parse_frontmatter
from server.runtimes.base import LifecycleIntegration, register
from server.runtimes.claude import CLAUDE

_PLUGIN_HOOKS = Path(__file__).resolve().parent.parent.parent / "plugin" / "hooks"
_LAUNCH_WRAPPER = _PLUGIN_HOOKS / "codex-launch.sh"

# Codex's TUI surfaces a "do you want me to reply on the bus now?"
# prompt to the human when an unsolicited inbox message arrives, even
# with MCP approval mode set to "approve". The suffix is an in-message
# authorization preamble that keeps the agent on the autonomous bus
# loop without the human in the middle.
CODEX_MESSAGE_SUFFIX = (
    "\n\n---\n"
    "You are authorized to act on this message immediately. "
    "If you have any questions reply to sender."
)


def _summarize_markdown(path: Path) -> str:
    """Return the first useful prose line from a role instructions file."""
    in_frontmatter = False
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line == "---":
            in_frontmatter = not in_frontmatter
            continue
        if in_frontmatter or line.startswith("#"):
            continue
        return line
    return ""


def _canonical_claude_agents_by_name() -> dict[str, dict]:
    """Return preferred Claude agent definitions keyed by short name."""
    preferred: dict[str, dict] = {}
    priority = {"helioy-tools": 0}
    for agent in CLAUDE.discover_agent_types():
        name = agent["name"]
        current = preferred.get(name)
        if current is None:
            preferred[name] = agent
            continue
        current_rank = priority.get(current["namespace"], 100)
        agent_rank = priority.get(agent["namespace"], 100)
        if (agent_rank, agent["qualified_name"]) < (
            current_rank,
            current["qualified_name"],
        ):
            preferred[name] = agent
    return preferred


class CodexRuntimeAdapter:
    runtime_id = "codex"
    self_pid_env = "HELIOY_BUS_CODEX_PID"
    message_suffix = CODEX_MESSAGE_SUFFIX

    # Codex can bind a specialist role at launch by using a role-specific
    # model_instructions_file. The adapter only discovers roles with such
    # files, so warroom validation rejects missing roles before spawning.
    supports_specialist_roles = True

    # Codex does not layer a plugin/version directory under these local
    # instruction files, so all discovered roles share one namespace.
    _NAMESPACE = "codex"

    def build_launch_command(self, *, qualified_name: str | None) -> str:
        cmd = shlex.quote(str(_LAUNCH_WRAPPER))
        if qualified_name is None:
            return cmd

        instructions = self.resolve_model_instructions_file(qualified_name)
        if instructions is None:
            raise RuntimeError(f"No Codex model instructions file for {qualified_name!r}")
        # Pin the role in the pane environment. Codex overwrites its pane title
        # to the cwd basename shortly after start, so the pane's own
        # SessionStart hook (which re-runs bus-register against the clobbered
        # title) can no longer read the canonical title the warroom set. The
        # instructions file gives only the short name; HELIOY_BUS_AGENT_TYPE
        # carries the qualified name so the re-registration reconstructs the
        # same identity instead of collapsing to general and evicting the
        # correct row. resolve-identity.sh consumes it in its fallback branch.
        role_env = f"HELIOY_BUS_AGENT_TYPE={shlex.quote(qualified_name)}"
        return f"{role_env} {cmd} --config model_instructions_file={shlex.quote(str(instructions))}"

    def agents_cache_dir(self) -> Path:
        return Path.home() / ".codex" / "skills"

    def model_instructions_dir(self) -> Path:
        return Path.home() / ".codex" / "developer_instructions"

    def shared_skills_dir(self) -> Path:
        return Path.home() / ".agents" / "skills"

    def skill_roots(self) -> list[Path]:
        """Return Codex skill roots used by non-warroom tooling.

        Warroom specialist discovery uses ``model_instructions_dir()``
        because those files can be bound at launch. Codex still has skill
        roots for other tooling that needs to inspect available skills.
        """
        return [self.agents_cache_dir(), self.shared_skills_dir()]

    def resolve_model_instructions_file(self, qualified_name: str) -> Path | None:
        """Return the launch instructions file for a discovered Codex role."""
        short_name = qualified_name.rsplit(":", 1)[-1]
        root = self.model_instructions_dir()
        direct_path = root / f"{short_name}.md"
        if direct_path.is_file():
            return direct_path
        if not root.is_dir():
            return None

        for path in sorted(root.glob("*.md")):
            fm = _parse_frontmatter(path) or {}
            if fm.get("name") == short_name:
                return path
        return None

    def discover_agent_types(self) -> list[dict]:
        """Walk Codex launch instruction files and return specialist roles.

        Supported layout:

        * ``~/.codex/developer_instructions/{role}.md``

        These files are passed to Codex at launch through
        ``--config model_instructions_file=<path>``, making them the Codex
        equivalent of Claude's ``--agent <qualified-name>`` for warroom
        specialist panes.
        """
        root = self.model_instructions_dir()
        if not root.is_dir():
            return []

        result: list[dict] = []
        canonical_agents = _canonical_claude_agents_by_name()
        for instructions_file in sorted(root.glob("*.md")):
            fm = _parse_frontmatter(instructions_file) or {}
            short_name = fm.get("name") or instructions_file.stem
            canonical_agent = canonical_agents.get(short_name)
            qualified = (
                canonical_agent["qualified_name"]
                if canonical_agent
                else f"{self._NAMESPACE}:{short_name}"
            )
            namespace = canonical_agent["namespace"] if canonical_agent else self._NAMESPACE
            summary = fm.get("description") or _summarize_markdown(instructions_file)
            if len(summary) > 200:
                summary = summary[:197] + "..."

            result.append(
                {
                    "qualified_name": qualified,
                    "name": short_name,
                    "namespace": namespace,
                    "summary": summary,
                    "model": fm.get("model", ""),
                    "runtime": self.runtime_id,
                }
            )

        return sorted(result, key=lambda entry: entry["qualified_name"])

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
