"""Checkpoints, and a re-claimed run resuming its session (docs/MOBILE_AGENTS.md, phase 1).

Runs against real git repos in tmp_path and a scripted stand-in for Claude,
so the snapshot, the bundle and the resume are exercised for real.
"""

import io
import json
import subprocess
import sys

import pytest

from workflow.deploy import checkpoint
from workflow.deploy.checkpoint import (REF_PREFIX, Checkpointer, discard,
                                        interrupted_session, resumable, run_base, snapshot)
from workflow.deploy.job_exec import Narrator
from workflow.deploy.jobs import (Job, JobCancelled, JobClient, claude_code_argv,
                                  run_job)


def git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    (root / "kept.txt").write_text("one\n")
    (root / ".gitignore").write_text(".env\n")
    git(root, "add", "-A")
    git(root, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "start")
    return root


@pytest.fixture
def config_dir(tmp_path, monkeypatch):
    """Claude's config directory, where it keeps session transcripts."""
    path = tmp_path / "claude"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(path))
    return path


def session_file(config_dir, session_id, text='{"a": 1}\n'):
    path = config_dir / "projects" / "-some-slug" / f"{session_id}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def narrator_with(session_id=None, steps=0):
    run = Narrator(io.StringIO(), io.StringIO())
    run.session_id, run.steps = session_id, steps
    return run


# --- the snapshot -------------------------------------------------------------

def test_a_snapshot_leaves_the_agents_tree_alone(repo, tmp_path):
    (repo / "kept.txt").write_text("two\n")
    (repo / "new.txt").write_text("new\n")
    (repo / ".env").write_text("SECRET=1\n")
    git(repo, "add", "kept.txt")             # staged, so the real index differs
    before = (git(repo, "status", "--porcelain"), git(repo, "rev-parse", "HEAD"))

    work = snapshot(str(repo), "job1", tmp_path)

    assert (git(repo, "status", "--porcelain"), git(repo, "rev-parse", "HEAD")) == before
    assert git(repo, "rev-parse", REF_PREFIX + "job1") == work["commit"]
    files = git(repo, "ls-tree", "-r", "--name-only", work["commit"]).split()
    assert "new.txt" in files and ".env" not in files, "untracked in, ignored out"
    assert git(repo, "show", f"{work['commit']}:kept.txt") == "two"
    assert git(repo, "rev-parse", f"{work['commit']}^") == before[1]


# --- taking checkpoints ---------------------------------------------------------

def test_a_checkpoint_carries_the_work_and_the_session(repo, tmp_path, config_dir):
    job_dir = tmp_path / "jobs" / "job1"
    job_dir.mkdir(parents=True)
    start = run_base(job_dir, str(repo))
    git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q",
        "--allow-empty", "-m", "agent commit")
    (repo / "half.txt").write_text("half done\n")
    # Claude mid-write: the last line is not whole yet.
    session_file(config_dir, "s-1", '{"turn": 1}\n{"turn": 2}\n{"tur')

    taker = Checkpointer(job_dir, str(repo), start, job={"title": "Fix it"})
    run = narrator_with("s-1", steps=3)
    run.cost = 0.5
    assert taker.take(run, "periodic")

    ckpt = job_dir / "checkpoint"
    manifest = json.loads((ckpt / "manifest.json").read_text())
    assert manifest["seq"] == 1 and manifest["why"] == "periodic"
    assert manifest["job"] == {"title": "Fix it"}
    assert manifest["spent"] == {"costUsd": 0.5, "tokens": None, "steps": 3}
    assert manifest["repo"]["base"] == start["base"]
    assert (ckpt / "session.jsonl").read_text() == '{"turn": 1}\n{"turn": 2}\n'

    # The bundle rebuilds the work in a fresh clone at the base.
    clone = tmp_path / "clone"
    git(tmp_path, "clone", "-q", str(repo), str(clone))
    git(clone, "checkout", "-q", start["base"])
    git(clone, "fetch", "-q", str(ckpt / "work.bundle"), f"{REF_PREFIX}job1")
    git(clone, "checkout", "-q", "FETCH_HEAD")
    git(clone, "reset", "-q", "HEAD~1")
    assert (clone / "half.txt").read_text() == "half done\n"
    assert git(clone, "log", "-1", "--format=%s") == "agent commit"
    assert "half.txt" in git(clone, "status", "--porcelain"), "uncommitted again"


def test_nothing_moved_no_new_checkpoint(repo, tmp_path):
    job_dir = tmp_path / "job1"
    job_dir.mkdir()
    taker = Checkpointer(job_dir, str(repo), run_base(job_dir, str(repo)))
    run = narrator_with()
    assert taker.take(run, "periodic")
    assert not taker.take(run, "periodic")
    (repo / "more.txt").write_text("x")
    assert taker.take(run, "periodic")
    assert json.loads((job_dir / "checkpoint" / "manifest.json").read_text())["seq"] == 2


def test_checkpoints_are_taken_at_most_every_so_often(repo, tmp_path):
    job_dir = tmp_path / "job1"
    job_dir.mkdir()
    clock = Clock()
    taker = Checkpointer(job_dir, str(repo), run_base(job_dir, str(repo)),
                         every=120, clock=clock)
    (repo / "a.txt").write_text("a")
    clock.now = 60
    assert not taker.maybe(narrator_with())
    clock.now = 121
    assert taker.maybe(narrator_with())


def test_a_run_outside_a_repo_still_keeps_its_session(tmp_path, config_dir):
    job_dir = tmp_path / "job1"
    job_dir.mkdir()
    session_file(config_dir, "s-1")
    taker = Checkpointer(job_dir, str(job_dir), run_base(job_dir, str(job_dir)))
    taker.note_session("s-1")
    assert taker.take(narrator_with("s-1"), "end")
    manifest = json.loads((job_dir / "checkpoint" / "manifest.json").read_text())
    assert "repo" not in manifest and manifest["session"] == {"id": "s-1", "copied": True}
    assert json.loads((job_dir / "session.json").read_text())["file"].endswith("s-1.jsonl")


def test_the_base_is_kept_across_attempts(repo, tmp_path):
    job_dir = tmp_path / "job1"
    job_dir.mkdir()
    first = run_base(job_dir, str(repo))
    git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q",
        "--allow-empty", "-m", "agent")
    assert run_base(job_dir, str(repo))["base"] == first["base"]
    assert first["common"].endswith(".git")


def test_a_finished_run_drops_its_checkpoint(repo, tmp_path):
    job_dir = tmp_path / "job1"
    job_dir.mkdir()
    (repo / "a.txt").write_text("a")
    Checkpointer(job_dir, str(repo), run_base(job_dir, str(repo))).take(narrator_with(), "end")
    discard(job_dir)
    assert not (job_dir / "checkpoint").exists()
    assert subprocess.run(["git", "-C", str(repo), "rev-parse", "--verify", "-q",
                           REF_PREFIX + "job1"], capture_output=True).returncode != 0


# --- picking an attempt up again -------------------------------------------------

def test_only_an_unfinished_attempt_is_picked_up(tmp_path):
    (tmp_path / "session.json").write_text(json.dumps({"id": "s-1", "cwd": "/w"}))
    assert interrupted_session(tmp_path)["id"] == "s-1"
    (tmp_path / "exit_code").write_text("0")
    assert interrupted_session(tmp_path) is None


def test_it_resumes_only_in_the_same_directory(tmp_path, config_dir):
    path = session_file(config_dir, "s-1")
    info = {"id": "s-1", "cwd": str(tmp_path), "file": str(path)}
    assert resumable(info, tmp_path, tmp_path) == "s-1"
    assert resumable(info, tmp_path, tmp_path / "elsewhere") is None
    assert resumable(None, tmp_path, tmp_path) is None


def test_a_lost_transcript_is_put_back_from_the_checkpoint(tmp_path, config_dir):
    path = session_file(config_dir, "s-1")
    (tmp_path / "checkpoint").mkdir()
    (tmp_path / "checkpoint" / "session.jsonl").write_text('{"saved": true}\n')
    path.unlink()
    info = {"id": "s-1", "cwd": str(tmp_path), "file": str(path)}
    assert resumable(info, tmp_path, tmp_path) == "s-1"
    assert path.read_text() == '{"saved": true}\n'


def test_a_resumed_run_passes_the_session_to_claude(tmp_path):
    client = JobClient("http://ng", "ws", token="t")
    job = Job.from_payload({"id": "job1", "model": "claude-code", "instructions": "x"})
    argv, _ = claude_code_argv(job, "carry on", tmp_path, client, resume="s-1")
    assert argv[-3:] == ["--resume", "s-1", "carry on"]
    argv, _ = claude_code_argv(job, "go", tmp_path, client)
    assert "--resume" not in argv


# --- end to end: a run interrupted, then claimed again ---------------------------

FAKE_CLAUDE = r"""
import json, os, sys, time
from pathlib import Path
def emit(e): print(json.dumps(e), flush=True)
resumed = "--resume" in sys.argv
sid = sys.argv[sys.argv.index("--resume") + 1] if resumed else "s-first"
emit({"type": "system", "subtype": "init", "model": "m", "cwd": os.getcwd(),
      "session_id": sid})
t = Path(os.environ["CLAUDE_CONFIG_DIR"]) / "projects" / "slug" / f"{sid}.jsonl"
t.parent.mkdir(parents=True, exist_ok=True)
with open(t, "a") as f:
    f.write(json.dumps({"prompt": sys.argv[-1][:40]}) + "\n")
if not resumed:
    Path("work.txt").write_text("half")
    time.sleep(30)
emit({"type": "result", "subtype": "success", "result": "resumed " + sid, "num_turns": 1})
"""


def test_an_interrupted_run_is_resumed_where_it_was(repo, tmp_path, config_dir, monkeypatch):
    script = tmp_path / "fake_claude.py"
    script.write_text(FAKE_CLAUDE)
    monkeypatch.setattr("workflow.deploy.jobs.JOB_ROOT", tmp_path / "jobs")
    monkeypatch.setattr("workflow.deploy.jobs.load_agent_rules", lambda client: "")
    calls = []

    def fake_argv(job, prompt, job_dir, client, **kw):
        calls.append({"prompt": prompt, "resume": kw.get("resume")})
        resume = ["--resume", kw["resume"]] if kw.get("resume") else []
        return [sys.executable, str(script), *resume, prompt], {}
    monkeypatch.setattr("workflow.deploy.jobs.claude_code_argv", fake_argv)

    job = Job.from_payload({"id": "job1", "model": "claude-code", "instructions": "Do it"})
    client = JobClient("http://ng", "ws", token="t")
    seen = []

    def until_working(text):
        seen.append(text)
        # The runner dies once the agent has done some work.
        return (tmp_path / "jobs" / "job1" / "repo" / "work.txt").exists()

    with pytest.raises(JobCancelled):
        run_job(job, config={}, use_tmux=False, client=client, cwd=repo,
                on_tick=until_working, tick_seconds=0.05)

    ticks = []
    out = run_job(job, config={}, use_tmux=False, client=client, cwd=repo,
                  on_tick=lambda text: ticks.append(text) or False, tick_seconds=0.05)

    assert calls[0]["resume"] is None
    assert calls[1] == {"prompt": checkpoint.RESUME_PROMPT, "resume": "s-first"}
    assert out["result"] == "resumed s-first"
    assert "↻ resuming" in "".join(ticks)
    # Same worktree, with the first attempt's file still in it.
    assert (tmp_path / "jobs" / "job1" / "repo" / "work.txt").read_text() == "half"
    # Counted from the real start, so the first attempt's work is in the record.
    assert "work.txt" in [s["path"] for s in out["changes"]["git"]["stat"]]


def test_a_finished_attempt_is_not_resumed(repo, tmp_path, config_dir, monkeypatch):
    monkeypatch.setattr("workflow.deploy.jobs.JOB_ROOT", tmp_path / "jobs")
    monkeypatch.setattr("workflow.deploy.jobs.load_agent_rules", lambda client: "")
    resumes = []

    def fake_argv(job, prompt, job_dir, client, **kw):
        resumes.append(kw.get("resume"))
        return [sys.executable, "-c", "print('ok')"], {}
    monkeypatch.setattr("workflow.deploy.jobs.claude_code_argv", fake_argv)
    job = Job.from_payload({"id": "job1", "model": "claude-code", "instructions": "x"})
    client = JobClient("http://ng", "ws", token="t")
    job_dir = tmp_path / "jobs" / "job1"
    job_dir.mkdir(parents=True)
    path = session_file(config_dir, "s-1")
    (job_dir / "session.json").write_text(json.dumps(
        {"id": "s-1", "cwd": str(job_dir / "repo"), "file": str(path)}))
    (job_dir / "exit_code").write_text("0")

    run_job(job, config={}, use_tmux=False, client=client, cwd=repo, tick_seconds=0.05)
    assert resumes == [None]
