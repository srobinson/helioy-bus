"""Runtime adapter contract and registry.

The ``RuntimeAdapter`` protocol is the only surface through which the core
bus talks about runtime-specific behavior. Core code imports
:func:`default_adapter` or :func:`for_id` rather than referring to a concrete
adapter, so adding a second runtime (Codex) is a registration step, not a
core-code edit.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class LifecycleIntegration:
    """How a runtime integrates with the bus agent lifecycle.

    Each adapter returns one of these to declare, explicitly, which
    scripts drive startup registration, shutdown registration, and
    optional usage capture. Replaces the implicit prior assumption that
    every runtime uses Claude's plugin hook mechanism: Codex has no
    hook system and drives registration from its own launch wrapper, so
    the adapter surface makes that asymmetry visible instead of hiding
    it in a shell script.

    Attributes:
      startup_script: Executable that registers the agent on the bus.
        Invocation semantics depend on ``registration_kind``.
      shutdown_script: Executable that unregisters the agent on
        teardown (end of session, trap on wrapper exit, ...).
      usage_capture_script: Optional executable that samples runtime
        usage metrics (token counts, cost) and writes them to the
        registry. ``None`` when the runtime has no such mechanism —
        Codex has no tmux-visible token counter, so it declares
        ``None`` rather than borrowing Claude's script.
      registration_kind: How the runtime triggers the scripts:

        * ``"hook"`` — the runtime's plugin system invokes the scripts
          directly on SessionStart / SessionEnd (Claude).
        * ``"wrapper"`` — an adapter-provided launch wrapper invokes
          the startup script up front and installs a trap for the
          shutdown script (Codex).
    """

    startup_script: Path
    shutdown_script: Path
    usage_capture_script: Path | None
    registration_kind: str


@runtime_checkable
class RuntimeAdapter(Protocol):
    """Contract every runtime adapter must satisfy.

    Responsibilities (from the multi-runtime architecture spec):
      * launch command construction
      * identity bootstrap (env var names)
      * runtime capability metadata (agent cache dir, runtime id,
        specialist-role support)
      * agent/skill catalogue discovery
      * lifecycle integration (startup/shutdown registration)
      * usage capture (token/cost sampling)
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

    def lifecycle_integration(self) -> LifecycleIntegration:
        """Return the scripts that drive this runtime's bus lifecycle.

        The returned :class:`LifecycleIntegration` describes startup
        registration, shutdown registration, and optional usage capture.
        Shared code that needs to reason about lifecycle (install
        tooling, contract tests) consults this instead of embedding
        runtime-specific script paths.
        """
        ...

    def capture_usage(self, pane_content: str) -> dict | None:
        """Extract usage metrics from raw pane content.

        ``pane_content`` is the tail of the runtime's tmux pane output,
        as captured by the usage capture hook. Returns a dict describing
        the current usage sample (e.g. ``{"tokens": int}``), or ``None``
        when no sample can be extracted.

        An adapter whose ``lifecycle_integration().usage_capture_script``
        is ``None`` also returns ``None`` here; the two signals agree so
        shared code can trust either as the "does this runtime sample
        usage" probe.
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
