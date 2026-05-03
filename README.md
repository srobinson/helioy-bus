# helioy-bus

Inter-agent message bus for coding-agent runtimes such as Claude Code and Codex. Enables multiple agent sessions running in tmux panes to discover each other, exchange messages, and coordinate work through a shared filesystem-based transport.

## How it works

Each runtime instance spawns `helioy-bus` as an MCP server over stdio. Shared state lives in `~/.helioy/bus/`: a SQLite registry for agent presence and file-based mailboxes for message delivery. Any agents sharing the same filesystem share the same bus.

Messages are delivered as atomic JSON files (temp + rename) to prevent partial reads. Recipients may be woken via tmux `send-keys` nudges when their runtime supports that path. Mailbox nudges are throttled to one per 30 seconds per recipient. Direct `nudge_message` calls send tmux text without writing mailbox files.

## Setup

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```bash
uv sync
```

The MCP server and hooks are managed by [helioy-plugins](https://github.com/srobinson/helioy-plugins). Once the plugin is activated, Claude sessions register on SessionStart and deregister on SessionEnd automatically. Codex sessions register through the launch wrapper used by the Codex runtime adapter.

To register manually as an MCP server:

```json
{
  "mcpServers": {
    "helioy-bus": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/helioy-bus", "python", "server/proxy.py"]
    }
  }
}
```

The proxy provides hot-reload during development. For production, point directly at `server/bus_server.py`.

## MCP Tools

| Tool | Purpose |
|------|---------|
| `whoami` | Return the caller's identity record from the registry |
| `register_agent` | Register a runtime instance on the bus |
| `unregister_agent` | Remove an agent from the registry |
| `list_agents` | List registered agents with optional tmux and cwd basename filtering |
| `heartbeat` | Update liveness timestamp for an agent |
| `send_message` | Send a message to an agent, a role, or broadcast to all |
| `nudge_message` | Send direct tmux text to an agent, a role, or broadcast |
| `get_messages` | Read and archive unread messages from an agent's inbox |

## Warroom MCP Tools

The repo also ships `helioy-warroom`, a companion MCP server for multi-agent orchestration:

| Tool | Purpose |
|------|---------|
| `warroom_discover` | Discover launchable agent catalogues across registered runtimes |
| `warroom_spawn_repos` | Spawn one general-purpose pane per Helioy repo |
| `warroom_spawn` | Spawn a named warroom with runtime-scoped agent validation |
| `warroom_status` | Return live warroom rows plus member registration and liveness state |
| `warroom_add` | Add a member pane to an active warroom |
| `warroom_remove` | Remove a member by stable member id or unique role |
| `warroom_kill` | Kill one warroom or all active warrooms |
| `warroom_presets` / `warroom_save_preset` | Read and persist reusable team compositions |

## Addressing

- **Direct**: `send_message(to="agent-id")` targets a specific agent
- **Role-based**: `send_message(to="role:backend-engineer")` targets all agents of that type
- **Broadcast**: `send_message(to="*")` delivers to every registered agent except the sender

## Warroom

`plugin/scripts/warroom.sh` remains as a legacy convenience wrapper around tmux workflows. Symlink it if you still want the shell entrypoint:

```bash
ln -sf "$(pwd)/plugin/scripts/warroom.sh" ~/.helioy/warroom.sh
```

```bash
# Repo-mode: one agent per helioy repo
warroom.sh

# Role-mode: named window of specialists in current directory
warroom.sh design "brand-guardian ui-designer visual-storyteller"

# Management
warroom.sh status
warroom.sh kill design
warroom.sh kill all
```

### Identity

Pane title format: `{repo}:{agent_type}:{session}:{window}.{pane}`

Examples:

- `fmm:general:main:2.1` (repo-mode)
- `helioy-bus:backend-engineer:main:3.1` (role-mode)

This title is the source of truth for agent identity. The Claude hooks read it at SessionStart, and the Codex wrapper uses the same identity convention.

## Development

```bash
just build                 # install dependencies
just check                 # ruff, mypy across server/, shellcheck when installed
just test                  # run pytest
```
