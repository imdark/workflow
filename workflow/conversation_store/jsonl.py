"""Append-only JSONL conversation store.

One file per day under ~/.wf/memory/conversations/, one message per line,
session id carried as a field rather than in the filename. Greppable and
diffable; search is a linear scan, which is fine at personal scale.
"""

import json
import time
from pathlib import Path
from typing import Optional

from workflow.conversation_store.base import (
    ConversationStore,
    SessionMeta,
    Summary,
    Turn,
)

DEFAULT_DIR = Path.home() / ".wf" / "memory" / "conversations"


class JsonlStore(ConversationStore):
    name = "jsonl"

    def __init__(self, directory=None):
        self.dir = Path(directory).expanduser() if directory else DEFAULT_DIR
        self.dir.mkdir(parents=True, exist_ok=True)
        self.sessions_file = self.dir / "sessions.jsonl"
        self.summaries_file = self.dir / "summaries.jsonl"

    def _day_file(self, ts: float) -> Path:
        return self.dir / f"{time.strftime('%Y-%m-%d', time.localtime(ts))}.jsonl"

    @staticmethod
    def _append(path: Path, record: dict) -> None:
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    @staticmethod
    def _read(path: Path):
        if not path.exists():
            return
        with path.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    # A torn final line from an interrupted write: skip it
                    # rather than failing the whole read.
                    continue

    # ── sessions ──────────────────────────────────────────────────────────

    def ensure_session(self, meta: SessionMeta) -> None:
        # Sessions are upserted by appending; the last record for an id wins.
        record = meta.to_dict()
        record["updated_at"] = time.time()
        self._append(self.sessions_file, record)

    def _session_index(self) -> dict:
        index = {}
        for record in self._read(self.sessions_file):
            sid = record.get("session_id")
            if sid:
                index[sid] = record
        return index

    def list_sessions(self, task_key: Optional[str] = None, limit: int = 50) -> list:
        metas = [SessionMeta(**r) for r in self._session_index().values()]
        if task_key:
            metas = [m for m in metas if m.task_key == task_key]
        metas.sort(key=lambda m: m.updated_at, reverse=True)
        return metas[:limit]

    # ── messages ──────────────────────────────────────────────────────────

    def _all_turns(self, session_id: Optional[str] = None):
        for path in sorted(self.dir.glob("[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9].jsonl")):
            for record in self._read(path):
                if session_id and record.get("session_id") != session_id:
                    continue
                yield record

    def fingerprints(self, session_id: str) -> list:
        turns = sorted(self._all_turns(session_id), key=lambda r: r.get("seq", 0))
        return [r.get("fingerprint", "") for r in turns]

    def append_turns(self, session_id: str, turns: list) -> int:
        existing = [r.get("seq", -1) for r in self._all_turns(session_id)]
        seq = (max(existing) + 1) if existing else 0
        for turn in turns:
            record = turn.to_dict()
            record["seq"] = seq
            record["fingerprint"] = turn.fingerprint()
            self._append(self._day_file(turn.ts), record)
            seq += 1
        return len(turns)

    def get_turns(self, session_id: str, limit: Optional[int] = None, offset: int = 0) -> list:
        records = sorted(self._all_turns(session_id), key=lambda r: r.get("seq", 0))
        records = records[offset:]
        if limit is not None:
            records = records[:limit]
        return [_to_turn(r) for r in records]

    def search(self, query: str, limit: int = 20, task_key: Optional[str] = None) -> list:
        needle = query.lower()
        allowed = None
        if task_key:
            allowed = {m.session_id for m in self.list_sessions(task_key=task_key, limit=10_000)}
        hits = []
        for record in self._all_turns():
            if allowed is not None and record.get("session_id") not in allowed:
                continue
            if needle in (record.get("content") or "").lower():
                hits.append(_to_turn(record))
                if len(hits) >= limit:
                    break
        return hits

    # ── summaries ─────────────────────────────────────────────────────────

    def save_summary(self, summary: Summary) -> None:
        self._append(self.summaries_file, summary.to_dict())

    def get_summaries(self, session_id=None, task_key=None, level=None) -> list:
        out = []
        for record in self._read(self.summaries_file):
            if session_id and record.get("session_id") != session_id:
                continue
            if task_key and record.get("task_key") != task_key:
                continue
            if level and record.get("level") != level:
                continue
            out.append(Summary(**record))
        out.sort(key=lambda s: s.ts, reverse=True)
        return out


def _to_turn(record: dict) -> Turn:
    return Turn(
        session_id=record.get("session_id", ""),
        seq=record.get("seq", 0),
        role=record.get("role", "user"),
        content=record.get("content", ""),
        ts=record.get("ts", 0.0),
        tool_name=record.get("tool_name"),
        tool_call_id=record.get("tool_call_id"),
        raw=record.get("raw"),
    )
