"""Runtime adapters for helioy-bus.

Each adapter encapsulates a coding-agent runtime (Claude Code, Codex, ...)
so the core bus and warroom logic remain runtime-agnostic. One adapter per
runtime. The default adapter is Claude while it is the incumbent runtime.

Callers resolve an adapter through :func:`default_adapter` or :func:`for_id`;
they never hard-code runtime names or command strings. Multi-runtime
identity resolution iterates :func:`registered_adapters`.
"""

from __future__ import annotations

from server.runtimes.base import (
    RuntimeAdapter,
    default_adapter,
    for_id,
    register,
    registered_adapters,
)
from server.runtimes.claude import ClaudeRuntimeAdapter
from server.runtimes.codex import CodexRuntimeAdapter

__all__ = [
    "ClaudeRuntimeAdapter",
    "CodexRuntimeAdapter",
    "RuntimeAdapter",
    "default_adapter",
    "for_id",
    "register",
    "registered_adapters",
]
