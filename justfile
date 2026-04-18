set dotenv-load := true

server_dir := "server"

default:
    @just --list

# Install dependencies
build:
    uv sync

# Fix formatting and auto-fixable lint issues
fmt:
    uv run ruff format {{ server_dir }}/
    uv run ruff check {{ server_dir }}/ --fix

# Verify: format, lint, types, shellcheck, tests (no mutations)
check:
    uv run ruff format {{ server_dir }}/ --check
    uv run ruff check {{ server_dir }}/
    uv run mypy {{ server_dir }}/ --explicit-package-bases --ignore-missing-imports
    @if command -v shellcheck >/dev/null 2>&1; then \
        shellcheck -x -P plugin/hooks plugin/hooks/*.sh plugin/hooks/lib/*.sh; \
    else \
        echo "shellcheck not installed, skipping hook lint (brew install shellcheck)"; \
    fi
    uv run pytest tests/ -v

# Run tests only
test:
    uv run pytest tests/ -v

# Run the MCP server directly (for manual testing)
run:
    uv run python {{ server_dir }}/bus_server.py

# Preflight: build + check
preflight: build check
