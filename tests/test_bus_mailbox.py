"""Mailbox tests: send/get/broadcast/atomic write, nudge suppression, role-based addressing."""

from __future__ import annotations

import json
from unittest.mock import patch

from server._tmux import gateway
from server.services import message as message_svc


# ── Mailbox ───────────────────────────────────────────────────────────────────


def test_send_message_to_registered_agent(set_sender):
    import server.bus_server as bm

    bm.register_agent(pwd="/tmp/beta")
    set_sender("alpha")
    result = bm.send_message(to="beta:general", content="hello", nudge=False)
    assert result["delivered"] is True
    assert "beta:general" in result["recipients"]


def test_send_message_writes_json_file(set_sender):
    import server._db as _db_mod
    import server.bus_server as bm

    bm.register_agent(pwd="/tmp/target")
    set_sender("src")
    bm.send_message(to="target:general", content="payload", nudge=False)

    inbox = _db_mod.INBOX_DIR / "target:general"
    files = list(inbox.glob("*.json"))
    assert len(files) == 1
    data = json.loads(files[0].read_text())
    assert data["content"] == "payload"
    assert data["from"] == "src"


def test_send_message_atomic_write(set_sender):
    """No .tmp files left after a successful send."""
    import server._db as _db_mod
    import server.bus_server as bm

    bm.register_agent(pwd="/tmp/target")
    set_sender("y")
    bm.send_message(to="target:general", content="x", nudge=False)

    inbox = _db_mod.INBOX_DIR / "target:general"
    tmp_files = list(inbox.glob("*.tmp"))
    assert tmp_files == []


def test_send_message_recipient_not_found(set_sender):
    import server.bus_server as bm

    set_sender("me")
    result = bm.send_message(to="ghost", content="hi", nudge=False)
    assert result["delivered"] is False
    assert "error" in result


def test_send_message_broadcast(set_sender):
    import server.bus_server as bm

    bm.register_agent(pwd="/tmp/a")
    bm.register_agent(pwd="/tmp/b")
    set_sender("sender")
    result = bm.send_message(to="*", content="hello all", nudge=False)
    assert set(result["recipients"]) == {"a:general", "b:general"}


def test_get_messages_returns_and_archives(set_sender):
    import server._db as _db_mod
    import server.bus_server as bm

    bm.register_agent(pwd="/tmp/reader")
    set_sender("w")
    bm.send_message(to="reader:general", content="msg1", nudge=False)
    bm.send_message(to="reader:general", content="msg2", nudge=False)

    messages = bm.get_messages("reader:general")
    assert len(messages) == 2
    assert messages[0]["content"] == "msg1"
    assert messages[1]["content"] == "msg2"

    # Messages archived
    inbox = _db_mod.INBOX_DIR / "reader:general"
    assert list(inbox.glob("*.json")) == []
    assert len(list((inbox / "archive").glob("*.json"))) == 2


def test_get_messages_empty_inbox():
    import server.bus_server as bm

    bm.register_agent(pwd="/tmp/empty")
    messages = bm.get_messages("empty:general")
    assert messages == []


def test_get_messages_idempotent(set_sender):
    import server.bus_server as bm

    bm.register_agent(pwd="/tmp/reader")
    set_sender("w")
    bm.send_message(to="reader:general", content="once", nudge=False)

    first = bm.get_messages("reader:general")
    assert len(first) == 1

    second = bm.get_messages("reader:general")
    assert second == []


# ── Nudge behavior ───────────────────────────────────────────────────────────


def test_send_message_nudge_skips_dead_pane(set_sender):
    import server.bus_server as bm

    bm.register_agent(pwd="/tmp/dead", tmux_target="main:9.9", agent_id="dead:main:9.9")
    set_sender("nudger")
    with patch.object(gateway, "pane_alive", return_value=False):
        result = bm.send_message(to="dead:main:9.9", content="wake up")
    assert result["delivered"] is True
    assert result["nudged"] is False


def test_send_message_nudge_suppressed_with_flag(set_sender):
    import server.bus_server as bm

    bm.register_agent(pwd="/tmp/quiet", tmux_target="main:0.0", agent_id="quiet:main:0.0")
    set_sender("sender")
    with (
        patch.object(gateway, "pane_alive", return_value=True),
        patch.object(gateway, "nudge", return_value=True) as mock_nudge,
    ):
        result = bm.send_message(to="quiet:main:0.0", content="shh", nudge=False)
    assert result["nudged"] is False
    mock_nudge.assert_not_called()


def test_send_message_nudges_live_pane(set_sender):
    import server.bus_server as bm

    bm.register_agent(pwd="/tmp/live", tmux_target="main:0.0", agent_id="live:main:0.0")
    set_sender("sender")
    with (
        patch.object(gateway, "pane_alive", return_value=True),
        patch.object(gateway, "nudge", return_value=True),
        patch.object(message_svc, "_nudge_allowed", return_value=True),
    ):
        result = bm.send_message(to="live:main:0.0", content="ping")
    assert result["nudged"] is True


def test_send_message_skips_codex_nudge(set_sender, monkeypatch):
    import server.bus_server as bm

    monkeypatch.setenv("HELIOY_RUNTIME", "codex")
    bm.register_agent(pwd="/tmp/codex", tmux_target="main:0.0", agent_id="codex")
    set_sender("sender")
    with (
        patch.object(gateway, "pane_alive") as mock_alive,
        patch.object(gateway, "nudge") as mock_nudge,
    ):
        result = bm.send_message(to="codex", content="ping")
    assert result["delivered"] is True
    assert result["nudged"] is False
    mock_alive.assert_not_called()
    mock_nudge.assert_not_called()


# ── Role-based messaging ─────────────────────────────────────────────────────


def test_send_message_role_addressing_delivers_to_matching_agents(set_sender):
    import server.bus_server as bm

    bm.register_agent(pwd="/tmp/be1", agent_id="be1", agent_type="backend-engineer")
    bm.register_agent(pwd="/tmp/be2", agent_id="be2", agent_type="backend-engineer")
    bm.register_agent(pwd="/tmp/fe", agent_id="fe", agent_type="frontend-engineer")

    set_sender("orch")
    result = bm.send_message(
        to="role:backend-engineer", content="build it", nudge=False
    )
    assert result["delivered"] is True
    assert set(result["recipients"]) == {"be1", "be2"}


def test_send_message_role_addressing_excludes_sender(set_sender):
    import server.bus_server as bm

    bm.register_agent(pwd="/tmp/be", agent_id="be", agent_type="backend-engineer")
    set_sender("be")
    result = bm.send_message(
        to="role:backend-engineer", content="self", nudge=False
    )
    assert result["delivered"] is False


def test_send_message_role_not_found_returns_error(set_sender):
    import server.bus_server as bm

    bm.register_agent(pwd="/tmp/x", agent_id="x", agent_type="general")
    set_sender("y")
    result = bm.send_message(
        to="role:nonexistent", content="x", nudge=False
    )
    assert result["delivered"] is False
    assert "error" in result


def test_send_message_role_creates_inbox_files(set_sender):
    import server._db as _db_mod
    import server.bus_server as bm

    bm.register_agent(pwd="/tmp/be", agent_id="be", agent_type="backend-engineer")
    bm.register_agent(pwd="/tmp/fe", agent_id="fe", agent_type="frontend-engineer")

    set_sender("orch")
    bm.send_message(
        to="role:backend-engineer", content="task", nudge=False
    )

    be_inbox = _db_mod.INBOX_DIR / "be"
    fe_inbox = _db_mod.INBOX_DIR / "fe"
    assert len(list(be_inbox.glob("*.json"))) == 1
    # frontend-engineer should not receive the message
    fe_files = list(fe_inbox.glob("*.json")) if fe_inbox.exists() else []
    assert fe_files == []
