"""Shared SSH session daemon behind the `ssh` MCP server (workflow/ssh_mcp.py).

One daemon per user holds every open SSH session, so a session opened by one
Claude session can be used by any other on the machine. Each MCP server
process is a thin client that talks to it over a Unix socket.

A session is one `ssh` process running a remote shell that reads commands from
stdin. Commands run in that shell with `eval`, so `cd` and `export` carry over
to the next command, and the connection is set up once instead of per command.
After each command the shell prints a per-command marker with the exit code
and working directory on stdout and stderr; the reader splits output on it.
Commands in one session run one at a time; open more sessions to run in
parallel.

Protocol: one JSON object per line each way, `{"op": ..., ...}` in and
`{"ok": true, ...}` or `{"ok": false, "error": ...}` out, then the connection
closes.
"""

import asyncio
import fcntl
import json
import os
import secrets
import shlex
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional

STATE_DIR = Path.home() / ".wf" / "ssh"
SOCKET_PATH = STATE_DIR / "daemon.sock"
LOCK_PATH = STATE_DIR / "daemon.lock"
LOG_PATH = STATE_DIR / "daemon.log"

# The remote login shell runs this, so it is written for any POSIX-ish shell.
REMOTE_SHELL = "command -v bash >/dev/null 2>&1 && exec bash -l; exec sh -l"

CONNECT_TIMEOUT = 30
IDLE_TIMEOUT = 8 * 3600
MAX_OUTPUT = 30000


class SessionError(Exception):
    pass


class Job:
    """One command's output, filled in by the session's readers."""

    def __init__(self, command: str, token: str):
        self.command = command
        self.token = token
        self.started = time.time()
        self.buf = {"stdout": bytearray(), "stderr": bytearray()}
        self.sent = {"stdout": 0, "stderr": 0}
        self.marker = {"stdout": None, "stderr": None}
        self.found = {"stdout": None, "stderr": None}
        self.done = asyncio.Event()

    def feed(self, stream: str, data: bytes):
        if self.marker[stream] is not None:
            return
        buf = self.buf[stream]
        needle = b"\n" + self.token.encode()
        start = self.found[stream]
        if start is None:
            start = max(0, len(buf) - len(needle))
        buf.extend(data)
        idx = buf.find(needle, start)
        if idx < 0:
            return
        self.found[stream] = idx
        end = buf.find(b"\n", idx + len(needle))
        if end < 0:
            return
        self.marker[stream] = buf[idx + len(needle):end].decode(errors="replace").strip()
        del buf[idx:]
        if all(m is not None for m in self.marker.values()):
            self.done.set()

    def visible(self, stream: str) -> int:
        """Bytes safe to hand out: the marker may be arriving at the end."""
        buf = self.buf[stream]
        if self.marker[stream] is not None:
            return len(buf)
        if self.found[stream] is not None:
            return self.found[stream]
        # Hold back only a tail that could be the start of the marker.
        needle = b"\n" + self.token.encode()
        for k in range(min(len(needle), len(buf)), 0, -1):
            if buf.endswith(needle[:k]):
                return max(self.sent[stream], len(buf) - k)
        return len(buf)

    def take(self, max_output: int) -> dict:
        out = {}
        for stream in ("stdout", "stderr"):
            upto = self.visible(stream)
            chunk = bytes(self.buf[stream][self.sent[stream]:upto])
            self.sent[stream] = upto
            text = chunk.decode(errors="replace")
            if len(text) > max_output:
                dropped = len(text) - max_output
                text = f"[... {dropped} characters cut, showing the end ...]\n" + text[-max_output:]
            out[stream] = text
        if self.done.is_set() and self.marker["stdout"] is None:
            out.update(running=False, exit_code=None, cwd=None)
        elif self.done.is_set():
            rc, _, cwd = self.marker["stdout"].partition(" ")
            out.update(running=False, exit_code=int(rc) if rc.lstrip("-").isdigit() else None, cwd=cwd)
        else:
            out.update(running=True, exit_code=None, elapsed=round(time.time() - self.started, 1))
        return out


class Session:
    def __init__(self, session_id: str, host: str, argv: List[str], label: Optional[str]):
        self.id = session_id
        self.host = host
        self.argv = argv
        self.label = label
        self.created = time.time()
        self.last_used = self.created
        self.proc: Optional[asyncio.subprocess.Process] = None
        self.job: Optional[Job] = None
        self.remote_pid: Optional[str] = None
        self.cwd: Optional[str] = None
        self.closed_reason: Optional[str] = None
        self.lock = asyncio.Lock()
        self.ssh_errors = bytearray()

    @property
    def alive(self) -> bool:
        return self.proc is not None and self.proc.returncode is None

    def describe(self) -> dict:
        return {
            "session_id": self.id,
            "host": self.host,
            "label": self.label,
            "alive": self.alive,
            "busy": bool(self.job and not self.job.done.is_set()),
            "running_command": self.job.command if self.job and not self.job.done.is_set() else None,
            "cwd": self.cwd,
            "created": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.created)),
            "idle_seconds": int(time.time() - self.last_used),
            "closed_reason": self.closed_reason,
        }

    async def start(self, cwd: Optional[str]):
        self.proc = await asyncio.create_subprocess_exec(
            *self.argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        asyncio.ensure_future(self._read("stdout", self.proc.stdout))
        asyncio.ensure_future(self._read("stderr", self.proc.stderr))
        setup = "echo $$" + (f"; cd {shlex.quote(cwd)}" if cwd else "")
        try:
            result = await asyncio.wait_for(self._run(setup), CONNECT_TIMEOUT)
        except asyncio.TimeoutError:
            await self.close("connect timed out")
            raise SessionError(f"ssh to {self.host} did not answer within {CONNECT_TIMEOUT}s")
        if result.get("exit_code") is None:
            await self.close("connect failed")
            detail = self.ssh_errors.decode(errors="replace").strip() or result.get("stderr", "")
            raise SessionError(f"ssh to {self.host} failed: {detail or 'connection closed'}")
        if result["exit_code"] != 0:
            await self.close("cd failed")
            raise SessionError(f"cd {cwd} failed: {result['stderr'].strip()}")
        # Login scripts may print first; the pid is the last line.
        lines = result["stdout"].strip().splitlines()
        self.remote_pid = lines[-1].strip() if lines and lines[-1].strip().isdigit() else None

    async def _read(self, stream: str, reader: asyncio.StreamReader):
        while True:
            data = await reader.read(65536)
            if not data:
                break
            if self.job is not None:
                self.job.feed(stream, data)
            elif stream == "stderr":
                # Before the first command: ssh's own errors (auth, host key).
                self.ssh_errors.extend(data[-4096:])
        if self.proc:
            await self.proc.wait()
        if not self.closed_reason:
            self.closed_reason = f"ssh exited with code {self.proc.returncode if self.proc else '?'}"
        if self.job is not None and not self.job.done.is_set():
            self.job.done.set()

    async def _run(self, command: str) -> dict:
        """Send a command and wait for it to finish (the caller bounds the wait)."""
        job = self._send(command)
        await job.done.wait()
        return job.take(MAX_OUTPUT)

    def _send(self, command: str) -> Job:
        if not self.alive:
            raise SessionError(f"session {self.id} is closed ({self.closed_reason})")
        token = "__WF_SSH_" + secrets.token_hex(8) + "__"
        self.job = Job(command, token)
        line = (
            f"eval {shlex.quote(command)} </dev/null; __wf_rc=$?; "
            f"printf '\\n%s %d %s\\n' {token} \"$__wf_rc\" \"$PWD\"; "
            f"printf '\\n%s %d\\n' {token} \"$__wf_rc\" >&2\n"
        )
        self.proc.stdin.write(line.encode())
        self.last_used = time.time()
        return self.job

    async def _wait(self, timeout: float, max_output: int) -> dict:
        try:
            await asyncio.wait_for(asyncio.shield(self.job.done.wait()), timeout)
        except asyncio.TimeoutError:
            pass
        self.last_used = time.time()
        result = self.job.take(max_output)
        if result["running"] is False and result.get("cwd"):
            self.cwd = result["cwd"]
        if self.job.done.is_set() and result.get("exit_code") is None:
            result["error"] = f"session closed: {self.closed_reason}"
        result["session_id"] = self.id
        return result

    async def exec(self, command: str, timeout: float, max_output: int) -> dict:
        if self.job and not self.job.done.is_set():
            raise SessionError(
                f"session {self.id} is still running {self.job.command!r}; "
                "call ssh_read to follow it, ssh_interrupt to stop it, or open another session")
        async with self.lock:
            self._send(command)
            await self.proc.stdin.drain()
            return await self._wait(timeout, max_output)

    async def read(self, timeout: float, max_output: int) -> dict:
        if self.job is None:
            raise SessionError(f"session {self.id} has not run a command yet")
        return await self._wait(timeout, max_output)

    async def interrupt(self) -> dict:
        if not (self.job and not self.job.done.is_set()):
            return {"interrupted": False, "detail": "nothing is running"}
        if not self.remote_pid:
            raise SessionError("remote shell pid unknown; close the session instead")
        # The session's own shell is busy, so signal its children over the
        # same ssh multiplexed connection.
        proc = await asyncio.create_subprocess_exec(
            *self.argv[:-1], f"pkill -INT -P {self.remote_pid}",
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, err = await asyncio.wait_for(proc.communicate(), CONNECT_TIMEOUT)
        return {"interrupted": proc.returncode == 0,
                "detail": err.decode(errors="replace").strip() or "sent SIGINT"}

    async def close(self, reason: str = "closed"):
        self.closed_reason = self.closed_reason or reason
        if self.proc and self.proc.returncode is None:
            try:
                self.proc.stdin.write(b"exit\n")
                await asyncio.wait_for(self.proc.wait(), 3)
            except Exception:
                pass
            if self.proc.returncode is None:
                self.proc.kill()
                await self.proc.wait()


def ssh_argv(host: str, user: Optional[str], port: Optional[int], identity_file: Optional[str],
             options: Optional[List[str]]) -> List[str]:
    argv = [
        "ssh", "-T",
        "-o", "BatchMode=yes",
        "-o", f"ConnectTimeout={CONNECT_TIMEOUT}",
        "-o", "ServerAliveInterval=30",
        "-o", "ServerAliveCountMax=4",
        # Sessions to the same host share one TCP connection, and
        # ssh_interrupt rides on it instead of a fresh handshake.
        "-o", "ControlMaster=auto",
        "-o", f"ControlPath={STATE_DIR}/cm-%C",
        "-o", "ControlPersist=60",
    ]
    if user:
        argv += ["-l", user]
    if port:
        argv += ["-p", str(port)]
    if identity_file:
        argv += ["-i", str(Path(identity_file).expanduser())]
    for opt in options or []:
        argv += ["-o", opt]
    return argv + [host, REMOTE_SHELL]


class Daemon:
    def __init__(self):
        self.sessions: Dict[str, Session] = {}

    def _get(self, session_id: str) -> Session:
        session = self.sessions.get(session_id)
        if session is None:
            raise SessionError(f"no session {session_id!r}; ssh_list shows the open ones")
        return session

    async def op_open(self, host, user=None, port=None, identity_file=None, options=None,
                      cwd=None, label=None):
        if not host:
            raise SessionError("host is required")
        session_id = "s-" + secrets.token_hex(4)
        session = Session(session_id, host, ssh_argv(host, user, port, identity_file, options), label)
        await session.start(cwd)
        self.sessions[session_id] = session
        return session.describe()

    async def op_exec(self, session_id, command, timeout=120, max_output=MAX_OUTPUT):
        return await self._get(session_id).exec(command, float(timeout), int(max_output))

    async def op_read(self, session_id, timeout=30, max_output=MAX_OUTPUT):
        return await self._get(session_id).read(float(timeout), int(max_output))

    async def op_interrupt(self, session_id):
        return await self._get(session_id).interrupt()

    async def op_close(self, session_id):
        session = self._get(session_id)
        await session.close()
        del self.sessions[session_id]
        return {"closed": session_id}

    async def op_list(self):
        return {"sessions": [s.describe() for s in self.sessions.values()]}

    async def op_ping(self):
        return {"pid": os.getpid(), "sessions": len(self.sessions)}

    async def op_shutdown(self):
        for session in list(self.sessions.values()):
            await session.close("daemon stopped")
        self.sessions.clear()
        asyncio.get_event_loop().call_later(0.1, os._exit, 0)
        return {"stopped": True}

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        try:
            request = json.loads(await reader.readline())
            op = getattr(self, "op_" + str(request.pop("op", "")), None)
            if op is None:
                raise SessionError("unknown op")
            response = {"ok": True, **(await op(**request))}
        except SessionError as e:
            response = {"ok": False, "error": str(e)}
        except Exception as e:
            response = {"ok": False, "error": f"{type(e).__name__}: {e}"}
        writer.write((json.dumps(response) + "\n").encode())
        try:
            await writer.drain()
        finally:
            writer.close()

    async def reap(self):
        """Drop sessions that died and close ones idle past IDLE_TIMEOUT."""
        while True:
            await asyncio.sleep(60)
            now = time.time()
            for session in list(self.sessions.values()):
                busy = session.job and not session.job.done.is_set()
                if not session.alive or (not busy and now - session.last_used > IDLE_TIMEOUT):
                    await session.close("idle timeout")
                    self.sessions.pop(session.id, None)

    async def serve(self):
        if SOCKET_PATH.exists():
            SOCKET_PATH.unlink()
        server = await asyncio.start_unix_server(self.handle, path=str(SOCKET_PATH))
        os.chmod(SOCKET_PATH, 0o600)
        asyncio.ensure_future(self.reap())
        async with server:
            await server.serve_forever()


def main():
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(STATE_DIR, 0o700)
    lock = open(LOCK_PATH, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("ssh daemon already running", file=sys.stderr)
        return
    lock.write(str(os.getpid()))
    lock.flush()
    asyncio.run(Daemon().serve())


if __name__ == "__main__":
    main()
