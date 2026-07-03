"""Message delivery and nudge orchestration.

Pure application service. Recipients are resolved from the agents table,
inbox files are written atomically, and tmux nudges are throttled per
recipient. All shell side effects go through the `gateway` singleton.

Archive TTL pruning is NOT performed during read; call
`reconciliation.prune_archived_messages()` to evict aged entries.
"""

from __future__ import annotations

import json
import os
import tempfile
import uuid
from datetime import UTC, datetime, timedelta

from server import _db
from server._tmux import gateway
from server.runtimes import for_id

NUDGE_THROTTLE_SECONDS = 30

# Runtimes whose TUI honors a tmux send-keys nudge. Unknown-runtime rows
# predate multi-runtime bookkeeping and are not safe to nudge. Any new
# runtime must be validated (hex-0d submit + literal text) before being
# added here. Grok validated live 2026-07-02: the three-step submit
# lands and the turn completes. claude-opus runs the same claude TUI,
# so claude's validation covers it.
_NUDGEABLE_RUNTIMES = frozenset({"claude", "claude-opus", "codex", "grok", "grok-fast"})


# ── Nudge throttling (data-layer policy, not tmux concern) ───────────────────


def _inbox_has_unread(agent_id: str) -> bool:
    """Return True if the agent's inbox contains unread messages."""
    inbox = _db.INBOX_DIR / agent_id
    if not inbox.exists():
        return False
    return bool(list(inbox.glob("*.json")))


def _nudge_allowed(agent_id: str) -> bool:
    """Return True if a nudge should be sent to the agent.

    Allows re-nudging within the throttle window if the inbox still has
    unread messages, meaning the previous nudge did not wake the agent.
    """
    with _db.db() as conn:
        row = conn.execute(
            "SELECT nudged_at FROM nudge_log WHERE agent_id = ? ORDER BY nudged_at DESC LIMIT 1",
            (agent_id,),
        ).fetchone()
        if row is None:
            return True
        last = row["nudged_at"]
        cutoff_dt = datetime.now(UTC) - timedelta(seconds=NUDGE_THROTTLE_SECONDS)
        if last < cutoff_dt.isoformat():
            return True
        if _inbox_has_unread(agent_id):
            _db._dbg(
                f"_nudge_allowed: {agent_id!r} throttled but inbox has unread messages, "
                "allowing re-nudge"
            )
            return True
        return False


def _record_nudge(agent_id: str) -> None:
    with _db.db() as conn:
        conn.execute(
            "INSERT INTO nudge_log (agent_id, nudged_at) VALUES (?, ?)",
            (agent_id, _db._now()),
        )
        conn.execute(
            "DELETE FROM nudge_log WHERE nudged_at < ?",
            ((datetime.now(UTC) - timedelta(hours=24)).isoformat(),),
        )


# ── Service operations ────────────────────────────────────────────────────────


def _suffix_for_runtime(runtime: str) -> str:
    """Return the runtime-specific message suffix, or '' when unknown.

    Unknown-runtime rows (legacy pre-migration data) get no suffix; we
    only know the authorization preamble is correct for runtimes we
    explicitly model.
    """
    if not runtime:
        return ""
    try:
        return for_id(runtime).message_suffix
    except KeyError:
        return ""


def _resolve_single(conn, *, sender_id: str, to: str) -> tuple[list[dict], str | None]:
    """Resolve one address token. Returns (recipients, error_message_or_None)."""
    if to == "*":
        rows = conn.execute(
            "SELECT agent_id, tmux_target, pane_id, runtime FROM agents WHERE agent_id != ?",
            (sender_id,),
        ).fetchall()
        return [dict(r) for r in rows], None
    if to.startswith("role:"):
        role = to[len("role:") :]
        rows = conn.execute(
            "SELECT agent_id, tmux_target, pane_id, runtime FROM agents "
            "WHERE agent_type = ? AND agent_id != ?",
            (role, sender_id),
        ).fetchall()
        recipients = [dict(r) for r in rows]
        if not recipients:
            return [], f"No agents with role '{role}' found in registry"
        return recipients, None
    row = conn.execute(
        "SELECT agent_id, tmux_target, pane_id, runtime FROM agents WHERE agent_id = ?",
        (to,),
    ).fetchone()
    if row is None:
        return [], f"Recipient '{to}' not found in registry"
    return [dict(row)], None


def _resolve_recipients(
    conn, *, sender_id: str, to: str
) -> tuple[list[dict], list[dict], dict | None]:
    """Look up recipients for `to`.

    `to` may be a single address (`*`, `role:X`, or an agent_id) or a
    `;`-separated list of such addresses. Returns
    (recipients, failed_parts, fatal_error_or_None) where `failed_parts`
    is a list of {"to": part, "error": msg} for any address that did not
    resolve. A fatal error is returned only when zero recipients resolved
    across all parts.
    """
    parts = [p.strip() for p in to.split(";") if p.strip()]
    if not parts:
        return (
            [],
            [],
            {
                "message_id": None,
                "delivered": False,
                "nudged": False,
                "recipients": [],
                "error": "Recipient address is empty",
            },
        )

    recipients: list[dict] = []
    seen: set[str] = set()
    failed: list[dict] = []

    for part in parts:
        resolved, err = _resolve_single(conn, sender_id=sender_id, to=part)
        if err:
            failed.append({"to": part, "error": err})
            continue
        for r in resolved:
            if r["agent_id"] in seen:
                continue
            seen.add(r["agent_id"])
            recipients.append(r)

    if not recipients:
        # Preserve original single-recipient error shape when only one
        # address was supplied; otherwise return a combined message.
        if len(parts) == 1:
            error_msg = failed[0]["error"]
        else:
            error_msg = "; ".join(f"{f['to']}: {f['error']}" for f in failed)
        return (
            [],
            failed,
            {
                "message_id": None,
                "delivered": False,
                "nudged": False,
                "recipients": [],
                "error": error_msg,
            },
        )

    return recipients, failed, None


def send(
    *,
    sender_id: str,
    to: str,
    content: str,
    reply_to: str = "",
    topic: str = "",
    nudge: bool = True,
) -> dict:
    """Deliver a message to one or more recipients.

    Sender identity is supplied by the caller. The handler resolves it
    via `_self_agent_id()` and passes it in; the service stays purely
    a delivery role.
    """
    if not reply_to:
        reply_to = sender_id

    with _db.db() as conn:
        recipients, failed, error = _resolve_recipients(conn, sender_id=sender_id, to=to)
    if error:
        return error

    message_id = str(uuid.uuid4())
    now = _db._now()
    nudged_targets: list[str] = []
    delivered_to: list[str] = []

    for recipient in recipients:
        target_id = recipient["agent_id"]
        # Prefer the stable pane_id (%N) for tmux addressing: tmux_target
        # can point at an unrelated pane after window re-indexing.
        tmux_target = recipient.get("pane_id") or recipient.get("tmux_target", "")
        runtime = recipient.get("runtime", "")

        payload = {
            "id": message_id,
            "from": sender_id,
            "to": target_id,
            "reply_to": reply_to,
            "topic": topic or None,
            "content": content + _suffix_for_runtime(runtime),
            "sent_at": now,
        }

        inbox = _db.INBOX_DIR / target_id
        inbox.mkdir(parents=True, exist_ok=True)

        filename = f"{now.replace(':', '-')}_{message_id[:8]}.json"
        _db._dbg(f"send_message: delivering to={target_id!r} inbox={inbox} file={filename}")
        tmp_fd, tmp_path = tempfile.mkstemp(dir=str(inbox), suffix=".tmp")
        try:
            with os.fdopen(tmp_fd, "w") as f:
                json.dump(payload, f, indent=2)
            os.rename(tmp_path, str(inbox / filename))
        except Exception:
            os.unlink(tmp_path)
            raise

        delivered_to.append(target_id)

        if (
            nudge
            and tmux_target
            and runtime in _NUDGEABLE_RUNTIMES
            and _nudge_allowed(target_id)
            and gateway.pane_alive(tmux_target)
            and gateway.nudge(tmux_target, runtime=runtime)
        ):
            nudged_targets.append(target_id)
            _record_nudge(target_id)

    result = {
        "message_id": message_id,
        "delivered": bool(delivered_to),
        "nudged": bool(nudged_targets),
        "recipients": delivered_to,
    }
    if failed:
        result["failed"] = failed
    return result


def nudge_direct(*, sender_id: str, to: str, content: str) -> dict:
    """Send a direct tmux message without writing mailbox files."""
    if not content:
        return {
            "nudged": False,
            "recipients": [],
            "skipped": [],
            "error": "content is required",
        }

    with _db.db() as conn:
        recipients, failed, error = _resolve_recipients(conn, sender_id=sender_id, to=to)
    if error:
        return {
            "nudged": False,
            "recipients": [],
            "skipped": [],
            "error": error["error"],
        }

    nudged_targets: list[str] = []
    skipped: list[dict] = [{"agent_id": f["to"], "reason": f["error"]} for f in failed]

    for recipient in recipients:
        target_id = recipient["agent_id"]
        # Same pane_id-first addressing as send(); see comment there.
        tmux_target = recipient.get("pane_id") or recipient.get("tmux_target", "")
        runtime = recipient.get("runtime", "")

        if not tmux_target:
            skipped.append({"agent_id": target_id, "reason": "no_tmux_target"})
            continue
        if runtime not in _NUDGEABLE_RUNTIMES:
            skipped.append({"agent_id": target_id, "reason": "unsupported_runtime"})
            continue
        if not gateway.pane_alive(tmux_target):
            skipped.append({"agent_id": target_id, "reason": "pane_not_alive"})
            continue
        if gateway.nudge(tmux_target, runtime=runtime, content=content):
            nudged_targets.append(target_id)
            continue
        skipped.append({"agent_id": target_id, "reason": "tmux_send_failed"})

    return {
        "nudged": bool(nudged_targets),
        "recipients": nudged_targets,
        "skipped": skipped,
    }


def read(*, agent_id: str, topic: str = "") -> list[dict]:
    """Return unread messages for the agent, archiving them on read.

    Pure consumption: matching files are renamed into `inbox/archive/`
    so subsequent reads do not see them. Archive TTL pruning is not
    performed here; invoke `reconciliation.prune_archived_messages()`
    to age them out.
    """
    inbox = _db.INBOX_DIR / agent_id
    _db._dbg(
        f"get_messages: agent_id={agent_id!r} topic={topic!r} inbox={inbox} exists={inbox.exists()}"
    )

    if not inbox.exists():
        _db._dbg("get_messages: inbox missing \u2192 []")
        return []

    archive = inbox / "archive"
    archive.mkdir(parents=True, exist_ok=True)

    msg_files = sorted(inbox.glob("*.json"))
    _db._dbg(f"get_messages: found {len(msg_files)} file(s): {[p.name for p in msg_files]}")
    messages: list[dict] = []

    for path in msg_files:
        try:
            data = json.loads(path.read_text())
            if topic and data.get("topic") != topic:
                continue
            messages.append(data)
            path.rename(archive / path.name)
        except (json.JSONDecodeError, OSError):
            continue

    _db._dbg(f"get_messages: returning {len(messages)} message(s)")
    return messages
