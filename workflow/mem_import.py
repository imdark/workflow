"""Backfill the conversation store from Claude Code's own transcripts.

The proxy only captures sessions that ran through it. Everything before it
was switched on still sits in ~/.claude*/projects/<dir>/<session>.jsonl, one
JSON record per line. Importing those seeds the ladder, so recall works on
day one instead of after weeks of capture.

Records are normalized with the proxy's own Anthropic normalizer, and the
session id is Claude Code's, the same one the proxy reads from the
x-claude-code-session-id header. A session that was both captured live and
imported therefore reconciles into one, and re-running an import appends
nothing.
"""

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Iterator, Optional

from workflow.conversation_store.base import SessionMeta, reconcile
from workflow.proxy.protocols import _anthropic_turns

# "personal-113-ideas-worth..." -> PERSONAL-113; "feat/x" has no task.
_BRANCH_KEY_RE = re.compile(r"^(?:[\w.-]+/)?([A-Za-z][A-Za-z0-9]*)-(\d+)\b")


def transcript_files(roots=None) -> Iterator[Path]:
    """Every top-level session transcript under the Claude config dirs."""
    if roots is None:
        roots = sorted(Path.home().glob(".claude*/projects"))
    for root in roots:
        root = Path(root).expanduser()
        if root.is_dir():
            # Subagent transcripts live one level deeper and belong to their
            # parent session, so only the top level is read.
            yield from sorted(root.glob("*/*.jsonl"))


def task_key_from_branch(branch: Optional[str]) -> Optional[str]:
    m = _BRANCH_KEY_RE.match(branch or "")
    return f"{m.group(1).upper()}-{m.group(2)}" if m else None


def _project_for_path(path: Optional[str]) -> Optional[str]:
    if not path:
        return None
    try:
        from workflow.projects import list_projects
        for name, cfg in (list_projects() or {}).items():
            for repo in (cfg.get("repositories") or {}):
                if path.startswith(repo):
                    return name
    except Exception:
        pass
    return None


def _timestamp(value) -> Optional[float]:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


def parse_transcript(path: Path):
    """Return (SessionMeta, turns) for one transcript, or (None, []) if empty."""
    meta, turns = None, []
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if record.get("type") not in ("user", "assistant") or record.get("isSidechain"):
                continue
            message = record.get("message") or {}
            session_id = record.get("sessionId") or path.stem
            ts = _timestamp(record.get("timestamp"))

            if meta is None:
                cwd = record.get("cwd")
                meta = SessionMeta(
                    session_id=session_id, agent="claude-code", protocol="anthropic",
                    task_key=task_key_from_branch(record.get("gitBranch")),
                    project=_project_for_path(cwd), repo=cwd,
                    started_at=ts or path.stat().st_mtime,
                )
            if not meta.task_key:
                meta.task_key = task_key_from_branch(record.get("gitBranch"))
            if message.get("model"):
                meta.model = message["model"]

            body = {"messages": [{"role": message.get("role", record["type"]),
                                  "content": message.get("content")}]}
            for turn in _anthropic_turns(body, session_id):
                if ts:
                    turn.ts = ts
                turns.append(turn)
    return meta, turns


def import_transcripts(store, files, task_key: Optional[str] = None) -> list:
    """Append what each transcript has that the store does not.

    Returns (session_id, task_key, turns_written) for every session that
    gained turns.
    """
    imported = []
    for path in files:
        meta, turns = parse_transcript(path)
        if meta is None or not turns:
            continue
        if task_key and meta.task_key != task_key:
            continue
        fresh = reconcile(store.fingerprints(meta.session_id), turns)
        if not fresh:
            continue
        store.ensure_session(meta)
        written = store.append_turns(meta.session_id, fresh)
        imported.append((meta.session_id, meta.task_key, written))
    return imported
