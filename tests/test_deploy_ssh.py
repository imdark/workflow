"""`wf deploy ssh NAME`: ssh with the project's key, user, port and options filled in.

Nothing here opens a connection: the command line is built and checked, and `os.execvp` is replaced.
"""
from __future__ import annotations

import pytest
import yaml
from typer.testing import CliRunner

from workflow import cli_deploy
from workflow.deploy import ssh_config
from workflow.deploy.registry import KIND_FOLDER, Device
from workflow.deploy.registry.local import LocalRegistry
from workflow.deploy.ssh import runner_for

runner = CliRunner()

CONFIG = {
    "task_backend": "markdown",
    "ssh": {"key": "/keys/everywhere", "options": ["ServerAliveInterval=30"]},
    "projects": {
        "personal": {"name": "PERSONAL", "ssh": {
            "default": "prod", "user": "ubuntu", "key": "/keys/personal.pem",
            "hosts": {"prod": {"host": "18.0.0.1"},
                      "bastion": {"host": "10.0.0.9", "user": "ops", "port": 2222, "key": "/keys/bastion",
                                  "options": ["ProxyJump=none"]},
                      "short": "198.51.100.7"}}},
        "work": {"name": "PEN"},
    },
}


@pytest.fixture
def world(tmp_path, monkeypatch):
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(yaml.dump(CONFIG))
    monkeypatch.setattr("workflow.config.CFG", cfg_path)
    monkeypatch.setattr(ssh_config, "current_project", lambda: "personal")
    monkeypatch.setattr(cli_deploy, "get_registry", LocalRegistry)
    ran = []
    monkeypatch.setattr("os.execvp", lambda prog, argv: ran.append(argv))
    return type("W", (), {"cfg": cfg_path, "ran": ran})()


def _wf(*args):
    return runner.invoke(cli_deploy.deploy_app, list(args))


def test_a_name_becomes_the_whole_ssh_command(world):
    assert _wf("ssh", "prod").exit_code == 0
    assert world.ran == [["ssh", "-i", "/keys/personal.pem", "-o", "IdentitiesOnly=yes",
                          "-o", "ServerAliveInterval=30", "ubuntu@18.0.0.1"]]


def test_each_setting_comes_from_the_most_specific_place_that_has_it():
    bastion = ssh_config.resolve("bastion", "personal", CONFIG)
    assert (bastion.destination, bastion.port, bastion.key, bastion.options) == \
        ("ops@10.0.0.9", 2222, "/keys/bastion", ["ProxyJump=none"])
    prod = ssh_config.resolve("prod", "personal", CONFIG)
    assert prod.sources == {"user": "project personal defaults", "key": "project personal defaults",
                            "options": "global defaults"}
    # A host can be written as just its address.
    assert ssh_config.resolve("short", "personal", CONFIG).destination == "ubuntu@198.51.100.7"
    # Another project doesn't see personal's machines, only the global defaults.
    assert ssh_config.resolve("prod", "work", CONFIG) is None
    assert ssh_config.sections("work", CONFIG) == [("global", CONFIG["ssh"])]


def test_what_follows_the_name_goes_to_ssh_as_typed(world):
    _wf("ssh", "prod", "-L", "8080:localhost:8080", "-N")
    assert world.ran[-1][:4] == ["ssh", "-L", "8080:localhost:8080", "-N"] and world.ran[-1][-1] == "ubuntu@18.0.0.1"
    # A command keeps its own flags: ssh's options end at the first other word.
    _wf("ssh", "prod", "tail", "-f", "/var/log/syslog")
    assert world.ran[-1][-4:] == ["ubuntu@18.0.0.1", "tail", "-f", "/var/log/syslog"]
    _wf("ssh", "bastion", "-v", "--", "ls", "-la")
    assert world.ran[-1] == ["ssh", "-v", "-i", "/keys/bastion", "-o", "IdentitiesOnly=yes", "-o", "ProxyJump=none",
                             "-p", "2222", "ops@10.0.0.9", "ls", "-la"]


def test_an_option_given_by_hand_replaces_the_configured_one(world):
    _wf("ssh", "bastion", "-p", "22", "-l", "root", "-i", "/keys/mine")
    assert world.ran[-1] == ["ssh", "-p", "22", "-l", "root", "-i", "/keys/mine", "-o", "ProxyJump=none", "10.0.0.9"]


def test_split_args_reads_options_as_ssh_does():
    assert ssh_config.split_args(["-vvv", "-L80:x:80", "-o", "A=b", "ls", "-l"]) == \
        (["-vvv", "-L80:x:80", "-o", "A=b"], ["ls", "-l"])
    assert ssh_config.split_args([]) == ([], [])
    assert ssh_config.split_args(["-N", "--", "-weird"]) == (["-N"], ["-weird"])


def test_no_name_means_the_projects_default_machine(world):
    _wf("ssh")
    assert world.ran[-1][-1] == "ubuntu@18.0.0.1"
    # A first word that isn't a machine is a command for the default one.
    _wf("ssh", "uptime")
    assert world.ran[-1][-2:] == ["ubuntu@18.0.0.1", "uptime"]
    _wf("ssh", "-N")
    assert world.ran[-1][1] == "-N"


def test_no_machine_and_no_default_says_what_exists(world):
    r = _wf("ssh", "nowhere", "--project", "work")
    assert r.exit_code == 1 and "'nowhere' is not a machine" in r.output and "ssh-host" in r.output
    assert world.ran == []
    assert _wf("ssh", "prod", "--project", "nope").exit_code == 1


def test_dry_run_prints_the_command_and_where_settings_came_from(world):
    r = _wf("ssh", "--dry-run", "prod", "uptime")
    assert r.exit_code == 0 and world.ran == []
    assert "ssh -i /keys/personal.pem -o IdentitiesOnly=yes -o ServerAliveInterval=30 ubuntu@18.0.0.1 uptime" in r.output
    assert "key: project personal defaults" in r.output


def test_a_registered_target_takes_the_projects_defaults(world):
    registry = LocalRegistry()
    registry.register(Device(id="rig", host="rig.local", user="pantheon"))
    registry.register(Device(id="rig:code", kind=KIND_FOLDER, host="rig.local", user="pantheon",
                             parent="rig", path="/home/p/my code"))
    _wf("ssh", "rig")
    # Its own user, the project's key.
    assert world.ran[-1] == ["ssh", "-i", "/keys/personal.pem", "-o", "IdentitiesOnly=yes",
                             "-o", "ServerAliveInterval=30", "pantheon@rig.local"]
    # A folder target lands in its folder, with a terminal for the shell that stays.
    _wf("ssh", "rig:code")
    assert world.ran[-1][-3:] == ["-t", "pantheon@rig.local", "cd '/home/p/my code' && exec \"${SHELL:-sh}\" -l"]
    _wf("ssh", "rig:code", "git", "status")
    assert world.ran[-1][-1] == "cd '/home/p/my code' && git status"


def test_the_other_deploy_commands_connect_the_same_way(world):
    ssh = runner_for(Device(id="rig", host="rig.example", port=22))
    assert ssh.device.ssh_destination == "ubuntu@rig.example"
    assert ssh.extra_args == ["-i", "/keys/personal.pem", "-o", "IdentitiesOnly=yes", "-o", "ServerAliveInterval=30"]
    assert ssh._base_args()[-6:] == ssh.extra_args
    # A caller that passes its own arguments keeps them.
    assert runner_for(Device(id="rig", host="rig.example"), extra_args=["-v"]).extra_args == ["-v"]


def test_ssh_host_and_ssh_config_write_the_projects_settings(world):
    r = _wf("ssh-host", "staging", "deploy@203.0.113.4:2200", "--key", "~/.ssh/staging", "--default")
    assert r.exit_code == 0, r.output
    saved = yaml.safe_load(world.cfg.read_text())["projects"]["personal"]["ssh"]
    assert saved["hosts"]["staging"] == {"host": "203.0.113.4", "user": "deploy", "port": 2200, "key": "~/.ssh/staging"}
    assert saved["default"] == "staging" and saved["hosts"]["prod"] == {"host": "18.0.0.1"}
    # Changing one thing keeps the rest.
    _wf("ssh-host", "staging", "--key", "/keys/new")
    assert yaml.safe_load(world.cfg.read_text())["projects"]["personal"]["ssh"]["hosts"]["staging"]["host"] == "203.0.113.4"
    assert _wf("ssh-host", "brand-new").exit_code == 1
    _wf("ssh-host", "staging", "--remove")
    saved = yaml.safe_load(world.cfg.read_text())["projects"]["personal"]["ssh"]
    assert "staging" not in saved["hosts"] and "default" not in saved

    assert _wf("ssh-config", "--user", "admin", "--port", "2022", "--global").exit_code == 0
    cfg = yaml.safe_load(world.cfg.read_text())
    assert cfg["ssh"]["user"] == "admin" and cfg["ssh"]["port"] == 2022 and cfg["ssh"]["key"] == "/keys/everywhere"
    listing = _wf("ssh-config").output
    assert "prod" in listing and "ubuntu@18.0.0.1:2022" in listing and "key from project personal defaults" in listing


def test_the_tunnel_connects_the_same_way_and_takes_a_named_machine(world, monkeypatch):
    from workflow.deploy.tunnel import _tunnel_args
    args = _tunnel_args(Device(id="prod", host="ignored.example"), 8099, 7000)
    # The project's host, user and key; the tunnel's own safety options stay.
    assert args[:5] == ["ssh", "-N", "-i", "/keys/personal.pem", "-o"] and args[-1] == "ubuntu@18.0.0.1"
    assert "ExitOnForwardFailure=yes" in args and args[args.index("-R") + 1] == "8099:127.0.0.1:7000"
    bastion = _tunnel_args(Device(id="bastion", host="x"), 8099, 7000)
    assert bastion[bastion.index("-p") + 1] == "2222" and bastion[-1] == "ops@10.0.0.9"

    opened = []

    class Held:
        def __enter__(self):
            return type("P", (), {"wait": lambda self: None})()

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr("workflow.proxy.daemon.status", lambda: {"running": True})
    monkeypatch.setattr("workflow.deploy.tunnel.reverse_tunnel",
                        lambda device, remote_port: opened.append((device, remote_port)) or Held())
    r = _wf("tunnel", "prod", "--port", "9000")
    assert r.exit_code == 0, r.output
    (device, port), = opened
    assert (device.id, device.ssh_destination, port) == ("prod", "ubuntu@18.0.0.1", 9000)
    assert _wf("tunnel", "nowhere").exit_code == 1
