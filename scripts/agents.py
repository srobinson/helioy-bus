"""Print registered agents from the helioy-bus registry."""
import os
import sqlite3
from pathlib import Path

db_path = Path.home() / ".helioy" / "bus" / "registry.db"

if not db_path.exists():
    print("no agents registered (registry not found)")
else:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM agents ORDER BY registered_at").fetchall()
    if not rows:
        print("no agents registered")
    else:
        for row in rows:
            d = dict(row)
            print(f"  {d['agent_id']:20s}  cwd={d['cwd']}  tmux={d['tmux_target'] or '—'}")
    conn.close()
