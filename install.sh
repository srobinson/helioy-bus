#!/usr/bin/env bash
# install.sh — Install helioy-bus: MCP server + hooks into ~/.claude/settings.json
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SETTINGS="$HOME/.claude/settings.json"
SERVER_PATH="$SCRIPT_DIR/server/bus_server.py"
MCP_HMR_BIN="$SCRIPT_DIR/.venv/bin/mcp-hmr"
HOOKS_DIR="$SCRIPT_DIR/plugin/hooks"

echo "helioy-bus installer"
echo "  repo:     $SCRIPT_DIR"
echo "  settings: $SETTINGS"

# Ensure settings file exists
mkdir -p "$(dirname "$SETTINGS")"
if [[ ! -f "$SETTINGS" ]]; then
    echo "{}" > "$SETTINGS"
fi

# Make hooks executable
chmod +x "$HOOKS_DIR/check-mail.sh"
chmod +x "$HOOKS_DIR/bus-register.sh"
chmod +x "$HOOKS_DIR/bus-unregister.sh"

# Inject MCP server + hooks using Python
python3 - <<PYEOF
import json

settings_path = "$SETTINGS"
server_path = "$SERVER_PATH"
mcp_hmr_bin = "$MCP_HMR_BIN"
hooks_dir = "$HOOKS_DIR"

with open(settings_path) as f:
    settings = json.load(f)

# ── MCP server ────────────────────────────────────────────────────────────────
settings.setdefault("mcpServers", {})
settings["mcpServers"]["helioy-bus"] = {
    "type": "stdio",
    "command": mcp_hmr_bin,
    "args": [server_path + ":mcp"],
}

# ── Hooks ─────────────────────────────────────────────────────────────────────
settings.setdefault("hooks", {})

def upsert_hook(event: str, matcher: str, command: str) -> None:
    hooks = settings["hooks"].setdefault(event, [])
    # Remove any existing helioy-bus entry for this event/matcher
    settings["hooks"][event] = [
        h for h in hooks
        if not (
            h.get("matcher") == matcher and
            any("helioy-bus" in str(cmd) or "bus-" in str(cmd) or "check-mail" in str(cmd)
                for hook in h.get("hooks", [])
                for cmd in [hook.get("command", "")])
        )
    ]
    settings["hooks"][event].append({
        "matcher": matcher,
        "hooks": [{"type": "command", "command": command}],
    })

def upsert_lifecycle_hook(event: str, command: str) -> None:
    hooks = settings["hooks"].setdefault(event, [])
    # Remove existing helioy-bus entries
    settings["hooks"][event] = [
        h for h in hooks
        if not any("helioy-bus" in str(cmd) or "bus-" in str(cmd)
                   for hook in h.get("hooks", [])
                   for cmd in [hook.get("command", "")])
    ]
    settings["hooks"][event].append({
        "hooks": [{"type": "command", "command": command}],
    })

# PreToolUse: check-mail.sh on every common tool use
upsert_hook(
    "PreToolUse",
    "TodoWrite|ToolSearch|WebFetch|WebSearch|Agent|Read|Write|Edit|Glob|Bash",
    f"{hooks_dir}/check-mail.sh",
)

# SessionStart: register agent
upsert_lifecycle_hook("SessionStart", f"{hooks_dir}/bus-register.sh")

# SessionEnd: unregister agent
upsert_lifecycle_hook("SessionEnd", f"{hooks_dir}/bus-unregister.sh")

with open(settings_path, "w") as f:
    json.dump(settings, f, indent=2)
    f.write("\n")

print(f"  configured mcpServers.helioy-bus")
print(f"  configured PreToolUse hook: check-mail.sh")
print(f"  configured SessionStart hook: bus-register.sh")
print(f"  configured SessionEnd hook: bus-unregister.sh")
PYEOF

# Install Python deps
echo "  installing Python dependencies..."
cd "$SCRIPT_DIR" && uv sync --quiet

echo ""
echo "Done. Restart Claude Code to activate helioy-bus."
echo ""
echo "Quick checks:"
echo "  just agents   — show registered agents"
echo "  just inboxes  — show inbox state"
