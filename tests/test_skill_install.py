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
