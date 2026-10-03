"""Storage interface for captured agent conversations.

Mirrors the `workflow.backends` pattern: one abstract class, several
implementations, a config-driven factory in `__init__.py`. Everything the
proxy captures flows through `ConversationStore`, so a new destination
(notes-graph, a team database, S3) only has to implement this interface.
"""

import hashlib
import json
import time
from dataclasses import dataclass, field, asdict
from typing import Any, Optional


# Roles are normalized across protocols before they reach a store, so a
# backend never has to know whether a turn arrived as an Anthropic content
# block or an OpenAI tool message.
ROLES = ("system", "user", "assistant", "tool_call", "tool_result")

# Summary levels, in the order the ladder produces them.
LEVELS = ("session", "facts", "rollup")


def _canonical_json(text: str) -> str:
    """Re-serialize JSON deterministically; return the text unchanged if it is not JSON."""
    stripped = (text or "").strip()
    if not stripped or stripped[0] not in "{[":
        return text
    try:
        parsed = json.loads(stripped)
    except (ValueError, TypeError):
        return text
    return json.dumps(parsed, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


@dataclass
class SessionMeta:
    """One agent conversation, scoped to the task that was active."""

    session_id: str
    agent: str = "unknown"          # claude-code | codex | opencode | ...
    protocol: str = "anthropic"     # anthropic | openai-chat | openai-responses
    model: str = ""
    task_key: Optional[str] = None
    project: Optional[str] = None
    repo: Optional[str] = None
    started_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Turn:
    """A single normalized message within a session."""

    session_id: str
    seq: int
    role: str
    content: str
    ts: float = field(default_factory=time.time)
    tool_name: Optional[str] = None
    tool_call_id: Optional[str] = None
    raw: Optional[dict] = None

    def fingerprint(self) -> str:
        """Stable hash of the semantic payload, used to de-duplicate.

        Every request resends the whole history, so the proxy compares
        fingerprints against what is already stored and appends only the
        tail. `ts` and `seq` are excluded: the same message re-sent in a
        later request must hash identically.

        Tool payloads are canonicalized first. The same call reaches us
        twice in two different spellings -- assembled from streamed
        `input_json_delta` fragments (`{"file":"cli.py"}`), then echoed
        back in the next request's history after a JSON round-trip
        (`{"file": "cli.py"}`). Hashing the raw text would make those look
        like two distinct calls and duplicate the whole tail of the
        conversation on every turn.
        """
        h = hashlib.sha256()
        content = self.content
        if self.role in ("tool_call", "tool_result"):
            content = _canonical_json(content)
        for part in (self.role, content, self.tool_name or "", self.tool_call_id or ""):
            h.update(part.encode("utf-8", "replace"))
            h.update(b"\x00")
        return h.hexdigest()[:16]

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Summary:
    """An LLM-generated artifact derived from one or more sessions."""

    level: str                       # one of LEVELS
    content: str
    session_id: Optional[str] = None  # None for task-wide rollups
    task_key: Optional[str] = None
    model: str = ""
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return asdict(self)


class ConversationStore:
    """Abstract destination for captured conversations.

    Implementations must be safe to call from the proxy's event loop thread;
    anything slow should buffer internally. Every method is expected to
    tolerate being called for a session it has not seen before.
    """

    name = "base"

    def ensure_session(self, meta: SessionMeta) -> None:
        """Create the session if new, refresh `updated_at` if not."""
        raise NotImplementedError

    def append_turns(self, session_id: str, turns: list) -> int:
        """Append already-deduplicated turns. Returns how many were written."""
        raise NotImplementedError

    def get_turns(self, session_id: str, limit: Optional[int] = None, offset: int = 0) -> list:
        raise NotImplementedError

    def list_sessions(self, task_key: Optional[str] = None, limit: int = 50) -> list:
        raise NotImplementedError

    def search(self, query: str, limit: int = 20, task_key: Optional[str] = None) -> list:
        """Full-text search over turn content. Returns Turn objects."""
        raise NotImplementedError

    def save_summary(self, summary: Summary) -> None:
        raise NotImplementedError

    def get_summaries(
        self,
        session_id: Optional[str] = None,
        task_key: Optional[str] = None,
        level: Optional[str] = None,
    ) -> list:
        raise NotImplementedError

    def fingerprints(self, session_id: str) -> list:
        """Ordered fingerprints of stored turns, for tail reconciliation."""
        raise NotImplementedError

    def close(self) -> None:
        """Flush and release resources. Safe to call more than once."""
        return None


def reconcile(stored: list, incoming: list) -> list:
    """Return the turns in `incoming` that are not yet in `stored`.

    `stored` is the fingerprint list for the session; `incoming` is the full
    normalized history from this request. Agents resend everything every
    turn, so this is what keeps the log from growing quadratically.

    This is a multiset difference, not a prefix match. Prefix matching looks
    right -- history is append-only, so stored *should* be a prefix of
    incoming -- but it fails catastrophically against a real client: Claude
    Code stamps a per-request build id into its system prompt, so position 0
    differs on every request, nothing matches, and the entire history
    re-appends every turn. One observed session held 352 distinct turns
    stored 16,901 times.

    Counting occurrences rather than testing membership keeps a genuinely
    repeated message. If you type "do it" twice, incoming holds two and
    stored holds one, so the second is correctly seen as new.

    Order is preserved, and a turn that changed in place (an edited system
    prompt) appends rather than rewriting: the raw record stays append-only.
    """
    from collections import Counter

    remaining = Counter(stored)
    fresh = []
    for turn in incoming:
        fingerprint = turn.fingerprint()
        if remaining.get(fingerprint, 0) > 0:
            remaining[fingerprint] -= 1   # accounted for by a stored turn
        else:
            fresh.append(turn)
    return fresh
