"""Feeding the summarization ladder back to agents.

The ladder (see `workflow.ladder`) turns captured conversations into session
summaries, facts and a per-task rollup. This module is the read side, and it
splits memory by size the way TencentDB Agent Memory does:

  injected  the rollup and the task's facts: small and stable, rendered once
            into the task prompt so the prompt prefix (and the upstream KV
            cache) stays the same for the whole session
  tools     raw transcripts and the full fact set: large and query-specific,
            searched on demand through the wf-tools MCP server

The injected block is wrapped in <wf-memory>, which `proxy.sanitize` strips
before capture, so the ladder never re-learns its own output.

Each fact read (injected or returned by a search) is counted in a small JSON
file. The counts rank recall, and `wf mem usage` lists facts nothing reads.

Everything here degrades to "no memory" rather than raising: a missing or
broken store must never stop an agent from starting.
"""

import hashlib
import json
import re
import time
from pathlib import Path
from typing import Optional

USAGE_FILE = Path.home() / ".wf" / "memory" / "usage.json"

# Budgets for the injected block. The rollup is already a 10-25 line digest;
# the caps only stop a runaway model output from crowding out the task.
MAX_ROLLUP_CHARS = 4000
MAX_FACTS = 25
MAX_FACTS_CHARS = 4000
MAX_FALLBACK_CHARS = 3000
MAX_PROJECT_CHARS = 1200

# Only these fact types are worth carrying across tasks in a project.
PROJECT_FACT_TYPES = ("decision", "constraint", "preference")


def _store():
    from workflow.conversation_store import get_store
    return get_store()


# ── usage counts ─────────────────────────────────────────────────────────────

def fact_id(content: str) -> str:
    """Stable id for a fact, insensitive to case and punctuation."""
    from workflow.ladder import _normalize
    return hashlib.sha256(_normalize(content).encode("utf-8")).hexdigest()[:12]


def load_usage() -> dict:
    try:
        return json.loads(USAGE_FILE.read_text())
    except (OSError, ValueError):
        return {}


def record_usage(kind: str, keys) -> None:
    """Count one read of each key. Never raises: counts are only a ranking hint."""
    keys = [k for k in keys if k]
    if not keys:
        return
    try:
        usage = load_usage()
        bucket = usage.setdefault(kind, {})
        now = time.time()
        for key in keys:
            entry = bucket.setdefault(key, {"count": 0, "last": 0})
            entry["count"] += 1
            entry["last"] = now
        USAGE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = USAGE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(usage, indent=1))
        tmp.replace(USAGE_FILE)
    except OSError:
        pass


def _rank(facts: list) -> list:
    """Most-read first. Stable, so the newest-first order breaks ties."""
    counts = load_usage().get("fact") or {}
    return sorted(facts, key=lambda f: -(counts.get(f["id"]) or {}).get("count", 0))


# ── reading the ladder's output ──────────────────────────────────────────────

def _facts(store, task_key: Optional[str]) -> list:
    """Every fact known for a task, newest first, de-duplicated."""
    from workflow.ladder import _known_facts, _normalize

    seen, out = set(), []
    for fact in _known_facts(store, task_key):
        key = _normalize(fact["content"])
        if key in seen:
            continue
        seen.add(key)
        out.append({**fact, "task_key": task_key, "id": fact_id(fact["content"])})
    return out


def project_task_keys(store, project: Optional[str], exclude: Optional[str] = None) -> list:
    """Task keys with captured sessions in a project, most recent first."""
    keys = []
    if not project:
        return keys
    for meta in store.list_sessions(limit=2000):
        if meta.project == project and meta.task_key and meta.task_key != exclude:
            if meta.task_key not in keys:
                keys.append(meta.task_key)
    return keys


def _project_facts(store, project: Optional[str], exclude: Optional[str]) -> list:
    """Decisions, constraints and preferences from the project's other tasks."""
    out = []
    for key in project_task_keys(store, project, exclude=exclude)[:10]:
        out += [f for f in _facts(store, key) if f["type"] in PROJECT_FACT_TYPES]
    return out


def _legacy_summary(task_key: str) -> str:
    """Tail of ~/.wf/memory/<task>/summary.md, for tasks with no ladder output."""
    from workflow.memory import ROOT

    path = ROOT / task_key / "summary.md"
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""
    if len(text) <= MAX_FALLBACK_CHARS:
        return text
    # Keep whole sessions: start at the first "--- <timestamp> ---" in the tail.
    tail = text[-MAX_FALLBACK_CHARS:]
    cut = tail.find("\n--- ")
    return tail[cut:].strip() if cut != -1 else tail


def memory_block(task_key: Optional[str], store=None, project: Optional[str] = None) -> str:
    """The stable memory block for a task prompt, or "" when there is none."""
    if not task_key:
        return ""

    rollup, facts, shared = None, [], []
    try:
        store = store or _store()
        from workflow.ladder import get_rollup
        rollup = get_rollup(task_key, store)
        facts = _rank(_facts(store, task_key))
        shared = _rank(_project_facts(store, project, exclude=task_key))
    except Exception as e:
        print(f"⚠️  Could not read task memory for {task_key}: {e}")

    read_ids, parts = [], []
    if rollup:
        parts.append(f"BRIEFING:\n{rollup.strip()[:MAX_ROLLUP_CHARS]}")
    else:
        # No rollup yet: the mirrored session summaries are the next best thing.
        legacy = _legacy_summary(task_key)
        if legacy:
            parts.append(f"PREVIOUS SESSIONS:\n{legacy}")

    if facts:
        lines, used = [], 0
        for fact in facts[:MAX_FACTS]:
            line = f"- ({fact['type']}) {fact['content']}"
            if lines and used + len(line) > MAX_FACTS_CHARS:
                break
            lines.append(line)
            used += len(line)
        read_ids += [f["id"] for f in facts[:len(lines)]]
        more = len(facts) - len(lines)
        if more > 0:
            lines.append(f"- ... {more} more: call search_task_facts")
        parts.append("KNOWN FACTS:\n" + "\n".join(lines))

    if shared:
        lines, chars = [], 0
        for fact in shared:
            line = f"- ({fact['type']}, {fact['task_key']}) {fact['content']}"
            if chars + len(line) > MAX_PROJECT_CHARS:
                break
            lines.append(line)
            chars += len(line)
        if lines:
            read_ids += [f["id"] for f in shared[:len(lines)]]
            parts.append(f"FROM OTHER TASKS IN {project}:\n" + "\n".join(lines))

    if not parts:
        return ""
    record_usage("fact", read_ids)
    body = "\n\n".join(parts)
    return f"<wf-memory task=\"{task_key}\">\n{body}\n</wf-memory>"


# ── on-demand recall, behind the wf-tools MCP server ─────────────────────────

def _terms(query: str) -> list:
    return [t for t in re.split(r"\W+", (query or "").lower()) if t]


def search_facts(query: str, task_key: Optional[str] = None, limit: int = 20,
                 store=None) -> list:
    """Facts whose text contains every query term. An empty query lists them all."""
    store = store or _store()
    terms = _terms(query)
    hits = _rank([
        f for f in _facts(store, task_key)
        if all(t in f["content"].lower() for t in terms)
    ])[:limit]
    record_usage("fact", [f["id"] for f in hits])
    return hits


def search_conversations(query: str, task_key: Optional[str] = None, limit: int = 10,
                         snippet_chars: int = 300, store=None) -> list:
    """Full-text hits over captured turns, trimmed to a snippet around the match."""
    store = store or _store()
    out = []
    for turn in store.search(query, limit=limit, task_key=task_key):
        out.append({
            "session_id": turn.session_id,
            "role": turn.role,
            "when": time.strftime("%Y-%m-%d %H:%M", time.localtime(turn.ts)),
            "snippet": _snippet(turn.content, query, snippet_chars),
        })
    return out


def _snippet(text: str, query: str, width: int) -> str:
    text = " ".join((text or "").split())
    if len(text) <= width:
        return text
    lower = text.lower()
    at = min((i for i in (lower.find(t) for t in _terms(query)) if i >= 0), default=0)
    start = max(0, at - width // 3)
    end = start + width
    return ("…" if start else "") + text[start:end] + ("…" if end < len(text) else "")
