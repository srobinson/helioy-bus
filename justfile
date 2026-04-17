set dotenv-load := true

server_dir := "server"

default:
    @just --list

# Install dependencies
build:
    uv sync

# Lint and type-check
check:
    uv run ruff check {{ server_dir }}/
    uv run mypy {{ server_dir }}/ --explicit-package-bases --ignore-missing-imports
    @if command -v shellcheck >/dev/null 2>&1; then \
        shellcheck -x -P plugin/hooks plugin/hooks/*.sh plugin/hooks/lib/*.sh; \
    else \
        echo "shellcheck not installed, skipping hook lint (brew install shellcheck)"; \
    fi

# Auto-fix lint issues
fmt:
    uv run ruff check {{ server_dir }}/ --fix
    uv run ruff format {{ server_dir }}/

# Run tests
test:
    uv run pytest tests/ -v

# Lint, type-check, and test
ci: check test
    @echo "All CI checks passed"

# Run the MCP server directly (for manual testing)
run:
    uv run python {{ server_dir }}/bus_server.py

# Preflight: build + check + test
preflight: build check test
