"""Markdown conversation store -- one file per session.

Aimed at note vaults that ingest a directory of markdown (notes-graph,
Obsidian, a git-tracked notes repo). Point `proxy.capture.markdown.dir` at
the vault and sessions show up as readable documents with YAML frontmatter.

Write-oriented: reads are supported so `wf mem` keeps working against it,
but full-text search is a file scan. Pair it with sqlite as the primary
when you want both a searchable index and human-readable notes.
"""

import re
import time
from pathlib import Path
from typing import Optional

from workflow.conversation_store.base import (
    ConversationStore,
    SessionMeta,
    Summary,
    Turn,
)

DEFAULT_DIR = Path.home() / ".wf" / "memory" / "notes"

ROLE_LABEL = {
    "user": "User",
    "assistant": "Assistant",
    "system": "System",
    "tool_call": "Tool call",
    "tool_result": "Tool result",
}


class MarkdownStore(ConversationStore):
    name = "markdown"

    def __init__(self, directory=None):
        self.dir = Path(directory).expanduser() if directory else DEFAULT_DIR
        self.dir.mkdir(parents=True, exist_ok=True)

    def _session_path(self, session_id: str, meta: Optional[SessionMeta] = None) -> Path:
        # Prefer an existing file so a session keeps one path even if its task
        # is assigned after the first turn.
        for path in self.dir.glob(f"*{_slug(session_id)}.md"):
            return path
        task = (meta.task_key if meta and meta.task_key else "untasked").lower()
        stamp = time.strftime("%Y-%m-%d", time.localtime(meta.started_at if meta else time.time()))
        return self.dir / f"{stamp}-{_slug(task)}-{_slug(session_id)}.md"

    def ensure_session(self, meta: SessionMeta) -> None:
        path = self._session_path(meta.session_id, meta)
        if path.exists():
            return
        path.write_text(
            "---\n"
            f"session_id: {meta.session_id}\n"
            f"agent: {meta.agent}\n"
            f"protocol: {meta.protocol}\n"
            f"model: {meta.model}\n"
            f"task: {meta.task_key or ''}\n"
            f"project: {meta.project or ''}\n"
            f"repo: {meta.repo or ''}\n"
            f"started: {_iso(meta.started_at)}\n"
            "---\n\n"
            f"# {meta.task_key or 'Session'} — {meta.agent}\n\n",
            encoding="utf-8",
        )

    def append_turns(self, session_id: str, turns: list) -> int:
        if not turns:
            return 0
        path = self._session_path(session_id)
        with path.open("a", encoding="utf-8") as f:
            for turn in turns:
                label = ROLE_LABEL.get(turn.role, turn.role)
                if turn.tool_name:
                    label = f"{label}: {turn.tool_name}"
                f.write(f"## {label}\n<!-- fp:{turn.fingerprint()} ts:{_iso(turn.ts)} -->\n\n")
                f.write(turn.content.rstrip() + "\n\n")
        return len(turns)

    def fingerprints(self, session_id: str) -> list:
        path = self._session_path(session_id)
        if not path.exists():
            return []
        return re.findall(r"<!-- fp:([0-9a-f]+)", path.read_text(encoding="utf-8"))

    def get_turns(self, session_id: str, limit: Optional[int] = None, offset: int = 0) -> list:
        path = self._session_path(session_id)
        if not path.exists():
            return []
        turns = []
        body = path.read_text(encoding="utf-8")
        # Sections look like: "## Label\n<!-- fp:... ts:... -->\n\nbody"
        # `label` and the marker fields must be newline-free: DOTALL is on for
        # the body, and a `.` there would let the label swallow the rest of
        # the document and collapse every section into one match.
        pattern = re.compile(
            r"^## (?P<label>[^\n]+)\n<!-- fp:(?P<fp>[0-9a-f]+) ts:(?P<ts>[^\s]+) -->\n\n"
            r"(?P<body>.*?)(?=\n## |\n---\n|\Z)",
            re.MULTILINE | re.DOTALL,
        )
        for seq, m in enumerate(pattern.finditer(body)):
            label = m.group("label")
            role = next((r for r, lbl in ROLE_LABEL.items() if label.startswith(lbl)), "user")
            turns.append(Turn(
                session_id=session_id, seq=seq, role=role,
                content=m.group("body").strip(), ts=_epoch(m.group("ts")),
            ))
        turns = turns[offset:]
        return turns[:limit] if limit is not None else turns

    def list_sessions(self, task_key: Optional[str] = None, limit: int = 50) -> list:
        metas = []
        for path in sorted(self.dir.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True):
            meta = _parse_frontmatter(path)
            if meta and (not task_key or meta.task_key == task_key):
                metas.append(meta)
            if len(metas) >= limit:
                break
        return metas

    def search(self, query: str, limit: int = 20, task_key: Optional[str] = None) -> list:
        needle = query.lower()
        hits = []
        for meta in self.list_sessions(task_key=task_key, limit=10_000):
            for turn in self.get_turns(meta.session_id):
                if needle in turn.content.lower():
                    hits.append(turn)
                    if len(hits) >= limit:
                        return hits
        return hits

    def save_summary(self, summary: Summary) -> None:
        if summary.session_id:
            path = self._session_path(summary.session_id)
            if path.exists():
                with path.open("a", encoding="utf-8") as f:
                    f.write(f"\n---\n\n## Summary ({summary.level})\n"
                            f"<!-- level:{summary.level} ts:{_iso(summary.ts)} -->\n\n"
                            f"{summary.content.strip()}\n")
                return
        # Task-wide rollups have no session file of their own.
        name = f"rollup-{_slug(summary.task_key or 'global')}.md"
        (self.dir / name).write_text(
            f"---\nlevel: {summary.level}\ntask: {summary.task_key or ''}\n"
            f"updated: {_iso(summary.ts)}\n---\n\n{summary.content.strip()}\n",
            encoding="utf-8",
        )

    def get_summaries(self, session_id=None, task_key=None, level=None) -> list:
        out = []
        for path in self.dir.glob("rollup-*.md"):
            text = path.read_text(encoding="utf-8")
            meta_level = _frontmatter_value(text, "level") or "rollup"
            meta_task = _frontmatter_value(text, "task")
            if level and meta_level != level:
                continue
            if task_key and meta_task != task_key:
                continue
            if session_id:
                continue
            out.append(Summary(
                level=meta_level, content=text.split("---", 2)[-1].strip(),
                task_key=meta_task or None, ts=path.stat().st_mtime,
            ))
        return out


def _slug(text: str) -> str:
    return re.sub(r"[^a-zA-Z0-9._-]+", "-", str(text)).strip("-")[:60] or "x"


def _iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(ts))


def _epoch(stamp: str) -> float:
    try:
        return time.mktime(time.strptime(stamp, "%Y-%m-%dT%H:%M:%S"))
    except ValueError:
        return 0.0


def _frontmatter_value(text: str, key: str):
    m = re.search(rf"^{re.escape(key)}:\s*(.*)$", text, re.MULTILINE)
    return m.group(1).strip() if m else None


def _parse_frontmatter(path: Path):
    try:
        head = path.read_text(encoding="utf-8")[:2000]
    except OSError:
        return None
    sid = _frontmatter_value(head, "session_id")
    if not sid:
        return None
    return SessionMeta(
        session_id=sid,
        agent=_frontmatter_value(head, "agent") or "unknown",
        protocol=_frontmatter_value(head, "protocol") or "anthropic",
        model=_frontmatter_value(head, "model") or "",
        task_key=_frontmatter_value(head, "task") or None,
        project=_frontmatter_value(head, "project") or None,
        repo=_frontmatter_value(head, "repo") or None,
        started_at=path.stat().st_ctime,
        updated_at=path.stat().st_mtime,
    )
