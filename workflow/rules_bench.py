"""`wf bench`: does an edit to the Agent rules (or a skill) help or hurt?

Agent rules and `Skill:` notes change with no deploy, so with no test
either. This runs the same small coding tasks headless with and without a
prompt and compares them, after Ponytail's benchmarks/agentic:

* each run is in a fresh temp directory outside any git repo (inside one, the
  model sees recent commit messages and the result leaks), with only project
  settings loaded (there are none there) and auto memory off;
* each task has a hidden check, `check.sh`, run in the directory afterwards
  and never copied in, so the agent can't read or game it;
* per arm: checks passed, and the median of the per-task ratios against the
  no-prompt arm for cost, time and lines written.

A task is a directory: `prompt.md` (what the agent is asked), `check.sh`
(exit 0 when the work is right) and optionally `files/`, copied in as the
starting code. See bench/tasks for examples.
"""

import json
import shutil
import statistics
import subprocess
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional

BASELINE = "none"
RUN_TIMEOUT_SECONDS = 15 * 60
CHECK_TIMEOUT_SECONDS = 120


@dataclass
class Run:
    task: str
    arm: str
    n: int
    passed: bool
    cost_usd: float
    seconds: float
    lines: int
    error: str = ""


def load_tasks(root: Path) -> list:
    tasks = sorted(p for p in Path(root).iterdir()
                   if (p / "prompt.md").is_file() and (p / "check.sh").is_file())
    if not tasks:
        raise LookupError(f"no tasks in {root} (each needs prompt.md and check.sh)")
    return tasks


def _lines_written(start: Optional[Path], work: Path) -> int:
    """Lines in files the agent added or changed, against the starting code."""
    total = 0
    for path in work.rglob("*"):
        if not path.is_file() or any(p.startswith(".") for p in path.relative_to(work).parts):
            continue
        rel = path.relative_to(work)
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        before = start / rel if start else None
        if before and before.is_file() and before.read_text(encoding="utf-8") == text:
            continue
        old = set(before.read_text(encoding="utf-8").splitlines()) if before and before.is_file() else set()
        total += sum(1 for line in text.splitlines() if line.strip() and line not in old)
    return total


def claude_argv(prompt: str, model: str, system_prompt: str) -> list:
    return ["claude", "--print", "--output-format", "json", "--model", model,
            "--setting-sources", "project", "--strict-mcp-config",
            "--permission-mode", "bypassPermissions", "--no-session-persistence",
            *(["--append-system-prompt", system_prompt] if system_prompt else []),
            prompt]


def run_once(task: Path, arm: str, system_prompt: str, n: int, model: str, runner=subprocess.run) -> Run:
    start = task / "files"
    work = Path(tempfile.mkdtemp(prefix=f"wf-bench-{task.name}-"))
    try:
        if start.is_dir():
            shutil.copytree(start, work, dirs_exist_ok=True)
        began = time.monotonic()
        error, cost = "", 0.0
        try:
            done = runner(claude_argv((task / "prompt.md").read_text(), model, system_prompt),
                          cwd=work, capture_output=True, text=True, timeout=RUN_TIMEOUT_SECONDS,
                          env=_env())
            reply = json.loads(done.stdout or "{}")
            cost = float(reply.get("total_cost_usd") or 0)
            if done.returncode or reply.get("is_error"):
                error = (done.stderr or str(reply.get("result")) or "failed")[:200]
        except (subprocess.TimeoutExpired, ValueError) as e:
            error = str(e)[:200]
        seconds = time.monotonic() - began
        try:
            check = runner(["sh", str((task / "check.sh").resolve())], cwd=work,
                           capture_output=True, text=True, timeout=CHECK_TIMEOUT_SECONDS)
            passed = check.returncode == 0
        except subprocess.TimeoutExpired:
            passed = False
        return Run(task.name, arm, n, passed, cost, round(seconds, 1),
                   _lines_written(start if start.is_dir() else None, work), error)
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _env() -> dict:
    import os
    return {**os.environ, "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1"}


def _median_ratio(runs: list, arm: str, field: str) -> Optional[float]:
    """Median over tasks of (arm's median / baseline's median) for `field`."""
    ratios = []
    for task in sorted({r.task for r in runs}):
        def med(a):
            values = [getattr(r, field) for r in runs if r.task == task and r.arm == a]
            return statistics.median(values) if values else None
        mine, base = med(arm), med(BASELINE)
        if mine is not None and base:
            ratios.append(mine / base)
    return statistics.median(ratios) if ratios else None


def summarize(runs: list) -> str:
    rows = ["arm            passed   cost×   time×   lines×   (× = median per-task ratio to 'none')"]
    for arm in dict.fromkeys(r.arm for r in runs):
        mine = [r for r in runs if r.arm == arm]
        cells = []
        for field in ("cost_usd", "seconds", "lines"):
            ratio = _median_ratio(runs, arm, field)
            cells.append(f"{ratio:6.2f}" if ratio is not None else "     –")
        passed = f"{sum(r.passed for r in mine)}/{len(mine)}"
        rows.append(f"{arm:14} {passed:>6}  {cells[0]}  {cells[1]}  {cells[2]}")
    return "\n".join(rows)


def run_bench(tasks_dir: Path, arms: dict, runs: int, model: str, out: Optional[Path],
              runner=subprocess.run, echo=print) -> list:
    """Every task × arm × run; `arms` maps a name to its system prompt text.

    The no-prompt arm is always run, as the baseline. Results go to `out`
    as JSON lines as they finish, so a stopped bench keeps what it did.
    """
    tasks = load_tasks(tasks_dir)
    arms = {BASELINE: "", **arms}
    results = []
    for task in tasks:
        for n in range(runs):
            for arm, prompt in arms.items():
                run = run_once(task, arm, prompt, n, model, runner)
                results.append(run)
                echo(f"{task.name:20} {arm:12} #{n + 1} {'pass' if run.passed else 'FAIL'} "
                     f"${run.cost_usd:.3f} {run.seconds:.0f}s {run.lines} lines {run.error}")
                if out:
                    with open(out, "a") as f:
                        f.write(json.dumps(asdict(run)) + "\n")
    echo("")
    echo(summarize(results))
    return results
