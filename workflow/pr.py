"""Pull requests as an agent asks about them: one call, one answer.

Agents look at pull requests all the time, and through raw `gh` it takes a
handful of calls each time. Counted over a few months of Claude Code
transcripts: `gh run view` 561 times, `gh pr view` 558, `gh pr list` 255,
`gh run list` 222, `gh run watch` 203, `gh pr diff` 105, and 130 `gh api`
calls for a run's jobs and their logs. Nearly all of it asks one of four
things, and each is a function here:

    pr_summary   is it open, mergeable, reviewed and green, and what task is
                 it for (the fields `gh pr view --json` was asked for most:
                 state, mergeable, base and head, merge commit, checks)
    pr_checks    which check failed, at which step, with the end of its log
    ci_runs /    the workflow runs of a branch, and one run's jobs (a deploy
    ci_run       or a build is watched this way as often as a PR's checks)
    pr_review    everything a review reads: the task, the description, the
                 comments, the files and the diff

Every summary names the task the pull request belongs to. The key comes
from the head branch (`wf start` names branches `<key>-<title>`) or the
title, and the task from the backend of the project that owns the
repository, with its NotesGraph note and block when it was cut from one.

Everything here only reads. `gh` does the talking to GitHub, so it uses
whatever account `gh` is signed in to.
"""
from __future__ import annotations

import json
import re
import subprocess
from typing import Callable, List, Optional

# Enough of a failing job's log to show the error without flooding an
# agent's context: a job reports its failure last.
LOG_TAIL = 4000
# A review reads the diff, but a generated lockfile shouldn't crowd out the
# rest of the answer.
DIFF_LIMIT = 60000
# Failing jobs whose logs one call fetches; the rest are named only.
MAX_FAILED_LOGS = 3

SUMMARY_FIELDS = (
    "number,title,url,state,isDraft,author,baseRefName,headRefName,headRefOid,"
    "mergeable,mergeStateStatus,reviewDecision,mergedAt,mergeCommit,additions,"
    "deletions,changedFiles,statusCheckRollup,updatedAt"
)
LIST_FIELDS = (
    "number,title,url,state,isDraft,author,baseRefName,headRefName,"
    "mergeable,reviewDecision,statusCheckRollup,updatedAt"
)
RUN_FIELDS = "databaseId,workflowName,displayTitle,status,conclusion,event,headBranch,headSha,createdAt,url"

_TASK_KEY = re.compile(r"^(?:[\w.-]+/)?([A-Za-z][A-Za-z0-9]*)-(\d+)\b")
_ONLY_KEY = re.compile(r"^[A-Za-z][A-Za-z0-9]*-\d+$")
_JOB_URL = re.compile(r"/actions/runs/(\d+)(?:/job/(\d+))?")
_JOB_REPO = re.compile(r"github\.com/([^/]+/[^/]+)/actions/")
_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
_LOG_STAMP = re.compile(r"^(?:\ufeff)?\d{4}-\d\d-\d\dT[\d:.]+Z ")
_NOTE_BLOCK = re.compile(r"docId (\S+?), blockId (\S+?)[.,\s]")

FAILED = {"FAILURE", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED", "STARTUP_FAILURE", "ERROR"}
PASSED = {"SUCCESS"}
SKIPPED = {"SKIPPED", "NEUTRAL"}


class PrError(Exception):
    """GitHub could not be asked, or had no such pull request or run."""


def gh(args: List[str], cwd: Optional[str] = None, timeout: int = 60) -> str:
    """Run `gh` and return its output; PrError says what it complained of."""
    try:
        result = subprocess.run(["gh", *args], cwd=cwd, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        raise PrError("gh (the GitHub CLI) is not installed")
    except subprocess.TimeoutExpired:
        raise PrError(f"gh {' '.join(args[:3])} gave no answer in {timeout}s")
    if result.returncode != 0:
        raise PrError((result.stderr or result.stdout).strip() or f"gh {' '.join(args[:3])} failed")
    return result.stdout


Runner = Callable[..., str]


def _repo_args(repo: Optional[str]) -> List[str]:
    return ["--repo", repo] if repo else []


def _json(run: Runner, args: List[str], cwd: Optional[str]):
    out = run(args, cwd=cwd)
    try:
        return json.loads(out or "null")
    except ValueError:
        raise PrError(f"gh {' '.join(args[:3])} did not answer in JSON")


def task_key(*texts: Optional[str]) -> Optional[str]:
    """The task key a branch or title starts with: `personal-12-fix` and
    `PERSONAL-12 Fix it` both give PERSONAL-12."""
    for text in texts:
        m = _TASK_KEY.match((text or "").strip())
        if m:
            return f"{m.group(1).upper()}-{m.group(2)}"
    return None


def find_task(key: str, repo_path: Optional[str] = None):
    """The task of this key in the backend of the project that owns the
    repository (else the current project), or None."""
    from workflow.backends import get_backend
    from workflow.config import load_config, load_effective_config
    from workflow.projects import get_current_project, project_for_path

    project = project_for_path(repo_path) or get_current_project()
    backend = (get_backend(load_effective_config(project)) if project
               else get_backend(load_config(), force_type="markdown"))
    if backend is None:
        return None
    if hasattr(backend, "get_task"):
        return backend.get_task(key)
    return next((t for t in backend.list_tasks() if t.key.lower() == key.lower()), None)


def linked_task(head: Optional[str], title: Optional[str], repo_path: Optional[str] = None,
                lookup: Optional[Callable] = None, describe: bool = False) -> Optional[dict]:
    """The task a pull request is for: its key, and from the tracker its
    title and status, where it lives, and the NotesGraph note block it was
    cut from. Only the key when the tracker has no such task."""
    key = task_key(head, title)
    if not key:
        return None
    try:
        task = (lookup or find_task)(key, repo_path)
    except Exception:
        task = None
    if task is None:
        return {"key": key, "found": False}
    description = getattr(task, "description", "") or ""
    out = {"key": task.key, "found": True, "title": task.title, "status": task.status}
    for name, attr in (("url", "url"), ("file", "file_path")):
        if getattr(task, attr, None):
            out[name] = str(getattr(task, attr))
    note = _NOTE_BLOCK.search(description + " ")
    if note:
        out["note"] = {"docId": note.group(1), "blockId": note.group(2)}
    if describe:
        out["description"] = description
    return out


def summarize_checks(rollup: Optional[list]) -> dict:
    """A pull request's checks, counted, with the failing and the running
    ones named. A check run and a commit status are told apart by GitHub;
    here they are one list."""
    counts = {"passed": 0, "failed": 0, "pending": 0, "skipped": 0}
    failing, pending = [], []
    for item in rollup or []:
        if item.get("__typename") == "StatusContext":
            name, workflow, url = item.get("context"), None, item.get("targetUrl")
            state = (item.get("state") or "").upper()
            done, outcome = state not in ("PENDING", "EXPECTED", ""), state
        else:
            name, workflow, url = item.get("name"), item.get("workflowName"), item.get("detailsUrl")
            done = (item.get("status") or "").upper() == "COMPLETED"
            outcome = (item.get("conclusion") or "").upper()
        entry = {"name": name, **({"workflow": workflow} if workflow else {}), "url": url}
        ids = _JOB_URL.search(url or "")
        if ids:
            entry["run_id"] = int(ids.group(1))
            if ids.group(2):
                entry["job_id"] = int(ids.group(2))
        if not done:
            counts["pending"] += 1
            pending.append(entry)
        elif outcome in PASSED:
            counts["passed"] += 1
        elif outcome in SKIPPED:
            counts["skipped"] += 1
        else:
            counts["failed"] += 1
            failing.append({**entry, "conclusion": outcome.lower() or "failed"})
    total = sum(counts.values())
    state = ("none" if not total else "failing" if counts["failed"]
             else "pending" if counts["pending"] else "passing")
    return {"state": state, **counts, "failing": failing, "pending_checks": pending}


def verdict(pr: dict) -> str:
    """Where the pull request stands, in a line: the first thing in the way
    of merging it, or that nothing is."""
    checks = pr["checks"]
    if pr["state"] == "MERGED":
        return "merged" + (f" as {pr['merge_commit'][:8]}" if pr.get("merge_commit") else "")
    if pr["state"] == "CLOSED":
        return "closed without merging"
    if pr["mergeable"] == "CONFLICTING":
        return f"conflicts with {pr['base']}"
    blockers = []
    if pr["draft"]:
        blockers.append("a draft")
    if pr["review"] == "CHANGES_REQUESTED":
        blockers.append("changes requested")
    if checks["failed"]:
        blockers.append(f"{checks['failed']} check{'s' if checks['failed'] != 1 else ''} failing")
    elif checks["pending"]:
        blockers.append(f"{checks['pending']} check{'s' if checks['pending'] != 1 else ''} still running")
    if pr["review"] == "REVIEW_REQUIRED":
        blockers.append("review required")
    if blockers:
        return "not ready: " + ", ".join(blockers)
    if pr["mergeable"] == "UNKNOWN":
        return "GitHub is still working out whether it merges cleanly"
    return "ready to merge"


def resolve_pr(pr: Optional[str], repo_path: Optional[str], repo: Optional[str], run: Runner) -> Optional[str]:
    """What to hand `gh pr view`: a number, URL or branch as given, nothing
    for the current branch's pull request, and for a task key the newest
    pull request whose branch or title carries it."""
    if not pr or not _ONLY_KEY.match(pr.strip()):
        return pr
    key = pr.strip().upper()
    found = _json(run, ["pr", "list", "--state", "all", "--search", key, "--limit", "30",
                        "--json", "number,title,headRefName", *_repo_args(repo)], repo_path)
    for row in found or []:
        if task_key(row.get("headRefName"), row.get("title")) == key:
            return str(row["number"])
    raise PrError(f"No pull request for {key} in this repository")


def _shape(raw: dict) -> dict:
    out = {
        "number": raw.get("number"), "title": raw.get("title"), "url": raw.get("url"),
        "state": raw.get("state"), "draft": bool(raw.get("isDraft")),
        "author": (raw.get("author") or {}).get("login"),
        "base": raw.get("baseRefName"), "head": raw.get("headRefName"),
        "mergeable": raw.get("mergeable"), "review": raw.get("reviewDecision") or None,
        "updated_at": raw.get("updatedAt"),
        "checks": summarize_checks(raw.get("statusCheckRollup")),
    }
    if "headRefOid" in raw:
        out.update({
            "head_sha": raw.get("headRefOid"), "merge_state": raw.get("mergeStateStatus"),
            "merged_at": raw.get("mergedAt"),
            "merge_commit": (raw.get("mergeCommit") or {}).get("oid"),
            "size": {"files": raw.get("changedFiles"), "additions": raw.get("additions"),
                     "deletions": raw.get("deletions")},
        })
    return out


def pr_summary(repo_path: Optional[str] = None, pr: Optional[str] = None, repo: Optional[str] = None,
               run: Runner = gh, lookup: Optional[Callable] = None) -> dict:
    """One pull request: state, branches, whether it merges, review, checks,
    size, the task it is for, and a one-line verdict."""
    target = resolve_pr(pr, repo_path, repo, run)
    raw = _json(run, ["pr", "view", *([target] if target else []), "--json", SUMMARY_FIELDS,
                      *_repo_args(repo)], repo_path)
    out = _shape(raw)
    out["task"] = linked_task(out["head"], out["title"], repo_path, lookup)
    out["verdict"] = verdict(out)
    return out


def pr_list(repo_path: Optional[str] = None, state: str = "open", mine: bool = False,
            search: Optional[str] = None, limit: int = 30, repo: Optional[str] = None,
            tasks: bool = True, run: Runner = gh, lookup: Optional[Callable] = None) -> List[dict]:
    """The repository's pull requests, each with its checks counted and its
    task. `tasks=False` gives only each task's key (no tracker lookups)."""
    args = ["pr", "list", "--state", state, "--limit", str(limit), "--json", LIST_FIELDS,
            *(["--author", "@me"] if mine else []), *(["--search", search] if search else []),
            *_repo_args(repo)]
    rows = []
    for raw in _json(run, args, repo_path) or []:
        row = _shape(raw)
        if tasks:
            row["task"] = linked_task(row["head"], row["title"], repo_path, lookup)
        else:
            key = task_key(row["head"], row["title"])
            row["task"] = {"key": key} if key else None
        rows.append(row)
    return rows


def _tail(text: str, limit: int) -> str:
    text = text.rstrip()
    return text if len(text) <= limit else "… " + text[-limit:]


def _failed_log(run: Runner, job_id: int, repo_path: Optional[str], repo: Optional[str], tail: int,
                url: Optional[str] = None) -> str:
    try:
        out = run(["run", "view", "--job", str(job_id), "--log-failed", *_repo_args(repo)],
                  cwd=repo_path, timeout=120)
    except PrError as e:
        # `gh run view` gives no logs until the whole run has ended; a job
        # that already failed has its own log through the API.
        owner = _JOB_REPO.search(url or "")
        try:
            if not owner:
                raise e
            # Logs carry colour codes, which `gh api` otherwise refuses to print.
            out = run(["api", "--allow-escape-sequences",
                       f"repos/{owner.group(1)}/actions/jobs/{job_id}/logs"],
                      cwd=repo_path, timeout=120)
        except PrError:
            return f"(log not available: {e})"
        lines = [_LOG_STAMP.sub("", _ANSI.sub("", line)) for line in out.splitlines()]
        # The whole job's log: stop at its last error, before the cleanup
        # steps that follow a failure.
        errors = [i for i, line in enumerate(lines) if "##[error]" in line]
        return _tail("\n".join(lines[:errors[-1] + 1] if errors else lines), tail)
    # `gh` prefixes every line with the job and step names and a timestamp.
    lines = [re.sub(r"^[^\t]*\t[^\t]*\t(?:﻿)?(?:\d{4}-\d\d-\d\dT[\d:.]+Z )?", "", line)
             for line in out.splitlines()]
    return _tail("\n".join(lines), tail)


def _jobs(run: Runner, run_id: int, repo_path: Optional[str], repo: Optional[str]) -> dict:
    return _json(run, ["run", "view", str(run_id), "--json",
                       "databaseId,workflowName,displayTitle,status,conclusion,event,headBranch,"
                       "headSha,createdAt,url,attempt,jobs", *_repo_args(repo)], repo_path)


def _shape_job(job: dict) -> dict:
    return {
        "id": job.get("databaseId"), "name": job.get("name"), "status": job.get("status"),
        "conclusion": job.get("conclusion") or None, "url": job.get("url"),
        "failed_steps": [s.get("name") for s in job.get("steps") or []
                         if (s.get("conclusion") or "").lower() == "failure"],
    }


def pr_checks(repo_path: Optional[str] = None, pr: Optional[str] = None, repo: Optional[str] = None,
              logs: bool = True, tail: int = LOG_TAIL, run: Runner = gh) -> dict:
    """A pull request's checks, and for each failing job the step that
    failed and the end of its log (the first few jobs; the rest are named)."""
    target = resolve_pr(pr, repo_path, repo, run)
    raw = _json(run, ["pr", "view", *([target] if target else []), "--json",
                      "number,title,url,headRefName,statusCheckRollup", *_repo_args(repo)], repo_path)
    checks = summarize_checks(raw.get("statusCheckRollup"))
    steps_of: dict = {}
    fetched = 0
    for check in checks["failing"]:
        run_id, job_id = check.get("run_id"), check.get("job_id")
        if not (logs and run_id and job_id):
            continue
        if run_id not in steps_of:
            try:
                steps_of[run_id] = {j.get("databaseId"): _shape_job(j)
                                    for j in _jobs(run, run_id, repo_path, repo).get("jobs") or []}
            except PrError:
                steps_of[run_id] = {}
        job = steps_of[run_id].get(job_id)
        if job:
            check["failed_steps"] = job["failed_steps"]
        if fetched < MAX_FAILED_LOGS and check["conclusion"] != "cancelled":
            check["log_tail"] = _failed_log(run, job_id, repo_path, repo, tail, check.get("url"))
            fetched += 1
    return {"number": raw.get("number"), "title": raw.get("title"), "url": raw.get("url"),
            "head": raw.get("headRefName"), **checks}


def ci_runs(repo_path: Optional[str] = None, branch: Optional[str] = None, workflow: Optional[str] = None,
            commit: Optional[str] = None, limit: int = 10, repo: Optional[str] = None,
            run: Runner = gh) -> List[dict]:
    """Recent workflow runs, newest first, narrowed to a branch, a workflow
    (its name or file) or a commit."""
    args = ["run", "list", "--limit", str(limit), "--json", RUN_FIELDS,
            *(["--branch", branch] if branch else []), *(["--workflow", workflow] if workflow else []),
            *(["--commit", commit] if commit else []), *_repo_args(repo)]
    return [{
        "id": r.get("databaseId"), "workflow": r.get("workflowName"), "title": r.get("displayTitle"),
        "status": r.get("status"), "conclusion": r.get("conclusion") or None, "event": r.get("event"),
        "branch": r.get("headBranch"), "sha": r.get("headSha"), "created_at": r.get("createdAt"),
        "url": r.get("url"),
    } for r in _json(run, args, repo_path) or []]


def ci_run(run_id: int, repo_path: Optional[str] = None, repo: Optional[str] = None,
           logs: bool = True, tail: int = LOG_TAIL, run: Runner = gh) -> dict:
    """One workflow run: where it stands, its jobs, and for each failed job
    the step that failed and the end of its log."""
    raw = _jobs(run, int(run_id), repo_path, repo)
    jobs = [_shape_job(j) for j in raw.get("jobs") or []]
    fetched = 0
    for job in jobs:
        if (job["conclusion"] or "").lower() == "failure" and logs and fetched < MAX_FAILED_LOGS:
            job["log_tail"] = _failed_log(run, job["id"], repo_path, repo, tail, job.get("url"))
            fetched += 1
    return {
        "id": raw.get("databaseId"), "workflow": raw.get("workflowName"), "title": raw.get("displayTitle"),
        "status": raw.get("status"), "conclusion": raw.get("conclusion") or None,
        "event": raw.get("event"), "branch": raw.get("headBranch"), "sha": raw.get("headSha"),
        "attempt": raw.get("attempt"), "created_at": raw.get("createdAt"), "url": raw.get("url"),
        "jobs": jobs,
    }


def pr_review(repo_path: Optional[str] = None, pr: Optional[str] = None, repo: Optional[str] = None,
              diff_limit: int = DIFF_LIMIT, run: Runner = gh, lookup: Optional[Callable] = None) -> dict:
    """What a review of the pull request reads, gathered once: the summary,
    the task with its description, the pull request's own description, what
    reviewers and commenters said, the files, and the diff."""
    target = resolve_pr(pr, repo_path, repo, run)
    raw = _json(run, ["pr", "view", *([target] if target else []), "--json",
                      SUMMARY_FIELDS + ",body,files,comments,reviews", *_repo_args(repo)], repo_path)
    out = _shape(raw)
    out["task"] = linked_task(out["head"], out["title"], repo_path, lookup, describe=True)
    out["verdict"] = verdict(out)
    out["body"] = raw.get("body") or ""
    out["files"] = [{"path": f.get("path"), "additions": f.get("additions"), "deletions": f.get("deletions")}
                    for f in raw.get("files") or []]
    said = []
    for review in raw.get("reviews") or []:
        if review.get("body") or review.get("state") not in (None, "COMMENTED"):
            said.append({"kind": "review", "state": review.get("state"),
                         "author": (review.get("author") or {}).get("login"),
                         "at": review.get("submittedAt"), "body": review.get("body") or ""})
    for comment in raw.get("comments") or []:
        said.append({"kind": "comment", "author": (comment.get("author") or {}).get("login"),
                     "at": comment.get("createdAt"), "body": comment.get("body") or ""})
    out["discussion"] = sorted(said, key=lambda c: c.get("at") or "")
    try:
        diff = run(["pr", "diff", str(out["number"]), *_repo_args(repo)], cwd=repo_path, timeout=120)
    except PrError as e:
        diff = f"(diff not available: {e})"
    out["diff_truncated"] = len(diff) > diff_limit
    out["diff"] = diff[:diff_limit]
    return out


# --- how the CLI prints them ---------------------------------------------

def _checks_line(checks: dict) -> str:
    if checks["state"] == "none":
        return "no checks"
    parts = [f"{checks[k]} {k}" for k in ("passed", "failed", "pending", "skipped") if checks[k]]
    return ", ".join(parts)


def _task_line(task: Optional[dict]) -> str:
    if not task:
        return "none found in the branch or title"
    if not task.get("found", True) or "title" not in task:
        return task["key"] + (" (not in the tracker)" if task.get("found") is False else "")
    return f"{task['key']} [{task['status']}] {task['title']}"


def format_summary(pr: dict) -> str:
    lines = [
        f"#{pr['number']} {pr['title']}",
        f"  {pr['url']}",
        f"  {pr['verdict']}",
        f"  {pr['state'].lower()}{', draft' if pr['draft'] else ''}: {pr['head']} → {pr['base']}"
        + (f" by {pr['author']}" if pr.get("author") else ""),
        f"  checks: {_checks_line(pr['checks'])}",
    ]
    for check in pr["checks"]["failing"]:
        lines.append(f"    ✗ {check['name']}" + (f" ({check['workflow']})" if check.get("workflow") else ""))
    if pr.get("review"):
        lines.append(f"  review: {pr['review'].lower().replace('_', ' ')}")
    if pr.get("size"):
        size = pr["size"]
        lines.append(f"  size: {size['files']} files, +{size['additions']} -{size['deletions']}")
    lines.append(f"  task: {_task_line(pr.get('task'))}")
    task = pr.get("task") or {}
    if task.get("note"):
        lines.append(f"    note {task['note']['docId']}, block {task['note']['blockId']}")
    for key in ("url", "file"):
        if task.get(key):
            lines.append(f"    {task[key]}")
    return "\n".join(lines)


def format_list(rows: List[dict]) -> str:
    if not rows:
        return "No pull requests."
    out = []
    for pr in rows:
        mark = {"passing": "✓", "failing": "✗", "pending": "…", "none": "–"}[pr["checks"]["state"]]
        flags = "".join(f" [{f}]" for f, on in (
            ("draft", pr["draft"]), ("conflicts", pr["mergeable"] == "CONFLICTING"),
            ("changes requested", pr["review"] == "CHANGES_REQUESTED"),
            ("approved", pr["review"] == "APPROVED")) if on)
        out.append(f"{mark} #{pr['number']} {pr['title']}{flags}")
        out.append(f"    {pr['head']} → {pr['base']} · checks: {_checks_line(pr['checks'])}"
                   f" · task: {_task_line(pr.get('task'))}")
    return "\n".join(out)


def format_checks(result: dict) -> str:
    lines = [f"#{result['number']} {result['title']}", f"  checks: {_checks_line(result)}"]
    for check in result["failing"]:
        lines.append(f"\n✗ {check['name']}" + (f" ({check['workflow']})" if check.get("workflow") else "")
                     + f": {check['conclusion']}")
        if check.get("failed_steps"):
            lines.append(f"  failed at: {', '.join(check['failed_steps'])}")
        if check.get("url"):
            lines.append(f"  {check['url']}")
        if check.get("log_tail"):
            lines.append(_indent(check["log_tail"]))
    pending = [check["name"] for check in result["pending_checks"]]
    if pending:
        more = f" and {len(pending) - 5} more" if len(pending) > 5 else ""
        lines.append(f"\n… still running: {', '.join(pending[:5])}{more}")
    return "\n".join(lines)


def _indent(text: str) -> str:
    return "\n".join("    " + line for line in text.splitlines())


def _run_mark(status: Optional[str], conclusion: Optional[str]) -> str:
    if (status or "").lower() != "completed":
        return "…"
    return "✓" if (conclusion or "").lower() == "success" else "–" if (conclusion or "").lower() == "skipped" else "✗"


def format_runs(rows: List[dict]) -> str:
    if not rows:
        return "No workflow runs."
    return "\n".join(
        f"{_run_mark(r['status'], r['conclusion'])} {r['id']} {r['workflow']} · {r['branch']} "
        f"{(r['sha'] or '')[:8]} · {r['conclusion'] or r['status']} · {r['created_at']}" for r in rows)


def format_run(result: dict) -> str:
    lines = [f"{_run_mark(result['status'], result['conclusion'])} {result['id']} {result['workflow']}: "
             f"{result['conclusion'] or result['status']}"
             + (f" (attempt {result['attempt']})" if (result.get("attempt") or 1) > 1 else ""),
             f"  {result['branch']} {(result['sha'] or '')[:8]} · {result['url']}"]
    for job in result["jobs"]:
        lines.append(f"  {_run_mark(job['status'], job['conclusion'])} {job['name']}: "
                     f"{job['conclusion'] or job['status']}")
        if job["failed_steps"]:
            lines.append(f"      failed at: {', '.join(job['failed_steps'])}")
        if job.get("log_tail"):
            lines.append(_indent(_indent(job["log_tail"])))
    return "\n".join(lines)


def format_review(pr: dict) -> str:
    task = pr.get("task") or {}
    parts = [format_summary(pr)]
    if task.get("description"):
        parts += ["", f"## Task {task['key']}", task["description"].strip()]
    parts += ["", "## Description", pr["body"].strip() or "(none)"]
    if pr["discussion"]:
        parts += ["", "## Discussion"]
        for item in pr["discussion"]:
            state = f" ({item['state'].lower().replace('_', ' ')})" if item.get("state") else ""
            parts += [f"- {item['author']}{state}, {item['at']}:", _indent(item["body"].strip() or "(no text)")]
    parts += ["", "## Files"] + [f"- {f['path']} (+{f['additions']} -{f['deletions']})" for f in pr["files"]]
    parts += ["", "## Diff" + (f" (first {len(pr['diff'])} characters; the rest is cut)"
                               if pr["diff_truncated"] else ""), pr["diff"].rstrip()]
    return "\n".join(parts)
