"""The device half of remote agent execution.

A device polls the inventory for work rather than being called. That
direction is the point: a machine in the field sits behind NAT, so having
it initiate every connection means nothing has to route inward and no
credential for it lives on the server.

    wf agent serve

claims queued jobs for this device, runs them through the configured AI
provider, and reports the result back. Each job runs in its own tmux
session (`wf agent attach` to watch one) and its transcript is streamed to
the server as it goes, so the same text shows in NotesGraph.
"""

import codecs
import json
import os
import shlex
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from workflow.deploy.registry.notesgraph import NotesGraphError, load_token

# How long a claim is held before another runner may take the job. Renewed
# by a heartbeat while a run is in progress.
LEASE_SECONDS = 300
HEARTBEAT_SECONDS = 60
DEFAULT_POLL_SECONDS = 5
# How often a running job's new transcript text is sent up.
TICK_SECONDS = 2
# Per-job working directories: prompt, transcript, result.
JOB_ROOT = Path.home() / ".wf" / "agent-jobs"


@dataclass
class Job:
    id: str
    agent_id: str
    agent_name: str
    instructions: str
    context: str
    model: Optional[str] = None
    tools: list = None
    max_steps: int = 8

    @classmethod
    def from_payload(cls, payload: dict) -> "Job":
        return cls(
            id=payload["id"],
            agent_id=payload.get("agentId", ""),
            agent_name=payload.get("agentName", "agent"),
            instructions=payload.get("instructions", ""),
            context=payload.get("context", ""),
            model=payload.get("model"),
            tools=payload.get("tools") or [],
            max_steps=int(payload.get("maxSteps") or 8),
        )


class JobClient:
    """Claim and report jobs against the inventory."""

    def __init__(self, url: str, workspace: str, token: Optional[str] = None,
                 timeout: float = 30.0):
        self.url = url.rstrip("/")
        self.workspace = workspace
        self.token = token or load_token()
        self.timeout = timeout

    def _request(self, method: str, path: str, payload: Optional[dict] = None) -> dict:
        if not self.token:
            raise NotesGraphError(
                "No NotesGraph token. Run 'wf deploy notes-login <token>' first."
            )
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = urllib.request.Request(
            f"{self.url}{path}", data=body, method=method,
            headers={"Authorization": f"Bearer {self.token}",
                     "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read().decode("utf-8")
                if not raw.strip():
                    return {}
                try:
                    return json.loads(raw)
                except json.JSONDecodeError as e:
                    # An API path the server doesn't have falls through to the
                    # SPA, which answers 200 with index.html. Without this the
                    # failure reads as a parse error rather than what it is.
                    if raw.lstrip()[:1] == "<":
                        raise NotesGraphError(
                            f"{path} returned a web page, not JSON — the agent "
                            "jobs API is not deployed on this server."
                        ) from e
                    raise NotesGraphError(f"{path} returned invalid JSON: {e}") from e
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8")[:300]
            except Exception:
                pass
            raise NotesGraphError(f"{method} {path} failed: {e.code} {detail}") from e
        except (urllib.error.URLError, OSError) as e:
            raise NotesGraphError(f"inventory unreachable at {self.url}: {e}") from e

    def _base(self) -> str:
        return f"/api/inventory/workspaces/{self.workspace}"

    def claim(self, device_key: str, runner_id: str) -> Optional[Job]:
        payload = self._request(
            "POST", f"{self._base()}/devices/{device_key}/jobs/claim",
            {"runnerId": runner_id, "leaseSeconds": LEASE_SECONDS},
        )
        job = payload.get("job")
        return Job.from_payload(job) if job else None

    def report(self, job_id: str, **fields) -> Optional[dict]:
        """Report on a job; returns the job as the server now has it."""
        return self._request("POST", f"{self._base()}/jobs/{job_id}/report",
                             {"leaseSeconds": LEASE_SECONDS, **fields}).get("job")

    def list_jobs(self, device_key: Optional[str] = None) -> list:
        query = f"?device={device_key}" if device_key else ""
        return self._request("GET", f"{self._base()}/jobs{query}").get("jobs", [])

    def get_job(self, job_id: str, log_from: int = 0) -> dict:
        """One job, with its transcript from absolute offset `log_from`."""
        return self._request(
            "GET", f"{self._base()}/jobs/{job_id}?logFrom={log_from}").get("job") or {}

    def resolve_job(self, prefix: str) -> Optional[str]:
        """The full id of the newest job whose id starts with `prefix`."""
        matches = [j["id"] for j in self.list_jobs() if j["id"].startswith(prefix)]
        return matches[0] if matches else None


def follow_log(client: JobClient, job_id: str, follow: bool = False,
               poll_seconds: float = 2.0, sleep=time.sleep):
    """Yield ("log", text) and ("question", q) and finally ("end", job).

    Without `follow`, yields what there is now and ends. With it, keeps
    polling for new text until the job reaches a terminal state, asking
    only for what is new each time. An open question is yielded once.
    """
    log_from, seen_questions = 0, set()
    while True:
        job = client.get_job(job_id, log_from)
        if job.get("log"):
            yield "log", job["log"]
        log_from = job.get("logEnd", log_from)
        for question in job.get("questions") or []:
            if not question.get("answeredAt") and question["id"] not in seen_questions:
                seen_questions.add(question["id"])
                yield "question", question
        if not follow or job.get("status") in ("done", "error", "cancelled"):
            yield "end", job
            return
        sleep(poll_seconds)


def tmux_session_name(job_id: str) -> str:
    """The tmux session a job runs in: `tmux attach -t <this>` to watch it."""
    return f"wf-job-{job_id[:8]}"


def _prompt(job: Job) -> str:
    prompt = job.instructions
    if job.context.strip():
        prompt += f"\n\n--- context ---\n{job.context}"
    return prompt


def _provider_argv(job: Job, config: dict) -> tuple:
    prompt = _prompt(job)

    provider = ((config.get("ai") or {}).get("provider") or "claude").lower()
    commands = {
        # stream-json so the transcript shows each tool call as it happens,
        # rather than one block of text when the run is already over.
        "claude": ["claude", "--print", "--verbose", "--output-format", "stream-json", prompt],
        "opencode": ["opencode", "run", prompt],
        "flow": ["flow", "--print", prompt],
    }
    if provider not in commands:
        provider = "claude"
    return provider, commands[provider]


CLAUDE_CODE_MODEL = "claude-code"

# Appended to Claude Code's own system prompt for a claude-code job. The
# point is that the agent asks rather than guesses, and that each answer is
# written into NotesGraph so the next run finds it instead of asking again.
CLAUDE_CODE_SYSTEM_PROMPT = """\
You are running as an agent inside NotesGraph, the user's notes app, started
by them from a note. You have NotesGraph tools (mcp__notesgraph__*) to search,
read and write their notes, and mcp__run__ask_user to ask them a question.
They are not watching a terminal; they see your questions in NotesGraph.

Do not guess facts about the user's life: the people in it, their ages,
relationships, preferences, plans, constraints. For each one you need:
1. Look in NotesGraph first (keyword_search and semantic_search; read the
   notes you find). A fact already written down must not be asked again.
2. If it is not there, ask with mcp__run__ask_user. One short, specific
   question; group closely related unknowns into it. For a task like "find a
   show to watch with Cosmo", ask "Who is Cosmo, and how old are they?"
   rather than assuming.
3. As soon as you have the answer, save it to NotesGraph before continuing:
   update the existing note about that person or topic, or create one titled
   with the subject (e.g. "Cosmo") and link it under a note titled
   "Agent memory" (create that note if it does not exist). Write the fact
   plainly and date it, e.g. "Cosmo: the user's son, 7 years old (as of
   2026-10-02)."

Ask before anything irreversible or outside NotesGraph that you were not
plainly asked to do. Tools that need permission are put to the user for
you; if they deny one, find another way or explain what you could not do.

Finish with your answer to the task itself.
"""

# Tools a claude-code job may use without asking. Everything else -- shell,
# file edits -- goes through the permission prompt to the user.
CLAUDE_CODE_ALLOWED_TOOLS = [
    "mcp__notesgraph", "mcp__run__ask_user",
    "WebSearch", "WebFetch", "Read", "Glob", "Grep",
]

# Claude Code gives an MCP tool call this long (ms) before giving up. A
# question waits for a person, who may be at dinner.
ASK_TIMEOUT_MS = 24 * 60 * 60 * 1000


def claude_code_argv(job: Job, prompt: str, job_dir: Path,
                     client: "JobClient") -> tuple:
    """Claude Code with NotesGraph's tools and a way to ask the user.

    Returns (argv, env). The MCP config carries the NotesGraph token, so it
    is written owner-only and removed when the run ends (see run_job).
    """
    mcp_path = job_dir / "mcp.json"
    mcp_config = {"mcpServers": {
        # NotesGraph's own MCP endpoint, as the user who owns this runner.
        "notesgraph": {
            "type": "http",
            "url": f"{client.url}/api/workspaces/{client.workspace}/mcp",
            "headers": {"Authorization": f"Bearer {client.token}"},
        },
        "run": {
            "type": "stdio",
            "command": sys.executable,
            "args": ["-m", "workflow.deploy.agent_mcp"],
            "env": {"NG_URL": client.url, "NG_WORKSPACE": client.workspace,
                    "NG_JOB": job.id, "NG_TOKEN": client.token or "",
                    "NG_JOB_DIR": str(job_dir)},
        },
    }}
    fd = os.open(mcp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(mcp_config, f)

    argv = [
        "claude", "--print", "--verbose", "--output-format", "stream-json",
        "--mcp-config", str(mcp_path), "--strict-mcp-config",
        "--append-system-prompt", CLAUDE_CODE_SYSTEM_PROMPT,
        # `=` form: --allowedTools is variadic and would otherwise swallow
        # the prompt that follows it.
        f"--allowedTools={','.join(CLAUDE_CODE_ALLOWED_TOOLS)}",
        "--permission-prompt-tool", "mcp__run__approve",
        # No --max-turns: Claude Code counts every tool call as a turn, so an
        # agent's step limit (sized for the in-tab loop) ended real work a
        # couple of dozen calls in. The run's time limit bounds it instead.
        prompt,
    ]
    return argv, {"MCP_TOOL_TIMEOUT": str(ASK_TIMEOUT_MS)}


def _git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], capture_output=True, text=True)


def job_workdir(job_dir: Path, cwd: Path) -> Path:
    """Where a claude-code job runs.

    Inside a git repo, that is a worktree of it under the job directory, at
    the runner's HEAD: the agent can branch and commit there without moving
    the checkout someone is working in. Branches it makes are ordinary
    branches of the repo. Outside a repo, the job directory itself.
    """
    top = _git("-C", str(cwd), "rev-parse", "--show-toplevel")
    if top.returncode != 0:
        return job_dir
    root = Path(top.stdout.strip())
    tree = job_dir / "repo"
    # A re-claimed job carries on in the worktree its first attempt left.
    if not tree.exists():
        added = _git("-C", str(root), "worktree", "add", "--detach", str(tree), "HEAD")
        if added.returncode != 0:
            raise RuntimeError(f"could not make a worktree: {added.stderr.strip()[:300]}")
    return tree / cwd.resolve().relative_to(root.resolve())


def _drop_worktree_if_empty(job_dir: Path) -> None:
    """Remove a job's worktree unless removing it would lose work.

    It stays when it has uncommitted changes, or commits on a detached HEAD
    that no branch holds; commits on a branch outlive the worktree.
    """
    tree = job_dir / "repo"
    if not tree.exists():
        return
    status = _git("-C", str(tree), "status", "--porcelain")
    if status.returncode != 0 or status.stdout.strip():
        return
    on_branch = _git("-C", str(tree), "symbolic-ref", "-q", "HEAD").returncode == 0
    if not on_branch:
        held = _git("-C", str(tree), "branch", "-a", "--contains", "HEAD")
        if not held.stdout.strip():
            # Commits made on the detached HEAD: nothing else points at them.
            return
    _git("-C", str(tree), "worktree", "remove", str(tree))


def _launch(job_dir: Path, session: Optional[str]) -> Optional[subprocess.Popen]:
    """Start job_exec, inside a detached tmux session when there is one.

    Returns the process when it runs without tmux; with tmux the session owns
    it and the runner only watches files.
    """
    argv = [sys.executable, "-m", "workflow.deploy.job_exec", str(job_dir)]
    if session:
        # A runner that died can leave the previous attempt's session behind
        # when the job is re-claimed; replace it rather than failing.
        subprocess.run(["tmux", "kill-session", "-t", session],
                       capture_output=True)
        subprocess.run(
            ["tmux", "new-session", "-d", "-s", session, "-x", "200", "-y", "50",
             "-c", os.getcwd(), shlex.join(argv)],
            check=True, capture_output=True,
        )
        return None
    return subprocess.Popen(argv, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)


def _stop(session: Optional[str], proc: Optional[subprocess.Popen]) -> None:
    if session:
        subprocess.run(["tmux", "kill-session", "-t", session], capture_output=True)
    elif proc and proc.poll() is None:
        proc.terminate()


class JobCancelled(Exception):
    """The job was cancelled from NotesGraph while it ran."""


def run_job(job: Job, config: Optional[dict] = None, timeout: int = 900,
            on_tick: Optional[Callable[[str], bool]] = None,
            use_tmux: Optional[bool] = None,
            tick_seconds: float = TICK_SECONDS,
            client: Optional["JobClient"] = None,
            cwd: Optional[Path] = None) -> dict:
    """Execute one job through the configured AI provider.

    Runs non-interactively, in a tmux session named by `tmux_session_name`
    when tmux is installed, so anyone on the machine can attach and watch.
    Every `tick_seconds` the transcript produced since the last tick is
    passed to `on_tick`; it returns True to cancel the run.

    A job whose model is "claude-code" runs Claude Code with NotesGraph's
    tools and can ask the user questions (`client` is then required, to
    reach NotesGraph). Time spent waiting for an answer does not count
    toward `timeout`. It runs in its own worktree of the repo at `cwd`
    (default: the runner's), never in that checkout (see `job_workdir`).

    Returns {"result", "steps", "session"}. When the capture proxy is
    reachable -- directly or down a reverse tunnel -- the conversation is
    recorded and billed to the account that owns the tunnel, so nothing
    about the agent's identity has to live on this machine.
    """
    if config is None:
        from workflow.config import load_effective_config
        config = load_effective_config()
    if use_tmux is None:
        use_tmux = shutil.which("tmux") is not None

    job_dir = JOB_ROOT / job.id
    job_dir.mkdir(parents=True, exist_ok=True)
    for stale in ("log.txt", "result.json", "exit_code", *job_dir.glob("waiting-*")):
        (job_dir / stale).unlink(missing_ok=True)

    env: dict = {}
    workdir: Optional[Path] = None
    if job.model == CLAUDE_CODE_MODEL:
        if client is None:
            raise RuntimeError("a claude-code job needs a NotesGraph connection")
        provider = "claude-code"
        argv, env = claude_code_argv(job, _prompt(job), job_dir, client)
        workdir = job_workdir(job_dir, cwd or Path.cwd())
    else:
        provider, argv = _provider_argv(job, config)
    (job_dir / "cmd.json").write_text(json.dumps(
        {"argv": argv, "provider": provider, "env": env,
         "cwd": str(workdir) if workdir else None}))
    try:
        return _watch(job, job_dir, timeout, on_tick, use_tmux, tick_seconds)
    finally:
        # Holds the NotesGraph token; nothing needs it once the run is over.
        (job_dir / "mcp.json").unlink(missing_ok=True)
        _drop_worktree_if_empty(job_dir)


def _watch(job: Job, job_dir: Path, timeout: int,
           on_tick: Optional[Callable[[str], bool]], use_tmux: bool,
           tick_seconds: float) -> dict:
    """Launch the prepared job and follow it to the end (see run_job)."""
    session = tmux_session_name(job.id) if use_tmux else None
    try:
        proc = _launch(job_dir, session)
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"could not start tmux: {(e.stderr or b'').decode()[:300]}") from e

    log_path, exit_path = job_dir / "log.txt", job_dir / "exit_code"
    offset = 0
    # Incremental, so a multi-byte character split across two reads is held
    # back until its second half arrives instead of being mangled.
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    deadline = time.monotonic() + timeout

    def drain() -> str:
        nonlocal offset
        if not log_path.exists():
            return ""
        with open(log_path, "rb") as f:
            f.seek(offset)
            chunk = f.read()
        offset += len(chunk)
        return decoder.decode(chunk)

    while True:
        done = exit_path.exists() or (proc is not None and proc.poll() is not None)
        text = drain()
        if on_tick and on_tick(text) and not done:
            _stop(session, proc)
            raise JobCancelled()
        if done:
            break
        if any(job_dir.glob("waiting-*")):
            # Waiting on the user: their thinking time is not the run's.
            deadline += tick_seconds
        elif time.monotonic() > deadline:
            _stop(session, proc)
            raise RuntimeError(f"the run exceeded {timeout}s")
        time.sleep(tick_seconds)

    try:
        outcome = json.loads((job_dir / "result.json").read_text())
    except (OSError, json.JSONDecodeError):
        outcome = {"result": None, "steps": 1,
                   "error": "the run ended without writing a result"}
    if outcome.get("error") or outcome.get("result") is None:
        raise RuntimeError(outcome.get("error") or "the run produced no result")
    return {"result": outcome["result"], "steps": int(outcome.get("steps") or 1),
            "session": session}


class _Reporter:
    """Ships a running job's transcript and renews its lease.

    Called on every tick of `run_job`. Text is sent as it arrives; with
    nothing new, a bare report still goes out every HEARTBEAT_SECONDS so a
    long quiet run is not re-claimed. Single-threaded on purpose: reports
    for one job then can never arrive out of order.
    """

    def __init__(self, client: JobClient, job_id: str, session: Optional[str]):
        self.client, self.job_id, self.session = client, job_id, session
        self.pending = ""
        self.last = 0.0

    def __call__(self, text: str) -> bool:
        self.pending += text
        if not self.pending and time.monotonic() - self.last < HEARTBEAT_SECONDS:
            return False
        fields = {"status": "running"}
        if self.pending:
            fields["logAppend"] = self.pending
        if self.session and not self.last:
            fields["tmuxSession"] = self.session
        try:
            job = self.client.report(self.job_id, **fields)
        except NotesGraphError:
            # Keep the text for the next tick. A lost heartbeat is survivable:
            # the lease lapses and the job becomes claimable again, which is
            # the correct outcome if this runner has actually died.
            return False
        self.pending, self.last = "", time.monotonic()
        return (job or {}).get("status") == "cancelled"


def serve(client: JobClient, device_key: str, runner_id: Optional[str] = None,
          poll_seconds: int = DEFAULT_POLL_SECONDS, once: bool = False,
          on_event=None, config=None) -> int:
    """Claim and run jobs until interrupted. Returns how many ran."""
    runner_id = runner_id or f"{socket.gethostname()}-{int(time.time())}"
    emit = on_event or (lambda *a, **k: None)
    completed = 0

    while True:
        try:
            job = client.claim(device_key, runner_id)
        except NotesGraphError as e:
            emit("error", f"claim failed: {e}")
            if once:
                return completed
            time.sleep(min(poll_seconds * 4, 60))
            continue

        if not job:
            if once:
                return completed
            time.sleep(poll_seconds)
            continue

        session = tmux_session_name(job.id) if shutil.which("tmux") else None
        emit("claimed", job, session or "")
        reporter = _Reporter(client, job.id, session)
        try:
            outcome = run_job(job, config, on_tick=reporter,
                              use_tmux=session is not None, client=client)
            client.report(job.id, status="done", result=outcome["result"],
                          steps=outcome["steps"],
                          **({"logAppend": reporter.pending} if reporter.pending else {}))
            completed += 1
            emit("done", job)
        except JobCancelled:
            emit("cancelled", job)
        except Exception as e:
            try:
                client.report(job.id, status="error", error=str(e)[:2000],
                              **({"logAppend": reporter.pending} if reporter.pending else {}))
            except NotesGraphError:
                pass
            emit("failed", job, str(e))

        if once:
            return completed
