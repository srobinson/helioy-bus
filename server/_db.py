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
        CREATE TABLE IF NOT EXISTS warroom_members (
            warroom_member_id TEXT PRIMARY KEY,
            warroom_id        TEXT NOT NULL REFERENCES warrooms(warroom_id) ON DELETE CASCADE,
            desired_runtime   TEXT NOT NULL,
            desired_role      TEXT NOT NULL,
            desired_repo      TEXT,
            state             TEXT NOT NULL DEFAULT 'pending',
            agent_instance_id TEXT,
            spawn_order       INTEGER NOT NULL,
            tmux_target       TEXT NOT NULL,
            pane_id           TEXT NOT NULL,
            created_at        TEXT NOT NULL,
            updated_at        TEXT NOT NULL
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
    # Migration: add warroom layout/runtime_policy/metadata for existing warrooms
    with contextlib.suppress(sqlite3.OperationalError):
        conn.execute("ALTER TABLE warrooms ADD COLUMN layout TEXT NOT NULL DEFAULT 'tiled'")
    with contextlib.suppress(sqlite3.OperationalError):
        conn.execute("ALTER TABLE warrooms ADD COLUMN runtime_policy TEXT")
    with contextlib.suppress(sqlite3.OperationalError):
        conn.execute("ALTER TABLE warrooms ADD COLUMN metadata TEXT")
    _migrate_warroom_members(conn)
    conn.executescript("""
        CREATE INDEX IF NOT EXISTS idx_warroom_members_warroom_order
            ON warroom_members(warroom_id, spawn_order);
        CREATE INDEX IF NOT EXISTS idx_warroom_members_warroom_target
            ON warroom_members(warroom_id, tmux_target);
    """)
    _db_initialized = True


def _migrate_warroom_members(conn: sqlite3.Connection) -> None:
    """Evolve warroom_members to the spec-aligned schema.

    Two prior shapes may exist on disk:

    1. Pre-stable-member-id: (warroom_id, agent_type) composite PK with
       no member_id, no runtime column.
    2. Intermediate stable-member-id: warroom_member_id + runtime/role/repo/
       agent_id/spawned_at column names.

    Both get rebuilt into the canonical shape with desired_runtime,
    desired_role, desired_repo, state, agent_instance_id, created_at,
    and updated_at. SQLite cannot rename multiple columns or drop NOT NULL
    constraints in place, so we copy via a legacy-named temp table.
    """
    cols = {
        row["name"]
        for row in conn.execute("PRAGMA table_info(warroom_members)").fetchall()
    }
    if not cols or "desired_role" in cols:
        return

    conn.executescript("""
        ALTER TABLE warroom_members RENAME TO warroom_members_legacy;
        CREATE TABLE warroom_members (
            warroom_member_id TEXT PRIMARY KEY,
            warroom_id        TEXT NOT NULL REFERENCES warrooms(warroom_id) ON DELETE CASCADE,
            desired_runtime   TEXT NOT NULL,
            desired_role      TEXT NOT NULL,
            desired_repo      TEXT,
            state             TEXT NOT NULL DEFAULT 'pending',
            agent_instance_id TEXT,
            spawn_order       INTEGER NOT NULL,
            tmux_target       TEXT NOT NULL,
            pane_id           TEXT NOT NULL,
            created_at        TEXT NOT NULL,
            updated_at        TEXT NOT NULL
        );
    """)

    if "agent_type" in cols:
        # Pre-stable-id shape. Pre-multi-runtime rows were factually Claude.
        conn.executescript("""
            INSERT INTO warroom_members (
                warroom_member_id, warroom_id, desired_runtime, desired_role,
                desired_repo, state, agent_instance_id, spawn_order,
                tmux_target, pane_id, created_at, updated_at
            )
            SELECT lower(hex(randomblob(8))),
                   warroom_id, 'claude', agent_type, NULL,
                   CASE WHEN agent_id IS NULL THEN 'pending' ELSE 'active' END,
                   agent_id,
                   ROW_NUMBER() OVER (
                       PARTITION BY warroom_id ORDER BY spawned_at, rowid
                   ) - 1,
                   tmux_target, pane_id, spawned_at, spawned_at
            FROM warroom_members_legacy;
        """)
    else:
        # Intermediate shape: stable member_id already present, implementation-named columns.
        conn.executescript("""
            INSERT INTO warroom_members (
                warroom_member_id, warroom_id, desired_runtime, desired_role,
                desired_repo, state, agent_instance_id, spawn_order,
                tmux_target, pane_id, created_at, updated_at
            )
            SELECT warroom_member_id, warroom_id, runtime, role, repo,
                   CASE WHEN agent_id IS NULL THEN 'pending' ELSE 'active' END,
                   agent_id, spawn_order, tmux_target, pane_id,
                   spawned_at, spawned_at
            FROM warroom_members_legacy;
        """)

    conn.execute("DROP TABLE warroom_members_legacy")


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
