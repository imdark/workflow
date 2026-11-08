"""`wf install`: idempotent rc-file edits, alias placement, and the report."""
import os
from pathlib import Path
from unittest import mock

from typer.testing import CliRunner

from workflow import install


def test_ensure_lines_appends_once(tmp_path):
    rc = tmp_path / ".zshrc"
    rc.write_text("export A=1\n")
    assert install.ensure_lines(rc, ["# c", "alias x='y'"])
    assert not install.ensure_lines(rc, ["# c", "alias x='y'"])
    assert rc.read_text() == "export A=1\n\n# c\nalias x='y'\n"


def test_ensure_lines_creates_missing_file(tmp_path):
    rc = tmp_path / ".bashrc"
    assert install.ensure_lines(rc, ["alias x='y'"])
    assert rc.read_text() == "\nalias x='y'\n"


def test_install_aliases_user_files(tmp_path):
    files = [tmp_path / ".zshrc", tmp_path / ".bashrc"]
    step = install.install_aliases(files=files)
    assert step.ok and step.changed
    for f in files:
        assert install.CLAUDE_ALIAS in f.read_text()
    again = install.install_aliases(files=files)
    assert again.ok and not again.changed


def test_install_aliases_system_skips_absent_shells_and_uses_sudo(tmp_path):
    present = tmp_path / "zshrc"
    present.write_text("# system\n")
    absent = tmp_path / "bashrc"
    calls = []

    def fake_run(cmd, **kw):
        calls.append((cmd, kw.get("input")))
        with open(cmd[-1].split(">> ")[1].strip("'"), "a") as f:
            f.write(kw["input"])
        return mock.Mock(returncode=0)

    with mock.patch.object(install, "_run", fake_run), \
         mock.patch.object(install, "_sudo_available", return_value=True), \
         mock.patch.object(os, "geteuid", return_value=501):
        step = install.install_aliases(system=True, files=[present, absent])
    assert step.ok and step.changed, step
    assert len(calls) == 1 and calls[0][0][:2] == ["sudo", "sh"]
    assert install.CLAUDE_ALIAS in present.read_text()
    assert not absent.exists()


def test_install_aliases_system_without_terminal_reports_instead_of_hanging():
    with mock.patch.object(install, "_sudo_available", return_value=False), \
         mock.patch.object(os, "geteuid", return_value=501):
        step = install.install_aliases(system=True)
    assert not step.ok and "sudo" in step.detail


def test_install_completion_zsh_writes_file_and_rc(tmp_path):
    with mock.patch.object(install, "completion_script", return_value="#compdef wf\n_wf() {}\n"):
        step = install.install_completion("zsh", home=tmp_path)
    assert step.ok and step.changed, step
    assert (tmp_path / ".zsh/completion/_wf").read_text().startswith("#compdef wf")
    assert "fpath+=~/.zsh/completion" in (tmp_path / ".zshrc").read_text()
    with mock.patch.object(install, "completion_script", return_value="#compdef wf\n_wf() {}\n"):
        assert not install.install_completion("zsh", home=tmp_path).changed


def test_install_completion_refuses_empty_script(tmp_path):
    with mock.patch.object(install, "completion_script", return_value=""):
        step = install.install_completion("zsh", home=tmp_path)
    assert not step.ok
    assert not (tmp_path / ".zsh/completion/_wf").exists()


def test_cli_runs_requested_steps_and_reports():
    ok = install.Step("aliases", True, "in ~/.zshrc", changed=True)
    bad = install.Step("launcher", False, "needs: sudo ln -sf a b")
    with mock.patch.object(install, "install_all", return_value=install.Report([ok, bad])) as m:
        from workflow.cli import app
        r = CliRunner().invoke(app, ["install", "--no-deps", "--system"])
    assert r.exit_code == 1, r.output
    assert m.call_args.kwargs["deps"] is False and m.call_args.kwargs["system"] is True
    assert "✅ aliases: in ~/.zshrc" in r.output and "❌ launcher" in r.output


def test_install_aliases_upgrades_an_older_alias_line(tmp_path):
    rc = tmp_path / ".zshrc"
    rc.write_text("export A=1\n\n# old comment\nalias claude='wf ai --'\n")
    step = install.install_aliases(files=[rc])
    assert step.ok and step.changed
    text = rc.read_text()
    assert text.count("alias claude=") == 1
    assert install.CLAUDE_ALIAS in text and "alias claude='wf ai --'\n" not in text
