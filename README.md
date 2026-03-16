# helioy-bus

Inter-agent message bus for Claude Code instances. Enables multiple Claude Code sessions running in tmux panes to discover each other, exchange messages, and coordinate work through a shared filesystem-based transport.

## How it works

Each Claude Code instance spawns `helioy-bus` as an MCP server over stdio. Shared state lives in `~/.helioy/bus/`: a SQLite registry for agent presence and file-based mailboxes for message delivery. Any agents sharing the same filesystem share the same bus.

Messages are delivered as atomic JSON files (temp + rename) to prevent partial reads. Recipients are woken via tmux `send-keys` nudges, throttled to one per 30 seconds per recipient.

## Setup

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```bash
uv sync
```

The MCP server and hooks are managed by [helioy-plugins](https://github.com/srobinson/helioy-plugins). Once the plugin is activated, Claude Code instances register on SessionStart and deregister on SessionEnd automatically.

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
| `register_agent` | Register a Claude Code instance on the bus |
| `unregister_agent` | Remove an agent from the registry |
| `list_agents` | List registered agents with optional tmux session/window filtering |
| `heartbeat` | Update liveness timestamp for an agent |
| `send_message` | Send a message to an agent, a role, or broadcast to all |
| `get_messages` | Read and archive unread messages from an agent's inbox |

## Addressing

- **Direct**: `send_message(to="agent-id")` targets a specific agent
- **Role-based**: `send_message(to="role:backend-engineer")` targets all agents of that type
- **Broadcast**: `send_message(to="*")` delivers to every registered agent except the sender

## Warroom

`plugin/scripts/warroom.sh` spawns multi-agent tmux layouts. Symlink it for convenience:

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

This title is the source of truth for agent identity. The hooks read it at SessionStart to derive `agent_id` and `agent_type`.

## Debug Scripts

```bash
python scripts/agents.py    # dump the agent registry
python scripts/inboxes.py   # show inbox message counts
```

## Development

```bash
uv sync                    # install dependencies
uv run pytest              # run tests
uv run ruff check .        # lint
uv run mypy server/        # type check
```
