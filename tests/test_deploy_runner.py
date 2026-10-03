"""Tests for recipe loading and execution.

Everything runs against a fake SSH runner, so no test opens a connection.
The recipes themselves are data; what is tested here is the contract around
them -- ordering, failure propagation, optional steps, and how probe
results roll into a device's state.
"""

import pytest
import yaml

from workflow.deploy import runner as deploy_runner
from workflow.deploy.recipes import (
    PRESETS,
    Recipe,
    Step,
    available_recipes,
    load_recipe,
)
from workflow.deploy.registry import Device, STATE_DEGRADED, STATE_OFFLINE, STATE_ONLINE
from workflow.deploy.ssh import Result


class FakeRunner:
    """Records commands and replays canned results.

    `failures` maps a substring of a command to the result it should get,
    so a test can fail one step without scripting all of them.
    """

    def __init__(self, failures=None, reachable_ok=True, version="abc1234"):
        self.commands = []
        self.failures = failures or {}
        self.reachable_ok = reachable_ok
        self.version = version

    def run(self, command, timeout=None, cwd=None):
        self.commands.append(command)
        for needle, message in self.failures.items():
            if needle in command:
                return Result(ok=False, exit_code=1, stderr=message, command=command)
        if "rev-parse --short HEAD" in command:
            return Result(ok=True, exit_code=0, stdout=self.version + "\n", command=command)
        return Result(ok=True, exit_code=0, stdout="ok\n", command=command)

    def reachable(self):
        return Result(ok=self.reachable_ok, exit_code=0 if self.reachable_ok else 255)

    def close(self):
        pass


def rig(**overrides) -> Device:
    base = dict(id="gem5", host="gem5.local", user="pantheon",
                recipe="pantheon", repo="/home/pantheon/robots_realtime", branch="main")
    base.update(overrides)
    return Device(**base)


@pytest.fixture
def cfg_file(tmp_path, monkeypatch):
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(yaml.dump({"task_backend": "markdown"}))
    monkeypatch.setattr("workflow.config.CFG", cfg_path)
    return cfg_path


# ---------------------------------------------------------------------------
# recipes
# ---------------------------------------------------------------------------

def test_builtin_recipes_load(cfg_file):
    for name in PRESETS:
        recipe = load_recipe(name)
        assert recipe.steps and recipe.health


def test_unknown_recipe_names_the_alternatives(cfg_file):
    with pytest.raises(ValueError, match="Built in: generic, pantheon"):
        load_recipe("nonsense")


def test_pantheon_never_restarts_as_part_of_a_deploy(cfg_file):
    """A deploy to a live rig must not bounce it as a side effect."""
    recipe = load_recipe("pantheon")
    assert all("relaunch" not in s.run for s in recipe.steps)
    # relaunch exists, but only as an explicit restart step
    assert any("clean_relaunch" in s.run for s in recipe.restart)


def test_placeholders_render_from_the_device(cfg_file):
    recipe = load_recipe("pantheon")
    context = recipe.context(rig(pin="v2.0"))
    rendered = [s.render(context) for s in recipe.steps]

    assert any("/home/pantheon/robots_realtime" in r for r in rendered)
    # a pin outranks the branch as the wanted revision
    assert any("checkout v2.0" in r for r in rendered)


def test_unknown_placeholders_are_left_alone(cfg_file):
    step = Step(name="x", run="echo ${SHELL_VAR} {repo} {nope}")
    assert step.render({"repo": "/srv"}) == "echo ${SHELL_VAR} /srv {nope}"


def test_config_overrides_one_list_and_keeps_the_rest(cfg_file):
    cfg = {"deploy": {"recipes": {"pantheon": {
        "restart": [{"name": "bounce", "run": "systemctl restart rr"}],
    }}}}
    recipe = load_recipe("pantheon", cfg)

    assert [s.name for s in recipe.restart] == ["bounce"]
    # the preset's deploy and health steps survive the partial override
    assert any(s.name == "install" for s in recipe.steps)
    assert any(s.name == "rig" for s in recipe.health)


def test_a_config_only_recipe_needs_no_preset(cfg_file):
    cfg = {"deploy": {"recipes": {"webapp": {
        "steps": ["git pull", {"name": "build", "run": "npm ci && npm run build"}],
    }}}}
    recipe = load_recipe("webapp", cfg)
    assert [s.name for s in recipe.steps] == ["git", "build"]
    assert "webapp" in available_recipes(cfg)


def test_a_step_without_a_command_is_rejected():
    with pytest.raises(ValueError, match="needs a 'run' command"):
        Step.from_config({"name": "broken"})


# ---------------------------------------------------------------------------
# execution
# ---------------------------------------------------------------------------

def test_steps_run_in_order(cfg_file):
    fake = FakeRunner()
    report = deploy_runner.deploy(rig(), runner=fake)

    assert report.ok
    assert [r.step.name for r in report.results] == [
        "fetch", "checkout", "submodules", "install",
    ]


def test_version_is_read_after_a_successful_deploy(cfg_file):
    report = deploy_runner.deploy(rig(), runner=FakeRunner(version="deadbee"))
    assert report.version == "deadbee"


def test_a_failed_step_halts_the_rest(cfg_file):
    fake = FakeRunner(failures={"submodule": "private repo unreachable"})
    report = deploy_runner.deploy(rig(), runner=fake)

    assert not report.ok
    names = {r.step.name: r for r in report.results}
    assert names["submodules"].ok is False
    # install must not run against a half-updated checkout
    assert names["install"].skipped is True
    assert "install.sh" not in " ".join(fake.commands)


def test_no_version_is_read_when_a_deploy_failed(cfg_file):
    report = deploy_runner.deploy(rig(), runner=FakeRunner(failures={"install.sh": "boom"}))
    assert report.version is None


# ---------------------------------------------------------------------------
# health
# ---------------------------------------------------------------------------

def test_healthy_rig_is_online(cfg_file):
    status = deploy_runner.check(rig(), runner=FakeRunner())
    assert status.state == STATE_ONLINE
    assert status.version == "abc1234"


def test_unreachable_device_is_offline_and_runs_no_probes(cfg_file):
    fake = FakeRunner(reachable_ok=False)
    status = deploy_runner.check(rig(), runner=fake)

    assert status.state == STATE_OFFLINE
    assert status.checks == []
    assert fake.commands == []


def test_a_failing_required_probe_is_degraded(cfg_file):
    status = deploy_runner.check(
        rig(), runner=FakeRunner(failures={"rev-parse --is-inside-work-tree": "not a repo"})
    )
    assert status.state == STATE_DEGRADED
    assert "checkout" in status.detail


def test_a_failing_optional_probe_does_not_degrade_the_device(cfg_file):
    """A sim box has no rig; rr-debug failing there is not ill health."""
    status = deploy_runner.check(rig(), runner=FakeRunner(failures={"rr-debug": "no hardware"}))
    assert status.state == STATE_ONLINE


def test_probes_keep_running_after_one_fails(cfg_file):
    """Health is a report, not a deploy: it should not stop at the first problem."""
    fake = FakeRunner(failures={"rev-parse --is-inside-work-tree": "nope"})
    status = deploy_runner.check(rig(), runner=fake)
    assert {c["name"] for c in status.checks} == {"reachable", "checkout", "rig"}


def test_restart_runs_only_the_restart_steps(cfg_file):
    fake = FakeRunner()
    report = deploy_runner.restart(rig(), runner=fake)

    assert report.ok
    assert [r.step.name for r in report.results] == ["relaunch"]
    assert "install.sh" not in " ".join(fake.commands)


# ---------------------------------------------------------------------------
# tunnel
# ---------------------------------------------------------------------------

def test_tunnel_env_points_the_agent_at_the_forwarded_port():
    from workflow.deploy.tunnel import export_line, remote_env

    env = remote_env(8099)
    assert env["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:8099"
    assert env["OPENAI_BASE_URL"] == "http://127.0.0.1:8099/v1"
    assert "ANTHROPIC_BASE_URL=" in export_line(8099)


def test_tunnel_fails_loudly_rather_than_forwarding_nothing():
    """ExitOnForwardFailure turns a bound remote port into a real error."""
    from workflow.deploy.tunnel import _tunnel_args

    args = _tunnel_args(rig(), 8099, 8099)
    assert "ExitOnForwardFailure=yes" in args
    assert "-R" in args and "8099:127.0.0.1:8099" in args


# ---------------------------------------------------------------------------
# local transport
# ---------------------------------------------------------------------------

def test_this_machine_is_recognized_by_its_own_names():
    """Registering the machine you are sitting at must not require SSH.

    Remote Login is off by default on macOS, so insisting on SSH would make
    the local case the hardest one to set up.
    """
    import socket
    from workflow.deploy.ssh import is_local_host

    assert is_local_host("localhost")
    assert is_local_host("127.0.0.1")
    assert is_local_host(socket.gethostname())
    # `hostname` reports Foo.local; a user registers foo
    assert is_local_host(socket.gethostname().removesuffix(".local"))
    assert not is_local_host("gem5.local")


def test_runner_for_picks_the_transport():
    from workflow.deploy.ssh import LocalRunner, SshRunner, runner_for

    assert isinstance(runner_for(rig(host="localhost")), LocalRunner)
    assert isinstance(runner_for(rig(host="gem5.local")), SshRunner)


def test_local_runner_executes_and_reports(tmp_path):
    from workflow.deploy.ssh import LocalRunner

    runner = LocalRunner(rig(host="localhost"))
    assert runner.reachable().ok

    ok = runner.run("echo hello")
    assert ok.ok and ok.first_line() == "hello"

    # shell operators behave as they would over ssh
    assert runner.run("echo a && echo b").output.splitlines() == ["a", "b"]

    bad = runner.run("exit 3")
    assert not bad.ok and bad.exit_code == 3


def test_local_runner_honours_cwd(tmp_path):
    from workflow.deploy.ssh import LocalRunner

    (tmp_path / "marker.txt").write_text("x")
    result = LocalRunner(rig(host="localhost")).run("ls", cwd=str(tmp_path))
    assert "marker.txt" in result.output


def test_local_runner_reports_a_missing_directory_instead_of_raising():
    from workflow.deploy.ssh import LocalRunner

    result = LocalRunner(rig(host="localhost")).run("ls", cwd="/nope/not/here")
    assert not result.ok and result.exit_code == 127


def test_a_health_check_runs_locally_without_ssh(tmp_path):
    """End to end against this machine, using a real git checkout."""
    import subprocess
    from workflow.deploy.registry import STATE_ONLINE

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-q", "--allow-empty", "-m", "init"], cwd=repo, check=True)

    device = Device(id="here", host="localhost", user="", recipe="generic", repo=str(repo))
    status = deploy_runner.check(device, recipe=load_recipe("generic"))

    assert status.state == STATE_ONLINE
    assert status.version  # the short sha was read back
