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

Several jobs run at once (`--max-jobs`): each has its own job directory,
worktree and tmux session, so they don't share a checkout.
"""

import codecs
import json
import os
import shlex
import shutil
import socket
import subprocess
import sys
import threading
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
# Jobs one runner works on at once.
DEFAULT_MAX_JOBS = 4
# Held while a job is set up or torn down. Those steps chdir (process-wide,
# so a concurrent job would resolve paths against the wrong directory) and
# add or remove git worktrees, which race on the repo's own bookkeeping.
# They take seconds; the runs themselves go in parallel.
_SETUP_LOCK = threading.RLock()


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
    # How to run it, as the server says (system prompt, allowed tools, tool
    # timeout); the constants below stand in for an older server's jobs.
    profile: Optional[dict] = None

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
            profile=payload.get("profile") or None,
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
# Claude Code started the way `wf start` + `wf ai` start a task: a ticket in
# the task backend, wf's branch and worktree, the project's Claude account,
# its skills and task memory (see `start_job_task`).
WORKFLOW_MODEL = "workflow"
# Claude Code with OmniSeek (github.com/Battam1111/omniseek) attached: a
# self-hosted MCP server of research tools -- cross-lingual search, reading
# PDFs and pages, transcription, a scholarly and evidence graph. It has no
# model of its own; Claude Code drives it (see `research_argv`).
RESEARCH_MODEL = "research"
# Jobs that run Claude Code against NotesGraph, so need a client to reach it.
CLAUDE_MODELS = (CLAUDE_CODE_MODEL, WORKFLOW_MODEL, RESEARCH_MODEL)
# A plain shell command, for NotesGraph monitors that read something on this
# machine (a script, a database through psql/sqlite3). Its output is the
# result. No AI is involved, and no permission prompt either, so it runs only
# where the owner has said so: `agent.allow_commands: true` in the wf config.
COMMAND_MODEL = "command"
# A monitor's command reads a value; one still going after this is stuck.
COMMAND_TIMEOUT_SECONDS = 60


def command_argv(job: "Job", config: dict) -> list:
    """`sh -c <command>` for a command job, if this device allows them."""
    if not ((config.get("agent") or {}).get("allow_commands")):
        raise RuntimeError(
            "This device doesn't run commands for NotesGraph monitors. To allow it, "
            "set agent.allow_commands: true in ~/.wf/config.yaml and restart "
            "wf agent serve.")
    command = (job.instructions or "").strip()
    if not command:
        raise RuntimeError("The monitor sent no command to run.")
    return ["sh", "-c", command]

# Appended to Claude Code's own system prompt for a claude-code job. The
# point is that the agent asks rather than guesses, and that each answer is
# written into NotesGraph so the next run finds it instead of asking again.
CLAUDE_CODE_SYSTEM_PROMPT = """\
You are running as an agent inside NotesGraph, the user's notes app, started
by them from a note. You have NotesGraph tools (mcp__notesgraph__*) to search,
read and write their notes, and mcp__run__ask_user to ask them a question.
They are not watching a terminal; they see your questions in NotesGraph.

Your run is listed in NotesGraph under the text of the block it started
from. Once you know what you are doing, name it with mcp__run__set_title:
a few words a person would recognise, e.g. "Pick a show to watch with
Cosmo". Rename it if the work turns out to be something else.

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
plainly asked to do. When a question has a few likely answers (yes/no,
which of these, go on or stop), pass them as `options` to
mcp__run__ask_user so the user can pick one with a click.
Tools that need permission are put to the user for
you; if they deny one, find another way or explain what you could not do.

Finish with your answer to the task itself.
"""

# Tools a claude-code job may use without asking. Everything else -- shell,
# file edits -- goes through the permission prompt to the user.
CLAUDE_CODE_ALLOWED_TOOLS = [
    "mcp__notesgraph", "mcp__run__ask_user", "mcp__run__set_title",
    "WebSearch", "WebFetch", "Read", "Glob", "Grep",
]

# Claude Code gives an MCP tool call this long (ms) before giving up. A
# question waits for a person, who may be at dinner.
ASK_TIMEOUT_MS = 24 * 60 * 60 * 1000

# Added to CLAUDE_CODE_SYSTEM_PROMPT for a research job. The method follows
# OmniSeek's own investigate skill: sweep wide, zoom in, then structure.
RESEARCH_SYSTEM_PROMPT = """\
This is a research run. Besides your usual tools you have OmniSeek
(mcp__omniseek__*), a research toolkit that reaches what ordinary web search
misses: sources in other languages, PDFs and papers, audio and video, forums
and comment threads, and a scholarly citation graph.

Work in three passes:
1. Sweep: search broadly with omniseek_search, in more than one language when
   the topic has non-English sources. Use omniseek_gather to run several
   searches or reads at once rather than one after another.
2. Zoom: read the most promising sources in full with omniseek_read (pages,
   PDFs, arXiv) and, for papers, omniseek_paper_enrich or the graph tools to
   follow citations, authors and related work.
3. Structure: answer with what you found, grouped by finding. Back every
   claim with its source URL; say plainly where sources disagree or where you
   could not find support.

Write the findings into the note you were started from, or a new note linked
from it, with mcp__notesgraph, so they outlast the run.
"""

# What a research job may use without asking, on top of a claude-code job's.
# OmniSeek's tools read and search; none of them act on the user's behalf.
RESEARCH_ALLOWED_TOOLS = ["mcp__omniseek"]

# Where OmniSeek listens by default (`python -m omniseek.serve_http`, or its
# docker compose), and the token file it writes on first start.
OMNISEEK_URL = "http://127.0.0.1:8765"
OMNISEEK_TOKEN_PATH = Path.home() / ".omniseek" / "credentials" / "omniseek_http.json"


def omniseek_endpoint(config: dict) -> tuple:
    """(url, token) of this device's OmniSeek, checked to be up.

    The URL is `agent.omniseek.url` in the wf config, else the default local
    port. Raises with what to do when OmniSeek isn't installed or running, so
    the run fails with that rather than with Claude finding no tools.
    """
    url = (((config.get("agent") or {}).get("omniseek") or {}).get("url")
           or OMNISEEK_URL).rstrip("/")
    try:
        token = (json.loads(OMNISEEK_TOKEN_PATH.read_text()) or {}).get("token")
    except (OSError, ValueError):
        token = None
    if not token:
        raise RuntimeError(
            f"OmniSeek isn't set up on this device: no token at {OMNISEEK_TOKEN_PATH}. "
            "Install it (github.com/Battam1111/omniseek: docker compose up -d) and run again.")
    try:
        with urllib.request.urlopen(f"{url}/healthz", timeout=5) as response:
            response.read()
    except (urllib.error.URLError, OSError) as e:
        raise RuntimeError(
            f"OmniSeek isn't running on this device ({url}: {e}). "
            "Start it (docker compose up -d in your omniseek checkout) and run again.") from e
    return url, token


def research_argv(job: Job, prompt: str, job_dir: Path, client: "JobClient",
                  config: dict) -> tuple:
    """Claude Code as for a claude-code job, with OmniSeek's tools added."""
    url, token = omniseek_endpoint(config)
    return claude_code_argv(
        job, prompt, job_dir, client,
        extra_mcp={"omniseek": {
            "type": "http",
            "url": f"{url}/mcp",
            "headers": {"Authorization": f"Bearer {token}"},
        }},
        extra_allowed=RESEARCH_ALLOWED_TOOLS,
        extra_prompt=RESEARCH_SYSTEM_PROMPT,
    )


def profile_skills_plugin(job: Job, job_dir: Path) -> Optional[Path]:
    """The skills the server's profile names, as one Claude plugin.

    Each is a SKILL.md fetched from its own repo (e.g. OmniSeek's
    omniseek-investigate for a research run), as the cloud runner does. One
    that can't be fetched is left out; the run goes on without it.
    """
    skills = (job.profile or {}).get("skills") or []
    root = job_dir / "skills-plugin"
    loaded = 0
    for skill in skills:
        name = "".join(c if c.isalnum() or c in "._-" else "-" for c in str(skill.get("name") or "skill"))
        try:
            with urllib.request.urlopen(str(skill["url"]), timeout=20) as response:
                text = response.read(512 * 1024 + 1).decode("utf-8")
            if len(text) > 512 * 1024:
                continue
        except (urllib.error.URLError, OSError, KeyError, ValueError, UnicodeDecodeError):
            continue
        (root / "skills" / name).mkdir(parents=True, exist_ok=True)
        (root / "skills" / name / "SKILL.md").write_text(text)
        loaded += 1
    if not loaded:
        return None
    (root / ".claude-plugin").mkdir(parents=True, exist_ok=True)
    (root / ".claude-plugin" / "plugin.json").write_text(json.dumps(
        {"name": "notesgraph-skills", "version": "1.0.0",
         "description": "Skills from the agent profile"}))
    return root


def claude_code_argv(job: Job, prompt: str, job_dir: Path,
                     client: "JobClient", plugin_dir: Optional[Path] = None,
                     extra_mcp: Optional[dict] = None,
                     extra_allowed: Optional[list] = None,
                     extra_prompt: str = "") -> tuple:
    """Claude Code with NotesGraph's tools and a way to ask the user.

    Returns (argv, env). The MCP config carries the NotesGraph token, so it
    is written owner-only and removed when the run ends (see run_job).
    `plugin_dir` carries the task's wf skills, as `wf ai` passes them.
    `extra_mcp`, `extra_allowed` and `extra_prompt` add MCP servers,
    pre-approved tools and system prompt for a variant (see research_argv).
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
        **(extra_mcp or {}),
    }}
    fd = os.open(mcp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(mcp_config, f)

    # The server's profile for the job's model is the whole agent: its prompt
    # already has the research part, its tools the research ones.
    profile = job.profile or {}
    system_prompt = profile.get("systemPrompt") or (
        CLAUDE_CODE_SYSTEM_PROMPT + (f"\n{extra_prompt}" if extra_prompt else ""))
    allowed = profile.get("allowedTools") or [*CLAUDE_CODE_ALLOWED_TOOLS, *(extra_allowed or [])]
    tool_timeout = int(profile.get("toolTimeoutMs") or ASK_TIMEOUT_MS)
    skills_dir = profile_skills_plugin(job, job_dir)
    argv = [
        "claude", "--print", "--verbose", "--output-format", "stream-json",
        "--mcp-config", str(mcp_path), "--strict-mcp-config",
        "--append-system-prompt", system_prompt,
        # `=` form: --allowedTools is variadic and would otherwise swallow
        # the prompt that follows it.
        f"--allowedTools={','.join(allowed)}",
        "--permission-prompt-tool", "mcp__run__approve",
        *(["--plugin-dir", str(plugin_dir)] if plugin_dir else []),
        *(["--plugin-dir", str(skills_dir)] if skills_dir else []),
        # No --max-turns: Claude Code counts every tool call as a turn, so an
        # agent's step limit (sized for the in-tab loop) ended real work a
        # couple of dozen calls in. The run's time limit bounds it instead.
        prompt,
    ]
    return argv, {"MCP_TOOL_TIMEOUT": str(tool_timeout)}


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


def _task_title(job: Job) -> str:
    """A ticket title for a job: the first line of what it was asked to do."""
    for line in (job.instructions or "").splitlines():
        if line.strip():
            line = " ".join(line.split())
            return line if len(line) <= 72 else line[:71] + "…"
    return job.agent_name or f"Agent job {job.id[:8]}"


def start_job_task(job: Job, job_dir: Path, cwd: Path) -> tuple:
    """Start a workflow job the way `wf start` starts a task.

    Returns (issue, workdir, repo_path). The job becomes a task in the
    configured backend, moved to In Progress; it is created once and its
    key kept in the job directory, so a re-claimed job keeps its ticket.
    In a configured repo the task's branch goes in a worktree beside the
    repo (`session.start_in_worktree`) -- never the checkout the runner
    was started in. Outside one, the job runs in its job directory.

    Unlike `wf start` it leaves this machine's current task alone: a job
    in the background must not change what the terminal is working on.
    """
    from contextlib import chdir

    from workflow.backends import get_backend
    from workflow.config import is_git_enabled, load_effective_config
    from workflow.session import (base_branch_for, configured_repos,
                                  resolve_repo, start_in_worktree)

    backend = get_backend(load_effective_config())
    task_file = job_dir / "task.json"
    issue = None
    if task_file.exists():
        try:
            issue = backend.get(json.loads(task_file.read_text())["key"])
        except Exception:
            issue = None
    if issue is None:
        issue = backend.create_issue(_task_title(job), _prompt(job), "Task")
        if issue is None:
            raise RuntimeError("the task backend could not create a task for this job")
        task_file.write_text(json.dumps({"key": issue.key}))
    backend.move_to_in_progress(issue, custom_fields={})

    repo_path = None
    if is_git_enabled():
        repos = configured_repos()
        with chdir(cwd):
            repo_path, _ = resolve_repo(repos)
        if repo_path:
            workdir = Path(start_in_worktree(issue, repo_path, base_branch_for(repo_path, repos)))
            return issue, workdir, str(workdir)
    return issue, job_dir, repo_path


def _job_context(issue, job: Job, workdir: Path) -> str:
    """The prompt `wf ai` would build for the task, or the job's own."""
    try:
        from workflow.ai_context import build_context
        return build_context(issue, repo_path=str(workdir))
    except Exception:
        return _prompt(job)


def _save_session(issue, job_dir: Path, workdir: Path) -> None:
    """File the run's transcript in the task's memory, as `wf ai` does."""
    from contextlib import chdir

    from workflow.memory import save_meta, save_session

    log = job_dir / "log.txt"
    if not log.exists():
        return
    try:
        save_session(issue, log.read_text(encoding="utf-8", errors="replace"))
        # save_meta reads the branch from the current directory.
        with chdir(workdir):
            save_meta(issue, CLAUDE_CODE_MODEL)
    except Exception:
        pass


def _launch(job_dir: Path, session: Optional[str]) -> Optional[subprocess.Popen]:
    """Start job_exec, inside a detached tmux session when there is one.

    Returns the process when it runs without tmux; with tmux the session owns
    it and the runner only watches files.
    """
    argv = [sys.executable, "-m", "workflow.deploy.job_exec", str(job_dir)]
    with _SETUP_LOCK:
        # Not while another job's setup has chdir'd somewhere else.
        cwd = os.getcwd()
    if session:
        # A runner that died can leave the previous attempt's session behind
        # when the job is re-claimed; replace it rather than failing.
        subprocess.run(["tmux", "kill-session", "-t", session],
                       capture_output=True)
        subprocess.run(
            ["tmux", "new-session", "-d", "-s", session, "-x", "200", "-y", "50",
             "-c", cwd, shlex.join(argv)],
            check=True, capture_output=True,
        )
        return None
    return subprocess.Popen(argv, cwd=cwd, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)


def _stop(session: Optional[str], proc: Optional[subprocess.Popen]) -> None:
    if session:
        subprocess.run(["tmux", "kill-session", "-t", session], capture_output=True)
    elif proc and proc.poll() is None:
        proc.terminate()


class JobCancelled(Exception):
    """The job was cancelled from NotesGraph while it ran."""


def run_job(job: Job, config: Optional[dict] = None, timeout: Optional[int] = None,
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
    reach NotesGraph). There is no time limit unless `timeout` (seconds)
    is given; time spent waiting for an answer does not count toward it. It runs in its own worktree of the repo at `cwd`
    (default: the runner's), never in that checkout (see `job_workdir`).

    A "workflow" job is the same Claude Code, started as a task the way
    `wf start` and `wf ai` start one -- ticket, branch, worktree, Claude
    account, skills, task memory -- in the repo `cwd` resolves to; see
    `start_job_task`.

    A "research" job is Claude Code with this device's OmniSeek attached,
    in the job directory; see `research_argv`. It fails up front if
    OmniSeek isn't installed or running.

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
    issue = repo_path = None
    with _SETUP_LOCK:
        if job.model in CLAUDE_MODELS:
            if client is None:
                raise RuntimeError(f"a {job.model} job needs a NotesGraph connection")
            provider = job.model
        if job.model == CLAUDE_CODE_MODEL:
            argv, env = claude_code_argv(job, _prompt(job), job_dir, client)
            workdir = job_workdir(job_dir, cwd or Path.cwd())
        elif job.model == RESEARCH_MODEL:
            argv, env = research_argv(job, _prompt(job), job_dir, client, config)
            # Research reads the world, not a repo: the job directory, no worktree.
            workdir = job_dir
        elif job.model == COMMAND_MODEL:
            provider, argv = COMMAND_MODEL, command_argv(job, config)
            workdir = job_dir
            if timeout is None:
                timeout = COMMAND_TIMEOUT_SECONDS
        elif job.model == WORKFLOW_MODEL:
            issue, workdir, repo_path = start_job_task(job, job_dir, cwd or Path.cwd())
            from workflow.ai_providers.claude import skills_plugin_dir
            from workflow.session import apply_claude_env
            plugin_dir = skills_plugin_dir(issue, issue.key.lower(),
                                           Path.home() / ".wf" / "tasks" / issue.key.lower(),
                                           repo_path)
            argv, env = claude_code_argv(job, _job_context(issue, job, workdir),
                                         job_dir, client, plugin_dir)
            # Same Claude account and capture proxy as `wf ai`.
            env.update(apply_claude_env(dict(os.environ)))
        else:
            provider, argv = _provider_argv(job, config)
        (job_dir / "cmd.json").write_text(json.dumps(
            {"argv": argv, "provider": provider, "env": env,
             "cwd": str(workdir) if workdir else None}))
        if repo_path:
            from workflow.pid_manager import create_ai_pid_file
            from workflow.session import task_branch_name
            # So `wf start` sees this repo is busy, as it does for `wf ai`.
            create_ai_pid_file(repo_path, issue.key, task_branch_name(issue))
    try:
        return _watch(job, job_dir, timeout, on_tick, use_tmux, tick_seconds)
    finally:
        # Holds the NotesGraph token; nothing needs it once the run is over.
        (job_dir / "mcp.json").unlink(missing_ok=True)
        with _SETUP_LOCK:
            if job.model == CLAUDE_CODE_MODEL:
                _drop_worktree_if_empty(job_dir)
            if issue is not None:
                _save_session(issue, job_dir, workdir)
            if repo_path:
                from workflow.pid_manager import remove_ai_pid_file
                remove_ai_pid_file(repo_path)


def _watch(job: Job, job_dir: Path, timeout: Optional[int],
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
    # No timeout: the run goes until it finishes or is cancelled.
    deadline = time.monotonic() + timeout if timeout is not None else None

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
        if deadline is None:
            pass
        elif any(job_dir.glob("waiting-*")):
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


def _run_claimed(client: JobClient, job: Job, config, emit) -> bool:
    """Run a claimed job and report how it ended. True if it finished."""
    session = tmux_session_name(job.id) if shutil.which("tmux") else None
    emit("claimed", job, session or "")
    reporter = _Reporter(client, job.id, session)
    try:
        outcome = run_job(job, config, on_tick=reporter,
                          use_tmux=session is not None, client=client)
        client.report(job.id, status="done", result=outcome["result"],
                      steps=outcome["steps"],
                      **({"logAppend": reporter.pending} if reporter.pending else {}))
        emit("done", job)
        return True
    except JobCancelled:
        emit("cancelled", job)
    except Exception as e:
        try:
            client.report(job.id, status="error", error=str(e)[:2000],
                          **({"logAppend": reporter.pending} if reporter.pending else {}))
        except NotesGraphError:
            pass
        emit("failed", job, str(e))
    return False


def serve(client: JobClient, device_key: str, runner_id: Optional[str] = None,
          poll_seconds: int = DEFAULT_POLL_SECONDS, once: bool = False,
          on_event=None, config=None, max_jobs: int = DEFAULT_MAX_JOBS) -> int:
    """Claim and run jobs until interrupted. Returns how many ran.

    Up to `max_jobs` run at once, each on its own thread; with every slot
    taken nothing more is claimed, so the rest stay queued on the server
    for another runner. A slot that frees up is filled straight away, and
    jobs queued together start together rather than one per poll. `once`
    runs at most one job, here, and returns.

    The threads are daemons: Ctrl-C ends the runner without waiting for
    them, as it always ended mid-job. Their leases lapse and the jobs are
    claimed again.
    """
    runner_id = runner_id or f"{socket.gethostname()}-{int(time.time())}"
    emit = on_event or (lambda *a, **k: None)
    completed = 0
    counted = threading.Lock()
    slots = threading.BoundedSemaphore(max(1, max_jobs))

    def work(job: Job) -> None:
        nonlocal completed
        try:
            if _run_claimed(client, job, config, emit):
                with counted:
                    completed += 1
        finally:
            slots.release()

    while True:
        slots.acquire()
        try:
            job = client.claim(device_key, runner_id)
        except NotesGraphError as e:
            slots.release()
            emit("error", f"claim failed: {e}")
            if once:
                return completed
            time.sleep(min(poll_seconds * 4, 60))
            continue

        if not job:
            slots.release()
            if once:
                return completed
            time.sleep(poll_seconds)
            continue

        if once:
            work(job)
            return completed
        threading.Thread(target=work, args=(job,), name=f"job-{job.id[:8]}",
                         daemon=True).start()
