"""wf lint: run a repo's lint steps on the files a branch changed."""
import json
import subprocess
from pathlib import Path

import pytest

from workflow.lint import (
    LintError,
    changed_files,
    lint_repo,
    load_lint_steps,
    run_lint,
)


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A repo on main with one commit, then a branch `work` off it."""
    git(tmp_path, "init", "-q", "-b", "main")
    git(tmp_path, "config", "user.email", "t@example.com")
    git(tmp_path, "config", "user.name", "t")
    (tmp_path / "old.ts").write_text("old\n")
    (tmp_path / "notes.md").write_text("old\n")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-qm", "base")
    git(tmp_path, "checkout", "-qb", "work")
    return tmp_path


def write_config(repo: Path, text: str) -> None:
    (repo / "wf.yaml").write_text(text)


def test_changed_files_covers_commits_edits_and_new_files(repo: Path):
    (repo / "committed.ts").write_text("a\n")
    git(repo, "add", "committed.ts")
    git(repo, "commit", "-qm", "c")
    (repo / "old.ts").write_text("edited\n")
    (repo / "untracked.ts").write_text("u\n")

    assert changed_files(repo, "main") == ["committed.ts", "old.ts", "untracked.ts"]


def test_changed_files_leaves_out_deleted_files(repo: Path):
    git(repo, "rm", "-q", "old.ts")
    git(repo, "commit", "-qm", "rm")

    assert changed_files(repo, "main") == []


def test_steps_come_from_wf_yaml(repo: Path):
    write_config(repo, "lint:\n  - name: check\n    run: echo {files}\n    files: ['*.ts']\n")

    steps = load_lint_steps(repo)

    assert [(s.name, s.run, s.files) for s in steps] == [("check", "echo {files}", ["*.ts"])]


def test_a_step_runs_on_the_changed_files_its_globs_match(repo: Path):
    write_config(repo, "lint:\n  - name: ts\n    run: echo linting {files}\n    files: ['*.ts']\n")
    (repo / "a.ts").write_text("a\n")
    (repo / "b.md").write_text("b\n")

    [result] = run_lint(repo, base="main")

    assert result["ok"] is True
    assert result["files"] == ["a.ts"]
    assert "linting a.ts" in result["output"]


def test_a_step_with_no_matching_changed_files_is_skipped(repo: Path):
    write_config(repo, "lint:\n  - name: ts\n    run: 'false'\n    files: ['*.ts']\n")
    (repo / "only.md").write_text("b\n")

    [result] = run_lint(repo, base="main")

    assert result["skipped"] is True
    assert result["ok"] is True


def test_all_lints_the_whole_repo(repo: Path):
    write_config(repo, "lint:\n  - name: ts\n    run: echo linting {files}\n    files: ['*.ts']\n")

    [result] = run_lint(repo, base="main", all_files=True)

    assert result["ok"] is True
    assert "linting ." in result["output"]


def test_fix_runs_the_fix_command(repo: Path):
    write_config(
        repo,
        "lint:\n  - name: ts\n    run: 'false'\n    fix: echo fixing {files}\n    files: ['*.ts']\n",
    )
    (repo / "a.ts").write_text("a\n")

    [result] = run_lint(repo, base="main", fix=True)

    assert result["ok"] is True
    assert "fixing a.ts" in result["output"]


def test_a_failing_step_reports_its_exit_code_and_output(repo: Path):
    write_config(repo, "lint:\n  - name: ts\n    run: echo bad {files}; exit 3\n")
    (repo / "a.ts").write_text("a\n")

    [result] = run_lint(repo, base="main")

    assert result["ok"] is False
    assert result["exit_code"] == 3
    assert "bad a.ts" in result["output"]


def test_files_with_spaces_are_quoted(repo: Path):
    write_config(repo, "lint:\n  - name: ts\n    run: printf '<%s>' {files}\n")
    (repo / "two words.ts").write_text("a\n")

    [result] = run_lint(repo, base="main")

    assert "<two words.ts>" in result["output"]


def test_without_wf_yaml_the_package_lint_script_runs_on_the_whole_repo(repo: Path):
    (repo / "package.json").write_text(json.dumps({"scripts": {"lint": "echo pkg lint"}}))

    steps = load_lint_steps(repo)

    assert [s.name for s in steps] == ["package.json lint"]
    assert "{files}" not in steps[0].run


def test_no_lint_configured_is_an_empty_list(repo: Path):
    assert load_lint_steps(repo) == []


def test_lint_repo_runs_from_a_subdirectory(repo: Path):
    write_config(repo, "lint:\n  - name: ts\n    run: echo linting {files}\n    files: ['*.ts']\n")
    (repo / "sub").mkdir()
    (repo / "sub" / "a.ts").write_text("a\n")

    [result] = lint_repo(repo / "sub", base="main")

    assert result["files"] == ["sub/a.ts"]


def test_lint_repo_without_lint_configured_says_how_to_add_it(repo: Path):
    with pytest.raises(LintError, match="wf.yaml"):
        lint_repo(repo, base="main")


def test_lint_repo_outside_git_is_an_error(tmp_path: Path):
    with pytest.raises(LintError, match="Not a git repository"):
        lint_repo(tmp_path)


def test_the_mcp_tool_reports_overall_ok_and_each_step(repo: Path):
    from workflow import mcp as wf_mcp

    write_config(repo, "lint:\n  - name: ts\n    run: exit 1\n    files: ['*.ts']\n")
    (repo / "a.ts").write_text("a\n")
    tool = getattr(wf_mcp.lint, "fn", wf_mcp.lint)

    report = json.loads(tool(str(repo), base="main"))

    assert report["ok"] is False
    assert [s["name"] for s in report["steps"]] == ["ts"]


def test_the_mcp_tool_returns_config_problems_as_an_error(repo: Path):
    from workflow import mcp as wf_mcp

    tool = getattr(wf_mcp.lint, "fn", wf_mcp.lint)

    assert tool(str(repo), base="main").startswith("Error: No lint configured")


def test_a_step_without_run_is_a_config_error(repo: Path):
    write_config(repo, "lint:\n  - name: broken\n")

    with pytest.raises(LintError, match="needs a `run` command"):
        lint_repo(repo, base="main")
