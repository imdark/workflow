"""Run a NotesGraph automation's code on this device.

An automation job carries code, not instructions for a model: the body of
an async JavaScript function, given `input` (what triggered it), `secrets`
(the workspace secrets it names), `console` and `fetch`, and a shell automation (`shell` in the payload) `sh` as well,
to run commands in the folder wf serves from. It runs in its own `node`
process (automation_runner.js) with nothing of this runner's environment
but a PATH (a shell automation's commands get HOME and the like too, see
SHELL_ENV_KEYS), so a timeout or a cancel kills it — and its commands —
however busy it is. An 'actions' automation (`runtime` in the payload) is
YAML in wf's own action language instead, run the same way by
automation_actions.py. What it
returns, as JSON, is the run's result; its console lines are the log.

Only a device with `node` takes automation jobs (see `can_run`).
"""

import json
import os
import queue
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable, Optional

RUNNER_SCRIPT = Path(__file__).with_name("automation_runner.js")
ACTIONS_SCRIPT = Path(__file__).with_name("automation_actions.py")
# The folder the `workflow` package is in.
WF_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_TIMEOUT_SECONDS = 60
# Heap the code's node process may use.
MEMORY_MB = 256
TICK_SECONDS = 2


class AutomationFailed(RuntimeError):
    pass


class AutomationCancelled(Exception):
    pass


def node_path() -> Optional[str]:
    return shutil.which("node")


def can_run() -> bool:
    """Whether this device can take automation jobs at all."""
    return node_path() is not None and RUNNER_SCRIPT.exists()


# What of this runner's environment a shell automation's commands get: enough
# for `gh`, `git` over ssh and the like to find their own config and keys,
# none of wf's tokens. Plain code gets only a PATH.
SHELL_ENV_KEYS = ("HOME", "USER", "LOGNAME", "SHELL", "LANG", "LC_ALL",
                  "TMPDIR", "SSH_AUTH_SOCK", "TERM")


def _code_env(shell: bool) -> dict:
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin")}
    if shell:
        env.update({k: os.environ[k] for k in SHELL_ENV_KEYS if k in os.environ})
    return env


def _kill_group(process: subprocess.Popen) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        if process.poll() is None:
            process.kill()


def _reader(stream, lines: "queue.Queue") -> None:
    for line in stream:
        lines.put(line)
    lines.put(None)


def run_automation(payload: dict, secrets: Optional[dict] = None,
                   on_tick: Optional[Callable[[str], bool]] = None,
                   node: Optional[str] = None) -> str:
    """Run the code to its end; returns its output as JSON ("" for none).

    `on_tick` gets the log text written since its last call, every couple
    of seconds, and returns True when the job was cancelled. Raises
    AutomationFailed (a timeout's message starts "Timed out", which the
    server reads as timed_out) or AutomationCancelled.
    """
    code = (payload or {}).get("code") or ""
    if not code.strip():
        raise AutomationFailed("The job carries no code")
    timeout = int(payload.get("timeoutSeconds") or DEFAULT_TIMEOUT_SECONDS)
    runtime = payload.get("runtime") or ("shell" if payload.get("shell") else "node")
    if runtime == "actions":
        # wf's own actions YAML, run by this wf in a child of its own.
        argv = [sys.executable, str(ACTIONS_SCRIPT)]
    else:
        node = node or node_path()
        if not node:
            raise AutomationFailed("node is not installed on this device")
        argv = [node, f"--max-old-space-size={MEMORY_MB}", str(RUNNER_SCRIPT)]

    # Both run commands on this device: they get HOME and the like.
    shell = runtime in ("shell", "actions")
    process = subprocess.Popen(
        argv,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True,
        env={**_code_env(shell),
             # The actions runner imports wf from where this wf runs from.
             **({"PYTHONPATH": str(WF_ROOT)} if runtime == "actions" else {})},
        # Its own process group, so a timeout or cancel stops the commands
        # a shell automation started too.
        start_new_session=True,
    )
    process.stdin.write(json.dumps({
        "code": code,
        "input": payload.get("input"),
        "secrets": secrets or {},
        "shell": runtime == "shell",
    }))
    process.stdin.close()

    lines: "queue.Queue" = queue.Queue()
    threading.Thread(target=_reader, args=(process.stdout, lines),
                     daemon=True).start()
    deadline = time.monotonic() + timeout
    next_tick = time.monotonic() + TICK_SECONDS
    pending, outcome = "", None
    try:
        while True:
            try:
                line = lines.get(timeout=0.2)
            except queue.Empty:
                line = ""
            if line is None:
                break
            if line:
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    message = {"log": line.rstrip("\n")}
                if "log" in message:
                    pending += f"{message['log']}\n"
                elif "done" in message or "error" in message:
                    outcome = message
            now = time.monotonic()
            if now >= deadline:
                raise AutomationFailed(f"Timed out after {timeout}s")
            if on_tick and now >= next_tick:
                next_tick = now + TICK_SECONDS
                text, pending = pending, ""
                if on_tick(text):
                    raise AutomationCancelled()
        process.wait(timeout=5)
    finally:
        if process.poll() is None:
            _kill_group(process)
            process.wait()
        elif shell:
            # The code ended but may have left a command running.
            _kill_group(process)
        if on_tick and pending:
            on_tick(pending)

    if outcome is None:
        stderr = (process.stderr.read() or "").strip()
        raise AutomationFailed(
            f"{'python' if runtime == 'actions' else 'node'} exited "
            f"({process.returncode}) without a result"
            + (f": {stderr[-500:]}" if stderr else ""))
    if "error" in outcome:
        if on_tick and outcome.get("stack"):
            on_tick(f"{outcome['stack']}\n")
        raise AutomationFailed(outcome["error"])
    return outcome["done"] or ""
