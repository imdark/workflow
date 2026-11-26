"""Run a repository's lint on the files a branch changed.

The steps live in the repository, in `wf.yaml` at its root, so every machine
that checks it out (a laptop, the cloud runner) lints the same way:

    lint:
      - name: oxlint
        run: npx oxlint --deny-warnings {files}
        fix: npx oxlint --fix {files}
        files: ['*.ts', '*.tsx', '*.js', '*.mjs']

`{files}` becomes the changed files the step's `files` globs match (all of
them when it has none), or `.` with --all. A step with no matching changed
file is skipped. Without `wf.yaml`, a `lint` script in package.json runs on
the whole repository.
"""
from __future__ import annotations

import fnmatch
import json
import shlex
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

import yaml

CONFIG_FILE = "wf.yaml"
# Enough of a failing step's output to show what failed without flooding an
# agent's context: lint reports its errors last.
OUTPUT_TAIL = 8000


class LintError(Exception):
    """Lint could not run: not a repository, nothing configured, bad config."""


@dataclass
class LintStep:
    name: str
    run: str
    fix: Optional[str] = None
    files: List[str] = field(default_factory=list)


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True
    )


def load_lint_steps(repo: Path) -> List[LintStep]:
    """The repository's lint steps: wf.yaml's `lint`, else package.json's."""
    repo = Path(repo)
    config = repo / CONFIG_FILE
    if config.exists():
        data = yaml.safe_load(config.read_text()) or {}
        steps = []
        for i, step in enumerate(data.get("lint") or []):
            if not isinstance(step, dict) or not step.get("run"):
                raise LintError(f"{CONFIG_FILE}: lint step {i + 1} needs a `run` command")
            files = step.get("files") or []
            steps.append(LintStep(
                name=str(step.get("name") or f"step {i + 1}"),
                run=str(step["run"]),
                fix=str(step["fix"]) if step.get("fix") else None,
                files=[files] if isinstance(files, str) else [str(f) for f in files],
            ))
        return steps

    package = repo / "package.json"
    if package.exists():
        try:
            scripts = json.loads(package.read_text()).get("scripts") or {}
        except ValueError:
            scripts = {}
        if "lint" in scripts:
            runner = "yarn" if (repo / "yarn.lock").exists() else "npm run"
            fix = f"{runner} lint:fix" if "lint:fix" in scripts else None
            return [LintStep(name="package.json lint", run=f"{runner} lint", fix=fix)]
    return []


def default_base(repo: Path) -> str:
    """The branch to diff against: the remote's default branch, else main."""
    head = _git(repo, "symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD")
    if head.returncode == 0 and head.stdout.strip():
        return head.stdout.strip()
    for candidate in ("origin/main", "main", "origin/master", "master"):
        if _git(repo, "rev-parse", "--verify", "--quiet", candidate).returncode == 0:
            return candidate
    return "HEAD"


def changed_files(repo: Path, base: Optional[str] = None) -> List[str]:
    """Files the branch changed since it left `base`, plus uncommitted and
    untracked ones; deleted files are left out (there is nothing to lint)."""
    repo = Path(repo)
    base = base or default_base(repo)
    names = set()
    merge_base = _git(repo, "merge-base", base, "HEAD")
    if merge_base.returncode == 0:
        since = _git(repo, "diff", "--name-only", "--diff-filter=d",
                     merge_base.stdout.strip(), "HEAD")
        names.update(since.stdout.split("\n"))
    working = _git(repo, "diff", "--name-only", "--diff-filter=d", "HEAD")
    names.update(working.stdout.split("\n"))
    untracked = _git(repo, "ls-files", "--others", "--exclude-standard")
    names.update(untracked.stdout.split("\n"))
    return sorted(n for n in names if n and (repo / n).is_file())


def _matches(path: str, globs: List[str]) -> bool:
    if not globs:
        return True
    name = path.rsplit("/", 1)[-1]
    return any(fnmatch.fnmatch(path, g) or fnmatch.fnmatch(name, g) for g in globs)


def run_lint(repo: Path, base: Optional[str] = None, all_files: bool = False,
             fix: bool = False, files: Optional[List[str]] = None) -> List[dict]:
    """Run each lint step and report it: name, ok, skipped, exit_code, files
    it ran on, and the tail of its output."""
    repo = Path(repo)
    steps = load_lint_steps(repo)
    if not all_files and files is None:
        files = changed_files(repo, base)
    results = []
    for step in steps:
        command = step.fix if fix and step.fix else step.run
        targets: List[str] = []
        if not all_files:
            targets = [f for f in files or [] if _matches(f, step.files)]
            # A step limited to some files has nothing to do when none of
            # them changed, even one that finds its own files (no {files}).
            if not targets and ("{files}" in command or step.files):
                results.append({"name": step.name, "ok": True, "skipped": True,
                                "exit_code": None, "files": [], "output": ""})
                continue
        if "{files}" in command:
            command = command.replace(
                "{files}",
                "." if all_files else " ".join(shlex.quote(f) for f in targets))
        proc = subprocess.run(command, shell=True, cwd=repo, capture_output=True, text=True)
        output = (proc.stdout + proc.stderr).strip()
        results.append({
            "name": step.name,
            "ok": proc.returncode == 0,
            "skipped": False,
            "exit_code": proc.returncode,
            "files": targets,
            "output": output[-OUTPUT_TAIL:],
        })
    return results


def lint_repo(path, base: Optional[str] = None, all_files: bool = False,
              fix: bool = False) -> List[dict]:
    """Lint the repository `path` is in: what `wf lint` and the MCP tool run.
    Raises LintError when it can't (no repository, nothing configured)."""
    top = _git(Path(path), "rev-parse", "--show-toplevel")
    if top.returncode != 0:
        raise LintError(f"Not a git repository: {path}")
    repo = Path(top.stdout.strip())
    if not load_lint_steps(repo):
        raise LintError(
            f"No lint configured in {repo}: add a `lint:` list to {CONFIG_FILE} "
            "or a `lint` script to package.json.")
    return run_lint(repo, base=base, all_files=all_files, fix=fix)
