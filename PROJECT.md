# PROJECT.md

## Overview

helioy-bus is a pair of MCP servers that provide inter-agent communication and multi-agent orchestration for Claude Code instances. It is part of the [Helioy ecosystem](https://github.com/helioy), which includes context-matters (structured context store), attention-matters (geometric memory), fmm (code structural intelligence), nancyr (multi-agent orchestrator), markdown-matters (markdown indexing), and helioy-plugins (Claude Code plugin layer).

The bus solves a specific problem: Claude Code sessions are isolated stdio processes with no built-in way to discover or communicate with each other. helioy-bus bridges this gap using the filesystem as shared memory and tmux as the notification channel. The warroom extends this with coordinated multi-agent spawning and lifecycle management.

## Architecture

```
Claude Code A          Claude Code B          Claude Code C
     |                      |                      |
  [stdio]               [stdio]               [stdio]
     |                      |                      |
helioy-bus MCP         helioy-bus MCP         helioy-bus MCP
helioy-warroom MCP     helioy-warroom MCP     helioy-warroom MCP
     |                      |                      |
     +----------+-----------+----------+-----------+
                |                      |
        ~/.helioy/bus/           ~/.helioy/bus/
        registry.db              inbox/{agent_id}/
                                 presets/
```

Each Claude Code instance spawns its own helioy-bus process (and optionally a helioy-warroom process). There is no central daemon. Coordination happens through:

1. **SQLite registry** (`registry.db`): Agents register on startup and are pruned lazily when their tmux pane dies. WAL mode enables concurrent reads across processes.
2. **File-based mailboxes** (`inbox/{agent_id}/*.json`): Messages are atomic JSON files written via temp + rename. Read messages move to `inbox/{agent_id}/archive/` with 7-day TTL.
3. **tmux nudges**: When a message arrives, the bus sends `"you have mail!"` + Enter to the recipient's tmux pane, waking idle Claude sessions. Nudges are throttled (30s per recipient) and handle copy-mode gracefully.
4. **Warroom orchestration**: Spawns coordinated agent layouts in tmux windows, manages lifecycle, and supports presets for repeatable configurations.

## File Structure

```
server/
  bus_server.py        # Bus MCP server: 7 tools (registry, messaging, identity)
  warroom_server.py    # Warroom MCP server: 9 tools (spawn, status, presets)
  warroom_cli.py       # CLI entry point for warroom operations
  proxy.py             # Hot-reload dev proxy: watches server/ for changes, restarts transparently
  _db.py               # Shared database layer, path constants, logging
  _tmux.py             # Tmux operations: nudging, pane spawning, liveness checks
  _warroom.py          # Warroom helpers: frontmatter parsing, agent type scanning
  _identity.py         # Agent identity resolution (PID files, shell resolver, fallback)

plugin/
  hooks/
    bus-register.sh      # SessionStart: registers agent on the bus
    bus-unregister.sh    # SessionStop: unregisters agent
    bus-prune.sh         # Prunes stale agents from registry
    check-mail.sh        # PreToolUse: notifies agent of unread messages
    stop-check-mail.sh   # Stop: halts mail checking
    token-capture.sh     # Captures token usage metrics
    lib/
      resolve-identity.sh  # Authoritative identity resolver (shared by hooks)
  scripts/
    warroom.sh           # Legacy tmux layout spawner (repo-mode and role-mode)

tests/
  test_bus_server.py      # 40 test functions covering all bus tools
  test_warroom_server.py  # 39 test functions covering warroom operations
  conftest.py             # Shared fixtures
```

## MCP Tools: Bus Server

### whoami

Returns the calling agent's full identity record from the registry: agent_id, agent_type, tmux_target, cwd, session_id, registered_at, and token_usage.

### register_agent

Registers a Claude Code instance in the SQLite registry. Identity is derived from the working directory basename and tmux target (e.g., `helioy-bus:main:1.0`). Accepts an optional profile dict for structural identity: `owns`, `consumes`, `capabilities`, `domain`, `skills`.

### unregister_agent

Removes an agent from the registry by ID. Called on session teardown via the `bus-unregister.sh` hook.

### list_agents

Returns all registered agents. Performs lazy liveness pruning by checking whether each agent's tmux pane still exists. Supports `tmux_filter` to scope results to a tmux session or session:window.

### heartbeat

Updates the `last_seen` timestamp for an agent. Intended for periodic liveness signals.

### send_message

Delivers a message to one or more agents. Supports three addressing modes:

- **Direct**: `to="agent-id"` targets a single agent
- **Role-based**: `to="role:backend-engineer"` targets all agents with that `agent_type`
- **Broadcast**: `to="*"` delivers to all registered agents except the sender

Each delivery writes an atomic JSON file to the recipient's inbox directory. The payload includes `id`, `from`, `to`, `reply_to`, `topic`, `content`, and `sent_at`.

After delivery, the bus optionally sends a tmux nudge (literal keystroke injection) to wake idle recipients. Nudges are throttled to once per 30 seconds per recipient. The throttle resets when the recipient has unread messages. Copy-mode is detected and exited before sending keystrokes.

### get_messages

Reads all unread messages from an agent's inbox, moving them to `archive/` on read. Supports `topic` filtering, where non-matching messages remain unread in the inbox. Archived messages are cleaned up after 7 days.

## MCP Tools: Warroom Server

### warroom_discover

Searches available agent types by scanning the Claude Code plugin cache for agent definitions. Filters by query substring and/or plugin namespace.

### warroom_spawn_repos

Spawns a warroom window with one pane per Helioy repository, each running a general-purpose agent.

### warroom_spawn

Spawns a named warroom window with specialist agents (e.g., `backend-engineer`, `clinical-reviewer`) all working in a specified directory.

### warroom_status

Returns the current state of all warroom windows: panes, agent types, registration status, and liveness.

### warroom_add

Adds a new agent pane to an existing warroom window.

### warroom_remove

Removes an agent pane from a warroom window and unregisters it from the bus.

### warroom_kill

Destroys an entire warroom window and all its agents.

### warroom_presets

Lists saved warroom configurations for repeatable multi-agent setups.

### warroom_save_preset

Saves the current warroom configuration as a named preset.

## Database Schema

```sql
CREATE TABLE agents (
    agent_id      TEXT PRIMARY KEY,
    cwd           TEXT NOT NULL,
    tmux_target   TEXT NOT NULL DEFAULT '',
    pid           INTEGER,
    session_id    TEXT NOT NULL DEFAULT '',
    agent_type    TEXT NOT NULL DEFAULT 'general',
    profile       TEXT,
    token_usage   TEXT NOT NULL DEFAULT '{}',
    registered_at TEXT NOT NULL,
    last_seen     TEXT NOT NULL
);

CREATE TABLE nudge_log (
    agent_id  TEXT NOT NULL,
    nudged_at TEXT NOT NULL
);

CREATE TABLE warrooms (
    warroom_id   TEXT PRIMARY KEY,
    tmux_session TEXT NOT NULL,
    tmux_window  TEXT NOT NULL,
    cwd          TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'active'
);

CREATE TABLE warroom_members (
    warroom_id   TEXT NOT NULL REFERENCES warrooms(warroom_id) ON DELETE CASCADE,
    agent_type   TEXT NOT NULL,
    tmux_target  TEXT NOT NULL,
    pane_id      TEXT NOT NULL,
    agent_id     TEXT,
    spawned_at   TEXT NOT NULL,
    PRIMARY KEY (warroom_id, agent_type)
);
```

## Message Format

```json
{
  "id": "uuid",
  "from": "sender-agent-id",
  "to": "recipient-agent-id",
  "reply_to": "sender-agent-id",
  "topic": "optional-thread-identifier",
  "content": "message body (plain text or markdown)",
  "sent_at": "2026-03-16T12:00:00Z"
}
```

Messages are stored as `{timestamp}_{message_id_prefix}.json` in the recipient's inbox directory. Filenames use the ISO timestamp with colons replaced by hyphens for filesystem compatibility.

## Identity Resolution

Agent identity is resolved through a three-tier chain:

1. **PID file (fast path)**: At SessionStart, `bus-register.sh` writes the agent_id to `~/.helioy/bus/pids/{pid}`. The `_self_agent_id()` function reads this file for O(1) resolution.
2. **Shell resolver (slow path)**: Falls back to `resolve-identity.sh`, which reads the tmux pane title (format: `{repo}:{agent_type}:{session}:{window}.{pane}`) and derives a consistent identity.
3. **Basename fallback**: If both fail, uses `basename(cwd)` to maintain availability at the cost of potential identity divergence.

## Entry Points

```toml
helioy-bus           = "server.bus_server:mcp"
helioy-warroom       = "server.warroom_server:mcp"
helioy-warroom-cli   = "server.warroom_cli:main"
helioy-bus-initdb    = "server._db:_initdb_cli"
```

## Hot-Reload Proxy

`server/proxy.py` wraps either MCP server for development. It watches `server/` for Python file changes and restarts the inner process transparently, replaying the MCP `initialize` handshake so the Claude Code client never sees a disconnect.

## Development

```bash
uv sync                    # install dependencies
uv run pytest              # run tests (79 test functions)
uv run ruff check .        # lint
uv run mypy server/        # type check
```

## Dependencies

- **mcp[cli]** (>=1.0.0): MCP protocol SDK, provides FastMCP server framework
- **mcp-hmr**: Hot module reload support for MCP servers (used by proxy.py, pulls in watchfiles)

Dev dependencies: ruff, mypy, pytest, pytest-asyncio.
