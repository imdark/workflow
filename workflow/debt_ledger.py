"""`wf debt`: a repo's deliberate shortcuts as a NotesGraph task list.

Ponytail's /ponytail-debt collects `shortcut: <limit>, <when to upgrade>`
comments so "later" doesn't turn into "never". This does the same for
`shortcut:`, TODO, FIXME and HACK markers, but into a note that lasts: one
task per marker, so they show on the board. A marker that names no
condition for coming back to it is tagged #no-trigger.

The note is rewritten from the code each run, so a marker removed from the
code drops off. A task's status (`[-]`, `[X]`, a keyword) is kept while its
marker is still there.
"""

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

MARKER = re.compile(r"(?:#|//|/\*|--|<!--|\*)\s*(shortcut:|(?:TODO|FIXME|HACK)\b)(?:\([^)]*\))?:?\s*(.*)")
# Case matters: "todo" in prose or a "Todo" state isn't a marker.
# Words that say when to come back: "when there are >1k rows", "once v2 ships".
TRIGGER = re.compile(r"\b(when|once|until|after|before|if|unless)\b|[<>]=?\s*\d", re.I)
TASK_LINE = re.compile(r"^- (\[[ xX-]\]|[A-Z][A-Z-]+) (.*)$")
MAX_TEXT = 160
SKIP_SUFFIXES = {".md", ".lock", ".min.js", ".map", ".svg", ".json"}


@dataclass
class Marker:
    path: str
    line: int
    kind: str
    text: str

    @property
    def key(self) -> str:
        # Not the line number: it moves with every edit above the marker.
        return f"{self.path} {self.text}"

    @property
    def has_trigger(self) -> bool:
        return bool(TRIGGER.search(self.text))


def find_markers(root: Path) -> list:
    """Markers in the repo's tracked files, via `git grep`; [] outside a repo."""
    out = subprocess.run(
        ["git", "-C", str(root), "grep", "-n", "-I", "-E", "shortcut:|TODO|FIXME|HACK"],
        capture_output=True, text=True)
    markers = []
    for row in out.stdout.splitlines():
        path, _, rest = row.partition(":")
        number, _, source = rest.partition(":")
        if Path(path).suffix in SKIP_SUFFIXES or not number.isdigit():
            continue
        match = MARKER.search(source)
        if not match:
            continue
        text = match.group(2).strip().rstrip("*/->").strip()
        if len(text) > MAX_TEXT:
            text = text[:MAX_TEXT - 1] + "…"
        markers.append(Marker(path, int(number), match.group(1).rstrip(":").lower(), text))
    return markers


def _statuses(markdown: str) -> dict:
    """{marker key: status annotation} from the note as it is now."""
    found = {}
    for line in markdown.splitlines():
        match = TASK_LINE.match(line.strip())
        if not match:
            continue
        key = re.match(r"`([^`]+):\d+` (.*?)(?: #.*)?$", match.group(2))
        if key:
            found[f"{key.group(1)} {key.group(2)}"] = match.group(1)
    return found


def render_ledger(markers: list, tags: str, previous: str = "") -> str:
    statuses = _statuses(previous)
    lines = [
        "Deliberate shortcuts and TODOs in the code, one task per marker. "
        "Written by `wf debt` from the code; run it again to refresh. A marker "
        "removed from the code drops off; a status set here stays while the "
        "marker is there. #no-trigger means it names no condition for coming back to it.",
        "",
    ]
    for m in markers:
        status = statuses.get(m.key, "[ ]")
        extra = "" if m.has_trigger else " #no-trigger"
        lines.append(f"- {status} `{m.path}:{m.line}` {m.text or m.kind} {tags}{extra}")
    if not markers:
        lines.append("No markers found.")
    return "\n".join(lines) + "\n"


def repo_name(root: Path) -> str:
    """The repo's own name, the same from any worktree of it."""
    common = subprocess.run(["git", "-C", str(root), "rev-parse", "--path-format=absolute",
                             "--git-common-dir"], capture_output=True, text=True)
    path = Path(common.stdout.strip()) if common.returncode == 0 else root / ".git"
    return (path.parent if path.name == ".git" else path).name.removesuffix(".git")


def ledger_title(name: str) -> str:
    return f"{name} shortcut ledger"


def run_debt(root: Path, tag: str, under: Optional[str], dry_run: bool,
             reader=None, echo=print) -> int:
    """Find the markers and write the ledger note; returns the exit code."""
    from workflow.deploy.registry.notesgraph import NotesGraphError

    top = subprocess.run(["git", "-C", str(root), "rev-parse", "--show-toplevel"],
                         capture_output=True, text=True)
    if top.returncode != 0:
        echo(f"❌ {root} is not in a git repo")
        return 1
    root = Path(top.stdout.strip())
    markers = find_markers(root)
    name = repo_name(root)
    project = re.sub(r"[^a-z0-9-]+", "-", name.lower()).strip("-")
    tags = f"#{tag} #project:{project}"
    title = ledger_title(name)
    no_trigger = sum(not m.has_trigger for m in markers)
    if dry_run:
        echo(render_ledger(markers, tags))
        echo(f"{len(markers)} marker(s), {no_trigger} with no trigger; nothing written.")
        return 0
    try:
        if reader is None:
            from workflow.config import load_effective_config
            from workflow.deploy.notes import reader_from_config
            reader = reader_from_config(load_effective_config())
        note = reader.find(title)
        if note:
            content = render_ledger(markers, tags, reader.read(note["docId"]))
            reader.call("update_document", {"docId": note["docId"], "content": content, "agent": "wf debt"})
        else:
            reader.call("create_document", {"title": title, "content": render_ledger(markers, tags),
                                            "agent": "wf debt"})
            note = reader.find(title)
            if under and note:
                parent = reader.find(under)
                if not parent:
                    echo(f"⚠️  no note titled \"{under}\" to file the ledger under")
                else:
                    reader.call("link_document", {"parentDocId": parent["docId"],
                                                  "childDocId": note["docId"]})
    except NotesGraphError as e:
        echo(f"❌ {e}")
        return 1
    echo(f"{len(markers)} marker(s), {no_trigger} with no trigger → \"{title}\"")
    return 0
