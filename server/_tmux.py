"""tmux integration for helioy-bus.

`TmuxGateway` is the single boundary between application logic and the
tmux binary. All subprocess calls go through it. Handlers and services
depend on the `gateway` singleton; nothing else in the codebase runs
`subprocess.run(["tmux", ...])`.

Nudge throttling policy lives in `services.message` since it is a
data-layer concern about messaging, not about tmux.
"""

from __future__ import annotations

import os
import subprocess

from server import _db
from server.runtimes import default_adapter, for_id

# ── TmuxGateway: the only place that runs subprocess.run(["tmux", ...]) ──────


class TmuxGateway:
    """Boundary between application logic and the tmux binary.

    Every subprocess invocation of tmux goes through `_run` (raises on
    failure) or `_run_silent` (best-effort, returns bool). Public methods
    expose intent-level operations; callers never construct argument
    lists themselves.
    """

    def __init__(self, tmux_binary: str = "tmux", default_timeout: float = 5.0) -> None:
        self._tmux = tmux_binary
        self._default_timeout = default_timeout

    def _run(self, *args: str, timeout: float | None = None) -> str:
        """Run tmux <args>. Returns stdout (str). Raises RuntimeError on failure."""
        wait = timeout if timeout is not None else self._default_timeout
        try:
            result = subprocess.run(
                [self._tmux, *args],
                capture_output=True,
                timeout=wait,
            )
        except FileNotFoundError as err:
            raise RuntimeError("tmux is not installed or not in PATH") from err
        except subprocess.TimeoutExpired as err:
            raise RuntimeError(f"tmux {args[0]} timed out") from err
        if result.returncode != 0:
            stderr = result.stderr.decode().strip()
            raise RuntimeError(f"tmux {args[0]} failed: {stderr}")
        return result.stdout.decode().strip()

    def _run_silent(self, *args: str, timeout: float | None = None) -> bool:
        """Best-effort run that swallows failures. Returns True on success."""
        try:
            self._run(*args, timeout=timeout)
            return True
        except RuntimeError:
            return False

    # --- environment & session ---

    def inside_tmux(self) -> bool:
        """Return True if the current process is running inside a tmux session."""
        return bool(os.environ.get("TMUX"))

    def current_session_name(self, pane: str = "") -> str | None:
        """Return a tmux session name, or None if it cannot be resolved.

        With ``pane`` (a ``%N`` id or any tmux target) the answer is scoped
        to that pane. Without it tmux answers for whichever session is
        currently attached, which is only the caller's session by
        coincidence.
        """
        target = ["-t", pane] if pane else []
        try:
            return self._run("display-message", "-p", *target, "#{session_name}")
        except RuntimeError:
            return None

    # --- pane lifecycle ---

    def pane_alive(self, target: str) -> bool:
        """Return True if the tmux target pane exists and is reachable."""
        return self._run_silent("list-panes", "-t", target, timeout=3)

    def pane_snapshot(self) -> dict[str, str] | None:
        """Return {pane_id: "session:window.pane"} for every pane on the server.

        One tmux call captures the current address of every pane, so
        reconciliation can refresh drifted tmux_target values (window
        kills re-index surviving windows; the stable %N pane id is the
        identity, the address is derived). Returns None when tmux is
        unreachable so callers skip the sync rather than acting on an
        empty picture.
        """
        try:
            out = self._run(
                "list-panes",
                "-a",
                "-F",
                "#{pane_id} #{session_name}:#{window_index}.#{pane_index}",
                timeout=3,
            )
        except RuntimeError as err:
            _db._dbg(f"pane_snapshot: unavailable: {err}")
            return None
        snapshot: dict[str, str] = {}
        for line in out.splitlines():
            pane_id, _, target = line.strip().partition(" ")
            if pane_id and target:
                snapshot[pane_id] = target
        return snapshot

    def kill_pane(self, pane_id: str) -> bool:
        """Kill a tmux pane. Best-effort; returns True on success."""
        return self._run_silent("kill-pane", "-t", pane_id)

    def kill_window(self, session: str, window: str) -> bool:
        """Kill a tmux window using exact-match (=) prefix. Best-effort."""
        # '=' prefix forces exact name match (tmux 2.x+); without it 'eng'
        # would also match 'engineering' and kill an unrelated window.
        return self._run_silent("kill-window", "-t", f"{session}:={window}")

    def select_layout(self, session: str, window: str, layout: str = "tiled") -> bool:
        """Reflow a window's panes with the named layout. Best-effort."""
        return self._run_silent("select-layout", "-t", f"{session}:{window}", layout)

    def target_for_pane(self, pane_id: str) -> str:
        """Return the current mutable tmux target for a stable pane id."""
        return self._run(
            "display-message",
            "-t",
            pane_id,
            "-p",
            "#{session_name}:#{window_index}.#{pane_index}",
        )

    def set_pane_title(self, pane_id: str, title: str) -> None:
        """Set a pane title used by runtime startup hooks for identity."""
        self._run("select-pane", "-t", pane_id, "-T", title)

    # --- nudging ---

    def nudge(self, tmux_target: str, runtime: str = "", content: str = "you have mail!") -> bool:
        """Send a text keystroke message to an agent session.

        Runtime-neutral three-step submit: type the text literally,
        commit a hex carriage return to prime the input buffer, then
        fire the named ``Enter`` key to submit. Neither submit call
        alone is reliable across both Claude Code and Codex TUIs
        (Codex swallows the paste-adjacent CR when it arrives without
        a priming step; Claude's prompt requires the Enter keyname
        binding). Both together cover both runtimes.
        """
        del runtime  # retained for call-site compatibility
        try:
            mode = self._run(
                "display-message",
                "-t",
                tmux_target,
                "-p",
                "#{pane_in_mode}",
                timeout=3,
            )
        except RuntimeError as err:
            _db._dbg(f"nudge: target={tmux_target!r} mode-check failed: {err}")
            return False
        if mode == "1":
            self._run_silent("send-keys", "-t", tmux_target, "-X", "cancel", timeout=3)
            _db._dbg(f"nudge: exited copy-mode on {tmux_target!r}")
        if not self._run_silent("send-keys", "-t", tmux_target, "-l", content, timeout=3):
            _db._dbg(f"nudge: target={tmux_target!r} text send failed")
            return False
        if not self._run_silent("send-keys", "-t", tmux_target, "-H", "0d", timeout=3):
            _db._dbg(f"nudge: target={tmux_target!r} prime (0d) failed")
            return False
        ok = self._run_silent("send-keys", "-t", tmux_target, "Enter", timeout=3)
        _db._dbg(f"nudge: target={tmux_target!r} delivered={ok}")
        return ok

    # --- pane spawning ---

    def spawn_pane(
        self,
        session: str,
        window: str,
        cwd: str,
        agent_type: str,
        qualified_name: str | None,
        is_first: bool,
        layout: str,
        runtime: str | None = None,
        launch: bool = True,
    ) -> dict:
        """Create a single tmux pane running a runtime agent.

        Returns a dict with tmux_target, pane_id, agent_type,
        qualified_name, and runtime (the adapter's runtime_id). The
        ordering contract: pane title is set BEFORE send-keys so
        identity resolution works when the runtime's startup hook
        fires. When qualified_name is None, spawns a general session
        without a specialist role (repo-mode).

        The concrete launch command comes from the runtime adapter
        resolved by ``runtime`` (or the default adapter when
        ``runtime`` is None); this gateway stays runtime-agnostic.
        """
        if is_first:
            # -a appends after current window, avoiding index collisions.
            # Trailing colon on session ensures tmux targets the session.
            pane_id = self._run(
                "new-window",
                "-a",
                "-t",
                f"{session}:",
                "-n",
                window,
                "-c",
                cwd,
                "-P",
                "-F",
                "#{pane_id}",
            )
        else:
            pane_id = self._run(
                "split-window",
                "-t",
                f"{session}:{window}",
                "-c",
                cwd,
                "-P",
                "-F",
                "#{pane_id}",
            )

        if is_first:
            self._run(
                "set-option",
                "-t",
                f"{session}:{window}",
                "allow-rename",
                "off",
            )

        adapter = for_id(runtime) if runtime else default_adapter()
        self._run("select-layout", "-t", f"{session}:{window}", layout)
        tmux_target = self.target_for_pane(pane_id)

        if launch:
            return self.launch_pane(
                pane_id=pane_id,
                cwd=cwd,
                agent_type=agent_type,
                qualified_name=qualified_name,
                runtime=runtime,
                tmux_target=tmux_target,
            )

        return {
            "agent_type": agent_type,
            "qualified_name": qualified_name,
            "tmux_target": tmux_target,
            "pane_id": pane_id,
            "runtime": adapter.runtime_id,
        }

    def launch_pane(
        self,
        *,
        pane_id: str,
        cwd: str,
        agent_type: str,
        qualified_name: str | None,
        runtime: str | None = None,
        tmux_target: str | None = None,
    ) -> dict:
        """Launch a runtime in an existing pane after identity is stable."""
        adapter = for_id(runtime) if runtime else default_adapter()
        current_target = tmux_target or self.target_for_pane(pane_id)
        repo = os.path.basename(cwd)
        display_name = qualified_name if qualified_name is not None else agent_type
        identity = f"{repo}:{display_name}:{current_target}"

        # Pane title must be set BEFORE launching the runtime: the
        # runtime's SessionStart hook reads it to resolve identity.
        self.set_pane_title(pane_id, identity)

        cmd = adapter.build_launch_command(qualified_name=qualified_name)
        self._run("send-keys", "-t", pane_id, cmd, "Enter")

        return {
            "agent_type": agent_type,
            "qualified_name": qualified_name,
            "tmux_target": current_target,
            "pane_id": pane_id,
            "runtime": adapter.runtime_id,
        }


# Singleton: handlers and services depend on this name.
gateway = TmuxGateway()
