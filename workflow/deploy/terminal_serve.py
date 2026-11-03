"""Device side of NotesGraph's remote terminal (`wf terminal serve`).

Connects to the server's socket.io gateway as this device, starts each
harness a browser asks for (Claude Code, Codex, Gemini CLI, a shell) inside a
pty, streams what it prints and applies keystrokes and resizes. Nothing
listens on this machine: the connection goes out, so it works behind NAT.
Protocol: docs/reference/remote-terminal.md in the NotesGraph repo.

Claude starts as `wf ai` would have it: the project's Claude account and the
capture proxy (session.apply_claude_env), plus NotesGraph's MCP tools as the
runner's own user. A plain shell is a login shell on this machine for anyone
with settings access to the workspace, so it is refused unless the device's
config allows it (terminal.allow_shell).

POSIX only (pty).
"""

import asyncio
import fcntl
import json
import os
import pty
import re
import shlex
import shutil
import signal
import struct
import termios
from dataclasses import dataclass, field
from pathlib import Path
from collections import deque
from typing import Callable, Optional

# The server refuses output frames over 256 KB; stay well under.
CHUNK = 32 * 1024

DEFAULT_HARNESSES = {
    "claude": ["claude"],
    "codex": ["codex"],
    "gemini": ["gemini"],
}
RESUME_ARGS = {
    "claude": ["--resume"],
    "codex": ["resume"],
    "gemini": ["--resume"],
}
# Per-session files (Claude's MCP config, which holds the token).
SESSIONS_DIR = Path.home() / ".wf" / "terminal"
# How often an --exit-on-update runner checks for new code.
UPDATE_POLL_SECONDS = 10.0
# Output kept per session while the socket is down, sent on reconnect.
BACKLOG_LIMIT = 512 * 1024

# A session id names its folder under SESSIONS_DIR, so it may not be a path:
# no separators, and not "." or "..".
SESSION_ID = re.compile(r"^(?!\.+$)[A-Za-z0-9._-]{1,128}$")


class HarnessRefused(ValueError):
    """A harness this device doesn't run."""


def harness_argv(harness: str, resume: Optional[str] = None,
                 overrides: Optional[dict] = None,
                 allow_shell: bool = False) -> list:
    """Command line for a harness. `overrides` maps harness -> command string."""
    if harness == "shell":
        if not allow_shell:
            raise HarnessRefused(
                "this device doesn't serve a plain shell "
                "(set terminal.allow_shell: true in its ~/.wf/config.yaml)")
        return [os.environ.get("SHELL") or "/bin/sh", "-l"]
    if overrides and overrides.get(harness):
        argv = shlex.split(overrides[harness])
    elif harness in DEFAULT_HARNESSES:
        argv = list(DEFAULT_HARNESSES[harness])
    else:
        raise HarnessRefused(f"unknown harness {harness!r}")
    if resume:
        argv += RESUME_ARGS[harness] + [resume]
    return argv


@dataclass(frozen=True)
class NotesGraphMcp:
    """Where Claude reaches NotesGraph's tools, as the runner's user."""
    url: str
    workspace: str
    token: str

    def config(self) -> dict:
        return {"mcpServers": {"notesgraph": {
            "type": "http",
            "url": f"{self.url}/api/workspaces/{self.workspace}/mcp",
            "headers": {"Authorization": f"Bearer {self.token}"},
        }}}

    def env(self) -> dict:
        """What `wf notes` reads to work on this workspace from the session."""
        return {"NOTESGRAPH_URL": self.url, "NOTESGRAPH_WORKSPACE": self.workspace,
                "NOTESGRAPH_TOKEN": self.token}


def claude_launch(argv: list, session_dir: Path,
                  mcp: Optional[NotesGraphMcp]) -> tuple:
    """Claude with NotesGraph's MCP tools and wf's Claude account.

    Returns (argv, env). The MCP config carries the token, so it is written
    owner-only into the session's own folder, removed when the session ends.
    """
    from workflow.session import apply_claude_env

    env: dict = {}
    apply_claude_env(env)
    if mcp:
        session_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = session_dir / "mcp.json"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(mcp.config(), f)
        argv = [argv[0], "--mcp-config", str(path), *argv[1:]]
    return argv, env


def set_winsize(fd: int, cols: int, rows: int) -> None:
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


@dataclass
class Pty:
    id: str
    pid: int
    fd: int
    reader: Optional[asyncio.Task] = None
    killed: bool = False
    session_dir: Optional[Path] = None


@dataclass
class Device:
    """Runs the sessions the gateway hands this machine."""
    workspace_id: str
    device_key: str
    emit: Callable  # async (event, payload) -> None
    cwd: Optional[str] = None
    overrides: dict = field(default_factory=dict)
    allow_shell: bool = False
    mcp: Optional[NotesGraphMcp] = None
    sessions_dir: Path = SESSIONS_DIR
    ptys: dict = field(default_factory=dict)
    # Output and exits that couldn't be sent while the socket was down, by
    # session, in order; `flush` sends them on reconnect.
    backlog: dict = field(default_factory=dict)

    def _ref(self, session_id: str) -> dict:
        return {"workspaceId": self.workspace_id, "sessionId": session_id}

    async def _send(self, event: str, payload: dict) -> None:
        """Send, or keep it for `flush` when the socket is down.

        A harness keeps running through a dropped connection: its output and
        exit wait here instead of failing the reader that produced them.
        """
        sid = payload["sessionId"]
        queue = self.backlog.get(sid)
        if not queue:
            try:
                await self.emit(event, payload)
                return
            except Exception:
                queue = self.backlog.setdefault(sid, deque())
        queue.append((event, payload))
        size = sum(len(p.get("data", "")) for _, p in queue)
        # Drop the oldest output past the limit; never an exit.
        while size > BACKLOG_LIMIT and queue[0][0] == "terminal:device:output":
            size -= len(queue.popleft()[1].get("data", ""))

    async def flush(self) -> set:
        """Send what waited while disconnected. Returns the sessions whose
        exit went out with it."""
        ended = set()
        for sid in list(self.backlog):
            queue = self.backlog[sid]
            while queue:
                event, payload = queue[0]
                try:
                    await self.emit(event, payload)
                except Exception:
                    return ended
                queue.popleft()
                if event == "terminal:device:exit":
                    ended.add(sid)
            self.backlog.pop(sid, None)
        return ended

    def _launch(self, session: dict, sid: str) -> tuple:
        """(argv, env, session_dir) for a session; raises if refused."""
        harness = session.get("harness", "shell")
        argv = harness_argv(harness, session.get("resume"), self.overrides,
                            self.allow_shell)
        own_claude = harness == "claude" and not self.overrides.get("claude")
        # A shell and wf's own Claude can run `wf notes` on the workspace the
        # session was opened from. Not Codex, Gemini or an owner's override:
        # the token would be in the environment of a program wf didn't vet,
        # for it and whatever it runs to log or pass on.
        notes_env = self.mcp.env() if self.mcp and (harness == "shell" or own_claude) else {}
        # Only wf's own Claude gets the MCP config; an override is the
        # device owner's command, run as written.
        if own_claude:
            session_dir = self.sessions_dir / sid
            argv, env = claude_launch(argv, session_dir, self.mcp)
            return argv, {**notes_env, **env}, session_dir
        return argv, notes_env, None

    async def spawn(self, session: dict) -> None:
        sid = str(session.get("id") or "")
        if not SESSION_ID.match(sid):
            # Not one the gateway makes; nothing safe to name a folder after.
            return
        if sid in self.ptys:
            return
        cols = int(session.get("cols") or 80)
        rows = int(session.get("rows") or 24)
        session_dir = None
        try:
            argv, env, session_dir = self._launch(session, sid)
            pid, fd = pty.fork()
        except Exception as err:  # refused harness, fork failure
            if session_dir:
                shutil.rmtree(session_dir, ignore_errors=True)
            await self._send("terminal:device:started", self._ref(sid))
            await self._send("terminal:device:output",
                             {**self._ref(sid), "data": f"\r\n[wf] cannot start: {err}\r\n"})
            await self._send("terminal:device:exit", {**self._ref(sid), "code": 127})
            return
        if pid == 0:  # child
            try:
                if self.cwd:
                    os.chdir(self.cwd)
                os.environ.update(env)
                os.environ["TERM"] = "xterm-256color"
                os.execvp(argv[0], argv)
            except Exception as err:
                os.write(2, f"[wf] cannot start {argv[0]}: {err}\r\n".encode())
            os._exit(127)
        set_winsize(fd, cols, rows)
        p = Pty(id=sid, pid=pid, fd=fd, session_dir=session_dir)
        self.ptys[sid] = p
        await self._send("terminal:device:started", self._ref(sid))
        p.reader = asyncio.create_task(self._pump(p))

    async def _pump(self, p: Pty) -> None:
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue()

        def on_readable():
            try:
                data = os.read(p.fd, CHUNK)
            except OSError:
                data = b""
            queue.put_nowait(data)
            if not data:
                loop.remove_reader(p.fd)

        loop.add_reader(p.fd, on_readable)
        decoder_tail = b""
        try:
            while True:
                data = await queue.get()
                if not data:
                    break
                data = decoder_tail + data
                text = data.decode("utf-8", errors="ignore")
                # Keep an incomplete trailing multi-byte sequence for next read.
                try:
                    data.decode("utf-8")
                    decoder_tail = b""
                except UnicodeDecodeError as err:
                    if err.end == len(data):
                        decoder_tail = data[err.start:]
                        text = data[:err.start].decode("utf-8", errors="ignore")
                    else:
                        decoder_tail = b""
                if text:
                    await self._send("terminal:device:output",
                                     {**self._ref(p.id), "data": text})
        finally:
            code = await self._reap(p)
            self.ptys.pop(p.id, None)
            try:
                os.close(p.fd)
            except OSError:
                pass
            if p.session_dir:
                shutil.rmtree(p.session_dir, ignore_errors=True)
            await self._send("terminal:device:exit", {**self._ref(p.id), "code": code})

    async def _reap(self, p: Pty) -> int:
        for _ in range(50):
            try:
                pid, status = os.waitpid(p.pid, os.WNOHANG)
            except ChildProcessError:
                return 0
            if pid:
                return os.waitstatus_to_exitcode(status)
            await asyncio.sleep(0.1)
        self._signal(p, signal.SIGKILL)
        try:
            _, status = os.waitpid(p.pid, 0)
            return os.waitstatus_to_exitcode(status)
        except ChildProcessError:
            return -9

    @staticmethod
    def _signal(p: Pty, sig: int) -> None:
        try:
            os.killpg(os.getpgid(p.pid), sig)
        except (ProcessLookupError, PermissionError):
            try:
                os.kill(p.pid, sig)
            except ProcessLookupError:
                pass

    def stdin(self, session_id: str, data: str) -> None:
        p = self.ptys.get(str(session_id))
        if p:
            os.write(p.fd, data.encode("utf-8"))

    def resize(self, session_id: str, cols: int, rows: int) -> None:
        p = self.ptys.get(str(session_id))
        if p:
            set_winsize(p.fd, int(cols), int(rows))

    def kill(self, session_id: str) -> None:
        p = self.ptys.get(str(session_id))
        if p:
            p.killed = True
            self._signal(p, signal.SIGHUP)

    async def kill_all(self) -> None:
        for p in list(self.ptys.values()):
            self._signal(p, signal.SIGHUP)
        tasks = [p.reader for p in self.ptys.values() if p.reader]
        if tasks:
            await asyncio.wait(tasks, timeout=7)


async def serve(url: str, token: str, workspace_id: str, device_key: str,
                cwd: Optional[str] = None, overrides: Optional[dict] = None,
                allow_shell: bool = False, mcp: Optional[NotesGraphMcp] = None,
                stop_when: Optional[Callable[[], bool]] = None,
                on_event: Callable = lambda *a: None) -> None:
    """Stay connected and run sessions until cancelled.

    With `stop_when` (an UpdateWatch), returns once it says so and no session
    is running, so a supervisor can restart this on new code without cutting
    anyone's session off.
    """
    import socketio

    sio = socketio.AsyncClient(reconnection=True, reconnection_delay=2,
                               reconnection_delay_max=30)

    async def emit(event: str, payload: dict):
        await sio.emit(event, payload)

    device = Device(workspace_id, device_key, emit, cwd, overrides or {},
                    allow_shell, mcp)
    stopping = False

    async def announce():
        ack = await sio.call("terminal:device:attach",
                             {"workspaceId": workspace_id, "deviceKey": device_key},
                             timeout=15)
        if isinstance(ack, dict) and ack.get("error"):
            raise RuntimeError(ack["error"].get("message", "refused"))
        data = (ack or {}).get("data") or {}
        # What sessions printed, and how they ended, while the socket was down.
        ended = await device.flush()
        # `live` sessions we no longer have (this process restarted) are gone.
        for s in data.get("live") or []:
            if str(s["id"]) not in device.ptys and str(s["id"]) not in ended \
                    and str(s["id"]) not in device.backlog:
                await emit("terminal:device:exit",
                           {"workspaceId": workspace_id, "sessionId": s["id"], "code": None})
        for s in data.get("pending") or []:
            await device.spawn(s)
        on_event("attached", device_key)

    @sio.event
    async def connect():
        await announce()

    @sio.on("terminal:spawn")
    async def _spawn(session):
        if stopping:
            # Restarting for an update: the next process picks it up as pending.
            return
        on_event("spawn", session)
        await device.spawn(session)

    @sio.on("terminal:stdin")
    async def _stdin(body):
        device.stdin(body["sessionId"], body["data"])

    @sio.on("terminal:resize")
    async def _resize(body):
        device.resize(body["sessionId"], body["cols"], body["rows"])

    @sio.on("terminal:kill")
    async def _kill(body):
        device.kill(body["sessionId"])

    # retry: keep trying when NotesGraph (or the network) isn't there yet,
    # as at login, instead of exiting for the supervisor to give up on.
    await sio.connect(url, headers={"Authorization": f"Bearer {token}"},
                      transports=["websocket"], wait_timeout=20, retry=True)
    try:
        if stop_when is None:
            await sio.wait()
        else:
            while True:
                if stop_when():
                    if not stopping:
                        stopping = True
                        on_event("stopping", len(device.ptys))
                    if not device.ptys:
                        break
                await asyncio.sleep(UPDATE_POLL_SECONDS)
    finally:
        await device.kill_all()
        await sio.disconnect()
