"""`wf terminal serve`: the device end of NotesGraph's remote terminal.

The browser asks NotesGraph to open a harness (Claude Code, Codex, Gemini
CLI or a shell) on a registered machine. NotesGraph's terminal gateway
relays that over socket.io to this process, which starts the harness in a
pty, streams what it prints back, and applies keystrokes and resizes. The
protocol is in notes-graph's docs/reference/remote-terminal.md.

Like `wf agent serve` this only connects outward, with the device's
NotesGraph token, so it works from behind NAT. The harnesses are children
of this process: stopping it ends them.
"""

import codecs
import fcntl
import json
import os
import re
import shutil
import signal
import struct
import subprocess
import termios
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional

HARNESSES = ("claude", "codex", "gemini", "shell")

# The gateway refuses output over 256 KB per event; stay well under it.
READ_CHUNK = 64 * 1024
# Output kept while the socket is down, per session, sent on reconnect.
BACKLOG_LIMIT = 512 * 1024
KILL_GRACE_SECONDS = 3.0

SESSION_ID = re.compile(r"^[A-Za-z0-9._-]{1,128}$")

Emit = Callable[[str, dict], None]


class SpawnRefused(Exception):
    """A harness this device won't or can't start; the message is shown."""


@dataclass
class Settings:
    workspace: str
    device_key: str
    url: str
    token: str
    cwd: Path
    allow_shell: bool = False
    # CLAUDE_CONFIG_DIR for Claude: the project's account (see claude_accounts).
    claude_config_dir: Optional[str] = None
    state_dir: Path = field(default_factory=lambda: Path.home() / ".wf" / "terminal")


def _session_dir(settings: Settings, session_id: str) -> Path:
    return settings.state_dir / session_id


def write_mcp_config(settings: Settings, session_id: str) -> Path:
    """NotesGraph's MCP tools for Claude, as this device's user.

    Carries the token, so it is owner-only and removed when the session ends.
    """
    directory = _session_dir(settings, session_id)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = directory / "mcp.json"
    config = {"mcpServers": {"notesgraph": {
        "type": "http",
        "url": f"{settings.url}/api/workspaces/{settings.workspace}/mcp",
        "headers": {"Authorization": f"Bearer {settings.token}"},
    }}}
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(config, f)
    return path


def harness_argv(settings: Settings, harness: str, session_id: str,
                 resume: Optional[str] = None,
                 which: Optional[Callable[[str], Optional[str]]] = None) -> List[str]:
    """The command line for a harness, or SpawnRefused saying why not."""
    which = which or shutil.which
    if harness not in HARNESSES:
        raise SpawnRefused(f"Unknown harness '{harness}'.")
    if resume is not None and not SESSION_ID.match(resume):
        raise SpawnRefused("That isn't a conversation id this device can resume.")

    if harness == "shell":
        if not settings.allow_shell:
            raise SpawnRefused(
                "A plain shell is turned off on this device. To allow it, set "
                "terminal.allow_shell: true in ~/.wf/config.yaml and restart "
                "`wf terminal serve`.")
        return [os.environ.get("SHELL") or "/bin/sh", "-l"]

    program = {"claude": "claude", "codex": "codex", "gemini": "gemini"}[harness]
    if not which(program):
        raise SpawnRefused(f"`{program}` isn't installed on this device (not on PATH).")

    if harness == "claude":
        mcp = write_mcp_config(settings, session_id)
        argv = [program, "--mcp-config", str(mcp)]
        return argv + (["--resume", resume] if resume else [])
    if harness == "codex":
        return [program, "resume", resume] if resume else [program]
    return [program, "--resume", resume] if resume else [program]


def harness_env(settings: Settings, harness: str) -> dict:
    env = dict(os.environ)
    env["TERM"] = "xterm-256color"
    env["COLORTERM"] = "truecolor"
    # Set when wf itself runs inside Claude Code; a child claude would take
    # itself for a nested session.
    for key in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT"):
        env.pop(key, None)
    if harness == "claude" and settings.claude_config_dir and not env.get("CLAUDE_CONFIG_DIR"):
        env["CLAUDE_CONFIG_DIR"] = settings.claude_config_dir
    return env


def _set_size(fd: int, cols: int, rows: int) -> None:
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


def _clamp(value, fallback: int) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return fallback
    return n if 1 <= n <= 500 else fallback


class PtySession:
    """One harness in a pty. `on_output(text)` gets what it prints and
    `on_exit(code)` is called once, after the last output."""

    def __init__(self, session_id: str, argv: List[str], env: dict, cwd: Path,
                 cols: int, rows: int,
                 on_output: Callable[[str], None], on_exit: Callable[[Optional[int]], None]):
        self.id = session_id
        self._on_output = on_output
        self._on_exit = on_exit
        master, slave = os.openpty()
        _set_size(master, cols, rows)
        try:
            self.process = subprocess.Popen(
                argv, stdin=slave, stdout=slave, stderr=slave, cwd=str(cwd), env=env,
                start_new_session=True, close_fds=True,
                # The pty becomes the harness's controlling terminal, so it
                # gets SIGWINCH on resize and Ctrl-C as SIGINT.
                preexec_fn=lambda: fcntl.ioctl(0, termios.TIOCSCTTY, 0))
        except BaseException:
            os.close(master)
            raise
        finally:
            os.close(slave)
        self._master = master
        self._reader = threading.Thread(target=self._read, name=f"pty-{session_id[:8]}",
                                        daemon=True)
        self._reader.start()

    def _read(self) -> None:
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        while True:
            try:
                chunk = os.read(self._master, READ_CHUNK)
            except OSError:  # EIO: the harness closed the pty
                chunk = b""
            if not chunk:
                break
            text = decoder.decode(chunk)
            if text:
                self._on_output(text)
        tail = decoder.decode(b"", final=True)
        if tail:
            self._on_output(tail)
        code = self.process.wait()
        try:
            os.close(self._master)
        except OSError:
            pass
        self._on_exit(code)

    def write(self, data: str) -> None:
        payload = data.encode("utf-8")
        while payload:
            try:
                written = os.write(self._master, payload)
            except OSError:
                return  # already gone; the exit is on its way
            payload = payload[written:]

    def resize(self, cols: int, rows: int) -> None:
        try:
            _set_size(self._master, cols, rows)
        except OSError:
            pass

    def kill(self) -> None:
        """Hang up the harness, then force it after a grace period."""
        if self.process.poll() is not None:
            return
        try:
            os.killpg(self.process.pid, signal.SIGHUP)
        except OSError:
            return

        def force():
            try:
                self.process.wait(timeout=KILL_GRACE_SECONDS)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(self.process.pid, signal.SIGKILL)
                except OSError:
                    pass
        threading.Thread(target=force, daemon=True).start()

    def wait(self, timeout: Optional[float] = None) -> None:
        self._reader.join(timeout)


class TerminalDevice:
    """The device's side of the protocol, independent of the socket.

    `emit(event, payload)` sends to the gateway; it raises when the socket
    is down, and output is then kept (up to BACKLOG_LIMIT) for `attached`.
    """

    def __init__(self, settings: Settings, emit: Emit,
                 spawn: Callable[..., PtySession] = PtySession,
                 log: Callable[[str], None] = lambda message: None):
        self.settings = settings
        self._emit = emit
        self._spawn = spawn
        self._log = log
        self._lock = threading.Lock()
        self.sessions: Dict[str, PtySession] = {}
        self._backlog: Dict[str, deque] = {}
        # Ended sessions, and exit codes not yet delivered (held behind
        # output still queued from while the socket was down).
        self._finished: set = set()
        self._exit_codes: Dict[str, Optional[int]] = {}

    def _ref(self, session_id: str, **extra) -> dict:
        return {"workspaceId": self.settings.workspace, "sessionId": session_id, **extra}

    def _send(self, event: str, payload: dict) -> bool:
        try:
            self._emit(event, payload)
            return True
        except Exception as e:  # socket down; caller decides what to keep
            self._log(f"couldn't send {event}: {e}")
            return False

    # ── from the gateway ────────────────────────────────────────────────

    def attached(self, reply: dict) -> None:
        """After `terminal:device:attach`: start what waited, settle the rest."""
        with self._lock:
            mine = set(self.sessions)
        for session in reply.get("pending") or []:
            if session.get("id") not in mine:
                self.spawn(session)
        for session in reply.get("live") or []:
            if session.get("id") not in mine:
                # Running on the server's books but not here: this process
                # restarted and the harness went with it.
                self._send("terminal:device:exit", self._ref(session["id"], code=None))
        self._flush_backlog()

    def spawn(self, session: dict) -> None:
        session_id = str(session.get("id") or "")
        if not SESSION_ID.match(session_id):
            self._log(f"ignored a spawn with a bad session id: {session_id!r}")
            return
        with self._lock:
            if session_id in self.sessions:
                return
        harness = session.get("harness")
        try:
            argv = harness_argv(self.settings, harness, session_id, session.get("resume"))
            pty = self._spawn(
                session_id, argv, harness_env(self.settings, harness), self.settings.cwd,
                _clamp(session.get("cols"), 120), _clamp(session.get("rows"), 32),
                on_output=lambda text: self._output(session_id, text),
                on_exit=lambda code: self._exited(session_id, code))
        except SpawnRefused as e:
            self._refuse(session_id, str(e))
            return
        except OSError as e:
            self._refuse(session_id, f"Couldn't start {harness}: {e}")
            return
        with self._lock:
            # A harness that exited at once has been through _exited already.
            if session_id not in self._finished:
                self.sessions[session_id] = pty
        self._send("terminal:device:started", self._ref(session_id))
        self._log(f"started {harness} for session {session_id[:8]}")

    def stdin(self, payload: dict) -> None:
        pty = self.sessions.get(str(payload.get("sessionId")))
        if pty and isinstance(payload.get("data"), str):
            pty.write(payload["data"])

    def resize(self, payload: dict) -> None:
        pty = self.sessions.get(str(payload.get("sessionId")))
        if pty:
            pty.resize(_clamp(payload.get("cols"), 120), _clamp(payload.get("rows"), 32))

    def kill(self, payload: dict) -> None:
        pty = self.sessions.get(str(payload.get("sessionId")))
        if pty:
            pty.kill()

    def kill_all(self) -> None:
        with self._lock:
            sessions = list(self.sessions.values())
        for pty in sessions:
            pty.kill()

    def busy(self) -> bool:
        with self._lock:
            return bool(self.sessions)

    # ── from the harnesses ──────────────────────────────────────────────

    def _output(self, session_id: str, text: str) -> None:
        with self._lock:
            backlog = self._backlog.get(session_id)
        if backlog:  # keep order: nothing new jumps a queue still waiting
            self._queue(session_id, text)
            return
        if not self._send("terminal:device:output", self._ref(session_id, data=text)):
            self._queue(session_id, text)

    def _queue(self, session_id: str, text: str) -> None:
        with self._lock:
            backlog = self._backlog.setdefault(session_id, deque())
            backlog.append(text)
            size = sum(len(part) for part in backlog)
            while size > BACKLOG_LIMIT and len(backlog) > 1:
                size -= len(backlog.popleft())

    def _flush_backlog(self) -> None:
        with self._lock:
            pending = {sid: q for sid, q in self._backlog.items() if q}
        for session_id, backlog in pending.items():
            while backlog:
                if not self._send("terminal:device:output",
                                  self._ref(session_id, data=backlog[0])):
                    return
                backlog.popleft()
            with self._lock:
                if not backlog:
                    self._backlog.pop(session_id, None)
            if session_id in self._exit_codes:
                self._send_exit(session_id)

    def _exited(self, session_id: str, code: Optional[int]) -> None:
        with self._lock:
            self.sessions.pop(session_id, None)
            self._finished.add(session_id)
            self._exit_codes[session_id] = code
            waiting = bool(self._backlog.get(session_id))
        shutil.rmtree(_session_dir(self.settings, session_id), ignore_errors=True)
        if not waiting:
            self._send_exit(session_id)
        self._log(f"session {session_id[:8]} ended ({code})")

    def _send_exit(self, session_id: str) -> None:
        code = self._exit_codes.get(session_id)
        if self._send("terminal:device:exit", self._ref(session_id, code=code)):
            with self._lock:
                self._exit_codes.pop(session_id, None)

    def _refuse(self, session_id: str, message: str) -> None:
        self._log(f"refused session {session_id[:8]}: {message}")
        self._send("terminal:device:output", self._ref(session_id, data=f"\r\n{message}\r\n"))
        self._send("terminal:device:exit", self._ref(session_id, code=127))


def _ack_error(reply) -> Optional[str]:
    if isinstance(reply, dict) and isinstance(reply.get("error"), dict):
        return reply["error"].get("message") or "error"
    return None


def serve(settings: Settings, log: Callable[[str], None] = print,
          stop_when: Optional[Callable[[], bool]] = None) -> None:
    """Connect to NotesGraph and serve terminals until interrupted.

    socket.io reconnects by itself; each (re)connect attaches as the device
    again, so the gateway sees it online and sends what queued meanwhile.
    """
    import socketio  # python-socketio[client]

    sio = socketio.Client(reconnection=True, reconnection_delay=1,
                          reconnection_delay_max=30, logger=False)

    def emit(event: str, payload: dict) -> None:
        if not sio.connected:
            raise ConnectionError("not connected")

        def acked(reply=None):
            error = _ack_error(reply)
            if error:
                log(f"{event}: {error}")
        sio.emit(event, payload, callback=acked)

    device = TerminalDevice(settings, emit, log=log)

    def attach() -> None:
        try:
            reply = sio.call("terminal:device:attach",
                             {"workspaceId": settings.workspace,
                              "deviceKey": settings.device_key}, timeout=15)
        except Exception as e:
            log(f"couldn't attach as {settings.device_key}: {e}")
            return
        error = _ack_error(reply)
        if error:
            log(f"NotesGraph refused this device: {error}")
            return
        log(f"connected as {settings.device_key}")
        device.attached((reply or {}).get("data") or {})

    # The attach call waits for its ack, which socket.io delivers on the
    # thread running this handler; run it beside, not inside.
    sio.on("connect", lambda: threading.Thread(target=attach, daemon=True).start())
    sio.on("disconnect", lambda *args: log("disconnected; reconnecting"))
    sio.on("terminal:spawn", lambda session: device.spawn(session or {}))
    sio.on("terminal:stdin", lambda payload: device.stdin(payload or {}))
    sio.on("terminal:resize", lambda payload: device.resize(payload or {}))
    sio.on("terminal:kill", lambda payload: device.kill(payload or {}))

    # Default transports: long-polling, upgraded to a websocket when the
    # websocket-client package is there (python-socketio[client] brings it).
    sio.connect(settings.url, headers={"Authorization": f"Bearer {settings.token}"},
                wait_timeout=15)
    try:
        while True:
            time.sleep(1)
            # After a wf update, come back on the new code once nobody's
            # terminal would be cut off by it.
            if stop_when is not None and stop_when() and not device.busy():
                log("wf was updated and no terminal is open: exiting to restart")
                return
    finally:
        device.kill_all()
        sio.disconnect()
