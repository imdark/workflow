"""`ssh` MCP server: persistent SSH sessions shared by every Claude session.

Each Claude session starts its own copy of this server (stdio); they all talk
to one daemon (workflow/ssh_daemon.py), started on first use, which holds the
connections. Open a session once with ssh_open, then send any number of
commands with ssh_exec and its session id. `wf ssh install` registers the
server for every Claude config directory on the machine.
"""

import json
import os
import socket
import subprocess
import sys
import time
from typing import Annotated, List, Optional

from workflow.ssh_daemon import LOG_PATH, SOCKET_PATH, STATE_DIR

# Room for the daemon to answer after a command's own timeout runs out.
SLACK_SECONDS = 15


class DaemonError(Exception):
    pass


def _request(payload: dict, timeout: float) -> dict:
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect(str(SOCKET_PATH))
        sock.sendall((json.dumps(payload) + "\n").encode())
        data = b""
        while not data.endswith(b"\n"):
            chunk = sock.recv(65536)
            if not chunk:
                break
            data += chunk
    finally:
        sock.close()
    return json.loads(data)


def start_daemon(wait_seconds: float = 5.0) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with open(LOG_PATH, "ab") as log:
        subprocess.Popen(
            [sys.executable, "-m", "workflow.ssh_daemon"],
            stdin=subprocess.DEVNULL, stdout=log, stderr=log,
            start_new_session=True, cwd=str(STATE_DIR),
            env={**os.environ, "PYTHONPATH": os.pathsep.join(p for p in sys.path if p)},
        )
    deadline = time.time() + wait_seconds
    while time.time() < deadline:
        time.sleep(0.1)
        if daemon_running():
            return
    raise DaemonError(f"ssh daemon did not start; see {LOG_PATH}")


def daemon_running() -> bool:
    try:
        return _request({"op": "ping"}, 2).get("ok", False)
    except (OSError, ValueError):
        return False


def call(op: str, wait: float = 60, **args) -> dict:
    """Send one request to the daemon, starting it if it is not running.

    `wait` bounds the socket; `args` go to the daemon (which has its own `timeout`).
    """
    payload = {"op": op, **{k: v for k, v in args.items() if v is not None}}
    try:
        response = _request(payload, wait)
    except (FileNotFoundError, ConnectionRefusedError):
        start_daemon()
        response = _request(payload, wait)
    if not response.get("ok"):
        raise DaemonError(response.get("error", "unknown error"))
    response.pop("ok")
    return response


def _tool(op: str, wait: float, **args) -> str:
    try:
        return json.dumps(call(op, wait, **args), indent=2, ensure_ascii=False)
    except DaemonError as e:
        return f"Error: {e}"
    except socket.timeout:
        return "Error: the ssh daemon did not answer in time"


def build_server():
    from fastmcp import FastMCP

    mcp = FastMCP(
        "ssh",
        instructions=(
            "Persistent SSH sessions. Call ssh_open once per host and reuse the returned "
            "session_id with ssh_exec for every command instead of running `ssh host cmd` "
            "in Bash. The shell keeps state between commands (cwd, exported variables). "
            "Sessions are shared by all Claude sessions on this machine (ssh_list), and "
            "one session runs one command at a time: open more sessions to run commands "
            "in parallel. Authentication is non-interactive (keys or ssh-agent; hosts "
            "from ~/.ssh/config work)."
        ),
    )

    @mcp.tool()
    def ssh_open(
        host: Annotated[str, "Host name, IP, or a Host alias from ~/.ssh/config"],
        user: Annotated[Optional[str], "Remote user (default: from ssh config)"] = None,
        port: Annotated[Optional[int], "Port (default: from ssh config, else 22)"] = None,
        identity_file: Annotated[Optional[str], "Private key path"] = None,
        options: Annotated[Optional[List[str]], "Extra `ssh -o` options, e.g. ['ProxyJump=bastion']"] = None,
        cwd: Annotated[Optional[str], "Remote directory to start in"] = None,
        label: Annotated[Optional[str], "A note on what the session is for, shown by ssh_list"] = None,
    ) -> str:
        """Open a persistent SSH session and return its session_id for ssh_exec."""
        return _tool("open", 60, host=host, user=user, port=port, identity_file=identity_file,
                     options=options, cwd=cwd, label=label)

    @mcp.tool()
    def ssh_exec(
        session_id: Annotated[str, "Id from ssh_open"],
        command: Annotated[str, "Shell command; runs in the session's shell, so cd/export persist"],
        timeout: Annotated[float, "Seconds to wait; a command still running after it keeps running (follow with ssh_read)"] = 120,
        max_output: Annotated[int, "Characters kept per stream; longer output keeps its end"] = 30000,
    ) -> str:
        """Run a command in an open session. Returns stdout, stderr, exit_code and cwd; stdin is /dev/null."""
        return _tool("exec", timeout + SLACK_SECONDS, session_id=session_id, command=command,
                     timeout=timeout, max_output=max_output)

    @mcp.tool()
    def ssh_read(
        session_id: Annotated[str, "Id from ssh_open"],
        timeout: Annotated[float, "Seconds to wait for the command to finish"] = 30,
        max_output: Annotated[int, "Characters kept per stream"] = 30000,
    ) -> str:
        """Get output a still-running command produced since the last read, and its exit code once done."""
        return _tool("read", timeout + SLACK_SECONDS, session_id=session_id, timeout=timeout,
                     max_output=max_output)

    @mcp.tool()
    def ssh_interrupt(session_id: Annotated[str, "Id from ssh_open"]) -> str:
        """Send SIGINT to the command running in a session (like Ctrl-C); the session stays open."""
        return _tool("interrupt", 45, session_id=session_id)

    @mcp.tool()
    def ssh_list() -> str:
        """List open sessions from every Claude session on this machine."""
        return _tool("list", 10)

    @mcp.tool()
    def ssh_close(session_id: Annotated[str, "Id from ssh_open"]) -> str:
        """Close a session and its connection."""
        return _tool("close", 15, session_id=session_id)

    return mcp


def main():
    build_server().run()


if __name__ == "__main__":
    main()
