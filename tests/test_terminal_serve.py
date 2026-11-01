import sys
import threading
import time
from pathlib import Path

import pytest

from workflow.deploy import supervisor
from workflow.deploy.terminal_serve import (
    PtySession, Settings, SpawnRefused, TerminalDevice, harness_argv, harness_env,
)


def settings(tmp_path: Path, **overrides) -> Settings:
    base = dict(workspace="ws", device_key="mac", url="https://ng.test", token="pat",
                cwd=tmp_path, state_dir=tmp_path / "state")
    base.update(overrides)
    return Settings(**base)


class Recorder:
    def __init__(self):
        self.events = []
        self.down = False
        self.changed = threading.Condition()

    def __call__(self, event, payload):
        if self.down:
            raise ConnectionError("down")
        with self.changed:
            self.events.append((event, payload))
            self.changed.notify_all()

    def named(self, event):
        return [payload for name, payload in self.events if name == event]

    def wait_for(self, event, timeout=10):
        deadline = time.monotonic() + timeout
        with self.changed:
            while not self.named(event):
                left = deadline - time.monotonic()
                if left <= 0:
                    raise AssertionError(f"no {event}: {self.events}")
                self.changed.wait(left)
        return self.named(event)

    def output(self):
        return "".join(p["data"] for p in self.named("terminal:device:output"))


def python_harness(script):
    """A spawn that ignores the harness and runs `script` in a real pty."""
    def spawn(session_id, argv, env, cwd, cols, rows, on_output, on_exit):
        return PtySession(session_id, [sys.executable, "-c", script], env, cwd,
                          cols, rows, on_output, on_exit)
    return spawn


def which_all(name):
    return f"/usr/bin/{name}"


@pytest.fixture(autouse=True)
def harnesses_installed(monkeypatch):
    """Every harness program "on PATH": the pty tests run python instead."""
    monkeypatch.setattr("workflow.deploy.terminal_serve.shutil.which", which_all)


def test_claude_gets_notesgraph_mcp_and_resume(tmp_path):
    s = settings(tmp_path)
    argv = harness_argv(s, "claude", "abc", resume="conv-1", which=which_all)
    assert argv[0] == "claude" and argv[-2:] == ["--resume", "conv-1"]
    mcp = Path(argv[argv.index("--mcp-config") + 1])
    assert mcp.stat().st_mode & 0o777 == 0o600
    assert "Bearer pat" in mcp.read_text()
    assert "/api/workspaces/ws/mcp" in mcp.read_text()


def test_other_harnesses_resume_their_own_way(tmp_path):
    s = settings(tmp_path)
    assert harness_argv(s, "codex", "a", "c1", which=which_all) == ["codex", "resume", "c1"]
    assert harness_argv(s, "gemini", "a", "c1", which=which_all) == ["gemini", "--resume", "c1"]
    assert harness_argv(s, "codex", "a", which=which_all) == ["codex"]


def test_shell_is_off_unless_allowed(tmp_path):
    with pytest.raises(SpawnRefused, match="allow_shell"):
        harness_argv(settings(tmp_path), "shell", "a")
    assert harness_argv(settings(tmp_path, allow_shell=True), "shell", "a")[-1] == "-l"


def test_refuses_missing_program_and_odd_resume(tmp_path):
    with pytest.raises(SpawnRefused, match="isn't installed"):
        harness_argv(settings(tmp_path), "codex", "a", which=lambda name: None)
    with pytest.raises(SpawnRefused, match="Unknown"):
        harness_argv(settings(tmp_path), "vim", "a")
    with pytest.raises(SpawnRefused):
        harness_argv(settings(tmp_path), "codex", "a", "x; rm -rf /", which=which_all)


def test_claude_env_uses_project_account(tmp_path, monkeypatch):
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.setenv("CLAUDECODE", "1")
    env = harness_env(settings(tmp_path, claude_config_dir="/acct"), "claude")
    assert env["CLAUDE_CONFIG_DIR"] == "/acct"
    assert env["TERM"] == "xterm-256color"
    assert "CLAUDECODE" not in env


def test_spawn_streams_output_and_exit(tmp_path):
    emit = Recorder()
    device = TerminalDevice(settings(tmp_path), emit,
                            spawn=python_harness("print('héllo'); raise SystemExit(3)"))
    device.spawn({"id": "s1", "harness": "codex", "cols": 80, "rows": 24})
    exits = emit.wait_for("terminal:device:exit")
    assert exits == [{"workspaceId": "ws", "sessionId": "s1", "code": 3}]
    assert "héllo" in emit.output()
    assert emit.named("terminal:device:started") == [{"workspaceId": "ws", "sessionId": "s1"}]
    assert not device.busy()


def test_stdin_and_resize_reach_the_harness(tmp_path):
    emit = Recorder()
    script = ("import os, sys\n"
              "c, r = os.get_terminal_size(0)\n"
              "print('size', c, r, flush=True)\n"
              "line = sys.stdin.readline()\n"
              "print('got', line.strip(), flush=True)\n")
    device = TerminalDevice(settings(tmp_path), emit, spawn=python_harness(script))
    device.spawn({"id": "s2", "harness": "codex", "cols": 91, "rows": 17})
    deadline = time.monotonic() + 10
    while "size" not in emit.output() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert "size 91 17" in emit.output()
    device.resize({"sessionId": "s2", "cols": 100, "rows": 30})
    device.stdin({"sessionId": "s2", "data": "ping\r"})
    emit.wait_for("terminal:device:exit")
    assert "got ping" in emit.output()


def test_kill_ends_the_harness(tmp_path):
    emit = Recorder()
    device = TerminalDevice(settings(tmp_path), emit,
                            spawn=python_harness("import time; time.sleep(60)"))
    device.spawn({"id": "s3", "harness": "codex"})
    emit.wait_for("terminal:device:started")
    device.kill({"sessionId": "s3"})
    assert emit.wait_for("terminal:device:exit")[0]["sessionId"] == "s3"


def test_refused_spawn_explains_and_exits(tmp_path):
    emit = Recorder()
    device = TerminalDevice(settings(tmp_path), emit)
    device.spawn({"id": "s4", "harness": "shell"})
    assert "allow_shell" in emit.output()
    assert emit.named("terminal:device:exit") == [
        {"workspaceId": "ws", "sessionId": "s4", "code": 127}]


def test_output_while_down_is_sent_on_reattach_in_order(tmp_path):
    emit = Recorder()
    device = TerminalDevice(settings(tmp_path), emit)
    emit.down = True
    device._output("s5", "one ")
    device._output("s5", "two")
    device._exited("s5", 0)
    assert emit.events == []
    emit.down = False
    device.attached({"pending": [], "live": []})
    assert emit.output() == "one two"
    assert emit.events[-1] == ("terminal:device:exit",
                               {"workspaceId": "ws", "sessionId": "s5", "code": 0})


def test_attach_starts_pending_and_ends_lost_sessions(tmp_path):
    emit = Recorder()
    started = []

    def spawn(session_id, *args, **kwargs):
        started.append(session_id)

        class Running:
            class process:
                @staticmethod
                def poll():
                    return None
        return Running()

    device = TerminalDevice(settings(tmp_path), emit, spawn=spawn)
    device.attached({"pending": [{"id": "new", "harness": "shell"}],
                     "live": [{"id": "lost"}]})
    # shell is refused (not allowed), so nothing spawns for it
    assert started == []
    assert {"workspaceId": "ws", "sessionId": "lost", "code": None} in emit.named(
        "terminal:device:exit")

    device = TerminalDevice(settings(tmp_path), emit, spawn=spawn)
    device.attached({"pending": [{"id": "new2", "harness": "codex"}], "live": []})
    assert started == ["new2"]
    # Sent again on a reconnect, it is not started twice.
    device.attached({"pending": [{"id": "new2", "harness": "codex"}], "live": []})
    assert started == ["new2"]


def test_terminal_program_installs_beside_the_runner(tmp_path):
    paths = supervisor.Paths(tmp_path)
    calls = []

    def run(cmd, **kwargs):
        calls.append(cmd)

        class R:
            stdout = stderr = ""
            returncode = 0
        return R()

    for directory in (paths.root, paths.programs, paths.logs):
        directory.mkdir(parents=True, exist_ok=True)
    paths.program_file(supervisor.PROGRAM).write_text("[program:wf-agent]\n")
    paths.program_file(supervisor.TERMINAL_PROGRAM).write_text("[program:wf-terminal]\n")
    supervisor.uninstall(paths, run=run, name=supervisor.TERMINAL_PROGRAM)
    # The runner is still there, so supervisord and its login item stay.
    assert paths.program_file(supervisor.PROGRAM).exists()
    assert not paths.program_file(supervisor.TERMINAL_PROGRAM).exists()
    assert not any("launchctl" in str(c) or "systemctl" in str(c) for c in calls)
    assert supervisor.terminal_command("mac")[-4:] == [
        "serve", "--exit-on-update", "--device", "mac"]
