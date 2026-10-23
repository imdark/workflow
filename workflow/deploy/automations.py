"""Run a NotesGraph automation's code on this device.

An automation job carries code, not instructions for a model: the body of
an async JavaScript function, given `input` (what triggered it), `secrets`
(the workspace secrets it names), `console` and `fetch`. It runs in its own
`node` process (automation_runner.js) with nothing of this runner's
environment, so a timeout or a cancel kills it however busy it is. What it
returns, as JSON, is the run's result; its console lines are the log.

Only a device with `node` takes automation jobs (see `can_run`).
"""

import json
import os
import queue
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable, Optional

RUNNER_SCRIPT = Path(__file__).with_name("automation_runner.js")
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
    node = node or node_path()
    if not node:
        raise AutomationFailed("node is not installed on this device")

    process = subprocess.Popen(
        [node, f"--max-old-space-size={MEMORY_MB}", str(RUNNER_SCRIPT)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True,
        # Nothing of the runner's environment (tokens, keys): the code gets
        # its secrets as `secrets`, and node needs only a PATH.
        env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
    )
    process.stdin.write(json.dumps({
        "code": code,
        "input": payload.get("input"),
        "secrets": secrets or {},
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
            process.kill()
            process.wait()
        if on_tick and pending:
            on_tick(pending)

    if outcome is None:
        stderr = (process.stderr.read() or "").strip()
        raise AutomationFailed(
            f"node exited ({process.returncode}) without a result"
            + (f": {stderr[-500:]}" if stderr else ""))
    if "error" in outcome:
        if on_tick and outcome.get("stack"):
            on_tick(f"{outcome['stack']}\n")
        raise AutomationFailed(outcome["error"])
    return outcome["done"] or ""
