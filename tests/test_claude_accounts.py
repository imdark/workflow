"""Tests for per-project Claude account switching.

Claude Pro/Max logins are OAuth credentials, not keys, so the switch is the
config directory: Claude Code keys its macOS Keychain entry to
CLAUDE_CONFIG_DIR. These tests cover the mapping and the environment
handling; they never invoke `claude` or touch a real credential.
"""

import pytest
import yaml

from workflow import claude_accounts


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    """A workflow config with two projects, isolated from ~/.wf.

    Patches the config *path*, not load_config/save_config. Several modules
    do `from .config import load_config` at import time, so replacing those
    functions leaks into whichever module gets imported while the patch is
    live and survives the revert.
    """
    config = {"projects": {
        "work": {"name": "PAN", "claude_config_dir": str(tmp_path / "claude-work")},
        "personal": {"name": "PERSONAL"},
    }}
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(yaml.dump(config))
    monkeypatch.setattr("workflow.config.CFG", cfg_path)
    return config


def test_reads_project_directory(cfg, tmp_path):
    assert claude_accounts.config_dir_for_project("work", cfg) == str(tmp_path / "claude-work")


def test_project_without_mapping_returns_none(cfg):
    """No mapping means Claude Code's own default applies; nothing is switched."""
    assert claude_accounts.config_dir_for_project("personal", cfg) is None


def test_unknown_project_returns_none(cfg):
    assert claude_accounts.config_dir_for_project("nope", cfg) is None


def test_tilde_is_expanded(cfg):
    cfg["projects"]["personal"]["claude_config_dir"] = "~/.claude-personal"
    resolved = claude_accounts.config_dir_for_project("personal", cfg)
    assert resolved.startswith("/") and "~" not in resolved


def test_apply_sets_env_and_creates_directory(cfg, tmp_path, monkeypatch):
    monkeypatch.setattr("workflow.projects.get_current_project", lambda: "work")
    env = {}
    applied = claude_accounts.apply(env)

    assert applied == str(tmp_path / "claude-work")
    assert env["CLAUDE_CONFIG_DIR"] == applied
    assert (tmp_path / "claude-work").is_dir()


def test_apply_respects_an_explicit_env_var(cfg, monkeypatch):
    """An operator who exported the variable outranks the project mapping."""
    monkeypatch.setattr("workflow.projects.get_current_project", lambda: "work")
    env = {"CLAUDE_CONFIG_DIR": "/somewhere/chosen"}

    assert claude_accounts.apply(env) is None
    assert env["CLAUDE_CONFIG_DIR"] == "/somewhere/chosen"


def test_apply_is_a_noop_without_a_mapping(cfg, monkeypatch):
    monkeypatch.setattr("workflow.projects.get_current_project", lambda: "personal")
    env = {}

    assert claude_accounts.apply(env) is None
    assert "CLAUDE_CONFIG_DIR" not in env


def test_shell_export_line(cfg, tmp_path):
    line = claude_accounts.shell_export("work")
    assert line == f"export CLAUDE_CONFIG_DIR={tmp_path / 'claude-work'}"
    assert claude_accounts.shell_export("personal") is None


def test_set_config_dir_rejects_unknown_project(cfg):
    with pytest.raises(ValueError, match="not found"):
        claude_accounts.set_config_dir("ghost", "~/somewhere")


def test_set_config_dir_persists(cfg, tmp_path):
    claude_accounts.set_config_dir("personal", "~/.claude-personal")
    written = yaml.safe_load((tmp_path / "config.yaml").read_text())
    assert written["projects"]["personal"]["claude_config_dir"].endswith("/.claude-personal")


def test_clearing_removes_the_mapping(cfg, tmp_path):
    claude_accounts.set_config_dir("work", None)
    written = yaml.safe_load((tmp_path / "config.yaml").read_text())
    assert "claude_config_dir" not in written["projects"]["work"]


def test_is_logged_in_detects_both_storage_shapes(tmp_path):
    """Linux writes .credentials.json; on macOS the credential is in the
    Keychain, so directory state is the only honest local signal."""
    linux = tmp_path / "linux"
    linux.mkdir()
    assert not claude_accounts.is_logged_in(str(linux))
    (linux / ".credentials.json").write_text("{}")
    assert claude_accounts.is_logged_in(str(linux))

    mac = tmp_path / "mac"
    mac.mkdir()
    assert not claude_accounts.is_logged_in(str(mac))
    (mac / "settings.json").write_text("{}")
    assert claude_accounts.is_logged_in(str(mac))


def test_default_dir_is_project_scoped():
    assert claude_accounts.default_dir_for("Work").endswith("/.claude-work")
