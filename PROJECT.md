# PROJECT.md

## Overview

helioy-bus is an MCP server that provides inter-agent communication for Claude Code instances. It is part of the [Helioy ecosystem](https://github.com/helioy), which includes context-matters (structured context store), fmm (code structural intelligence), nancyr (multi-agent orchestrator), markdown-matters (markdown indexing), and helioy-plugins (Claude Code plugin layer).

The bus solves a specific problem: Claude Code sessions are isolated stdio processes with no built-in way to discover or communicate with each other. helioy-bus bridges this gap using the filesystem as shared memory and tmux as the notification channel.

## Architecture

```
Claude Code A          Claude Code B          Claude Code C
     |                      |                      |
  [stdio]               [stdio]               [stdio]
     |                      |                      |
helioy-bus MCP         helioy-bus MCP         helioy-bus MCP
     |                      |                      |
     +----------+-----------+----------+-----------+
                |                      |
        ~/.helioy/bus/           ~/.helioy/bus/
        registry.db              inbox/{agent_id}/
```

Each Claude Code instance spawns its own helioy-bus process. There is no central daemon. Coordination happens through:

1. **SQLite registry** (`registry.db`): Agents register on startup and are pruned lazily when their tmux pane dies.
2. **File-based mailboxes** (`inbox/{agent_id}/*.json`): Messages are atomic JSON files written via temp + rename. Read messages move to `inbox/{agent_id}/archive/`.
3. **tmux nudges**: When a message arrives, the bus sends `"you have mail!"` + Enter to the recipient's tmux pane, waking idle Claude sessions. Nudges are throttled (30s per recipient) and handle copy-mode gracefully.

## File Structure

```
server/
  bus_server.py    # MCP server: 6 tools, SQLite registry, file mailboxes, tmux nudges
  proxy.py         # Hot-reload dev proxy: watches server/ for .py changes, restarts inner process

plugin/
  scripts/
    warroom.sh     # Tmux layout spawner: repo-mode and role-mode agent windows

scripts/
  agents.py        # Debug: dump agent registry
  inboxes.py       # Debug: show inbox counts

tests/
  test_bus_server.py  # 36 test cases covering all tools and edge cases
```

## MCP Tools

### register_agent

Registers a Claude Code instance in the SQLite registry. Identity is derived from the working directory basename and tmux target (e.g., `helioy-bus:main:1.0`). Accepts an optional profile dict for structural identity: `owns`, `consumes`, `capabilities`, `domain`, `skills`.

### unregister_agent

Removes an agent from the registry by ID. Called on session teardown.

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

Reads all unread messages from an agent's inbox, moving them to `archive/` on read. Supports `topic` filtering, where non-matching messages remain unread in the inbox.

## Database Schema

```sql
CREATE TABLE agents (
    agent_id      TEXT PRIMARY KEY,
    cwd           TEXT NOT NULL,
    tmux_target   TEXT NOT NULL DEFAULT '',
    pid           INTEGER,
    session_id    TEXT NOT NULL DEFAULT '',
    agent_type    TEXT NOT NULL DEFAULT 'general',
    profile       TEXT,           -- JSON blob, nullable
    registered_at TEXT NOT NULL,
    last_seen     TEXT NOT NULL
);

CREATE TABLE nudge_log (
    agent_id  TEXT NOT NULL,
    nudged_at TEXT NOT NULL
);
```

WAL mode is enabled for concurrent reads across multiple bus processes.

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

## Hot-Reload Proxy

`server/proxy.py` wraps the MCP server for development. It watches `server/` for Python file changes and restarts the inner process transparently, replaying the MCP `initialize` handshake so the Claude Code client never sees a disconnect.

## Warroom

`plugin/scripts/warroom.sh` is a tmux layout manager that spawns Claude Code agents in coordinated configurations.

**Repo-mode** (`warroom.sh` with no arguments): Creates a `warroom` window with one pane per Helioy repository. Each agent runs as `general` type.

**Role-mode** (`warroom.sh <name> "type1 type2 ..."`): Creates a named window with one pane per specialist role, all working in the current directory. Example: `warroom.sh review "clinical-reviewer code-reviewer"` spawns two review specialists.

Pane titles follow the format `{repo}:{agent_type}:{session}:{window}.{pane}`, which serves as the source of truth for identity resolution in bus hooks. Window title overrides are locked to prevent Claude Code from changing them.

## Identity Resolution

Agent IDs are derived from context:

1. If `agent_id` is passed explicitly to `register_agent`, it is used as-is.
2. If `tmux_target` is provided, the ID becomes `{basename(pwd)}:{tmux_target}`.
3. Otherwise, the ID is `basename(pwd)`.

The `_self_agent_id()` helper reads `TMUX_PANE` and the pane title to resolve identity for outbound messages when `from_agent` is omitted.

## Development

```bash
uv sync                    # install dependencies
uv run pytest              # run tests
uv run ruff check .        # lint
uv run mypy server/        # type check
```

## Dependencies

- **mcp[cli]** (>=1.0.0): MCP protocol SDK, provides FastMCP server framework
- **mcp-hmr**: Hot module reload support for MCP servers (used by proxy.py)
- **watchfiles**: Filesystem watcher for the hot-reload proxy

Dev dependencies: ruff, mypy, pytest, pytest-asyncio.
