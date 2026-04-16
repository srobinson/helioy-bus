"""Shared database layer, path constants, and logging for helioy-bus."""

from __future__ import annotations

import contextlib
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

# ── Paths ─────────────────────────────────────────────────────────────────────

BUS_DIR = Path.home() / ".helioy" / "bus"
REGISTRY_DB = BUS_DIR / "registry.db"
INBOX_DIR = BUS_DIR / "inbox"
PRESETS_DIR = BUS_DIR / "presets"

LOG_FILE = Path("/tmp/helioy-bus-debug.log")


# ── Logging ───────────────────────────────────────────────────────────────────


def _dbg(msg: str) -> None:
    ts = datetime.now(UTC).isoformat(timespec="seconds")
    with LOG_FILE.open("a") as f:
        f.write(f"[{ts}] {msg}\n")


# ── Database ──────────────────────────────────────────────────────────────────

_db_initialized = False


def _init_db(conn: sqlite3.Connection) -> None:
    global _db_initialized
    if _db_initialized:
        return
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
            warroom_member_id TEXT PRIMARY KEY,
            warroom_id        TEXT NOT NULL REFERENCES warrooms(warroom_id) ON DELETE CASCADE,
            runtime           TEXT NOT NULL,
            role              TEXT NOT NULL,
            repo              TEXT,
            spawn_order       INTEGER NOT NULL,
            tmux_target       TEXT NOT NULL,
            pane_id           TEXT NOT NULL,
            agent_id          TEXT,
            spawned_at        TEXT NOT NULL
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
    _migrate_warroom_members(conn)
    conn.executescript("""
        CREATE INDEX IF NOT EXISTS idx_warroom_members_warroom_order
            ON warroom_members(warroom_id, spawn_order);
        CREATE INDEX IF NOT EXISTS idx_warroom_members_warroom_target
            ON warroom_members(warroom_id, tmux_target);
    """)
    _db_initialized = True


def _migrate_warroom_members(conn: sqlite3.Connection) -> None:
    """Rebuild warroom_members from the legacy (warroom_id, agent_type) PK schema.

    SQLite cannot alter primary keys in place, so an old DB requires a
    table-rename-copy-drop. Detection: if `warroom_member_id` is missing,
    the table is the legacy shape.
    """
    cols = {
        row["name"]
        for row in conn.execute("PRAGMA table_info(warroom_members)").fetchall()
    }
    if not cols or "warroom_member_id" in cols:
        return
    conn.executescript("""
        ALTER TABLE warroom_members RENAME TO warroom_members_legacy;
        CREATE TABLE warroom_members (
            warroom_member_id TEXT PRIMARY KEY,
            warroom_id        TEXT NOT NULL REFERENCES warrooms(warroom_id) ON DELETE CASCADE,
            runtime           TEXT NOT NULL,
            role              TEXT NOT NULL,
            repo              TEXT,
            spawn_order       INTEGER NOT NULL,
            tmux_target       TEXT NOT NULL,
            pane_id           TEXT NOT NULL,
            agent_id          TEXT,
            spawned_at        TEXT NOT NULL
        );
        INSERT INTO warroom_members (
            warroom_member_id, warroom_id, runtime, role, repo,
            spawn_order, tmux_target, pane_id, agent_id, spawned_at
        )
        SELECT lower(hex(randomblob(8))),
               warroom_id, 'claude', agent_type, NULL,
               ROW_NUMBER() OVER (
                   PARTITION BY warroom_id ORDER BY spawned_at, rowid
               ) - 1,
               tmux_target, pane_id, agent_id, spawned_at
        FROM warroom_members_legacy;
        DROP TABLE warroom_members_legacy;
    """)


@contextmanager
def db():
    BUS_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(REGISTRY_DB), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
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


def _new_member_id() -> str:
    """Generate a stable warroom_member_id (16 hex chars)."""
    return secrets.token_hex(8)


def _initdb_cli() -> None:
    """CLI entry point: open db() to bootstrap schema, then exit.

    Used by shell hooks to initialize the database without duplicating DDL.
    """
    with db():
        pass
