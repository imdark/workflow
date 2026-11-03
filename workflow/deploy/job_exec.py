"""Run one agent job and narrate it, so a person can watch.

    python -m workflow.deploy.job_exec <job-dir>

This is what runs inside a job's tmux session. It reads the prompt and
command the runner wrote into <job-dir>, runs the provider, and prints a
human-readable transcript -- to the terminal for whoever attached, and to
log.txt, which the runner tails up to the server so the same text shows in
NotesGraph.

Files in <job-dir>:
    cmd.json     written by the runner: {"argv": [...], "provider": "...",
                 "env": {...extra environment}, "cwd": "where to run" | null,
                 "resumed"?: true when it resumes an interrupted attempt,
                 "caffeinate"?: true to keep the Mac awake while it runs,
                 "job"?: {...what the checkpoint manifest names it by}}
    log.txt      the transcript, appended as the run goes
    result.json  {"result": str, "steps": int, "error": str|null,
                  "costUsd": float|null, "tokens": int|null,
                  "changes"?: what the run changed (see run_changes)}
    exit_code    written last; its presence means the run is over

and base.json, session.json and checkpoint/, which say where the run started
and carry it if it has to start again (see checkpoint).
"""

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional, TextIO

from workflow.deploy.checkpoint import Checkpointer, run_base
from workflow.deploy.profile_automation import (SECRET_ENV, TOOLS_FILE, handed_over,
                                                kill_running, retarget_tools, run_steps,
                                                write_private)
from workflow.deploy.run_changes import ChangeRecorder, git_changes

# How long a finished session stays open for someone to read. The runner
# does not wait for this; it reads exit_code.
LINGER_SECONDS = 600
TOOL_RESULT_CHARS = 400


def _short(value, limit: int = 160) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _tool_result_text(content) -> str:
    if isinstance(content, list):
        content = "\n".join(
            part.get("text", "") for part in content if isinstance(part, dict)
        )
    return str(content or "")


class Narrator:
    """Turns provider output into transcript lines.

    Claude's stream-json is rendered event by event; any other provider's
    output, and any line that isn't JSON, passes through as it is.

    A subagent's events (they carry `parent_tool_use_id`) are indented under
    a ↳ and do not count as the run's own turns. Claude can emit more than
    one `result` -- the main turn ends, background agents report back, and
    it carries on -- so the outcome is the last one, said once by `finish`.
    """

    def __init__(self, out: TextIO, log: TextIO, cwd: Optional[str] = None):
        self.out, self.log = out, log
        self.recorder = ChangeRecorder(cwd)
        self.result: Optional[str] = None
        self.error: Optional[str] = None
        self.steps = 0
        self.cost: Optional[float] = None
        self.tokens: Optional[int] = None
        # Claude's session id, from its first init: what a resume needs.
        self.session_id: Optional[str] = None
        self.started = False
        self.plain: list = []

    def say(self, text: str, sub: bool = False) -> None:
        if sub:
            text = "\n".join(f"    ↳ {line}" for line in text.split("\n"))
        line = text if text.endswith("\n") else text + "\n"
        self.log.write(line)
        self.log.flush()
        if self.out is None:
            return
        try:
            self.out.write(line)
            self.out.flush()
        except (OSError, ValueError):
            # The terminal went (tmux kill-session): the log is still kept.
            self.out = None

    def feed(self, line: str) -> None:
        stripped = line.strip()
        if not stripped:
            return
        try:
            event = json.loads(stripped)
        except json.JSONDecodeError:
            event = None
        if not isinstance(event, dict) or "type" not in event:
            self.plain.append(line.rstrip("\n"))
            self.say(line.rstrip("\n"))
            return
        self._event(event)

    def _event(self, event: dict) -> None:
        kind = event.get("type")
        sub = bool(event.get("parent_tool_use_id"))
        if kind == "system" and event.get("subtype") == "init":
            if sub:
                return
            if self.started:
                # The same run picking up again after background work.
                self.say("▶ continuing")
                return
            self.started = True
            if isinstance(event.get("session_id"), str):
                self.session_id = event["session_id"]
            model = event.get("model") or "?"
            self.say(f"▶ started · model {model} · {event.get('cwd', '')}")
        elif kind == "assistant":
            if not sub:
                self.steps += 1
            for part in (event.get("message") or {}).get("content") or []:
                if part.get("type") == "text" and part.get("text", "").strip():
                    self.say(part["text"].rstrip(), sub)
                elif part.get("type") == "tool_use":
                    self.recorder.tool_use(part)
                    name = part.get("name", "tool")
                    if name.endswith("__ask_user"):
                        # The question is the event worth seeing, not the call.
                        question = (part.get("input") or {}).get("question", "")
                        self.say(f"? asking you: {question}  (answer in NotesGraph)", sub)
                    else:
                        self.say(f"→ {name}  {_short(part.get('input', {}))}", sub)
        elif kind == "user":
            for part in (event.get("message") or {}).get("content") or []:
                if isinstance(part, dict) and part.get("type") == "tool_result":
                    self.recorder.tool_result(part)
                    text = _tool_result_text(part.get("content"))
                    mark = "✗" if part.get("is_error") else "←"
                    self.say(f"  {mark} {_short(text, TOOL_RESULT_CHARS)}", sub)
        elif kind == "result" and not sub:
            self.steps = int(event.get("num_turns") or self.steps)
            cost = event.get("total_cost_usd")
            self.cost = cost if isinstance(cost, (int, float)) else self.cost
            # modelUsage covers subagents too; `usage` is the main loop only.
            usage = [u for u in (event.get("modelUsage") or {}).values() if isinstance(u, dict)]
            if usage:
                self.tokens = sum(
                    int(u.get(key) or 0) for u in usage
                    for key in ("inputTokens", "outputTokens",
                                "cacheReadInputTokens", "cacheCreationInputTokens"))
            if event.get("is_error") or event.get("subtype") != "success":
                self.result = None
                if event.get("subtype") == "error_max_budget_usd":
                    spent = f"${self.cost:.2f}" if self.cost is not None else "$?"
                    self.error = f"stopped at its cost cap ({spent} spent)"
                else:
                    self.error = str(event.get("result") or event.get("subtype") or "failed")
            else:
                self.result, self.error = str(event.get("result") or ""), None

    def finish(self) -> None:
        """Say how the run ended, once, from its last result."""
        tail = f" · ${self.cost:.4f}" if self.cost is not None else ""
        if self.error is not None:
            self.say(f"✗ failed after {self.steps} turns{tail}: {_short(self.error, 300)}")
        elif self.result is not None:
            self.say(f"✓ finished in {self.steps} turns{tail}")


class _SetupFailed(Exception):
    """A setup step failed; the agent is not run (see run)."""


class _Stopped(Exception):
    """The runner stopped the job: SIGTERM, or SIGHUP when it kills the
    job's tmux session."""


# How long a stopped agent gets to exit before it is killed.
STOP_GRACE_SECONDS = 10


class _StopHandler:
    """Turns a stop into _Stopped while setup or the agent runs, after taking
    down whatever step or tool is running; later (teardown, writing the
    result) it only records it, so the run still ends properly."""

    def __init__(self):
        self.requested = False
        self.armed = False

    def __call__(self, signum, frame) -> None:
        if self.requested:
            return
        self.requested = True
        kill_running()
        if self.armed:
            raise _Stopped()

    def install(self) -> None:
        for sig in (signal.SIGTERM, signal.SIGHUP):
            signal.signal(sig, self)


def _end_agent(proc: Optional[subprocess.Popen]) -> None:
    if proc is None or proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=STOP_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


def _scrub(job_dir: Path) -> None:
    """Once the run is over: the tools' spec goes, and cmd.json keeps no
    token a step was given."""
    (job_dir / TOOLS_FILE).unlink(missing_ok=True)
    path = job_dir / "cmd.json"
    try:
        spec = json.loads(path.read_text())
    except (OSError, ValueError):
        return
    env = (spec.get("automation") or {}).get("env") or {}
    if any(key in env for key in SECRET_ENV):
        for key in SECRET_ENV:
            env.pop(key, None)
        write_private(path, spec)


def keep_awake(pid: int) -> None:
    """Hold off idle and system sleep until the process `pid` exits.

    `caffeinate -w` watches the process and lets go when it ends, however it
    ends, so nothing has to stop it.
    """
    try:
        subprocess.Popen(["caffeinate", "-ims", "-w", str(pid)],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except FileNotFoundError:
        pass


def _automation_steps(spec: dict, phase: str, cwd: Optional[str], say) -> None:
    """Run the profile's `phase` steps (setup or teardown), as cmd.json carries
    them: {"setup": [...], "teardown": [...], "values": {...}, "env": {...}}.
    Raises StepFailed for a non-optional step that fails (see profile_automation)."""
    steps = spec.get(phase) or []
    if not steps:
        return
    env = dict(spec.get("env") or {})
    if cwd:
        env["AGENT_CWD"] = cwd
    # Setup runs in the job directory (what it prepares may not exist yet);
    # teardown where the agent worked, as its tools did -- unless setup
    # never got that far, when the job directory is all there is.
    where = cwd if phase == "teardown" and cwd and os.path.isdir(cwd) else env["JOB_DIR"]
    run_steps(phase, steps, spec.get("values") or {}, where, env, say)


def run(job_dir: Path, out: TextIO = sys.stdout) -> int:
    spec = json.loads((job_dir / "cmd.json").read_text())
    argv = spec["argv"]
    cwd = spec.get("cwd") or None
    automation = spec.get("automation") or {}
    if automation:
        automation.setdefault("env", {}).setdefault("JOB_DIR", str(job_dir))
    # A stop takes the running step, tool or agent with it, then the run
    # still ends properly: teardown, result.json, exit_code.
    stop = _StopHandler()
    stop.install()
    proc = None

    with open(job_dir / "log.txt", "a", encoding="utf-8") as log:
        narrator = Narrator(out, log, cwd or os.getcwd())
        narrator.say(f"$ {argv[0]} … ({spec.get('provider', argv[0])}, job {job_dir.name})")
        if spec.get("claude") or spec.get("provider") in ("claude-code", "workflow", "research"):
            narrator.say("  Claude can read and write your notes, and will ask you in "
                         "NotesGraph when it needs something.")
        if spec.get("provider") == "research":
            narrator.say("  Researching with OmniSeek: cross-lingual search, papers, "
                         "PDFs and media.")
        if spec.get("resumed"):
            narrator.say("↻ resuming the interrupted attempt's session, in the "
                         "worktree it left")
        code = 1
        prepared = True
        stop.armed = True
        if automation.get("setup") and spec.get("resumed"):
            # The interrupted attempt's setup made what it works in; running
            # it again could rebuild it under the session being resumed.
            narrator.say("⚙ setup skipped: resuming where the last attempt left it")
        elif automation.get("setup"):
            try:
                _automation_steps(automation, "setup", cwd, narrator.say)
            except _Stopped:
                narrator.error = "stopped during setup"
                narrator.say(f"✗ {narrator.error}")
                prepared, code = False, 143
            except Exception as e:  # StepFailed, or a step it couldn't run
                # Nothing to run the agent in: the run ends here, saying why.
                narrator.error = str(e) or type(e).__name__
                narrator.say(f"✗ {narrator.error}")
                prepared = False
        if cwd and prepared:
            # Setup may be what makes the agent's directory, or may say where
            # it is (automation.json, as `wf agent prepare-task` writes).
            moved = handed_over(job_dir).get("cwd")
            if moved and moved != cwd:
                retarget_tools(job_dir, moved)
            cwd = moved or cwd
            Path(cwd).mkdir(parents=True, exist_ok=True)
            narrator.recorder = ChangeRecorder(cwd)
        # No cwd: the runner's own checkout, not the job's.
        start = run_base(job_dir, cwd)
        extra_env = spec.get("env") or {}
        checkpoints = Checkpointer(job_dir, cwd, start, extra_env, spec.get("job"))
        try:
            if not prepared:
                raise _SetupFailed()
            if stop.requested:
                raise _Stopped()
            proc = subprocess.Popen(
                argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1, env={**os.environ, **extra_env},
                cwd=cwd,
            )
            if spec.get("caffeinate"):
                keep_awake(proc.pid)
            for line in proc.stdout:
                narrator.feed(line)
                checkpoints.note_session(narrator.session_id)
                checkpoints.maybe(narrator)
            code = proc.wait()
            narrator.finish()
        except FileNotFoundError:
            narrator.error = f"'{argv[0]}' is not on PATH on this device"
            narrator.say(f"✗ {narrator.error}")
            code = 127
        except _SetupFailed:
            pass
        except _Stopped:
            narrator.error = narrator.error or "stopped"
            narrator.say("✗ stopped")
            code = 143
        finally:
            stop.armed = False
            # Teardown must not run under an agent still writing there.
            _end_agent(proc)
            if automation.get("teardown"):
                # However the run ended; a failing teardown is said, not fatal.
                try:
                    _automation_steps(automation, "teardown", cwd, narrator.say)
                except Exception as e:
                    narrator.say(f"⚙ teardown did not finish: {e or type(e).__name__}")
            _scrub(job_dir)

        if stop.requested:
            # Stopped, not ended: no exit_code, which would mark the attempt
            # finished (checkpoint.interrupted_session) and keep it from being
            # resumed. `stopped` tells the runner the agent is down and
            # teardown done (jobs._stop waits for it).
            checkpoints.wait()
            (job_dir / "stopped").write_text(narrator.error or "stopped")
            return 143

        result = narrator.result
        if result is None and code == 0 and not narrator.error:
            # A provider without structured output: its output is the answer.
            result = "\n".join(narrator.plain).strip()
        error = narrator.error
        if code != 0 and not error:
            error = ("\n".join(narrator.plain[-20:]).strip()[:500]
                     or f"{argv[0]} exited {code}")

    if error:
        # The last one, so a failed run can be continued from where it ended.
        checkpoints.take(narrator, "error")
    else:
        # A finished run's checkpoint is dropped; just let one in flight end.
        checkpoints.wait()
    outcome = {"result": result, "steps": max(1, narrator.steps), "error": error,
               "costUsd": narrator.cost, "tokens": narrator.tokens}
    try:
        changes = narrator.recorder.changes(git_changes(cwd, start))
    except Exception:
        changes = None
    if changes:
        outcome["changes"] = changes
    (job_dir / "result.json").write_text(json.dumps(outcome))
    # Written last, and by rename, so the runner never sees a partial file.
    tmp = job_dir / "exit_code.tmp"
    tmp.write_text(str(code))
    os.replace(tmp, job_dir / "exit_code")
    return code


def _record_crash(job_dir: Path, error: str) -> None:
    """A run that died before its end still says how it ended, so the runner
    watching for exit_code never waits on it forever."""
    if (job_dir / "exit_code").exists():
        return
    (job_dir / "result.json").write_text(json.dumps(
        {"result": None, "steps": 1, "error": error, "costUsd": None, "tokens": None}))
    tmp = job_dir / "exit_code.tmp"
    tmp.write_text("1")
    os.replace(tmp, job_dir / "exit_code")


def main() -> None:
    job_dir = Path(sys.argv[1])
    try:
        code = run(job_dir)
    except BaseException as e:
        _record_crash(job_dir, f"the job's process failed: {type(e).__name__}: {e}"[:500])
        raise
    if code == 143:
        # Stopped: nobody is left to read the session.
        sys.exit(code)
    if os.environ.get("TMUX"):
        print(f"\n── job finished (exit {code}). This session closes in "
              f"{LINGER_SECONDS // 60} min; Ctrl-C to close it now.")
        try:
            time.sleep(LINGER_SECONDS)
        except KeyboardInterrupt:
            pass
    sys.exit(code)


if __name__ == "__main__":
    main()
