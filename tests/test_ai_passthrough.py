"""`wf ai -- <claude flags>` forwards everything after `--` to the claude CLI."""
from pathlib import Path
from unittest import mock

from typer.testing import CliRunner

from workflow.ai_providers.claude import claude_command, resumes_session


def test_resumes_session_detects_resume_and_continue_flags():
    assert resumes_session(["-r"])
    assert resumes_session(["--resume", "abc"])
    assert resumes_session(["--", "-c"])
    assert resumes_session(["--from-pr=12"])
    assert not resumes_session([])
    assert not resumes_session(["--model", "opus", "-p", "hi"])


def test_claude_command_pipes_prompt_and_quotes_args():
    cmd = claude_command(Path("/t/x y.txt"), Path("/t/p.prompt"), ["--model", "opus", "-p", "say hi"])
    import shlex
    words = shlex.split(cmd)
    assert words[:5] == ["script", "-q", "/t/x y.txt", "bash", "-c"]
    assert words[5] == "cat /t/p.prompt | claude --model opus -p 'say hi'"


def test_claude_command_without_prompt_keeps_stdin_free():
    cmd = claude_command(Path("/t/x.txt"), None, ["-r"])
    assert "cat" not in cmd
    assert cmd.endswith("bash -c 'claude -r'")


class _Provider:
    name = "claude"

    def __init__(self):
        self.calls = []

    def run(self, context, issue=None, args=None):
        self.calls.append((context, args))
        return ""


def _invoke(provider, argv, task=None):
    import workflow.cli as cli
    with mock.patch("workflow.ai.get_ai_provider", return_value=provider), \
         mock.patch("workflow.cli.get_current_task", return_value=task), \
         mock.patch("workflow.cli.build_context", return_value="CTX", create=True), \
         mock.patch("workflow.ai_context.build_context", return_value="CTX"):
        return CliRunner().invoke(cli.app, ["ai", *argv])


def test_cli_forwards_args_after_double_dash():
    p = _Provider()
    r = _invoke(p, ["--", "-r", "abc", "--model", "opus"])
    assert r.exit_code == 0, r.output
    assert p.calls == [(None, ["-r", "abc", "--model", "opus"])]


def test_cli_resume_skips_task_context_but_prompt_wins():
    task = mock.Mock(key="T-1")
    p = _Provider()
    with mock.patch("workflow.cli.create_ai_pid_file", create=True), \
         mock.patch("workflow.pid_manager.create_ai_pid_file"), \
         mock.patch("workflow.pid_manager.remove_ai_pid_file"), \
         mock.patch("workflow.memory.save_session"), mock.patch("workflow.memory.save_meta"):
        r = _invoke(p, ["--", "-c"], task=task)
        assert r.exit_code == 0, r.output
        assert p.calls[-1] == (None, ["-c"])
        r = _invoke(p, ["--prompt", "hello", "--", "-c"], task=task)
        assert r.exit_code == 0, r.output
        assert p.calls[-1] == ("hello", ["-c"])
        r = _invoke(p, [], task=task)
        assert r.exit_code == 0, r.output
        assert p.calls[-1] == ("CTX", [])


def test_cli_without_task_starts_without_context():
    p = _Provider()
    r = _invoke(p, ["--", "-p", "hi"])
    assert r.exit_code == 0, r.output
    assert p.calls == [(None, ["-p", "hi"])]
