# helioy-bus

Inter-agent message bus for Claude Code instances — shared SQLite registry and file-based mailboxes that let multiple Claude sessions communicate, coordinate, and spawn specialist roles.

## Setup

Install Python dependencies:

```bash
uv sync
```

The MCP server and hooks are managed by [helioy-plugins](https://github.com/srobinson/helioy-plugins). Once the plugin is activated, Claude Code instances register on SessionStart and deregister on SessionEnd automatically.

## warroom.sh

`warroom.sh` is the agent spawner. It lives under version control at `plugin/scripts/warroom.sh` and should be symlinked to `~/.helioy/warroom.sh`:

```bash
ln -sf "$(pwd)/plugin/scripts/warroom.sh" ~/.helioy/warroom.sh
```

### Usage

```bash
# Repo-mode: one general agent per helioy repo in a "warroom" window
warroom.sh

# Role-mode: specialist agents in the current repo in a "crew" window
warroom.sh "backend-engineer frontend-engineer"

# Kill
warroom.sh kill          # kills warroom window
warroom.sh kill crew     # kills crew window
warroom.sh kill all      # kills both
```

### Identity

Pane title format: `{repo}:{agent_type}:{session}:{window}.{pane}`

Examples:

- `fmm:general:7:2.1` (repo-mode)
- `helioy-bus:backend-engineer:7:3.1` (role-mode)

This title is the source of truth for agent identity. The hooks read it at SessionStart to derive `agent_id` and `agent_type`.

## Role-based messaging

Send to all agents of a given type:

```python
send_message(to="role:backend-engineer", content="implement the auth endpoint")
```

## Development

```bash
just check   # lint + typecheck
just test    # pytest + shell tests
just agents  # show registered agents
just inboxes # show inbox state
```
