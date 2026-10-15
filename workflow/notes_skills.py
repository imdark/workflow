"""Claude Code skills written as notes.

A runbook is easier to keep in NotesGraph than in a repo: it is read and
edited where the rest of the knowledge lives, and agents find it by search.
But Claude Code loads skills from SKILL.md files. This bridges the two:

    wf skill from-notes

finds every note titled "Skill: <name>" and writes it as
`.claude/skills/<name>/SKILL.md` in the repo, with the frontmatter Claude
Code wants. Run it again after editing a note and the file follows; a file
that already matches is left alone. The note stays the source; the file
says so in a comment, so nobody edits the copy.
"""

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

from workflow.deploy.notes import NotesReader

SKILL_TITLE_PREFIX = "Skill:"
# Claude Code reads a project's skills from here.
DEFAULT_SKILLS_DIR = Path(".claude") / "skills"
# Shown in Claude Code's skill list; longer is cut at a word.
MAX_DESCRIPTION_CHARS = 200


@dataclass
class SkillNote:
    doc_id: str
    title: str          # the note's full title, "Skill: Deploy steps"
    name: str           # the skill's name, "deploy-steps"


@dataclass
class SyncResult:
    skill: SkillNote
    path: Path
    status: str         # created | updated | unchanged


def skill_name(title: str) -> str:
    """"Skill: Deploy to prod!" -> "deploy-to-prod": what Claude Code allows
    as a skill name, and the directory it lives in."""
    rest = title.strip()
    if rest[:len(SKILL_TITLE_PREFIX)].casefold() == SKILL_TITLE_PREFIX.casefold():
        rest = rest[len(SKILL_TITLE_PREFIX):]
    slug = re.sub(r"[^a-z0-9]+", "-", rest.casefold()).strip("-")
    return slug


def is_skill_title(title: str) -> bool:
    return (title or "").strip()[:len(SKILL_TITLE_PREFIX)].casefold() == SKILL_TITLE_PREFIX.casefold() \
        and bool(skill_name(title))


def list_skill_notes(reader: NotesReader) -> list:
    """Every "Skill: …" note in the workspace, by title."""
    found, seen = [], set()
    for hit in reader.search(SKILL_TITLE_PREFIX):
        title = hit.get("title") or ""
        if not is_skill_title(title) or hit["docId"] in seen:
            continue
        seen.add(hit["docId"])
        found.append(SkillNote(doc_id=hit["docId"], title=title.strip(), name=skill_name(title)))
    return sorted(found, key=lambda s: s.name)


def description_of(markdown: str) -> str:
    """The first paragraph of prose: what Claude Code shows for the skill."""
    for block in re.split(r"\n\s*\n", markdown.strip()):
        line = " ".join(block.split())
        if not line or line.startswith(("#", "```", "---", "|", "<!--")):
            continue
        line = re.sub(r"^[-*>]\s+", "", line)
        if len(line) <= MAX_DESCRIPTION_CHARS:
            return line
        cut = line[:MAX_DESCRIPTION_CHARS].rsplit(" ", 1)[0]
        return cut.rstrip(" ,;:") + "…"
    return ""


def _yaml_text(value: str) -> str:
    """A string as a YAML scalar, quoted when it would otherwise be read
    as something else (a colon, a leading quote or dash)."""
    if re.search(r"[:#\"'\[\]{}&*!|>%@`]|^[-?]|^\s|\s$", value) or not value:
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return value


def render_skill(skill: SkillNote, markdown: str) -> str:
    """The SKILL.md for a note: frontmatter, a where-it-came-from note, the text."""
    body = markdown.strip()
    description = description_of(body) or skill.title
    return (
        "---\n"
        f"name: {skill.name}\n"
        f"description: {_yaml_text(description)}\n"
        "---\n"
        f"<!-- From the NotesGraph note \"{skill.title}\" ({skill.doc_id}). "
        "Edit the note, then run `wf skill from-notes`; edits here are overwritten. -->\n\n"
        f"{body}\n"
    )


def sync_skills(reader: NotesReader, dest: Path, names: Optional[Iterable[str]] = None) -> list:
    """Write each skill note under `dest/<name>/SKILL.md`; returns what happened.

    `names` limits it to those skills; a name with no note raises so a typo
    isn't a silent no-op.
    """
    notes = list_skill_notes(reader)
    if names is not None:
        wanted = {skill_name(n) for n in names}
        by_name = {s.name: s for s in notes}
        missing = sorted(wanted - by_name.keys())
        if missing:
            raise LookupError(
                f"no note titled \"{SKILL_TITLE_PREFIX} …\" for: {', '.join(missing)}")
        notes = [by_name[n] for n in sorted(wanted)]

    results = []
    for skill in notes:
        rendered = render_skill(skill, reader.read(skill.doc_id))
        path = dest / skill.name / "SKILL.md"
        if path.exists():
            status = "unchanged" if path.read_text(encoding="utf-8") == rendered else "updated"
        else:
            status = "created"
        if status != "unchanged":
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(rendered, encoding="utf-8")
        results.append(SyncResult(skill=skill, path=path, status=status))
    return results


def run_from_notes(names: Iterable[str], dest: Optional[Path], list_only: bool,
                   reader: Optional[NotesReader] = None, echo=print) -> int:
    """`wf skill from-notes`: sync (or with `list_only`, show) the skill notes.

    Returns the exit code. `reader` defaults to the configured workspace.
    """
    from workflow.deploy.registry.notesgraph import NotesGraphError

    try:
        if reader is None:
            from workflow.config import load_effective_config
            from workflow.deploy.notes import reader_from_config
            reader = reader_from_config(load_effective_config())
        if list_only:
            notes = list_skill_notes(reader)
            if not notes:
                echo(f'No notes titled "{SKILL_TITLE_PREFIX} <name>" in the workspace.')
                return 0
            for skill in notes:
                echo(f"{skill.name:24} {skill.title}  ({skill.doc_id})")
            return 0
        dest = Path(dest) if dest else default_skills_dir()
        results = sync_skills(reader, dest, list(names) or None)
    except (NotesGraphError, LookupError) as e:
        echo(f"❌ {e}")
        return 1
    if not results:
        echo(f'No notes titled "{SKILL_TITLE_PREFIX} <name>" in the workspace. '
             "Title a note that way and run this again.")
        return 0
    marks = {"created": "+", "updated": "~", "unchanged": "="}
    for result in results:
        echo(f"{marks[result.status]} {result.skill.name:24} {result.status:9} {result.path}")
    changed = sum(r.status != "unchanged" for r in results)
    echo(f"{len(results)} skill(s), {changed} written to {dest}")
    return 0


def default_skills_dir(cwd: Optional[Path] = None) -> Path:
    """`.claude/skills` at the root of the repo `cwd` is in, else under `cwd`."""
    import subprocess

    cwd = Path(cwd or Path.cwd())
    top = subprocess.run(["git", "-C", str(cwd), "rev-parse", "--show-toplevel"],
                         capture_output=True, text=True)
    root = Path(top.stdout.strip()) if top.returncode == 0 and top.stdout.strip() else cwd
    return root / DEFAULT_SKILLS_DIR
