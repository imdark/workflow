"""Put a task wf created into the day's journal note in NotesGraph.

A daily note (titled with the date, 2026-11-15) has a heading per project
("## workflow", "## notes graph") with that day's tasks listed under it. A
task an agent files with `wf task create` (or the wf-tools `create_task`
tool) from a `wf ai` session lands in Linear, Jira or the markdown store,
where the user doesn't see it while reading their day; this adds it to the
journal too, as `- [ ] PERSONAL-290 Title` under the project's heading.

The note is changed by reading its markdown, inserting the one line and
writing it back: NotesGraph's update_document diffs the blocks, so every
other block in the note stays as it was.
"""

import os
import re
import subprocess
from datetime import date
from pathlib import Path
from typing import Optional

AGENT = "wf task create"

_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
# The unchecked, empty item a daily note's template leaves under each heading.
_EMPTY_ITEM = re.compile(r"^\s*[-*]\s+\[ \]\s*$")


def _name_key(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


def in_wf_session() -> bool:
    """True inside a `wf ai` / `claude` session, which sets WF_PROJECT."""
    from workflow.projects import PROJECT_ENV

    return bool(os.environ.get(PROJECT_ENV))


def task_line(key: str, title: str, url: Optional[str] = None) -> str:
    title = " ".join((title or "").split())
    label = f"[{key}]({url})" if url else key
    return f"- [ ] {label} {title}".rstrip()


def insert_task(markdown: Optional[str], heading: str, line: str) -> str:
    """`markdown` with `line` added under the heading named like `heading`.

    The heading is matched ignoring case, spaces and dashes ("notes-graph"
    finds "## notes graph"). The line takes the place of the section's empty
    `- [ ]` item when it has one, else goes after the section's last text,
    before its first subheading ("### todos:"). A missing heading is added
    before "## Inbox", or at the end.
    """
    lines = (markdown or "").rstrip("\n").split("\n") if (markdown or "").strip() else []
    key = _name_key(heading)
    start = next((i for i, text in enumerate(lines)
                  if (m := _HEADING.match(text)) and _name_key(m.group(2)) == key), None)
    if start is None:
        block = [f"## {heading}", "", line]
        inbox = next((i for i, text in enumerate(lines)
                      if (m := _HEADING.match(text)) and _name_key(m.group(2)) == "inbox"), None)
        if inbox is None:
            return "\n".join(lines + ([""] if lines else []) + block) + "\n"
        return "\n".join(lines[:inbox] + block + [""] + lines[inbox:]) + "\n"

    end = next((i for i in range(start + 1, len(lines)) if _HEADING.match(lines[i])), len(lines))
    empty = next((i for i in range(start + 1, end) if _EMPTY_ITEM.match(lines[i])), None)
    if empty is not None:
        lines[empty] = line
        return "\n".join(lines) + "\n"
    last = max((i for i in range(start, end) if lines[i].strip()), default=start)
    insert = [line] if last > start else ["", line]
    if end < len(lines) and last + 1 == end:
        insert.append("")
    return "\n".join(lines[:last + 1] + insert + lines[last + 1:]) + "\n"


def _session_repo(repos: dict) -> Optional[str]:
    """The configured repo the current folder is in; a worktree counts as its main checkout."""
    candidates = []
    try:
        common = subprocess.run(["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
                                capture_output=True, text=True, timeout=5)
        if common.returncode == 0 and common.stdout.strip():
            candidates.append(str(Path(common.stdout.strip()).parent))
    except (OSError, subprocess.SubprocessError):
        pass
    candidates.append(os.getcwd())
    for folder in candidates:
        folder = os.path.realpath(folder)
        inside = [path for path in repos
                  if folder == os.path.realpath(path)
                  or folder.startswith(os.path.realpath(path).rstrip(os.sep) + os.sep)]
        if inside:
            return max(inside, key=len)
    return None


def journal_heading(project: Optional[str], repo: Optional[str] = None) -> str:
    """The journal heading for a task in `project`: the NotesGraph project of
    `repo` when given (a task filed for another repo), else of the repo the
    session is in (`notesgraph_project:` in its config, else the repo's
    folder name), else of the project's default repo, else the project's
    name."""
    from workflow.config import get_repositories
    from workflow.projects import get_project

    conf = get_project(project) if project else None
    repos = dict((conf or {}).get("repositories") or {}) if project else dict(get_repositories() or {})
    repo = repo or _session_repo(repos) or (conf or {}).get("default_repo") or next(iter(repos), None)
    if repo:
        repo_conf = repos.get(repo) if isinstance(repos.get(repo), dict) else {}
        return str(repo_conf.get("notesgraph_project") or "") or Path(repo).name
    return project or "Inbox"


def record_task(reader, heading: str, line: str, day: Optional[date] = None) -> dict:
    """Add `line` under `heading` in the journal note for `day` (today), made if missing.

    Returns {"docId", "title", "created"}.
    """
    from workflow.deploy.notes import upsert_note

    title = (day or date.today()).isoformat()
    result = upsert_note(reader, title, lambda current: insert_task(current, heading, line), AGENT)
    return {"docId": result.get("docId"), "title": title, "created": result.get("created")}


def record_created_task(project: Optional[str], key: str, title: str, url: Optional[str] = None,
                        reader=None, repo: Optional[str] = None, repo_tag: Optional[str] = None) -> dict:
    """record_task for a task wf just created, with the heading from journal_heading.

    `repo` (a path) files it under that repo's heading; `repo_tag` ends the
    line with `#repo:<name>`, which starting the task reads to go to that repo.
    """
    if reader is None:
        from workflow.cli_notes import reader_from_env

        reader = reader_from_env()
    heading = journal_heading(project, repo)
    line = task_line(key, title, url)
    if repo_tag:
        line = f"{line} #repo:{repo_tag}"
    result = record_task(reader, heading, line)
    return {**result, "heading": heading}
