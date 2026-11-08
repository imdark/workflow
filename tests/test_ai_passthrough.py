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

    def run(self, context, issue=None, args=None, project=None):
        self.calls.append((context, args))
        self.projects = getattr(self, "projects", []) + [project]
        return ""


def _invoke(provider, argv, task=None):
    import workflow.cli as cli
    def pick(project=None):
        provider.picked = project
        return provider

    with mock.patch("workflow.ai.get_ai_provider", side_effect=pick), \
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


def test_cli_without_task_explains_tasks_except_in_print_mode():
    p = _Provider()
    with mock.patch("workflow.projects.get_current_project", return_value="acme"):
        r = _invoke(p, ["--", "-p", "hi"])
        assert r.exit_code == 0, r.output
        assert p.calls == [(None, ["-p", "hi"])]  # one answer: no task talk
        r = _invoke(p, [])
        assert r.exit_code == 0, r.output
    context, args = p.calls[-1]
    assert args == [] and context.startswith("NO ACTIVE TASK")
    assert "No wf task is in progress" in context and "--project acme" in context


def test_cli_here_uses_the_folder_project_and_drops_foreign_task(tmp_path):
    task = mock.Mock(key="OTHER-7")
    p = _Provider()
    with mock.patch("workflow.cli.project_for_path", return_value="acme", create=True), \
         mock.patch("workflow.projects.project_for_path", return_value="acme"), \
         mock.patch("workflow.projects.get_project_key", return_value="ACME"), \
         mock.patch("workflow.projects.get_default_repo", return_value=None):
        r = _invoke(p, ["--here", "--", "--model", "opus"], task=task)
    assert r.exit_code == 0, r.output
    assert p.picked == "acme"
    assert p.projects == ["acme"]
    (context, args), = p.calls
    assert args == ["--model", "opus"]
    assert "belongs to another project" in r.output
    # OTHER-7 is not acme's task: the agent is told to make its own instead.
    assert context.startswith("NO ACTIVE TASK")
    assert "OTHER-7 belongs to another project" in context
    assert "wf task create" in context and "--project acme" in context
    assert "wf done --task <KEY>" in context


def test_cli_project_flag_rejects_unknown_and_excludes_here():
    p = _Provider()
    with mock.patch("workflow.projects.get_project", return_value=None):
        r = _invoke(p, ["--project", "nope"])
    assert r.exit_code == 2 and "Unknown project" in r.output
    r = _invoke(p, ["--here", "--project", "x"])
    assert r.exit_code == 2
    assert p.calls == []
