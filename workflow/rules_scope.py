"""Scoped sections of the "Agent rules" note.

The note is one rulebook for every run, but some rules only matter for some
runs ("adb is not on PATH" only for mobile work). Such rules sit under a
scope heading in the same note::

    ## For project: notes-graph
    ## For tag: mobile, #android
    ## For agent: Researcher

A run gets the unscoped text plus the scoped sections that match it. The
same rules are implemented in NotesGraph (TypeScript) for its cloud runs;
both follow one spec and pass the same test cases, so keep them in step.

* A heading is ``^(#{1,6})\\s+(.*?)\\s*#*\\s*$`` outside a fenced code block.
* A scope heading's text is ``for project(s)|tag(s)|agent(s): a, b``, any
  case. Its section runs to the next heading of the same or a higher rank;
  deeper headings stay inside it, and a scope nested in another applies only
  when the enclosing ones do too.
* A section matches when any of its values equals, normalized, any of the
  run's names of that kind; a run with none of that kind matches nothing.
"""

import re
from typing import Iterable, Optional

HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
SCOPE_RE = re.compile(r"^for\s+(projects?|tags?|agents?)\s*:\s*(.+)$", re.I)
FENCE_RE = re.compile(r"^\s*(```|~~~)")
KINDS = ("project", "tag", "agent")

# Added to the words around the rules when a scoped section was kept.
SCOPED_SENTENCE = ('Sections headed "For project/tag/agent: …" were included because they '
                   "match this run; sections for other projects, tags or agents were left out.")


def normalize(name: str) -> str:
    """Lowercase, trimmed, no leading `#`, runs of whitespace, `-` and `_`
    as one `-`: "Notes Graph", "notes-graph" and "notes_graph" are equal."""
    name = str(name or "").lower().strip()
    if name.startswith("#"):
        name = name[1:]
    return re.sub(r"[\s\-_]+", "-", name)


def parse_scope(heading_text: str) -> Optional[tuple]:
    """(kind, values) for a scope heading's text, else None."""
    found = SCOPE_RE.match(heading_text)
    if not found:
        return None
    kind = found.group(1).lower()
    kind = kind[:-1] if kind.endswith("s") else kind
    values = []
    for value in found.group(2).split(","):
        value = value.strip()
        if value.startswith("#"):
            value = value[1:]
        if value:
            values.append(value)
    return kind, values


def _heading(line: str) -> Optional[tuple]:
    found = HEADING_RE.match(line)
    return (len(found.group(1)), found.group(2)) if found else None


def _names(values: Iterable) -> set:
    return {n for n in (normalize(v) for v in (values or ()) if v) if n}


def filter_rules(markdown: str, projects: Iterable = (), tags: Iterable = (),
                 agents: Iterable = ()) -> tuple:
    """(the note as this run should see it, whether a scoped section was kept).

    Unscoped text and matching scoped sections, heading lines included, in
    their order; other scoped sections are dropped whole. 3+ blank lines left
    behind become 2. An empty result is "" (no rules).
    """
    context = {"project": _names(projects), "tag": _names(tags), "agent": _names(agents)}
    stack = []  # (level, matches) of the scoped sections the line is in
    kept, kept_scoped = [], False
    fence = None
    for line in (markdown or "").split("\n"):
        if fence:
            if line.lstrip().startswith(fence):
                fence = None
        else:
            opened = FENCE_RE.match(line)
            heading = None if opened else _heading(line)
            if opened:
                fence = opened.group(1)
            elif heading:
                level, text = heading
                while stack and stack[-1][0] >= level:
                    stack.pop()
                scope = parse_scope(text)
                if scope:
                    kind, values = scope
                    stack.append((level, bool(_names(values) & context[kind])))
                    if all(m for _, m in stack):
                        kept_scoped = True
        if all(m for _, m in stack):
            kept.append(line)

    out, blanks = [], 0
    for line in kept:
        blanks = blanks + 1 if not line.strip() else 0
        if blanks <= 2:
            out.append(line)
    while out and not out[0].strip():
        out.pop(0)
    while out and not out[-1].strip():
        out.pop()
    return "\n".join(out), kept_scoped


def _headings(lines: list) -> list:
    """(index, level, text) of each heading line, fenced code left out."""
    out, fence = [], None
    for i, line in enumerate(lines):
        if fence:
            if line.lstrip().startswith(fence):
                fence = None
            continue
        opened = FENCE_RE.match(line)
        if opened:
            fence = opened.group(1)
            continue
        heading = _heading(line)
        if heading:
            out.append((i, *heading))
    return out


def scope_sections(markdown: str) -> list:
    """The note's scope headings, for writing into one (see learn.py).

    Each is {"line", "level", "kind", "values", "end", "body_end", "nested"}
    over the note's lines: `end` is where the section stops, `body_end` where
    its own text stops (its first deeper heading, else `end`), `nested`
    whether it sits inside another scoped section.
    """
    lines = (markdown or "").split("\n")
    headings = _headings(lines)
    sections, stack = [], []
    for n, (i, level, text) in enumerate(headings):
        while stack and stack[-1] >= level:
            stack.pop()
        scope = parse_scope(text)
        if not scope:
            continue
        later = headings[n + 1:]
        end = next((j for j, lv, _ in later if lv <= level), len(lines))
        body_end = later[0][0] if later and later[0][0] < end else end
        sections.append({"line": i, "level": level, "kind": scope[0], "values": scope[1],
                         "end": end, "body_end": body_end, "nested": bool(stack)})
        stack.append(level)
    return sections


def has_kept_scope(filtered: str) -> bool:
    """Whether filtered rules hold a scope heading, i.e. a scoped section was
    kept (every scope heading left after filtering is one that matched)."""
    return any(parse_scope(text) for _, _, text in _headings((filtered or "").split("\n")))
