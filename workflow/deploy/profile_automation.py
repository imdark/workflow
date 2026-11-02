"""Profile automations on a Mac runner: the shell steps an agent profile runs
around the agent, and the commands it hands the agent as tools.

The server's profile for a job (plugins/registry/agent-profiles.ts) may carry
an `automation`:

    setup      steps run before Claude starts (clone, install, fetch sources)
    tools      commands the agent may call as mcp__automation__<name>; each
               param reaches the command as $ARG_<NAME>, never spliced in
    teardown   steps run after Claude exits, however it ended
    cwd        where the agent starts, e.g. "{job_dir}/repo"; default {job_dir}

Steps are in wf's recipe shape (recipes.py Step): `{placeholder}`s are
filled from the job, braces we don't know are left for the shell, and a
failing step stops the automation unless it is `optional`. The cloud runner
(tools/agent-runner/src/automation.ts) does the same; a profile written for
one runs on the other.

Placeholders: {job_id}, {job_short}, {job_dir}, {prompt_file}, {branch}
(cloud/{job_short}), {repo}, {base_branch}, {cache} (kept between jobs).
Tools and teardown also get $AGENT_CWD, where the agent works.

Setup and teardown run inside the job's process (job_exec.py), so their
output lands in the transcript and the runner's heartbeat and cancel work
as for the agent itself. The tools run in a stdio MCP server of their own
(automation_mcp.py), started by Claude Code from the job's mcp.json.
"""

import json
import os
import re
import signal
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable, Optional

DEFAULT_TIMEOUT_SECONDS = 600
# What of a command's output goes back to the agent or into the log.
OUTPUT_CHARS = 20_000
LOG_TAIL_CHARS = 2_000
# Where things kept between jobs go (a repo mirror, a tool's venv).
CACHE_DIR = Path.home() / ".wf" / "agent-cache"
# The tools' spec, written by the runner for automation_mcp to read. Not
# `automation.json`: that is what a setup step may write to hand things over
# to the agent (`wf agent prepare-task --out`), as on the cloud runner.
TOOLS_FILE = "automation-tools.json"
HANDOVER_FILE = "automation.json"


class StepFailed(RuntimeError):
    """A non-optional step failed; the message says which and why."""


def render(command: str, values: dict) -> str:
    """Fill in `{key}`s; leave any other braces for the shell."""
    rendered = command
    for key, value in values.items():
        if value is not None:
            rendered = rendered.replace("{" + key + "}", str(value))
    return rendered


def placeholders(job_id: str, job_dir: Path, cache: Path, repo: Optional[str],
                 base_branch: str, prompt_file: Optional[Path] = None) -> dict:
    """The placeholders a job's steps and tools may use (automation.ts jobPlaceholders)."""
    short = job_id[:8]
    values = {
        "job_id": job_id,
        "job_short": short,
        "job_dir": str(job_dir),
        "branch": f"cloud/{short}",
        "repo": repo or "",
        "base_branch": base_branch,
        "cache": str(cache),
    }
    if prompt_file is not None:
        values["prompt_file"] = str(prompt_file)
    return values


def arg_var(name: str) -> str:
    """`path` -> `ARG_PATH`, `max-results` -> `ARG_MAX_RESULTS`."""
    return "ARG_" + re.sub(r"[^A-Za-z0-9]", "_", name).upper()


def tool_env(spec: dict, args: dict, base_env: dict) -> dict:
    """The environment a tool's command runs with: the base plus one
    $ARG_<NAME> per param the agent passed. A missing required param is a
    KeyError naming it, so the command never runs half-configured."""
    env = dict(base_env)
    for name, param in (spec.get("params") or {}).items():
        value = args.get(name)
        if value is None or value == "":
            if (param or {}).get("required"):
                raise KeyError(name)
            continue
        env[arg_var(name)] = str(value)
    return env


def _tail(text: str, chars: int) -> str:
    trimmed = text.rstrip()
    return trimmed if len(trimmed) <= chars else "…" + trimmed[-chars:]


def _indent(text: str) -> str:
    return "\n".join(f"    {line}" for line in text.split("\n"))


# The step or tool running now, so a SIGTERM to job_exec takes its whole
# process group with it rather than orphaning a clone or an install.
_current = threading.local()


def kill_current() -> None:
    proc = getattr(_current, "proc", None)
    if proc is not None and proc.poll() is None:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            proc.kill()


def run_command(command: str, timeout: Optional[int], cwd: Optional[str], env: dict,
                on_output: Optional[Callable[[str], None]] = None) -> tuple:
    """Run `sh -c command`, stdout and stderr together, up to `timeout` seconds.

    Returns (exit code or None, output tail, timed_out). Lines go to
    `on_output` as they arrive, for the transcript. The command gets a
    process group of its own, so a timeout or a stop takes its children too.
    """
    timeout = timeout or DEFAULT_TIMEOUT_SECONDS
    try:
        proc = subprocess.Popen(
            ["sh", "-c", command], cwd=cwd or None, env=env,
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, errors="replace", bufsize=1, start_new_session=True,
        )
    except OSError as e:
        return None, str(e), False
    _current.proc = proc
    output: list = []
    size = 0
    timed_out = False

    def reap() -> None:
        nonlocal timed_out
        timed_out = True
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            proc.kill()

    timer = threading.Timer(timeout, reap)
    timer.start()
    try:
        for line in proc.stdout:
            if on_output:
                on_output(line.rstrip("\n"))
            output.append(line)
            size += len(line)
            # Keep the tail: the end of a failing build says why.
            while size > OUTPUT_CHARS * 2 and len(output) > 1:
                size -= len(output.pop(0))
        code = proc.wait()
    finally:
        timer.cancel()
        _current.proc = None
    return code, _tail("".join(output), OUTPUT_CHARS), timed_out


def run_steps(phase: str, steps: list, values: dict, cwd: Optional[str], env: dict,
              say: Callable[[str], None]) -> None:
    """Run steps in order, saying each in the transcript. A failing step
    stops the list (StepFailed) unless it is optional."""
    for step in steps or []:
        name = str(step.get("name") or step.get("run", "step").split()[0])[:40]
        timeout = int(step.get("timeout") or DEFAULT_TIMEOUT_SECONDS)
        started = time.monotonic()
        code, output, timed_out = run_command(
            render(str(step.get("run") or ""), values), timeout, cwd, env)
        seconds = f"{time.monotonic() - started:.1f}"
        if code == 0:
            say(f"⚙ {phase} · {name} ✓ ({seconds}s)")
            continue
        why = f"timed out after {timeout}s" if timed_out else f"exit {code}"
        say(f"⚙ {phase} · {name} ✗ {why} ({seconds}s)")
        if output.strip():
            say(_indent(_tail(output, LOG_TAIL_CHARS)))
        if not step.get("optional"):
            raise StepFailed(f"{phase} step '{name}' failed ({why}): {_tail(output, 300)}")


def run_tool(spec: dict, args: dict, values: dict, cwd: Optional[str], env: dict) -> tuple:
    """Run one tool call. Returns (text for the agent, ok): the exit status,
    then the output, as the cloud runner words it."""
    try:
        env = tool_env(spec, args, env)
    except KeyError as e:
        return f"{spec.get('name')} needs '{e.args[0]}'", False
    timeout = int(spec.get("timeout") or DEFAULT_TIMEOUT_SECONDS)
    code, output, timed_out = run_command(render(str(spec.get("run") or ""), values),
                                          timeout, cwd, env)
    ok = code == 0
    status = f"timed out after {timeout}s" if timed_out else f"exit code {code}"
    return f"{status}\n{output.rstrip()}", ok


def tool_schema(spec: dict) -> dict:
    """An MCP tool definition for a profile tool: its params as string fields."""
    properties, required = {}, []
    for name, param in (spec.get("params") or {}).items():
        properties[name] = {"type": "string", "description": str((param or {}).get("description") or "")}
        if (param or {}).get("required"):
            required.append(name)
    schema: dict = {"type": "object", "properties": properties}
    if required:
        schema["required"] = required
    return {"name": str(spec.get("name")), "description": str(spec.get("description") or ""),
            "inputSchema": schema}


def shell_env(job_id: str, job_dir: Path, agent_cwd: Optional[str] = None) -> dict:
    """What steps and tools run with: the runner's PATH and HOME, the job's
    whereabouts, and where the agent works. Not the whole environment: the
    runner's own secrets stay with it."""
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": os.environ.get("HOME", str(Path.home())),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "JOB_ID": job_id,
        "JOB_DIR": str(job_dir),
    }
    for key in ("GH_TOKEN", "GITHUB_TOKEN", "SSH_AUTH_SOCK", "TMPDIR"):
        if os.environ.get(key):
            env[key] = os.environ[key]
    if agent_cwd:
        env["AGENT_CWD"] = agent_cwd
    return env


def handed_over(job_dir: Path) -> dict:
    """What a setup step handed over for the agent, if one wrote
    automation.json: where to start (`cwd`); see `wf agent prepare-task`."""
    try:
        data = json.loads((job_dir / HANDOVER_FILE).read_text())
    except (OSError, ValueError):
        return {}
    out = {}
    if isinstance(data, dict) and isinstance(data.get("cwd"), str) and data["cwd"]:
        out["cwd"] = data["cwd"]
    return out


def write_tools_spec(job_dir: Path, tools: list, values: dict, cwd: Optional[str],
                     env: dict) -> Path:
    """What automation_mcp needs to serve the tools, in one file."""
    path = job_dir / TOOLS_FILE
    path.write_text(json.dumps({"tools": tools, "values": values, "cwd": cwd, "env": env}))
    return path
