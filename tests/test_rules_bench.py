import json
import subprocess
from pathlib import Path

from workflow.rules_bench import run_bench, summarize, Run

TASKS = Path(__file__).resolve().parents[1] / "bench" / "tasks"


def fake_claude(solution: dict, cost: dict):
    """A runner that writes the solution for the arm in place of Claude."""
    def run(argv, cwd, **kwargs):
        if argv[0] != "claude":
            return subprocess.run(argv, cwd=cwd, **kwargs)
        arm = "rules" if "--append-system-prompt" in argv else "none"
        # The hidden check must not be in the agent's directory.
        assert not (Path(cwd) / "check.sh").exists()
        assert not subprocess.run(["git", "-C", str(cwd), "rev-parse"],
                                  capture_output=True).returncode == 0
        (Path(cwd) / "slug.py").write_text(solution[arm])
        return subprocess.CompletedProcess(argv, 0, json.dumps({"total_cost_usd": cost[arm]}), "")
    return run


GOOD = ("import re, unicodedata\n"
        "def slugify(t):\n"
        "    t = unicodedata.normalize('NFKD', t).encode('ascii', 'ignore').decode()\n"
        "    return '-'.join(re.findall(r'[a-z0-9]+', t.lower()))\n")
BAD = "def slugify(t):\n    return t.lower().replace(' ', '-')\n"


def test_bench_runs_each_arm_and_compares_with_none(tmp_path):
    tasks = tmp_path / "tasks"
    (tasks / "slugify").mkdir(parents=True)
    for name in ("prompt.md", "check.sh"):
        (tasks / "slugify" / name).write_text((TASKS / "slugify" / name).read_text())
    out = tmp_path / "out.jsonl"

    runs = run_bench(tasks, {"rules": "Be brief."}, 2, "haiku", out,
                     runner=fake_claude({"none": BAD, "rules": GOOD}, {"none": 0.2, "rules": 0.1}),
                     echo=lambda *_: None)

    assert [(r.arm, r.passed) for r in runs] == [("none", False), ("rules", True)] * 2
    assert len(out.read_text().splitlines()) == 4
    table = summarize(runs)
    assert "rules             2/2    0.50" in table and "none              0/2    1.00" in table


def test_ratio_is_blank_without_a_baseline():
    assert "–" in summarize([Run("t", "rules", 0, True, 0.1, 1.0, 3)])
