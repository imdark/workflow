"""wf release: each repository is released its own way.

No test opens a connection or runs a release: machines answer through a fake shell, and the release command,
`gh` and the verify URL are replaced.
"""
import json
import subprocess

import pytest
import yaml
from typer.testing import CliRunner

from workflow import cli_release
from workflow import release as rel

runner = CliRunner()
OLD, NEW = "a" * 40, "b" * 40


def git(cwd, *args):
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True)


@pytest.fixture
def world(tmp_path, monkeypatch):
    """Two repositories of one project: an app released by a command, and a tool that runs on machines."""
    app, tool = tmp_path / "app", tmp_path / "tool"
    for repo in (app, tool):
        repo.mkdir()
        git(repo, "init", "-q", "-b", "main")
        git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "first")
    git(app, "worktree", "add", "-q", str(tmp_path / "app-task"), "-b", "task")
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(yaml.dump({"task_backend": "markdown", "projects": {"personal": {"repositories": {
        str(app): {"base_branch": "stage", "release": {
            "command": "scripts/release.sh", "watch": "deploy-prod.yml", "verify_url": "https://app.example/"}},
        str(tool): {"release": {"machines": {
            "laptop": {"path": "~/code/tool"},
            "prod": {"ssh": "prod", "path": "/srv/tool", "python": "/venv/bin/python3"}}}},
        str(tmp_path / "bare"): {},
    }}}}))
    monkeypatch.setattr("workflow.config.CFG", cfg_path)
    return type("W", (), {"app": app, "tool": tool, "task": tmp_path / "app-task", "cfg": cfg_path, "root": tmp_path})()


def _wf(*args):
    return runner.invoke(cli_release.release_app, list(args))


# ---------------------------------------------------------------- which release

def test_the_release_is_the_repositorys_own_also_from_a_worktree(world):
    assert rel.kind(rel.find_release(str(world.app))["section"]) == "command"
    assert rel.kind(rel.find_release(str(world.tool))["section"]) == "machines"
    # A task's worktree releases the repository it belongs to.
    info = rel.find_release(str(world.task))
    assert info["repo"] == str(world.app) and info["project"] == "personal"
    with pytest.raises(rel.ReleaseError, match="not in a git repository"):
        rel.find_release(str(world.root))
    other = world.root / "elsewhere"
    other.mkdir()
    git(other, "init", "-q")
    with pytest.raises(rel.ReleaseError, match="not a repository of any wf project"):
        rel.find_release(str(other))


# ---------------------------------------------------------------- released by a command

@pytest.fixture
def command_calls(monkeypatch):
    calls, codes = [], {"plan": 0, "release": 0}
    monkeypatch.setattr(rel, "run_command", lambda info, mode: calls.append((info["repo"], mode)) or codes[mode])
    monkeypatch.setattr("workflow.deploy.cloud.verify", lambda url: (True, "200"))
    return type("C", (), {"calls": calls, "codes": codes})()


def test_a_command_release_lists_then_releases_then_checks_the_url(world, command_calls):
    r = _wf("--yes", "--repo-path", str(world.app))
    assert r.exit_code == 0, r.output
    assert [mode for _, mode in command_calls.calls] == ["plan", "release"]
    assert "https://app.example/ answered 200" in r.output
    assert rel.command_line({"command": "scripts/release.sh"}, "plan") == "scripts/release.sh --plan"
    assert rel.command_line({"command": "scripts/release.sh"}, "release") == "scripts/release.sh --yes --wait"
    assert rel.command_line({"command": "make ship", "release_args": ""}, "release") == "make ship"


def test_plan_only_lists_and_no_answer_releases_nothing(world, command_calls):
    assert _wf("--plan", "--repo-path", str(world.app)).exit_code == 0
    assert [mode for _, mode in command_calls.calls] == ["plan"]
    r = runner.invoke(cli_release.release_app, ["--repo-path", str(world.app)], input="n\n")
    assert r.exit_code == 1 and "nothing released" in r.output
    assert [mode for _, mode in command_calls.calls] == ["plan", "plan"]


def test_a_failed_release_exits_with_its_code_and_shows_the_run(world, command_calls, monkeypatch):
    command_calls.codes["release"] = 7
    monkeypatch.setattr(rel, "latest_run", lambda info: {"id": 900, "status": "completed", "branch": "main",
                                                         "sha": "f" * 40, "url": "u"})
    monkeypatch.setattr("workflow.pr.ci_run", lambda run_id, repo_path=None: {
        "id": 900, "workflow": "Deploy prod", "status": "completed", "conclusion": "failure", "attempt": 1,
        "branch": "main", "sha": "f" * 40, "url": "u",
        "jobs": [{"name": "deploy", "status": "completed", "conclusion": "failure", "failed_steps": ["Roll"]}]})
    r = _wf("--yes", "--repo-path", str(world.app))
    assert r.exit_code == 7 and "release exited 7" in r.output
    assert "✗ 900 Deploy prod: failure" in r.output and "failed at: Roll" in r.output


def test_watch_follows_a_run_in_progress_and_exits_with_its_result(world, monkeypatch):
    followed = []
    state = {"status": "in_progress", "conclusion": None}
    monkeypatch.setattr(rel, "latest_run", lambda info: {"id": 901, "branch": "main", "sha": "f" * 40, "url": "u",
                                                         **state})

    def watch_run(info, run_id):
        followed.append(run_id)
        state.update(status="completed", conclusion="success")
        return 0
    monkeypatch.setattr(rel, "watch_run", watch_run)
    monkeypatch.setattr("workflow.pr.ci_run", lambda run_id, repo_path=None: {
        "id": run_id, "workflow": "Deploy prod", "attempt": 1, "branch": "main", "sha": "f" * 40, "url": "u",
        "jobs": [], **state})
    r = _wf("--watch", "--repo-path", str(world.app))
    assert r.exit_code == 0 and followed == [901]
    assert "following run 901" in r.output and "✓ 901 Deploy prod: success" in r.output
    # status doesn't wait: a run still going exits 3.
    state.update(status="in_progress", conclusion=None)
    r = _wf("status", "--repo-path", str(world.app))
    assert r.exit_code == 3 and followed == [901]


# ---------------------------------------------------------------- released to machines

class Fleet:
    """Stands in for the machines: each answers the commands a release sends it."""

    def __init__(self, **state):
        self.state, self.sent = state, []

    def __call__(self, machine):
        def run(command, timeout=60):
            me = self.state[machine.name]
            self.sent.append((machine.name, command))
            if me.get("down"):
                return 255, "ssh: connect to host prod port 22: Operation timed out"
            if "pip install" in command:
                return (1, "ERROR: No matching distribution") if me.get("pip_fails") else (0, "")
            if "merge -q --ff-only" in command:
                me["sha"] = NEW
                if me.get("restarts", True) and me["runners"]:
                    me["runners"] = [pid + 1000 for pid in me["runners"]]
                return 0, NEW
            return 0, "\n".join([me["sha"], me.get("branch", "main"), "::dirty", *me.get("dirty", []), "::runners",
                                 *map(str, me["runners"])])
        return run


def ships(monkeypatch, prs=("#96 personal-371-list-runs",), dependencies=()):
    monkeypatch.setattr(rel, "shipping", lambda repo, have, target: {
        "behind": 0 if have == target else 3, "ahead": 0, "prs": list(prs), "dependencies": list(dependencies)})


def section(world):
    return rel.find_release(str(world.tool))["section"]


def test_the_plan_says_what_each_machine_would_get(world, monkeypatch):
    ships(monkeypatch)
    fleet = Fleet(laptop={"sha": NEW, "runners": [11]}, prod={"sha": OLD, "runners": [22]})
    planned = rel.plan(section(world), world.tool, target=NEW, shells=fleet)
    assert [(m["name"], m["action"]) for m in planned["machines"]] == [("laptop", "current"), ("prod", "update")]
    text = rel.format_plan(planned)
    assert f"✓ laptop (~/code/tool): current · runner pid 11" in text
    assert "→ prod (prod:/srv/tool): aaaaaaaa, 3 commits behind · runner pid 22" in text
    assert "    #96 personal-371-list-runs" in text
    # `~` is left for the machine's own shell to expand.
    assert fleet.sent[0][1].startswith("cd ~/code/tool && git rev-parse HEAD")


def test_a_machine_with_local_work_or_off_the_branch_or_down_is_left_alone(world, monkeypatch):
    ships(monkeypatch)
    fleet = Fleet(laptop={"sha": OLD, "runners": [], "dirty": [" M workflow/cli.py"]},
                  prod={"sha": OLD, "runners": [22], "down": True})
    planned = rel.plan(section(world), world.tool, target=NEW, shells=fleet)
    assert [m["action"] for m in planned["machines"]] == ["skip: uncommitted changes", "unreachable"]
    assert "uncommitted: M workflow/cli.py" in rel.format_plan(planned)
    assert "✗ prod (prod:/srv/tool): unreachable: ssh: connect" in rel.format_plan(planned)
    result = rel.release(section(world), world.tool, planned=planned, shells=fleet)
    assert result["ok"] is False and not any("merge" in command for _, command in fleet.sent)
    fleet = Fleet(laptop={"sha": OLD, "runners": [], "branch": "my-fix"}, prod={"sha": NEW, "runners": []})
    assert rel.plan(section(world), world.tool, target=NEW, shells=fleet)["machines"][0]["action"] == "skip: not on main"


def test_a_release_moves_each_checkout_then_waits_for_the_runners(world, monkeypatch):
    ships(monkeypatch)
    fleet = Fleet(laptop={"sha": OLD, "runners": []}, prod={"sha": OLD, "runners": [22]})
    said = []
    result = rel.release(section(world), world.tool, shells=fleet, say=said.append, sleep=lambda s: None,
                         planned=rel.plan(section(world), world.tool, target=NEW, shells=fleet))
    assert result["ok"] is True
    assert [(m["name"], m["outcome"], m["runner"]) for m in result["machines"]] == [
        ("laptop", "updated", "no runner"), ("prod", "updated", "restarted")]
    assert f"git merge -q --ff-only {NEW}" in next(c for n, c in fleet.sent if n == "prod" and "merge" in c)
    assert said[0] == "==> laptop: updating aaaaaaaa..bbbbbbbb"
    text = rel.format_outcome(result)
    assert "✓ laptop: updated, no runner there" in text and "✓ prod: updated, runner restarted on the new code" in text


def test_a_runner_with_a_job_is_waited_for_and_reported_not_interrupted(world, monkeypatch):
    ships(monkeypatch)
    fleet = Fleet(laptop={"sha": NEW, "runners": []}, prod={"sha": OLD, "runners": [22], "restarts": False})
    slept = []
    result = rel.release(section(world), world.tool, shells=fleet, wait=30, sleep=slept.append,
                         planned=rel.plan(section(world), world.tool, target=NEW, shells=fleet))
    assert result["machines"][1]["runner"] == "waiting" and slept == [10, 10, 10]
    assert result["ok"] is True      # the code is there; the runner follows on its own
    assert "runner still on the old code: it restarts when its jobs end" in rel.format_outcome(result)
    assert not any("kill" in command for _, command in fleet.sent)


def test_changed_requirements_are_installed_before_the_code_moves(world, monkeypatch):
    ships(monkeypatch, dependencies=["requirements.txt"])
    fleet = Fleet(laptop={"sha": OLD, "runners": []}, prod={"sha": OLD, "runners": [22]})
    result = rel.release(section(world), world.tool, shells=fleet, sleep=lambda s: None,
                         planned=rel.plan(section(world), world.tool, target=NEW, shells=fleet))
    laptop, prod = result["machines"]
    # No interpreter named for the laptop: it isn't moved onto code it can't run.
    assert laptop["outcome"].startswith("failed: requirements.txt changed") and result["ok"] is False
    order = [c for n, c in fleet.sent if n == "prod" and ("pip install" in c or "merge" in c)]
    assert "/venv/bin/python3 -m pip install -q -r /dev/stdin" in order[0] and "merge" in order[1]
    fleet = Fleet(laptop={"sha": NEW, "runners": []}, prod={"sha": OLD, "runners": [22], "pip_fails": True})
    result = rel.release(section(world), world.tool, shells=fleet,
                         planned=rel.plan(section(world), world.tool, target=NEW, shells=fleet))
    assert result["machines"][1]["outcome"].startswith("failed: installing requirements failed")
    assert not any("merge" in c for n, c in fleet.sent if n == "prod")


def test_the_command_releases_to_machines_and_exits_by_the_outcome(world, monkeypatch):
    ships(monkeypatch)
    fleet = Fleet(laptop={"sha": NEW, "runners": [11]}, prod={"sha": OLD, "runners": [22]})
    monkeypatch.setattr(rel, "shell_for", fleet)
    monkeypatch.setattr(rel, "released_sha", lambda repo=None, branch="main": NEW)
    monkeypatch.setattr(rel.time, "sleep", lambda s: None)
    r = _wf("--plan", "--json", "--repo-path", str(world.tool))
    assert r.exit_code == 0 and json.loads(r.output)["machines"][1]["action"] == "update"
    r = _wf("--yes", "--repo-path", str(world.tool))
    assert r.exit_code == 0, r.output
    assert "✓ prod: updated, runner restarted on the new code" in r.output
    r = _wf("--repo-path", str(world.tool))
    assert r.exit_code == 0 and "nothing to release" in r.output


def test_config_add_and_remove_write_the_repositorys_release(world):
    bare = world.root / "bare"
    bare.mkdir()
    git(bare, "init", "-q")
    r = _wf("--repo-path", str(bare))
    assert r.exit_code == 2 and "has no release yet" in r.output
    assert _wf("config", "--command", "make ship", "--watch", "ship.yml", "--repo-path", str(bare)).exit_code == 0
    assert _wf("add", "edge", "--path", "/srv/x", "--ssh", "edge", "--repo-path", str(world.tool)).exit_code == 0
    assert _wf("remove", "laptop", "--repo-path", str(world.tool)).exit_code == 0
    repos = yaml.safe_load(world.cfg.read_text())["projects"]["personal"]["repositories"]
    assert repos[str(bare)]["release"] == {"command": "make ship", "watch": "ship.yml"}
    assert set(repos[str(world.tool)]["release"]["machines"]) == {"prod", "edge"}
    # The repository's other settings are kept.
    assert repos[str(world.app)]["base_branch"] == "stage"
    assert "machine edge: edge:/srv/x" in _wf("config", "--repo-path", str(world.tool)).output
