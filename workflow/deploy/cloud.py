"""Cloud deploys: ship a project's merged code to the service it runs as.

Unlike the device fleet in the registry, a cloud deploy is per project: it
runs the project's own deploy command (NotesGraph: `scripts/deploy-prod.sh`)
from a checkout of the base branch, so what goes live is exactly what was
merged. Configured on the project:

    projects:
      personal:
        cloud_deploy:
          repo: /Users/me/code/notes-graph-deploy   # checkout to deploy from
          branch: main
          command: scripts/deploy-prod.sh
          verify_url: https://app.notesgraph.com/

A deploy is what moves tasks up the shipping ladder (see `workflow.ship`):
Merged tasks become Deployed when the command succeeds, and Done once
`verify_url` answers.
"""

import subprocess
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from typing import Optional, Tuple

CONFIG_KEY = "cloud_deploy"


@dataclass
class CloudTarget:
    repo: str
    command: str
    branch: str = "main"
    verify_url: Optional[str] = None

    def to_config(self) -> dict:
        return {k: v for k, v in asdict(self).items() if v is not None}


def target_from(project_config: Optional[dict]) -> Optional[CloudTarget]:
    """The project's cloud target, or None if it has no complete one."""
    section = (project_config or {}).get(CONFIG_KEY) or {}
    if not section.get("repo") or not section.get("command"):
        return None
    return CloudTarget(repo=section["repo"], command=section["command"],
                       branch=section.get("branch") or "main",
                       verify_url=section.get("verify_url"))


def _git(repo: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True)


def sync_checkout(target: CloudTarget) -> Optional[str]:
    """Bring the deploy checkout to `origin/<branch>`; returns a problem or None.

    Never discards anything: a dirty tree, or a checkout on some other
    branch, is reported rather than reset -- it may be someone's work.
    """
    repo, branch = target.repo, target.branch
    fetch = _git(repo, "fetch", "origin", branch)
    if fetch.returncode != 0:
        return f"git fetch origin {branch} failed: {fetch.stderr.strip()}"

    dirty = _git(repo, "status", "--porcelain", "--untracked-files=no").stdout.strip()
    if dirty:
        return f"{repo} has uncommitted changes; commit or move them before deploying"

    current = _git(repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    if current == branch:
        moved = _git(repo, "merge", "--ff-only", f"origin/{branch}")
    elif current == "HEAD":
        moved = _git(repo, "checkout", "--detach", f"origin/{branch}")
    else:
        return (f"{repo} is on '{current}', not '{branch}'. Deploy from a checkout of "
                f"{branch}, e.g.: git worktree add --detach <path> origin/{branch}")
    if moved.returncode != 0:
        return f"could not move {repo} to origin/{branch}: {moved.stderr.strip()}"

    head = _git(repo, "rev-parse", "HEAD").stdout.strip()
    wanted = _git(repo, "rev-parse", f"origin/{branch}").stdout.strip()
    if head != wanted:
        return f"{repo} is at {head[:8]}, not origin/{branch} ({wanted[:8]})"
    return None


def head_revision(target: CloudTarget) -> str:
    return _git(target.repo, "rev-parse", "--short", "HEAD").stdout.strip()


def run_deploy(target: CloudTarget) -> int:
    """Run the deploy command in the checkout, streaming its output."""
    return subprocess.run(target.command, shell=True, cwd=target.repo).returncode


def verify(url: str, timeout: float = 30) -> Tuple[bool, str]:
    """Whether `url` answers without an error status, and what it said."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return response.status < 400, f"HTTP {response.status}"
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code}"
    except (urllib.error.URLError, OSError) as e:
        return False, str(getattr(e, "reason", e))
