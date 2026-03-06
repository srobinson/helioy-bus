"""Print inbox message counts for all agents."""
import glob
import os
from pathlib import Path

inbox_base = Path.home() / ".helioy" / "bus" / "inbox"

if not inbox_base.is_dir():
    print("no inboxes")
else:
    agents = sorted(p.name for p in inbox_base.iterdir() if p.is_dir())
    if not agents:
        print("no inboxes")
    else:
        for agent in agents:
            msgs = len(list((inbox_base / agent).glob("*.json")))
            archived = len(list((inbox_base / agent / "archive").glob("*.json")))
            print(f"  {agent:20s}  unread={msgs}  archived={archived}")
