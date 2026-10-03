"""tmux as a hard dependency of agent devices.

`wf deploy run` must refuse to touch any target when an agent target lacks
tmux, and `wf agent serve` must refuse to start -- both saying exactly what
to run to install it on that machine.
"""

from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from workflow.deploy import prereqs
from workflow.deploy.ssh import LocalRunner, Result


class FakeRunner:
    """Answers the probe with canned stdout and records what it was asked."""

    def __init__(self, stdout="", ok=True, stderr=""):
        self.stdout, self.ok, self.stderr = stdout, ok, stderr
        self.commands = []

    def run(self, command, timeout=None, cwd=None):
        self.commands.append(command)
        return Result(ok=self.ok, exit_code=0 if self.ok else 255,
                      stdout=self.stdout, stderr=self.stderr, command=command)

    def close(self):
        pass


@pytest.mark.parametrize("stdout, expected", [
    ("os Linux\npm apt-get\n", ["sudo apt-get update && sudo apt-get install -y tmux"]),
    ("os Linux\npm dnf\npm yum\n", ["sudo dnf install -y tmux"]),
    ("os Linux\npm pacman\n", ["sudo pacman -S --noconfirm tmux"]),
    ("os Linux\npm apk\n", ["sudo apk add tmux"]),
    ("os Darwin\npm brew\n", ["brew install tmux"]),
])
def test_the_install_command_fits_the_machine(stdout, expected):
    check = prereqs.check_tmux(FakeRunner(stdout))
    assert not check.installed
    assert check.install_steps == expected


def test_a_mac_without_homebrew_is_told_to_install_it_first():
    check = prereqs.check_tmux(FakeRunner("os Darwin\n"))
    steps = check.install_steps
    assert "Homebrew/install" in steps[0]
    assert steps[1] == "brew install tmux"


def test_an_installed_tmux_passes():
    check = prereqs.check_tmux(FakeRunner("have /opt/homebrew/bin/tmux\n"))
    assert check.installed
    assert check.path == "/opt/homebrew/bin/tmux"


def test_an_unreachable_device_fails_the_check_with_the_reason():
    check = prereqs.check_tmux(FakeRunner(ok=False, stderr="ssh: connect timed out"))
    assert not check.installed
    lines = prereqs.tmux_missing_message(check, "rig-1")
    assert "Couldn't check" in lines[0] and "timed out" in lines[0]


def test_an_unknown_system_still_gets_told_what_is_missing():
    check = prereqs.check_tmux(FakeRunner("os SunOS\n"))
    text = "\n".join(prereqs.tmux_missing_message(check, "box"))
    assert "tmux is not installed on box" in text
    assert "SunOS" in text


def test_the_probe_looks_past_a_bare_ssh_path():
    """ssh's non-interactive PATH often lacks Homebrew; probe it anyway."""
    runner = FakeRunner("have /opt/homebrew/bin/tmux\n")
    prereqs.check_tmux(runner)
    assert "/opt/homebrew/bin" in runner.commands[0]


def test_the_probe_runs_for_real_on_this_machine():
    check = prereqs.check_tmux(LocalRunner(None))
    assert check.error is None
    assert check.installed or check.os_name


# --- the commands ---------------------------------------------------------

def device(id, agent):
    return SimpleNamespace(id=id, agent_target=agent, recipe="generic",
                           ssh_destination=f"me@{id}", target_ref="main")


@pytest.fixture
def fleet(monkeypatch):
    """Two targets; an agent rig without tmux and a plain web box."""
    from workflow import cli_deploy

    devices = [device("web-1", agent=False), device("rig-1", agent=True)]
    registry = SimpleNamespace(list=lambda **kw: devices,
                               get=lambda id: next((d for d in devices if d.id == id), None))
    monkeypatch.setattr(cli_deploy, "get_registry", lambda: registry)
    probed = []

    def runner_for(d, **kw):
        probed.append(d.id)
        return FakeRunner("os Linux\npm apt-get\n")
    monkeypatch.setattr("workflow.deploy.ssh.runner_for", runner_for)

    deployed = []
    monkeypatch.setattr("workflow.deploy.runner.deploy",
                        lambda d, **kw: deployed.append(d.id))
    return SimpleNamespace(app=cli_deploy.deploy_app, probed=probed, deployed=deployed)


def test_deploy_stops_before_touching_anything(fleet):
    result = CliRunner().invoke(fleet.app, ["run", "--all", "--yes"])

    assert result.exit_code == 1
    assert "tmux is not installed on rig-1" in result.output
    assert "sudo apt-get update && sudo apt-get install -y tmux" in result.output
    assert "nothing was changed" in result.output
    # Not even the box that didn't need tmux was deployed.
    assert fleet.deployed == []
    # Only agent targets are probed.
    assert fleet.probed == ["rig-1"]


def test_serve_refuses_to_start_without_tmux(monkeypatch):
    from workflow import cli_agent

    monkeypatch.setattr("workflow.deploy.ssh.LocalRunner",
                        lambda d: FakeRunner("os Darwin\npm brew\n"))
    started = []
    monkeypatch.setattr(cli_agent, "_client_and_device",
                        lambda d: started.append(d) or (None, None))

    result = CliRunner().invoke(cli_agent.agent_app, ["serve"])

    assert result.exit_code == 1
    assert "tmux is not installed on this machine" in result.output
    assert "brew install tmux" in result.output
    assert started == [], "must stop before connecting or claiming anything"
