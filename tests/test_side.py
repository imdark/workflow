"""Tests for `wf side`, which forks a Claude conversation into a new tab.

Nothing here opens a tab: `subprocess.run` is replaced wherever open_side()
would reach the terminal.
"""

import subprocess

import pytest

from workflow import side


def test_fork_command_quotes_the_directory():
    assert side.fork_command("abc-123", "/tmp/my dir") == \
        "cd '/tmp/my dir' && claude --resume abc-123 --fork-session"


@pytest.mark.parametrize("env, expected", [
    ({"TMUX": "/tmp/tmux-501/default,1,0", "TERM_PROGRAM": "tmux"}, "tmux"),
    ({"TERM_PROGRAM": "iTerm.app"}, "iterm"),
    ({"TERM_PROGRAM": "Apple_Terminal"}, "terminal"),
    ({"TERM_PROGRAM": "vscode"}, None),
    ({}, None),
])
def test_detect_terminal(env, expected):
    assert side.detect_terminal(env) == expected


def test_terminal_script_targets_the_tty_and_escapes_the_command():
    script = side.terminal_script('cd "/x" && claude', "/dev/ttys001")
    assert 'if tty of t is "/dev/ttys001" then' in script
    assert 'keystroke "t" using command down' in script
    assert 'do script "cd \\"/x\\" && claude" in selected tab of front window' in script


def test_terminal_script_without_tty_uses_the_front_window():
    assert "tty of" not in side.terminal_script("claude", None)


def test_iterm_script_creates_a_tab():
    script = side.iterm_script("claude", "/dev/ttys002")
    assert 'if tty of s is "/dev/ttys002" then set target to w' in script
    assert "create tab with default profile" in script
    assert 'write text "claude"' in script


def _record(monkeypatch, returncode=0, stderr=""):
    calls = []

    def run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        if cmd[0] == "ps":
            return subprocess.CompletedProcess(cmd, 0, stdout="ttys001\n", stderr="")
        return subprocess.CompletedProcess(cmd, returncode, stdout="", stderr=stderr)

    monkeypatch.setattr(side.subprocess, "run", run)
    return calls


def test_open_side_in_tmux_opens_a_window_after_the_current_one(monkeypatch):
    calls = _record(monkeypatch)
    assert side.open_side("abc", "/repo", "tmux") == "tmux"
    assert calls[-1][0] == ["tmux", "new-window", "-a", "-c", "/repo",
                            "cd /repo && claude --resume abc --fork-session"]


def test_open_side_in_terminal_finds_the_sessions_tab(monkeypatch):
    calls = _record(monkeypatch)
    side.open_side("abc", "/repo", "terminal", pid=42)
    assert calls[0][0] == ["ps", "-o", "tty=", "-p", "42"]
    cmd, kwargs = calls[1]
    assert cmd == ["osascript", "-"]
    assert '"/dev/ttys001"' in kwargs["input"]


def test_open_side_reports_failures(monkeypatch):
    _record(monkeypatch, returncode=1, stderr="not allowed assistive access")
    with pytest.raises(RuntimeError, match="assistive access"):
        side.open_side("abc", "/repo", "terminal")


def test_open_side_needs_a_known_terminal(monkeypatch):
    monkeypatch.setenv("TERM_PROGRAM", "vscode")
    monkeypatch.delenv("TMUX", raising=False)
    with pytest.raises(RuntimeError, match="--terminal"):
        side.open_side("abc", "/repo")
