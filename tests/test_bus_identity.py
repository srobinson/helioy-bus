"""Identity tests: canonical agent-id contract, _self_agent_id, whoami token_usage."""

from __future__ import annotations


def _seed_agent(
    agent_id: str,
    *,
    tmux_target: str,
    pane_id: str,
    cwd: str = "/tmp/proj",
    agent_type: str = "general",
    pid: int = 1,
) -> None:
    from server._db import _now, db

    now = _now()
    with db() as conn:
        conn.execute(
            "INSERT INTO agents (agent_id, cwd, tmux_target, pane_id, pid, session_id, "
            "agent_type, runtime, registered_at, last_seen) "
            "VALUES (?, ?, ?, ?, ?, '', ?, 'claude', ?, ?)",
            (agent_id, cwd, tmux_target, pane_id, pid, agent_type, now, now),
        )


def _agent_row(agent_id: str):
    from server._db import db

    with db() as conn:
        return conn.execute(
            "SELECT * FROM agents WHERE agent_id = ?", (agent_id,)
        ).fetchone()


# ── Registration eviction scoping (window re-indexing safety) ─────────────────
# A killed window re-indexes survivors, so a live agent's recorded
# tmux_target can be claimed by a different pane's fresh registration.
# Eviction must key on the stable pane_id; the address arm is only for
# legacy rows without one. Reproduced live 2026-07-09: the old
# target-based arm deleted live survivors.


def test_register_eviction_spares_live_row_with_drifted_target():
    """A pane_id-carrying row whose stale target collides with a newcomer's
    fresh address is a different, live pane. It must survive."""
    from server.services import agent_registry

    _seed_agent("proj:general:6:3.1", tmux_target="6:2.1", pane_id="%10")

    agent_registry.register(
        pwd="/tmp/proj", tmux_target="6:2.1", agent_id="proj:general:6:2.1",
        session_id="", agent_type="general", pane_id="%11", profile=None,
    )

    assert _agent_row("proj:general:6:3.1") is not None, "live survivor was evicted"
    assert _agent_row("proj:general:6:2.1") is not None


def test_register_eviction_evicts_prior_occupant_of_same_pane():
    """Same pane, different identity: the previous occupant row is stale
    by definition and must be evicted (ownership assertion)."""
    from server.services import agent_registry

    _seed_agent("proj:general:6:3.1", tmux_target="6:3.1", pane_id="%11")

    agent_registry.register(
        pwd="/tmp/proj", tmux_target="6:2.1", agent_id="proj:reviewer:6:2.1",
        session_id="", agent_type="reviewer", pane_id="%11", profile=None,
    )

    assert _agent_row("proj:general:6:3.1") is None
    assert _agent_row("proj:reviewer:6:2.1") is not None


def test_register_eviction_evicts_legacy_row_claiming_target():
    """Rows without pane_id have no stable identity: an address claim is
    the only signal, so the new occupant of that address wins."""
    from server.services import agent_registry

    _seed_agent("proj:general:6:2.1", tmux_target="6:2.1", pane_id="")

    agent_registry.register(
        pwd="/tmp/proj", tmux_target="6:2.1", agent_id="proj:reviewer:6:2.1",
        session_id="", agent_type="reviewer", pane_id="%12", profile=None,
    )

    assert _agent_row("proj:general:6:2.1") is None
    assert _agent_row("proj:reviewer:6:2.1") is not None


# ── Identity continuity (fallback ids reuse the pane's registration) ──────────
# After /clear or compaction the TUI has clobbered the canonical pane
# title, so the hook resolver mints an id from the CURRENT address. After
# re-indexing that can be another agent's birth address: registering the
# minted id would silently steal that agent's row (identity takeover,
# reproduced live 2026-07-09). The pane's existing row is the truth.


def test_register_fallback_id_reuses_pane_identity_instead_of_taking_over():
    """The reproduced takeover: w3 re-registers with a fallback id equal to
    w2's birth id. Reuse w3's own identity; leave w2 untouched."""
    from server.services import agent_registry

    _seed_agent("proj:general:6:3.1", tmux_target="6:2.1", pane_id="%20")  # w2 (healed)
    _seed_agent("proj:general:6:4.1", tmux_target="6:4.1", pane_id="%21")  # w3 (stale)

    # w3's SessionStart re-fires at its new address 6:3.1 and mints w2's id.
    result = agent_registry.register(
        pwd="/tmp/proj", tmux_target="6:3.1", agent_id="proj:general:6:3.1",
        session_id="", agent_type="general", pane_id="%21", profile=None,
        id_source="fallback",
    )

    assert result["agent_id"] == "proj:general:6:4.1", "w3 must keep its birth id"
    w3 = _agent_row("proj:general:6:4.1")
    assert w3 is not None and w3["tmux_target"] == "6:3.1"
    w2 = _agent_row("proj:general:6:3.1")
    assert w2 is not None and w2["pane_id"] == "%20", "w2's row was taken over"


def test_register_empty_agent_id_reuses_pane_identity_and_type():
    """MCP path: no explicit id resolves to the pane's existing identity,
    preserving a specialist agent_type across re-registration."""
    from server.services import agent_registry

    _seed_agent(
        "proj:backend-engineer:6:3.1", tmux_target="6:3.1", pane_id="%30",
        agent_type="backend-engineer",
    )

    result = agent_registry.register(
        pwd="/tmp/proj", tmux_target="6:2.1", agent_id="",
        session_id="", agent_type="general", pane_id="%30", profile=None,
    )

    assert result["agent_id"] == "proj:backend-engineer:6:3.1"
    row = _agent_row("proj:backend-engineer:6:3.1")
    assert row["agent_type"] == "backend-engineer"
    assert row["tmux_target"] == "6:2.1"


def test_register_fallback_id_reuses_identity_when_same_process_changes_cwd():
    """Codex memories hooks run under ~/.codex/memories. The same runtime
    process must keep the repository identity and repository cwd registered
    by its initial SessionStart hook."""
    from server.services import agent_registry

    _seed_agent(
        "transport-matters:general:1:4.1",
        tmux_target="1:4.1",
        pane_id="%432",
        cwd="/work/transport-matters",
        pid=42036,
    )

    result = agent_registry.register(
        pwd="/Users/alphab/.codex/memories",
        tmux_target="1:4.1",
        agent_id="memories:general:1:4.1",
        session_id="same-session",
        agent_type="general",
        runtime="codex",
        pane_id="%432",
        profile=None,
        pid=42036,
        id_source="fallback",
    )

    assert result["agent_id"] == "transport-matters:general:1:4.1"
    row = _agent_row("transport-matters:general:1:4.1")
    assert row is not None
    assert row["cwd"] == "/work/transport-matters"
    assert _agent_row("memories:general:1:4.1") is None


def test_register_fallback_id_replaces_different_process_in_same_pane():
    """A new runtime process may reuse a pane for a different project."""
    from server.services import agent_registry

    _seed_agent(
        "transport-matters:general:1:4.1",
        tmux_target="1:4.1",
        pane_id="%432",
        cwd="/work/transport-matters",
        pid=42036,
    )

    result = agent_registry.register(
        pwd="/work/cubicell",
        tmux_target="1:4.1",
        agent_id="cubicell:general:1:4.1",
        session_id="new-session",
        agent_type="general",
        runtime="codex",
        pane_id="%432",
        profile=None,
        pid=42037,
        id_source="fallback",
    )

    assert result["agent_id"] == "cubicell:general:1:4.1"
    assert _agent_row("transport-matters:general:1:4.1") is None
    assert _agent_row("cubicell:general:1:4.1")["cwd"] == "/work/cubicell"


def test_register_disambiguates_when_id_claimed_by_live_pane(monkeypatch):
    """Address-derived ids are not unique over time: a fresh registration
    whose id equals a LIVE agent's birth id must never REPLACE that row.
    It takes a pane-suffixed id instead; the claimant is untouched."""
    from server._tmux import gateway
    from server.services import agent_registry

    _seed_agent("proj:general:6:3.1", tmux_target="6:2.1", pane_id="%20")
    monkeypatch.setattr(gateway, "pane_alive", lambda target: True)

    result = agent_registry.register(
        pwd="/tmp/proj", tmux_target="6:3.1", agent_id="proj:general:6:3.1",
        session_id="", agent_type="general", pane_id="%50", profile=None,
    )

    assert result["agent_id"] == "proj:general:6:3.1:%50"
    claimant = _agent_row("proj:general:6:3.1")
    assert claimant is not None and claimant["pane_id"] == "%20", "claimant row stolen"
    assert _agent_row("proj:general:6:3.1:%50")["pane_id"] == "%50"


def test_register_reclaims_id_from_dead_pane(monkeypatch):
    """A claimant whose pane is gone is a leak: evict it and keep the id."""
    from server._tmux import gateway
    from server.services import agent_registry

    _seed_agent("proj:general:6:3.1", tmux_target="6:3.1", pane_id="%20")
    monkeypatch.setattr(gateway, "pane_alive", lambda target: False)

    result = agent_registry.register(
        pwd="/tmp/proj", tmux_target="6:3.1", agent_id="proj:general:6:3.1",
        session_id="", agent_type="general", pane_id="%50", profile=None,
    )

    assert result["agent_id"] == "proj:general:6:3.1"
    assert _agent_row("proj:general:6:3.1")["pane_id"] == "%50"


def test_register_fallback_id_ignores_pane_row_from_other_project():
    """A pane relaunched from a different cwd is a new agent, not a
    continuation: no reuse across projects."""
    from server.services import agent_registry

    _seed_agent("other:general:6:3.1", tmux_target="6:3.1", pane_id="%40", cwd="/tmp/other")

    result = agent_registry.register(
        pwd="/tmp/proj", tmux_target="6:3.1", agent_id="proj:general:6:3.1",
        session_id="", agent_type="general", pane_id="%40", profile=None,
        id_source="fallback",
    )

    assert result["agent_id"] == "proj:general:6:3.1"


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


def test_self_agent_id_registry_lookup_by_tmux(monkeypatch, tmp_path):
    """Registry path: when no PID file matches, look up the agent by the
    caller's tmux_target. Covers runtimes (Codex) whose host PID is not
    knowable at hook-run time, so the PID file can never be keyed to it."""
    import server._identity as _id_mod
    from server._db import db

    # HELIOY_BUS_TMUX already carries the target; no need to shell out to tmux.
    tmux_target = "6:1.2"
    registered_id = "manicure:general:6:1.2"
    with db() as conn:
        conn.execute(
            """
            INSERT INTO agents
                (agent_id, cwd, tmux_target, pid, session_id,
                 agent_type, runtime, registered_at, last_seen)
            VALUES (?, ?, ?, ?, '', 'general', 'codex', '2026-04-17', '2026-04-17')
            """,
            (registered_id, "/Users/alphab/Dev/LLM/DEV/helioy/manicure", tmux_target, 99999),
        )
    monkeypatch.delenv("HELIOY_BUS_CLAUDE_PID", raising=False)
    monkeypatch.delenv("HELIOY_BUS_CODEX_PID", raising=False)
    monkeypatch.setenv("HELIOY_BUS_TMUX", tmux_target)
    # Disable the shell resolver so we prove the registry path wins,
    # not a downstream fallback.
    monkeypatch.setattr(
        _id_mod, "_RESOLVE_IDENTITY_SH", tmp_path / "does-not-exist.sh"
    )

    assert _id_mod._self_agent_id() == registered_id


def test_self_agent_id_registry_lookup_by_pid_ancestry(monkeypatch, tmp_path):
    """Registry pid-ancestry tier: when the caller has no tmux env (Codex
    strips env before spawning MCP subprocesses), walk up the process tree
    and match ancestor pids against the agents table. This is the path
    that carries Codex when the tmux_target tier silently misses."""
    import server._identity as _id_mod
    from server._db import db

    registered_id = "manicure:general:6:1.2"
    wrapper_pid = 77777
    with db() as conn:
        conn.execute(
            """
            INSERT INTO agents
                (agent_id, cwd, tmux_target, pid, session_id,
                 agent_type, runtime, registered_at, last_seen)
            VALUES (?, ?, '6:1.2', ?, '', 'general', 'codex',
                    '2026-04-17', '2026-04-17')
            """,
            (registered_id, "/Users/alphab/Dev/LLM/DEV/helioy/manicure", wrapper_pid),
        )
    monkeypatch.delenv("HELIOY_BUS_CLAUDE_PID", raising=False)
    monkeypatch.delenv("HELIOY_BUS_CODEX_PID", raising=False)
    monkeypatch.delenv("HELIOY_BUS_TMUX", raising=False)
    monkeypatch.delenv("TMUX_PANE", raising=False)
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.setattr(
        _id_mod, "_RESOLVE_IDENTITY_SH", tmp_path / "does-not-exist.sh"
    )

    # Fake a three-hop ancestor chain: self → mcp parent → wrapper pid.
    chain = {1000: 2000, 2000: 3000, 3000: wrapper_pid, wrapper_pid: 1}
    monkeypatch.setattr(_id_mod.os, "getpid", lambda: 1000)
    monkeypatch.setattr(_id_mod, "_parent_pid", lambda pid: chain.get(pid, 0))

    assert _id_mod._self_agent_id() == registered_id


def test_self_agent_id_registry_lookup_missing_falls_through(monkeypatch, tmp_path):
    """Registry path is best-effort: a tmux_target with no matching row
    falls through to the remaining tiers rather than short-circuiting."""
    import server._identity as _id_mod

    cwd = tmp_path / "orphan"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    monkeypatch.delenv("HELIOY_BUS_CLAUDE_PID", raising=False)
    monkeypatch.delenv("HELIOY_BUS_CODEX_PID", raising=False)
    monkeypatch.setenv("HELIOY_BUS_TMUX", "9:9.9")
    monkeypatch.setattr(
        _id_mod, "_RESOLVE_IDENTITY_SH", tmp_path / "does-not-exist.sh"
    )
    # Short-circuit the pid-ancestry walk so a stray ancestor pid doesn't
    # happen to match the test registry. Registry is empty here anyway,
    # but stubbing keeps the test pure (no ps subprocess).
    monkeypatch.setattr(_id_mod, "_parent_pid", lambda _pid: 0)

    # Registry has no row for 9:9.9 and pid-ancestry is stubbed empty,
    # so last-resort canonical form takes over and honors the tmux_target
    # env rather than returning a bare basename.
    assert _id_mod._self_agent_id() == "orphan:general:9:9.9"


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
