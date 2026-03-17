"""Shared database layer, path constants, and logging for helioy-bus."""

from __future__ import annotations

import contextlib
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

# ── Paths ─────────────────────────────────────────────────────────────────────

BUS_DIR = Path.home() / ".helioy" / "bus"
REGISTRY_DB = BUS_DIR / "registry.db"
INBOX_DIR = BUS_DIR / "inbox"
PRESETS_DIR = BUS_DIR / "presets"
PLUGINS_CACHE = Path.home() / ".claude" / "plugins" / "cache"

LOG_FILE = Path("/tmp/helioy-bus-debug.log")


# ── Logging ───────────────────────────────────────────────────────────────────


def _dbg(msg: str) -> None:
    from datetime import datetime as _dt

    ts = _dt.now().isoformat(timespec="seconds")
    with LOG_FILE.open("a") as f:
        f.write(f"[{ts}] {msg}\n")


# ── Database ──────────────────────────────────────────────────────────────────


def _init_db(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        PRAGMA journal_mode=WAL;
        PRAGMA synchronous=NORMAL;
        CREATE TABLE IF NOT EXISTS agents (
            agent_id      TEXT PRIMARY KEY,
            cwd           TEXT NOT NULL,
            tmux_target   TEXT NOT NULL DEFAULT '',
            pid           INTEGER,
            session_id    TEXT NOT NULL DEFAULT '',
            agent_type    TEXT NOT NULL DEFAULT 'general',
            profile       TEXT,
            registered_at TEXT NOT NULL,
            last_seen     TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS nudge_log (
            agent_id  TEXT NOT NULL,
            nudged_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_nudge_log_agent ON nudge_log(agent_id, nudged_at);
        CREATE TABLE IF NOT EXISTS warrooms (
            warroom_id   TEXT PRIMARY KEY,
            tmux_session TEXT NOT NULL,
            tmux_window  TEXT NOT NULL,
            cwd          TEXT NOT NULL,
            created_at   TEXT NOT NULL,
            status       TEXT NOT NULL DEFAULT 'active'
        );
        CREATE TABLE IF NOT EXISTS warroom_members (
            warroom_id   TEXT NOT NULL REFERENCES warrooms(warroom_id) ON DELETE CASCADE,
            agent_type   TEXT NOT NULL,
            tmux_target  TEXT NOT NULL,
            pane_id      TEXT NOT NULL,
            agent_id     TEXT,
            spawned_at   TEXT NOT NULL,
            PRIMARY KEY (warroom_id, agent_type)
        );
    """)
    # Migration: add session_id column for existing databases
    with contextlib.suppress(sqlite3.OperationalError):
        conn.execute("ALTER TABLE agents ADD COLUMN session_id TEXT NOT NULL DEFAULT ''")
    # Migration: add agent_type column for existing databases
    with contextlib.suppress(sqlite3.OperationalError):
        conn.execute("ALTER TABLE agents ADD COLUMN agent_type TEXT NOT NULL DEFAULT 'general'")
    # Migration: add profile column for existing databases (nullable, no default)
    with contextlib.suppress(sqlite3.OperationalError):
        conn.execute("ALTER TABLE agents ADD COLUMN profile TEXT")
    # Migration: add token_usage column for token tracking
    with contextlib.suppress(sqlite3.OperationalError):
        conn.execute("ALTER TABLE agents ADD COLUMN token_usage TEXT NOT NULL DEFAULT '{}'")


@contextmanager
def db():
    BUS_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(REGISTRY_DB), timeout=10)
    conn.row_factory = sqlite3.Row
    _init_db(conn)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _now() -> str:
    return datetime.now(UTC).isoformat()
