"""Tests for `wf skill install`, which links the repo's skills into ~/.claude/skills."""

import pytest

from workflow import skill_install


@pytest.fixture
def source(tmp_path):
    src = tmp_path / "repo" / "claude" / "skills"
    for name in ("alpha", "beta"):
        (src / name).mkdir(parents=True)
        (src / name / "SKILL.md").write_text(f"---\nname: {name}\n---\n")
    (src / "not-a-skill").mkdir()
    return src


def test_links_a_mod_folder_too(source, dest):
    # A mod (no SKILL.md, just .claude-plugin/plugin.json) is loaded from ~/.claude/skills as well.
    (source / "gamma" / ".claude-plugin").mkdir(parents=True)
    (source / "gamma" / ".claude-plugin" / "plugin.json").write_text('{"name": "gamma"}')
    assert skill_install.install(source, dest).linked == ["alpha", "beta", "gamma"]


@pytest.fixture
def dest(tmp_path):
    return tmp_path / "home" / ".claude" / "skills"


def test_links_every_skill(source, dest):
    result = skill_install.install(source, dest)
    assert result.linked == ["alpha", "beta"]
    assert (dest / "alpha").resolve() == (source / "alpha").resolve()
    assert not (dest / "not-a-skill").exists()


def test_reinstall_is_a_no_op(source, dest):
    skill_install.install(source, dest)
    result = skill_install.install(source, dest)
    assert result.linked == [] and result.current == ["alpha", "beta"]


def test_repoints_a_stale_link(source, dest, tmp_path):
    dest.mkdir(parents=True)
    (dest / "alpha").symlink_to(tmp_path)
    assert "alpha" in skill_install.install(source, dest).linked
    assert (dest / "alpha").resolve() == (source / "alpha").resolve()


def test_leaves_someone_elses_skill_unless_forced(source, dest):
    (dest / "alpha").mkdir(parents=True)
    (dest / "alpha" / "SKILL.md").write_text("theirs")
    assert skill_install.install(source, dest).skipped == ["alpha"]
    assert (dest / "alpha" / "SKILL.md").read_text() == "theirs"

    assert "alpha" in skill_install.install(source, dest, force=True).linked
    assert (dest / "alpha").is_symlink()


def test_installs_only_named_skills(source, dest):
    assert skill_install.install(source, dest, names=["beta"]).linked == ["beta"]
    assert not (dest / "alpha").exists()


def test_skill_dirs_cover_every_projects_claude_profile(tmp_path, monkeypatch):
    # A project with its own Claude account reads skills from <config dir>/skills, not ~/.claude/skills.
    monkeypatch.setattr(skill_install, "USER_SKILLS", tmp_path / ".claude" / "skills")
    cfg = {"projects": {"personal": {"claude_config_dir": str(tmp_path / ".claude-personal")},
                        "work": {"claude_config_dir": str(tmp_path / ".claude-work")},
                        "plain": {},
                        "again": {"claude_config_dir": str(tmp_path / ".claude-personal")}}}
    assert skill_install.skill_dirs(cfg) == [tmp_path / ".claude" / "skills",
                                             tmp_path / ".claude-personal" / "skills",
                                             tmp_path / ".claude-work" / "skills"]


def test_install_everywhere_links_into_each_profile(source, tmp_path, monkeypatch):
    monkeypatch.setattr(skill_install, "USER_SKILLS", tmp_path / ".claude" / "skills")
    cfg = {"projects": {"personal": {"claude_config_dir": str(tmp_path / ".claude-personal")}}}
    results = skill_install.install_everywhere(source, cfg=cfg)
    assert [(dest, r.linked) for dest, r in results] == [
        (tmp_path / ".claude" / "skills", ["alpha", "beta"]),
        (tmp_path / ".claude-personal" / "skills", ["alpha", "beta"]),
    ]
    assert (tmp_path / ".claude-personal" / "skills" / "alpha").resolve() == (source / "alpha").resolve()
