# PROJECT.md

## Overview

helioy-bus is a pair of MCP servers that provide inter-agent communication and multi-agent orchestration for coding-agent runtimes such as Claude Code and Codex. It is part of the [Helioy ecosystem](https://github.com/helioy), which includes context-matters (structured context store), attention-matters (geometric memory), fmm (code structural intelligence), nancyr (multi-agent orchestrator), markdown-matters (markdown indexing), and helioy-plugins (Claude Code plugin layer).

The bus solves a specific problem: coding-agent sessions are isolated stdio processes with no built-in way to discover or communicate with each other. helioy-bus bridges this gap using the filesystem as shared memory and tmux as the notification channel. The warroom extends this with coordinated multi-agent spawning and lifecycle management.

## Architecture

```
Agent Runtime A        Agent Runtime B        Agent Runtime C
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

Each runtime instance spawns its own helioy-bus process (and optionally a helioy-warroom process). There is no central daemon. Coordination happens through:

1. **SQLite registry** (`registry.db`): Agents register on startup and are pruned lazily when their tmux pane dies. WAL mode enables concurrent reads across processes.
2. **File-based mailboxes** (`inbox/{agent_id}/*.json`): Messages are atomic JSON files written via temp + rename. Read messages move to `inbox/{agent_id}/archive/` with 7-day TTL.
3. **tmux nudges**: When a message arrives, the bus may send `"you have mail!"` + Enter to a known recipient's tmux pane. Mailbox nudges are throttled (30s per recipient), suppressed for unsupported runtimes, and handle copy-mode gracefully. Direct nudges can also send caller provided text without writing mailbox files.
4. **Warroom orchestration**: Spawns coordinated agent layouts in tmux windows, manages lifecycle, and supports presets for repeatable configurations.

## File Structure

```
server/
  bus_server.py        # Bus MCP server: registry, messaging, identity tools
  warroom_server.py    # Warroom MCP server: spawn, status, presets tools
  warroom_cli.py       # CLI entry point for warroom operations
  proxy.py             # Hot-reload dev proxy: watches server/, restarts transparently
  _db.py               # Shared database layer, path constants, schema migration
  _tmux.py             # TmuxGateway: nudging, pane spawning, liveness checks
  _warroom.py          # Runtime-aware agent type discovery and short-name resolution
  _warroom_persist.py  # Shared warroom persistence and tmux preflight helpers
  _identity.py         # Agent identity resolution (PID files, shell resolver, fallback)
  runtimes/
    base.py            # RuntimeAdapter protocol
    claude.py          # Claude Code runtime adapter
    codex.py           # Codex runtime adapter
    _frontmatter.py    # Shared agent type frontmatter parser
  services/
    agent_registry.py  # Registration, identity, listing, heartbeat
    message.py         # Send, receive, nudge, throttle
    warroom.py         # Warroom lifecycle: spawn, kill, add, remove, presets, status
    reconciliation.py  # Member agent_id backfill (join warroom_members to agents)

plugin/
  hooks/
    bus-register.sh      # SessionStart or wrapper-start: registers the runtime on the bus
    bus-unregister.sh    # SessionStop: unregisters agent
    bus-prune.sh         # Prunes stale agents from registry
    check-mail.sh        # PreToolUse/UserPromptSubmit: summarizes unread mail without draining inbox
    stop-check-mail.sh   # Stop: halts mail checking
    token-capture.sh     # PreToolUse: captures token usage from tmux status line
    codex-launch.sh      # Launch wrapper: registers Codex sessions on the bus before exec
    lib/
      resolve-identity.sh  # Authoritative identity resolver (shared by hooks)
  scripts/
    warroom.sh           # Legacy tmux layout spawner (repo-mode and role-mode)

tests/
  conftest.py                         # Shared fixtures
  test_adapter_lifecycle.py           # Runtime adapter lifecycle contract
  test_bus_identity.py                # Identity resolution paths
  test_bus_lifecycle_and_db.py        # Init, migration, registration lifecycle
  test_bus_mailbox.py                 # Inbox delivery, archive, TTL
  test_bus_registry.py                # Agent registry operations
  test_resolve_identity.sh            # Shell harness for resolve-identity.sh
  test_runtime_adapters.py            # Runtime adapter protocol conformance
  test_runtime_discovery.py           # Runtime-aware agent type discovery
  test_schema_migration.py            # Legacy warroom_members shim
  test_shell_hooks.py                 # Shell hook contract (register, mail, token)
  test_smoke.py                       # End-to-end smoke coverage
  test_tmux_gateway.py                # TmuxGateway behavior
  test_warroom_agent_types.py         # Agent type resolution
  test_warroom_discover_presets.py    # Discovery and preset operations
  test_warroom_members.py             # Warroom membership operations
  test_warroom_spawn.py               # Warroom spawn paths
```

## MCP Tools: Bus Server

### whoami

Returns the calling agent's full identity record from the registry: agent_id, agent_type, runtime, tmux_target, cwd, session_id, registered_at, and token_usage.

### register_agent

Registers a runtime instance in the SQLite registry. Identity is derived from the working directory basename and tmux target (e.g., `helioy-bus:main:1.0`). Accepts an optional runtime id plus an optional profile dict for structural identity: `owns`, `consumes`, `capabilities`, `domain`, `skills`.

### unregister_agent

Removes an agent from the registry by ID. Called on session teardown via the `bus-unregister.sh` hook.

### list_agents

Returns all registered agents, including their runtime. Performs lazy liveness pruning by checking whether each agent's tmux pane still exists. Supports `tmux_filter` to scope results to a tmux session or session:window, and `cwd_basename` to return every agent whose registered working directory has that basename.

### heartbeat

Updates the `last_seen` timestamp for an agent. Intended for periodic liveness signals.

### send_message

Delivers a message to one or more agents. Supports three addressing modes:

- **Direct**: `to="agent-id"` targets a single agent
- **Role-based**: `to="role:backend-engineer"` targets all agents with that `agent_type`
- **Broadcast**: `to="*"` delivers to all registered agents except the sender

Each delivery writes an atomic JSON file to the recipient's inbox directory. The payload includes `id`, `from`, `to`, `reply_to`, `topic`, `content`, and `sent_at`.

After delivery, the bus optionally sends a tmux nudge (literal keystroke injection) to wake idle recipients when the recipient runtime supports that path. Mailbox nudges are throttled to once per 30 seconds per recipient, with re-nudging allowed while unread messages remain. Copy-mode is detected and exited before sending keystrokes.

### nudge_message

Sends caller provided text directly to one or more agents through tmux. Supports the same addressing modes as `send_message`, but does not create inbox files or durable message records. Use this for transient coordination prompts.

### get_messages

Reads all unread messages from an agent's inbox, moving them to `archive/` on read. Supports `topic` filtering, where non-matching messages remain unread in the inbox. Archived messages are cleaned up after 7 days.

## MCP Tools: Warroom Server

### warroom_discover

Searches available agent types across registered runtime adapters. Each runtime defines its own on-disk catalogue layout via ``discover_agent_types()``; empty ``runtime`` returns the union across every registered runtime. Filters by query substring and/or namespace.

### warroom_spawn_repos

Spawns a warroom window with one pane per Helioy repository, each running a general-purpose agent.

### warroom_spawn

Spawns a named warroom window with specialist agents (e.g., `backend-engineer`, `clinical-reviewer`) all working in a specified directory.

### warroom_status

Returns warroom rows with `warroom_id`, tmux coordinates, `cwd`, `layout`, `runtime_policy`, `metadata`, `status`, `created_at`, and a `members` array. Each member includes the desired runtime/role, reconciliation state, live registration fields, tmux coordinates, timestamps, and token usage when available.

### warroom_add

Adds a new agent pane to an existing warroom window and returns `{warroom_id, added, member_count}` where `added` is the spawned pane record annotated with `warroom_member_id`, `desired_role`, `desired_runtime`, and `spawn_order`.

### warroom_remove

Removes a member from a warroom window and returns `{warroom_id, removed: {warroom_member_id, desired_role}, remaining_members, warroom_killed}`.

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
    runtime       TEXT NOT NULL DEFAULT 'unknown',
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
    warroom_id     TEXT PRIMARY KEY,
    tmux_session   TEXT NOT NULL,
    tmux_window    TEXT NOT NULL,
    cwd            TEXT NOT NULL,
    layout         TEXT NOT NULL DEFAULT 'tiled',
    runtime_policy TEXT,
    metadata       TEXT,
    created_at     TEXT NOT NULL,
    status         TEXT NOT NULL DEFAULT 'active'
);

CREATE TABLE warroom_members (
    warroom_member_id TEXT PRIMARY KEY,
    warroom_id        TEXT NOT NULL REFERENCES warrooms(warroom_id) ON DELETE CASCADE,
    desired_runtime   TEXT NOT NULL,
    desired_role      TEXT NOT NULL,
    desired_repo      TEXT,
    state             TEXT NOT NULL DEFAULT 'pending',  -- 'pending' | 'active'
    agent_instance_id TEXT,                             -- FK to agents.agent_id once reconciled
    spawn_order       INTEGER NOT NULL,
    tmux_target       TEXT NOT NULL,
    pane_id           TEXT NOT NULL,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL
);
```

The `desired_*` columns record the orchestrator's intent at spawn time.
The reconciler (`services/reconciliation.backfill_warroom_member_agent_ids`)
joins members to `agents` on `tmux_target` and writes `agent_instance_id`
back, reconciling `state` between `pending` and `active`.

`warrooms.runtime_policy` and `warrooms.metadata` are schema-reserved surface.
They are returned by `warroom_status`, persisted through the shared warroom row,
and currently remain unpopulated (`NULL`) in this branch.

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
3. **Canonical fallback**: If both fail, `canonical_agent_id()` derives the
   same canonical `{repo}:{agent_type}` shape used elsewhere, preserving
   availability without reintroducing split-identity drift.

## Entry Points

```toml
helioy-bus           = "server.bus_server:mcp"
helioy-warroom       = "server.warroom_server:mcp"
helioy-warroom-cli   = "server.warroom_cli:main"
helioy-bus-initdb    = "server._db:_initdb_cli"
```

## Hot-Reload Proxy

`server/proxy.py` wraps either MCP server for development. It watches `server/` for Python file changes and restarts the inner process transparently, replaying the MCP `initialize` handshake so the outer MCP client never sees a disconnect.

## Development

```bash
just check                 # ruff + mypy across server/
just build                 # uv sync
just test                  # pytest
```

CI runs via `.github/workflows/ci.yml` on pushes to `main` and all pull
requests (docs-only paths are excluded). The job installs `uv`, syncs the
frozen lockfile, installs `just` and `shellcheck`, and runs `just check`
across a Python 3.12 and 3.13 matrix. The local gate mirrors this: `just
check` for lint, hook lint, and Python type-checking across `server/`, plus
`just test` for pytest coverage.

## Design Notes

### Specialist-role gating lives at the service boundary

`warroom.spawn` and `warroom.add` call `_require_specialist_support(adapter)` at the service layer, not inside `TmuxGateway`. The gateway is the low-level tmux wrapper and is intentionally runtime-agnostic; specialist support is a semantic property of the runtime adapter. Claude binds specialists through `--agent <qualified-name>`. Codex binds specialists by passing `--config model_instructions_file=<path>` through the Codex launch wrapper for roles discovered under `~/.codex/developer_instructions/`. Keeping the check adjacent to the adapter lookup prevents the gateway from growing runtime awareness.

### token-capture.sh shells to tmux directly

The PreToolUse hook runs synchronously before the calling agent's MCP server is available to it, so it cannot round-trip through `TmuxGateway`. It reads the tmux status line via `tmux capture-pane` and writes the extracted token count to `registry.db` via a parameterized sqlite3 call. This is the documented exception to the "all tmux side effects through the gateway" rule.

### Legacy warroom_members migration shim

`_migrate_warroom_members` in `server/_db.py` rebuilds pre-stable-member-id and intermediate schemas into the canonical shape on startup. It stays in place because helioy-bus databases live in each user's `~/.helioy/bus/` and may predate ALP-1787. The shim is a no-op on already-migrated databases. Remove after a future fleet-wide reset pass.

### runtime_policy and metadata are deferred schema surface

`warrooms.runtime_policy` and `warrooms.metadata` exist so the persisted row and
status payload have a stable place for future orchestration policy. This branch
does not populate them yet; callers should treat them as nullable, forward
compatible fields rather than active behavior knobs.

## Dependencies

- **mcp[cli]** (>=1.0.0): MCP protocol SDK, provides FastMCP server framework
- **mcp-hmr**: Hot module reload support for MCP servers (used by proxy.py, pulls in watchfiles)

Dev dependencies: ruff, mypy, pytest, pytest-asyncio.
