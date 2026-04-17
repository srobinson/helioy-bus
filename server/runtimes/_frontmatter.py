"""Shared YAML frontmatter parser for runtime agent/skill catalogues.

Runtime adapters share one constraint: their catalogue files are markdown
with a leading ``---``-delimited frontmatter block of scalar key/value
pairs. Each adapter imports this helper so the parser lives in one place
and cannot drift.
"""

from __future__ import annotations

import re
from pathlib import Path


def _parse_frontmatter(path: Path) -> dict | None:
    """Extract scalar frontmatter fields from a markdown agent or skill file.

    Uses regex to avoid a pyyaml dependency. Returns ``None`` if the file
    has no valid frontmatter block. Nested keys (dict/list values) are
    skipped; only top-level scalar keys are returned.
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    m = re.match(r"^---\s*\n(.*?)\n---", text, re.DOTALL)
    if not m:
        return None
    block = m.group(1)
    result: dict[str, str] = {}
    for line in block.splitlines():
        kv = re.match(r'^(\w[\w-]*)\s*:\s*"?([^"\n]+?)"?\s*$', line)
        if kv:
            result[kv.group(1)] = kv.group(2).strip()
    return result if result else None
