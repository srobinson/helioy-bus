# TLDR

helioy-bus lets Claude Code sessions talk to each other.

## The Problem

Claude Code runs as isolated stdio processes. Each session has no awareness of other sessions. There is no built-in discovery, messaging, or coordination mechanism.

## The Solution

Two MCP servers that use the filesystem as shared memory and tmux as the notification layer.

**Bus server** (7 tools): Agent registry, message delivery, identity resolution. Agents register in a shared SQLite database. Messages are atomic JSON files dropped into per-agent inbox directories. A tmux keystroke nudge wakes idle recipients.

**Warroom server** (9 tools): Multi-agent orchestration. Spawns coordinated agent layouts in tmux windows with specialist roles (backend-engineer, clinical-reviewer, etc.). Manages lifecycle, supports saved presets.

## How It Works

```
1. Agent starts    -> bus-register.sh hook writes to SQLite registry
2. Agent sends     -> JSON file written to ~/.helioy/bus/inbox/{recipient}/
3. Recipient woken -> tmux send-keys "you have mail!" + Enter
4. Recipient reads -> get_messages moves files to archive/
```

No central daemon. Every Claude Code instance spawns its own bus process. They coordinate through the shared filesystem and database (SQLite WAL mode).

## Key Concepts

**Agent ID**: Derived from `{basename(cwd)}:{tmux_target}`. Example: `helioy-bus:main:1.0`. Resolved via PID file (fast), pane title parsing (slow), or basename fallback.

**Addressing**: Direct (`to="agent-id"`), role-based (`to="role:backend-engineer"`), or broadcast (`to="*"`).

**Nudging**: Literal text injection into tmux panes. Throttled to 30s per recipient. Handles copy-mode. The recipient's mail skill triggers `get_messages` on receiving the nudge text.

**Warroom**: A tmux window with multiple Claude Code agents working in coordination. Spawned via MCP tools or the legacy `warroom.sh` script. Each pane runs one specialist agent.

**Presets**: Saved warroom configurations stored in `~/.helioy/bus/presets/`. Enables repeatable multi-agent setups.

## File Layout

```
server/           Python MCP servers, shared modules
plugin/hooks/     Claude Code lifecycle hooks (register, unregister, mail check)
plugin/scripts/   Legacy warroom shell script
tests/            Pytest and shell-harness coverage across bus and warroom
```

## Running

```bash
uv sync && uv run pytest
```

The servers are consumed as MCP servers by Claude Code, not run standalone. The proxy (`server/proxy.py`) provides hot-reload during development.
