"""Tests for `wf skill from-notes`: "Skill: …" notes written as SKILL.md files."""

from pathlib import Path

import pytest

from workflow import notes_skills as ns
from workflow.deploy.registry.notesgraph import NotesGraphError


class FakeReader:
    """A NotesReader over a dict of notes; no network."""

    def __init__(self, notes):
        self.notes = notes
        self.reads = 0

    def search(self, query):
        return [{"docId": doc_id, "title": note["title"]} for doc_id, note in self.notes.items()
                if query.casefold() in note["title"].casefold()]

    def read(self, doc_id):
        self.reads += 1
        return self.notes[doc_id]["markdown"]


DEPLOY = """\
# Deploying to prod

Deploy the notes app to production: build, push, verify. Never on a Friday.

1. `scripts/deploy-prod.sh`
2. Open https://app.notesgraph.com/ and check the version in the footer.
"""

BOARD_LOOP = "Work the board top to bottom.\n\nPull one card, finish it, report."


def notes():
    return {
        "d-deploy": {"title": "Skill: Deploy to prod", "markdown": DEPLOY},
        "d-loop": {"title": "skill: Board loop", "markdown": BOARD_LOOP},
        "d-plain": {"title": "Skills I want to learn", "markdown": "piano"},
        "d-empty": {"title": "Skill:", "markdown": "nothing"},
    }


def test_a_skill_note_is_known_by_its_title_prefix():
    assert ns.skill_name("Skill: Deploy to prod!") == "deploy-to-prod"
    assert ns.skill_name("skill:  Board / loop") == "board-loop"
    assert ns.is_skill_title("Skill: Deploy")
    assert not ns.is_skill_title("Skills I want to learn")
    assert not ns.is_skill_title("Skill:"), "a prefix with no name is no skill"


def test_only_skill_notes_are_listed_sorted_by_name():
    found = ns.list_skill_notes(FakeReader(notes()))
    assert [(s.name, s.doc_id) for s in found] == [("board-loop", "d-loop"), ("deploy-to-prod", "d-deploy")]


def test_the_description_is_the_first_prose_paragraph():
    assert ns.description_of(DEPLOY) == \
        "Deploy the notes app to production: build, push, verify. Never on a Friday."
    assert ns.description_of("# Only a heading") == ""
    long = "word " * 80
    cut = ns.description_of(long)
    assert len(cut) <= ns.MAX_DESCRIPTION_CHARS + 1 and cut.endswith("…")


def test_the_file_has_claude_codes_frontmatter_and_says_where_it_came_from():
    skill = ns.SkillNote(doc_id="d-deploy", title="Skill: Deploy to prod", name="deploy-to-prod")
    text = ns.render_skill(skill, DEPLOY)
    head, body = text.split("---\n", 2)[1:]
    assert head == ('name: deploy-to-prod\n'
                    'description: "Deploy the notes app to production: build, push, verify. '
                    'Never on a Friday."\n')
    assert body.startswith('<!-- From the NotesGraph note "Skill: Deploy to prod" (d-deploy).')
    assert body.rstrip().endswith("check the version in the footer.")


def test_a_description_without_yaml_specials_is_left_bare():
    skill = ns.SkillNote(doc_id="d", title="Skill: Board loop", name="board-loop")
    assert "description: Work the board top to bottom.\n" in ns.render_skill(skill, BOARD_LOOP)


def test_sync_writes_each_skill_and_leaves_a_matching_file_alone(tmp_path):
    reader = FakeReader(notes())
    first = ns.sync_skills(reader, tmp_path)
    assert [(r.skill.name, r.status) for r in first] == \
        [("board-loop", "created"), ("deploy-to-prod", "created")]
    assert (tmp_path / "deploy-to-prod" / "SKILL.md").read_text().endswith("footer.\n")

    again = ns.sync_skills(reader, tmp_path)
    assert [r.status for r in again] == ["unchanged", "unchanged"]

    reader.notes["d-loop"]["markdown"] = BOARD_LOOP + "\n\nThen stop."
    third = ns.sync_skills(reader, tmp_path)
    assert [(r.skill.name, r.status) for r in third] == \
        [("board-loop", "updated"), ("deploy-to-prod", "unchanged")]
    assert (tmp_path / "board-loop" / "SKILL.md").read_text().endswith("Then stop.\n")


def test_sync_can_be_limited_to_named_skills_and_a_typo_is_an_error(tmp_path):
    reader = FakeReader(notes())
    done = ns.sync_skills(reader, tmp_path, ["Deploy to prod"])
    assert [r.skill.name for r in done] == ["deploy-to-prod"]
    assert not (tmp_path / "board-loop").exists()
    with pytest.raises(LookupError, match="deploy-to-prd"):
        ns.sync_skills(reader, tmp_path, ["deploy-to-prd"])


def test_the_default_destination_is_the_repos_claude_skills(tmp_path):
    import subprocess
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    inner = tmp_path / "a" / "b"
    inner.mkdir(parents=True)
    assert ns.default_skills_dir(inner) == tmp_path / ".claude" / "skills"
    plain = tmp_path.parent / f"{tmp_path.name}-plain"
    plain.mkdir()
    assert ns.default_skills_dir(plain) == plain / ".claude" / "skills"


def test_the_command_reports_what_it_wrote(tmp_path):
    lines = []
    code = ns.run_from_notes([], tmp_path, False, reader=FakeReader(notes()), echo=lines.append)
    assert code == 0
    assert lines[0].startswith("+ board-loop") and "created" in lines[0]
    assert lines[-1] == f"2 skill(s), 2 written to {tmp_path}"

    lines.clear()
    assert ns.run_from_notes([], None, True, reader=FakeReader(notes()), echo=lines.append) == 0
    assert lines == ["board-loop               skill: Board loop  (d-loop)",
                     "deploy-to-prod           Skill: Deploy to prod  (d-deploy)"]


def test_the_command_says_when_there_is_nothing_or_notesgraph_is_down(tmp_path):
    lines = []
    assert ns.run_from_notes([], tmp_path, False, reader=FakeReader({}), echo=lines.append) == 0
    assert "Title a note that way" in lines[0]

    class Down:
        def search(self, q):
            raise NotesGraphError("NotesGraph unreachable")
    lines.clear()
    assert ns.run_from_notes([], tmp_path, False, reader=Down(), echo=lines.append) == 1
    assert lines == ["❌ NotesGraph unreachable"]
    assert not list(Path(tmp_path).iterdir())
