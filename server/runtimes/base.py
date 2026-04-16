"""Runtime adapter contract and registry.

The ``RuntimeAdapter`` protocol is the only surface through which the core
bus talks about runtime-specific behavior. Core code imports
:func:`default_adapter` or :func:`for_id` rather than referring to a concrete
adapter, so adding a second runtime (Codex) is a registration step, not a
core-code edit.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable


@runtime_checkable
class RuntimeAdapter(Protocol):
    """Contract every runtime adapter must satisfy.

    Responsibilities (from the multi-runtime architecture spec):
      * launch command construction
      * identity bootstrap (env var names)
      * runtime capability metadata (agent cache dir, runtime id,
        specialist-role support)
      * agent/skill catalogue discovery
    """

    runtime_id: str
    supports_specialist_roles: bool
    """Whether this runtime enacts a specialist role at launch time.

    ``True`` when the runtime exposes a CLI mechanism (e.g. Claude's
    ``--agent <qualified-name>`` flag) that actually binds the session
    to a persona. ``False`` for runtimes whose skill/agent system is
    contextual per-turn (e.g. Codex skills activated via slash command),
    where claiming a specialist role at spawn time would persist state
    that the runtime does not enact.

    ``services/warroom.spawn`` and ``add`` check this flag and reject
    specialist-role spawns for runtimes that return ``False``. Such
    runtimes can still be used via ``warroom_spawn_repos`` in
    general/repo mode.
    """

    def build_launch_command(self, *, qualified_name: str | None) -> str:
        """Return the shell command that launches a pane for this runtime.

        ``qualified_name`` is the agent type identifier when spawning a
        specialist role, or ``None`` for a general/repo-mode session.
        """
        ...

    @property
    def self_pid_env(self) -> str:
        """Env var name carrying the runtime's parent PID.

        The hot-reload proxy exports this env so the inner server can look
        up its own PID file and resolve canonical identity without racing
        the SessionStart hook.
        """
        ...

    def agents_cache_dir(self) -> Path:
        """Return the root of this runtime's agent/skill catalogue on disk."""
        ...

    def discover_agent_types(self) -> list[dict]:
        """Return all agent type definitions this runtime knows about.

        The return value is a list of dicts with the fields:

          * ``qualified_name`` — ``{namespace}:{name}``, unique within the
            catalogue returned by this adapter
          * ``name`` — short name
          * ``namespace`` — logical group (plugin name, skill scope, ...)
          * ``summary`` — human-readable description, truncated to <=200
            characters
          * ``model`` — optional model hint from frontmatter, or empty
            string if absent
          * ``runtime`` — this adapter's :attr:`runtime_id`

        Returned dicts contain no adapter-internal fields (e.g. mtime)
        and the list is sorted by ``qualified_name``. An adapter whose
        catalogue directory does not yet exist returns an empty list
        rather than raising.
        """
        ...


_adapters: dict[str, RuntimeAdapter] = {}
_default_id: str | None = None


def register(adapter: RuntimeAdapter, *, default: bool = False) -> None:
    """Register an adapter under its runtime id. First registration wins as default."""
    global _default_id
    _adapters[adapter.runtime_id] = adapter
    if default or _default_id is None:
        _default_id = adapter.runtime_id


def for_id(runtime_id: str) -> RuntimeAdapter:
    """Look up a registered adapter by id. Raises KeyError if unknown."""
    return _adapters[runtime_id]


def default_adapter() -> RuntimeAdapter:
    """Return the default runtime adapter.

    The default is selected at registration time (see :func:`register`)
    and is not tied to any specific runtime. Shared core code should
    prefer :func:`for_id` when the caller has a runtime id in hand and
    fall back to the default only when no runtime is specified.
    """
    if _default_id is None:
        raise RuntimeError("No runtime adapter registered")
    return _adapters[_default_id]


def registered_adapters() -> list[RuntimeAdapter]:
    """Return all currently registered adapters in registration order.

    Used by identity resolution to iterate every runtime's self-PID env
    var, and by discovery to build a union catalogue across runtimes,
    so the bus handles any registered runtime without a hardcoded list.
    """
    return list(_adapters.values())
