"""Propose skills from procedures the ladder sees repeated across sessions.

A skill here has the shape TencentDB Agent Memory extracts: when to use it,
when not to, the steps, and how to check it worked. The ladder only proposes;
a draft does nothing until the user promotes it, at which point it joins the
`ai_skills` config that `wf ai` already ships to Claude as a plugin.

Drafts live in ~/.wf/skills/drafts/<name>.md and, when NotesGraph publishing
is on, are mirrored as notes filed under `<project> / ai / skills` so they
can be read and edited there before promotion.

Usage comes from the captured transcripts themselves: every `Skill` tool call
the proxy recorded names the skill that was loaded.
"""

import json
import re
import time
from collections import Counter
from pathlib import Path
from typing import Optional

DRAFTS_DIR = Path.home() / ".wf" / "skills" / "drafts"

PROPOSE_PROMPT = """\
Below are summaries of agent coding sessions. Find procedures that were
carried out in at least two different sessions and would be worth turning
into a reusable skill: a deploy sequence, a release checklist, a debugging
routine for a recurring failure, a data migration pattern.

Prefer omission to noise. A one-off fix is not a skill; neither is generic
advice like "run the tests". Return at most {limit}.

{known_block}Return ONLY a JSON array, no prose:
[{{"name": "kebab-case-name",
   "description": "one line: what it does and when to reach for it",
   "use_when": "the situations that call for it",
   "not_when": "situations that look similar but should not use it",
   "steps": ["...", "..."],
   "verify": "how to tell it worked",
   "evidence": "which sessions showed it, in a few words"}}]

SESSION SUMMARIES:
{body}
"""


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")[:60]


def render_skill(skill: dict, status: str = "draft", version: int = 1) -> str:
    """A draft as Markdown with frontmatter, the same shape a SKILL.md uses."""
    steps = "\n".join(f"{i}. {s}" for i, s in enumerate(skill.get("steps") or [], 1))
    return f"""---
name: {skill['name']}
description: {skill.get('description', '').strip()}
status: {status}
version: {version}
proposed: {time.strftime('%Y-%m-%d')}
---

## When to use
{skill.get('use_when', '').strip()}

## When not to use
{skill.get('not_when', '').strip()}

## Steps
{steps}

## Verify
{skill.get('verify', '').strip()}

## Evidence
{skill.get('evidence', '').strip()}
"""


def parse_frontmatter(text: str) -> tuple:
    """Split a draft into (fields, body)."""
    m = re.match(r"^---\n(.*?)\n---\n?(.*)$", text, re.DOTALL)
    if not m:
        return {}, text
    fields = {}
    for line in m.group(1).splitlines():
        key, _, value = line.partition(":")
        if key.strip():
            fields[key.strip()] = value.strip()
    return fields, m.group(2)


def list_drafts(directory: Path = DRAFTS_DIR) -> list:
    out = []
    for path in sorted(directory.glob("*.md")):
        fields, _ = parse_frontmatter(path.read_text())
        out.append({"name": fields.get("name", path.stem), "path": path, **fields})
    return out


def propose_skills(store, config: Optional[dict] = None, task_key: Optional[str] = None,
                   max_sessions: int = 40, limit: int = 5,
                   directory: Path = DRAFTS_DIR) -> list:
    """Ask the provider for repeated procedures; write each new one as a draft.

    Returns the drafts written. Names that already have a draft or a
    promoted skill are passed to the model as known and skipped.
    """
    from workflow.ladder import _run_llm, _parse_json_array

    if config is None:
        from workflow.config import load_effective_config
        config = load_effective_config()

    sessions = store.get_summaries(task_key=task_key, level="session")[:max_sessions]
    if len(sessions) < 2:
        return []

    known = {d["name"] for d in list_drafts(directory)} | set(_promoted_names(config))
    known_block = ""
    if known:
        known_block = ("Skills that already exist -- do not propose these again:\n"
                       + "\n".join(f"- {n}" for n in sorted(known)) + "\n\n")

    body = "\n\n".join(
        f"[{s.task_key or 'no task'} · {time.strftime('%Y-%m-%d', time.localtime(s.ts))}]\n{s.content}"
        for s in sessions
    )
    raw = _run_llm(PROPOSE_PROMPT.format(limit=limit, known_block=known_block, body=body), config)
    if not raw:
        return []

    written = []
    directory.mkdir(parents=True, exist_ok=True)
    for item in _parse_json_array(raw)[:limit]:
        if not isinstance(item, dict):
            continue
        name = _slug(item.get("name", ""))
        if not name or name in known or not item.get("steps"):
            continue
        skill = {**item, "name": name}
        path = directory / f"{name}.md"
        path.write_text(render_skill(skill))
        known.add(name)
        written.append({"name": name, "path": path, "markdown": path.read_text()})
    return written


def _promoted_names(config: dict) -> list:
    skills = config.get("ai_skills") or {}
    names = list((skills.get("global") or {}).keys())
    for scoped in ("projects", "repos", "tasks"):
        for entries in (skills.get(scoped) or {}).values():
            names += list((entries or {}).keys())
    return names


def promote(name: str, project: Optional[str] = None,
            directory: Path = DRAFTS_DIR) -> Path:
    """Move a draft into `ai_skills`, scoped to a project or global.

    The draft file stays, marked active and version-bumped, so a later
    proposal run knows the skill exists and the history is visible.
    """
    from workflow.config import load_config, save_config

    path = directory / f"{_slug(name)}.md"
    if not path.exists():
        raise FileNotFoundError(f"No draft named '{name}' in {directory}")

    fields, body = parse_frontmatter(path.read_text())
    content = f"{fields.get('description', '')}\n{body}".strip()

    cfg = load_config()
    skills = cfg.setdefault("ai_skills", {})
    if project:
        skills.setdefault("projects", {}).setdefault(project, {})[fields.get("name", name)] = content
    else:
        skills.setdefault("global", {})[fields.get("name", name)] = content
    save_config(cfg)

    fields["status"] = "active"
    fields["version"] = str(int(fields.get("version") or 0) + 1)
    fields["promoted"] = time.strftime("%Y-%m-%d")
    front = "\n".join(f"{k}: {v}" for k, v in fields.items())
    path.write_text(f"---\n{front}\n---\n{body}")
    return path


def export_skill_md(name: str, dest_root: Path, directory: Path = DRAFTS_DIR) -> Path:
    """Write a draft as <dest_root>/<name>/SKILL.md, e.g. into .claude/skills."""
    path = directory / f"{_slug(name)}.md"
    fields, body = parse_frontmatter(path.read_text())
    out = Path(dest_root).expanduser() / _slug(name) / "SKILL.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(f"---\nname: {fields.get('name', name)}\n"
                   f"description: {fields.get('description', '')}\n---\n{body}")
    return out


def skill_usage(store) -> Counter:
    """How often each skill was loaded, counted from captured Skill tool calls."""
    from workflow.conversation_store.sqlite import SqliteStore

    target = getattr(store, "primary", store)
    if not isinstance(target, SqliteStore):
        return Counter()
    counts = Counter()
    rows = target._db.execute(
        "SELECT DISTINCT session_id, content FROM messages "
        "WHERE role = 'tool_call' AND tool_name = 'Skill'"
    )
    for row in rows:
        try:
            name = (json.loads(row["content"]) or {}).get("skill")
        except (ValueError, AttributeError):
            continue
        if name:
            # Plugin skills arrive as "plugin:name"; count by the bare name.
            counts[name.split(":")[-1]] += 1
    return counts


def publish_draft(draft: dict, project: Optional[str], config: dict) -> Optional[str]:
    """Mirror a draft into NotesGraph under `<project> / ai / skills`. Never raises."""
    try:
        from workflow.conversation_store import notesgraph as ng
        if not ng.is_enabled(config):
            return None
        # Frontmatter would render as a stray heading; show it as a list.
        fields, body = parse_frontmatter(draft["markdown"])
        header = "\n".join(f"* **{k}:** {v}" for k, v in fields.items())
        client = ng.client_from_config(config)
        folder = [project or "unscoped", "ai", "skills"]
        doc_id = client.create_doc(
            f"Skill draft: {draft['name']}", f"{header}\n{body}", folder=folder,
        )
        # Linked so the note tree nests it instead of leaving it in the Inbox.
        try:
            ledger = ng._load_ledger()
            client.link_doc(ng.ensure_path(client, folder, ledger), doc_id)
        except ng.NotesGraphError as e:
            print(f"⚠️  Published the skill draft but could not link it: {e}")
        return doc_id
    except Exception as e:
        print(f"⚠️  Could not publish skill draft to NotesGraph: {e}")
        return None
