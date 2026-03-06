set dotenv-load

server_dir := "server"

# Lint and type-check
check:
    uv run ruff check {{server_dir}}/
    uv run mypy {{server_dir}}/bus_server.py --ignore-missing-imports

# Install dependencies
build:
    uv sync

# Run tests
test:
    uv run pytest tests/ -v

# Run the MCP server directly (for manual testing)
run:
    uv run python {{server_dir}}/bus_server.py

# Install helioy-bus as a user-scoped MCP server in ~/.claude/settings.json
install:
    ./install.sh

# Show registered agents (quick CLI check)
agents:
    uv run python scripts/agents.py

# Show all inboxes and message counts
inboxes:
    uv run python scripts/inboxes.py
