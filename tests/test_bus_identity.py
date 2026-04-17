"""Identity tests: canonical agent-id contract, _self_agent_id, whoami token_usage."""

from __future__ import annotations


# ── Canonical identity contract ───────────────────────────────────────────────


def test_canonical_agent_id_shape_without_tmux():
    """canonical_agent_id() returns {repo}:{agent_type} when no tmux_target."""
    from server._identity import canonical_agent_id

    assert canonical_agent_id("/tmp/myproject") == "myproject:general"
    assert (
        canonical_agent_id("/tmp/myproject", "backend-engineer")
        == "myproject:backend-engineer"
    )


def test_canonical_agent_id_shape_with_tmux():
    """canonical_agent_id() returns the 4-segment form when tmux_target is set."""
    from server._identity import canonical_agent_id

    assert (
        canonical_agent_id("/tmp/myproject", "general", "7:1.2")
        == "myproject:general:7:1.2"
    )
    assert (
        canonical_agent_id("/tmp/myproject", "backend-engineer", "7:1.2")
        == "myproject:backend-engineer:7:1.2"
    )


def test_canonical_agent_id_normalizes_trailing_slash():
    from server._identity import canonical_agent_id

    assert canonical_agent_id("/tmp/myproject/") == "myproject:general"


def test_canonical_agent_id_empty_cwd_becomes_unknown():
    from server._identity import canonical_agent_id

    assert canonical_agent_id("") == "unknown:general"
    assert canonical_agent_id("/") == "unknown:general"


def test_canonical_agent_id_empty_type_defaults_to_general():
    """Empty agent_type defaults to 'general'; never produce bare repo."""
    from server._identity import canonical_agent_id

    assert canonical_agent_id("/tmp/myproject", "") == "myproject:general"


def test_register_agent_auto_derivation_matches_canonical_helper():
    """register_agent() auto-derivation must produce the exact output of
    canonical_agent_id() for the same inputs. This is the core invariant
    that prevents MCP-registered rows from diverging from hook-registered
    rows for the same live process."""
    import server.bus_server as bm
    from server._identity import canonical_agent_id

    for pwd, agent_type, tmux_target in [
        ("/tmp/proj", "general", ""),
        ("/tmp/proj", "backend-engineer", ""),
        ("/tmp/proj", "general", "7:1.2"),
        ("/tmp/proj", "backend-engineer", "main:0.0"),
        ("/tmp/proj", "voltagent-lang:rust-engineer", "9:3.4"),
    ]:
        expected = canonical_agent_id(pwd, agent_type, tmux_target)
        result = bm.register_agent(
            pwd=pwd, agent_type=agent_type, tmux_target=tmux_target
        )
        assert result["agent_id"] == expected, (
            f"auto-derive for ({pwd}, {agent_type}, {tmux_target}) "
            f"produced {result['agent_id']!r}, expected {expected!r}"
        )
        bm.unregister_agent(expected)


def test_self_agent_id_reads_pid_file(monkeypatch):
    """Fast path: _self_agent_id() reads the PID file written by the
    SessionStart hook and returns its contents verbatim. This is how
    hook-registered identity propagates to MCP self-resolution."""
    import server._db as _db_mod
    from server._identity import _self_agent_id

    agent_id = "myproject:backend-engineer:7:1.2"
    pids_dir = _db_mod.BUS_DIR / "pids"
    pids_dir.mkdir(parents=True, exist_ok=True)
    pid = "99999"
    (pids_dir / pid).write_text(agent_id)
    monkeypatch.setenv("HELIOY_BUS_CLAUDE_PID", pid)

    assert _self_agent_id() == agent_id


def test_self_agent_id_last_resort_uses_canonical_form(monkeypatch, tmp_path):
    """Last resort: with no PID file and no shell resolver available,
    _self_agent_id() still produces the canonical 2-segment shape rather
    than the legacy bare-basename form."""
    import server._identity as _id_mod

    cwd = tmp_path / "fakeproj"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    monkeypatch.delenv("HELIOY_BUS_CLAUDE_PID", raising=False)
    monkeypatch.delenv("HELIOY_BUS_TMUX", raising=False)
    monkeypatch.delenv("HELIOY_AGENT_TYPE", raising=False)
    monkeypatch.delenv("HELIOY_BUS_AGENT_TYPE", raising=False)
    # Disable the shell resolver so we exercise the Python fallback
    monkeypatch.setattr(
        _id_mod, "_RESOLVE_IDENTITY_SH", tmp_path / "does-not-exist.sh"
    )

    assert _id_mod._self_agent_id() == "fakeproj:general"


def test_self_agent_id_last_resort_honors_agent_type_env(monkeypatch, tmp_path):
    """Last resort honors HELIOY_AGENT_TYPE (hook-exported) so a late-booted
    MCP server still agrees with the hook-written identity."""
    import server._identity as _id_mod

    cwd = tmp_path / "myrepo"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    monkeypatch.delenv("HELIOY_BUS_CLAUDE_PID", raising=False)
    monkeypatch.delenv("HELIOY_BUS_TMUX", raising=False)
    monkeypatch.setenv("HELIOY_AGENT_TYPE", "backend-engineer")
    monkeypatch.setattr(
        _id_mod, "_RESOLVE_IDENTITY_SH", tmp_path / "does-not-exist.sh"
    )

    assert _id_mod._self_agent_id() == "myrepo:backend-engineer"


# ── Token tracking: whoami includes token_usage ──────────────────────────────


def test_whoami_includes_token_usage(monkeypatch):
    """whoami returns parsed token_usage (simplified format)."""
    from server._db import db

    import server.bus_server as bm

    token_data = '{"tokens": 20000, "updated": "2026-03-17T08:17:51Z"}'
    bm.register_agent(pwd="/tmp/myproj", agent_id="myproj")
    with db() as conn:
        conn.execute(
            "UPDATE agents SET token_usage = ? WHERE agent_id = 'myproj'",
            (token_data,),
        )

    monkeypatch.setattr(bm, "_self_agent_id", lambda: "myproj")
    result = bm.whoami()
    assert isinstance(result["token_usage"], dict)
    assert result["token_usage"]["tokens"] == 20000
