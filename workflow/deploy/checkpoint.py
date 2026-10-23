"""Checkpoints: what a run carries if it has to start again (docs/MOBILE_AGENTS.md).

A run is packed while it is healthy, because a device that has lost the
network can pack nothing. This is phase 1 of that design: checkpoints are
written into the job directory, and a job that is claimed again on the same
device resumes its Claude session (`claude --resume`) in the worktree its
first attempt left, instead of starting over. Uploading them, so a run can
move to another device, comes later.

Files in <job-dir>:
    base.json        where the repo stood before the first attempt
                     {"top", "base", "branch"?, "common", "remote"?}; kept
                     across attempts so the changes record and the
                     checkpoint both count from the real start
    session.json     the provider's session {"id", "cwd", "file"?},
                     written as soon as the session starts
    checkpoint/      the newest checkpoint, replaced whole each time:
        manifest.json    job, seq, when, why, device, session, repo,
                         spent, outside
        work.bundle      git bundle base..snapshot (see `snapshot`)
        session.jsonl    a copy of Claude's session transcript

A checkpoint holds no secrets: mcp.json (the NotesGraph token) is never in
it, and .gitignore'd files (.env, node_modules) are left out of the bundle.
"""

import json
import os
import shutil
import socket
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from workflow.deploy.run_changes import git_base

# A checkpoint is taken at most this often while a run goes, and only when
# something moved since the last one.
CHECKPOINT_EVERY_SECONDS = 120
# An untracked file bigger than this (a dataset, a build output nobody
# ignored) is left out of the snapshot, so it is not hashed into the repo
# every few minutes; the manifest lists it. A bundle bigger than the second
# is left out of the checkpoint rather than filling the disk.
MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_BUNDLE_BYTES = 50 * 1024 * 1024
# More big files than this and the work is not snapshotted at all: the
# exclusions would not fit on a command line.
MAX_SKIPPED_FILES = 200
GIT_TIMEOUT = 60
REF_PREFIX = "refs/wf/checkpoints/"
# Who the snapshot commits are by. They never reach a branch.
_IDENTITY = {"GIT_AUTHOR_NAME": "wf", "GIT_AUTHOR_EMAIL": "wf@localhost",
             "GIT_COMMITTER_NAME": "wf", "GIT_COMMITTER_EMAIL": "wf@localhost"}

# What a resumed run is told first. Its conversation is back as it was, so
# this only has to say why it stopped and that the files are as it left them.
RESUME_PROMPT = """\
This run was interrupted before it finished: the runner on this device
stopped or lost contact with NotesGraph, and the run has now been started
again here. Your conversation above is restored, and the working directory
is as you left it, uncommitted changes included. A tool call you were in the
middle of may not have completed.

Carry on with the task from where you were. Check what is already done
before redoing anything. If you were waiting on an answer from the user,
ask again: it may have been given meanwhile and will come straight back.
"""


def _git(cwd: str, *args: str, env: Optional[dict] = None) -> Optional[str]:
    try:
        proc = subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True,
                              timeout=GIT_TIMEOUT, errors="replace",
                              env={**os.environ, **env} if env else None)
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    return proc.stdout.strip() if proc.returncode == 0 else None


def _read_json(path: Path) -> Optional[dict]:
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _write_json(path: Path, value: dict) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(value))
    os.replace(tmp, path)


# --- where the run started --------------------------------------------------

def run_base(job_dir: Path, cwd: Optional[str]) -> Optional[dict]:
    """Where the repo at `cwd` stood before this job's first attempt.

    Saved on the first attempt and read back on the next, so a re-claimed
    run counts its changes from the real start rather than from wherever the
    interrupted attempt got to. A saved base for another checkout is stale
    and replaced.
    """
    if not cwd:
        return None
    current = git_base(cwd)
    if current is None:
        return None
    saved = _read_json(job_dir / "base.json")
    if saved and saved.get("top") == current["top"] and saved.get("base"):
        return saved
    common = _git(current["top"], "rev-parse", "--path-format=absolute", "--git-common-dir")
    if common:
        current["common"] = common
    remote = _git(current["top"], "remote", "get-url", "origin")
    if remote:
        current["remote"] = remote
    try:
        _write_json(job_dir / "base.json", current)
    except OSError:
        pass
    return current


# --- the provider's session ---------------------------------------------------

def claude_config_dir(env: Optional[dict] = None) -> Path:
    """Where Claude Code keeps its sessions for a run with this environment."""
    found = (env or {}).get("CLAUDE_CONFIG_DIR") or os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(found) if found else Path.home() / ".claude"


def find_session_file(session_id: str, env: Optional[dict] = None) -> Optional[Path]:
    """Claude's transcript for `session_id`: <config>/projects/<cwd slug>/<id>.jsonl.

    Found by name rather than by working out the slug, which Claude derives
    from the working directory in a way that has changed between versions.
    """
    if not session_id:
        return None
    for path in (claude_config_dir(env) / "projects").glob(f"*/{session_id}.jsonl"):
        return path
    return None


def save_session(job_dir: Path, session_id: str, cwd: Optional[str],
                 env: Optional[dict] = None) -> dict:
    """Record the run's session as soon as it starts; returns what was saved."""
    info = {"id": session_id, "cwd": cwd}
    found = find_session_file(session_id, env)
    if found:
        info["file"] = str(found)
    try:
        _write_json(job_dir / "session.json", info)
    except OSError:
        pass
    return info


def interrupted_session(job_dir: Path) -> Optional[dict]:
    """The session of an earlier attempt that never finished, or None.

    Read before the runner clears the previous attempt's files: an attempt
    that wrote exit_code ended on its own and is not picked up again.
    """
    if (job_dir / "exit_code").exists():
        return None
    info = _read_json(job_dir / "session.json")
    return info if info and info.get("id") else None


def resumable(info: Optional[dict], job_dir: Path, workdir: Optional[Path],
              env: Optional[dict] = None) -> Optional[str]:
    """The session id to resume in `workdir`, or None to start over.

    Claude finds a session by the directory it runs in, so it resumes only
    in the same one. A transcript that has gone from Claude's own directory
    is put back from the checkpoint's copy.
    """
    if not info or workdir is None or info.get("cwd") != str(workdir):
        return None
    session_id = info["id"]
    file = Path(info["file"]) if info.get("file") else find_session_file(session_id, env)
    if file and file.exists():
        return session_id
    saved = job_dir / "checkpoint" / "session.jsonl"
    if file is None or not saved.exists():
        return None
    try:
        file.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(saved, file)
    except OSError:
        return None
    return session_id


# --- the work ---------------------------------------------------------------

def _big_untracked(top: str) -> Optional[list]:
    """Untracked, unignored files over MAX_FILE_BYTES; None if git can't say."""
    listed = _git(top, "ls-files", "-z", "--others", "--exclude-standard")
    if listed is None:
        return None
    big = []
    for path in filter(None, listed.split("\0")):
        try:
            if os.lstat(os.path.join(top, path)).st_size > MAX_FILE_BYTES:
                big.append(path)
        except OSError:
            continue
    return big


def stage(top: str, scratch: Path) -> Optional[dict]:
    """The worktree at `top` as a tree, written without touching it.

    Tracked and untracked files (not ignored ones, nor untracked ones over
    MAX_FILE_BYTES) go through an index of our own, seeded from the
    worktree's so only changed files are hashed. The agent's index, branch
    and stash are untouched. Returns {"head", "tree", "skipped"}, or None if
    git could not do it.
    """
    index = scratch / "ckpt.index"
    real = _git(top, "rev-parse", "--path-format=absolute", "--git-path", "index")
    head = _git(top, "rev-parse", "HEAD")
    big = _big_untracked(top)
    if not head or big is None or len(big) > MAX_SKIPPED_FILES:
        return None
    try:
        if real and Path(real).exists():
            shutil.copyfile(real, index)
        else:
            index.unlink(missing_ok=True)
    except OSError:
        return None
    env = {"GIT_INDEX_FILE": str(index), **_IDENTITY}
    excluded = [f":(exclude,literal){path}" for path in big]
    if _git(top, "add", "-A", "--", ".", *excluded, env=env) is None:
        return None
    tree = _git(top, "write-tree", env=env)
    return {"head": head, "tree": tree, "skipped": big} if tree else None


def commit(top: str, job_id: str, staged: dict) -> Optional[str]:
    """Commit a staged tree on HEAD, held by refs/wf/checkpoints/<job>.

    Nothing is pushed and no branch moves; the ref keeps the snapshot from
    git's garbage collection until `discard`.
    """
    made = _git(top, "commit-tree", staged["tree"], "-p", staged["head"],
                "-m", f"wf checkpoint {job_id}", env=_IDENTITY)
    if not made or _git(top, "update-ref", REF_PREFIX + job_id, made) is None:
        return None
    return made


def snapshot(top: str, job_id: str, scratch: Path) -> Optional[dict]:
    """Stage and commit the worktree: {"commit", "head", "tree", "skipped"}."""
    staged = stage(top, scratch)
    made = commit(top, job_id, staged) if staged else None
    return {**staged, "commit": made} if made else None


def write_bundle(top: str, base: str, job_id: str, path: Path) -> Optional[str]:
    """Bundle what the run made since `base`; None when it fits, else why not."""
    if _git(top, "bundle", "create", str(path), f"{base}..{REF_PREFIX}{job_id}") is None:
        path.unlink(missing_ok=True)
        return "git could not bundle the work"
    if path.stat().st_size > MAX_BUNDLE_BYTES:
        path.unlink(missing_ok=True)
        return f"the work is over {MAX_BUNDLE_BYTES // (1024 * 1024)} MB"
    return None


def _copy_session(source: Path, dest: Path, chunk: int = 1024 * 1024) -> bool:
    """Copy a transcript Claude may be appending to, up to its last whole line.

    Streamed, so a long session is never read into memory at once.
    """
    try:
        with open(source, "rb") as src:
            size = os.fstat(src.fileno()).st_size
            # Where the last whole line ends, looking back from the end.
            end = size
            while end > 0:
                start = max(0, end - chunk)
                src.seek(start)
                found = src.read(end - start).rfind(b"\n")
                if found >= 0:
                    end = start + found + 1
                    break
                end = start
            src.seek(0)
            with open(dest, "wb") as out:
                left = end
                while left:
                    data = src.read(min(chunk, left))
                    if not data:
                        break
                    out.write(data)
                    left -= len(data)
    except OSError:
        return False
    return True


def _inside(path: Path, top: str) -> bool:
    try:
        path.resolve().relative_to(Path(top).resolve())
        return True
    except (ValueError, OSError):
        return False


# --- taking checkpoints -------------------------------------------------------

class Checkpointer:
    """Takes a run's checkpoints into its job directory as it goes.

    `maybe` is called as the run's output arrives. At most every `every`
    seconds it starts one in the background, so reading the run's output
    never waits on git; it is written only when the work, the session or the
    run's spend moved. `take` takes one now and waits for it (when a run
    fails); `wait` lets one in flight finish. Never raises: a checkpoint that
    fails leaves the previous one in place, and the run goes on.

    A repo that holds the job directory itself (a home directory kept in
    git, say) is not the agent's work, and is not snapshotted: it would take
    in the job's own files, the NotesGraph token among them.
    """

    def __init__(self, job_dir: Path, cwd: Optional[str], start: Optional[dict],
                 env: Optional[dict] = None, job: Optional[dict] = None,
                 every: float = CHECKPOINT_EVERY_SECONDS,
                 clock: Callable[[], float] = time.monotonic, background: bool = True):
        if start and _inside(job_dir, start["top"]):
            start = None
        self.job_dir, self.cwd, self.start, self.env = job_dir, cwd, start, env
        self.job = job or {}
        self.session: Optional[dict] = None
        self.every, self.clock, self.background = every, clock, background
        self.last_at = clock()
        self.last_state: Optional[tuple] = None
        self.thread: Optional[threading.Thread] = None
        previous = _read_json(job_dir / "checkpoint" / "manifest.json") or {}
        self.seq = int(previous.get("seq") or 0)

    def note_session(self, session_id: Optional[str]) -> None:
        """Record the run's session the moment it is known (see save_session)."""
        if session_id and (self.session or {}).get("id") != session_id:
            self.session = save_session(self.job_dir, session_id, self.cwd, self.env)

    def maybe(self, run) -> bool:
        """Start a checkpoint if one is due; True if one was started."""
        if self.clock() - self.last_at < self.every:
            return False
        if self.thread is not None and self.thread.is_alive():
            return False
        self.last_at = self.clock()
        facts = self._facts(run)
        if not self.background:
            return self._safe(facts, "periodic")
        self.thread = threading.Thread(target=self._safe, args=(facts, "periodic"),
                                       name="checkpoint", daemon=True)
        self.thread.start()
        return True

    def take(self, run, why: str) -> bool:
        """Take a checkpoint now, after any in flight; True if one was written."""
        self.wait()
        self.last_at = self.clock()
        return self._safe(self._facts(run), why)

    def wait(self) -> None:
        if self.thread is not None:
            self.thread.join()
            self.thread = None

    @staticmethod
    def _facts(run) -> dict:
        """What a checkpoint needs from the run, read on the run's own thread."""
        recorder = getattr(run, "recorder", None)
        outside = sorted({f["path"] for f in (getattr(recorder, "files", None) or [])
                          if os.path.isabs(f.get("path") or "")})
        return {"session_id": getattr(run, "session_id", None),
                "steps": getattr(run, "steps", 0), "cost": getattr(run, "cost", None),
                "tokens": getattr(run, "tokens", None), "outside": outside}

    def _safe(self, facts: dict, why: str) -> bool:
        try:
            return self._take(facts, why)
        except Exception:
            return False

    def _take(self, facts: dict, why: str) -> bool:
        session_id = facts["session_id"]
        session_file = find_session_file(session_id, self.env) if session_id else None
        if session_file and self.session and not self.session.get("file"):
            # Claude writes the transcript after it announces the session.
            self.session = save_session(self.job_dir, session_id, self.cwd, self.env)
        staged = stage(self.start["top"], self.job_dir) if self.start else None
        state = ((staged or {}).get("tree"), (staged or {}).get("head"),
                 session_file.stat().st_size if session_file else None,
                 facts["steps"], facts["cost"], facts["tokens"])
        # Before anything is committed: an idle run makes no new objects.
        if state == self.last_state:
            return False

        new = self.job_dir / "checkpoint.new"
        shutil.rmtree(new, ignore_errors=True)
        new.mkdir()
        manifest: dict = {
            "version": 1, "jobId": self.job_dir.name, "seq": self.seq + 1,
            "takenAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "why": why, "device": socket.gethostname(), "job": self.job,
            "spent": {"costUsd": facts["cost"], "tokens": facts["tokens"],
                      "steps": facts["steps"]},
        }
        if session_id:
            manifest["session"] = {"id": session_id, "copied": bool(
                session_file and _copy_session(session_file, new / "session.jsonl"))}
        made = commit(self.start["top"], self.job_dir.name, staged) if staged else None
        if self.start and made:
            repo = {key: self.start[key] for key in ("base", "branch", "remote")
                    if self.start.get(key)}
            repo.update(head=staged["head"], checkpoint=made)
            if staged["skipped"]:
                repo["filesSkipped"] = staged["skipped"]
            skipped = write_bundle(self.start["top"], self.start["base"],
                                   self.job_dir.name, new / "work.bundle")
            if skipped:
                repo["bundleSkipped"] = skipped
            manifest["repo"] = repo
        if facts["outside"]:
            manifest["outside"] = facts["outside"]
        (new / "manifest.json").write_text(json.dumps(manifest, indent=1))

        # Swap it in whole, so a reader never sees half of one.
        current, old = self.job_dir / "checkpoint", self.job_dir / "checkpoint.old"
        shutil.rmtree(old, ignore_errors=True)
        if current.exists():
            current.rename(old)
        new.rename(current)
        shutil.rmtree(old, ignore_errors=True)
        self.seq += 1
        self.last_state = state
        return True


# --- dropping them ------------------------------------------------------------

# A failed run's checkpoint is kept this long, to be continued from.
KEEP_DAYS = 14


def discard(job_dir: Path) -> None:
    """Drop a run's checkpoint: its ref in the repo and its files.

    For a run that finished or was cancelled; one that failed keeps its
    checkpoint for KEEP_DAYS (see `prune`). The repo is reached through its
    common git directory, since the job's worktree may already be gone.
    """
    base = _read_json(job_dir / "base.json") or {}
    if base.get("common") and Path(base["common"]).exists():
        try:
            subprocess.run(["git", "--git-dir", base["common"], "update-ref", "-d",
                            REF_PREFIX + job_dir.name],
                           capture_output=True, timeout=GIT_TIMEOUT)
        except (OSError, subprocess.SubprocessError):
            pass
    for name in ("checkpoint", "checkpoint.new", "checkpoint.old"):
        shutil.rmtree(job_dir / name, ignore_errors=True)
    for name in ("session.json", "ckpt.index"):
        (job_dir / name).unlink(missing_ok=True)


def prune(job_root: Path, days: float = KEEP_DAYS, now: Optional[float] = None) -> int:
    """Discard checkpoints not touched for `days`; returns how many."""
    cutoff = (time.time() if now is None else now) - days * 86400
    dropped = 0
    try:
        job_dirs = list(job_root.iterdir())
    except OSError:
        return 0
    for job_dir in job_dirs:
        marker = job_dir / "checkpoint" / "manifest.json"
        try:
            if marker.stat().st_mtime >= cutoff:
                continue
        except OSError:
            continue
        discard(job_dir)
        dropped += 1
    return dropped
