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
      * runtime capability metadata (agent cache dir, runtime id)
    """

    runtime_id: str

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
        """Return the plugin-agent-definition cache directory for this runtime."""
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
    """Return the default runtime adapter (Claude while it is the incumbent)."""
    if _default_id is None:
        raise RuntimeError("No runtime adapter registered")
    return _adapters[_default_id]
