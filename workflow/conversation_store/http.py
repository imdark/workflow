"""HTTP conversation store -- POST captured turns to an arbitrary service.

The escape hatch for any destination this repo does not implement natively:
a notes-graph ingest endpoint, a team database behind an API, a webhook.
Configure it with a base URL and optional headers:

    proxy:
      capture:
        store: sqlite
        mirrors: [http]
        http:
          url: http://127.0.0.1:3000/api/ingest
          headers:
            Authorization: Bearer xxx
          timeout: 5

Each POST body is {"type": "session"|"turns"|"summary", ...}. Writes are
best-effort and never block or fail a proxied request: a dead endpoint logs
and moves on. Reads are not supported -- pair it with a local primary store.
"""

import json
import threading
import urllib.error
import urllib.request
from typing import Optional

from workflow.conversation_store.base import ConversationStore, SessionMeta, Summary


class HttpStore(ConversationStore):
    name = "http"

    def __init__(self, url: str, headers: Optional[dict] = None, timeout: float = 5.0):
        if not url:
            raise ValueError("http store requires a 'url'")
        self.url = url
        self.headers = {"Content-Type": "application/json", **(headers or {})}
        self.timeout = timeout
        self._errors = 0

    def _post(self, payload: dict) -> None:
        """Fire-and-forget on a daemon thread; capture must not block a request."""
        def send():
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            req = urllib.request.Request(self.url, data=body, headers=self.headers, method="POST")
            try:
                with urllib.request.urlopen(req, timeout=self.timeout):
                    pass
            except (urllib.error.URLError, OSError) as e:
                self._errors += 1
                # Only announce the first few: a permanently dead endpoint
                # should not scroll the user's terminal on every turn.
                if self._errors <= 3:
                    print(f"⚠️  http store POST failed ({self._errors}): {e}")

        threading.Thread(target=send, daemon=True).start()

    def ensure_session(self, meta: SessionMeta) -> None:
        self._post({"type": "session", "session": meta.to_dict()})

    def append_turns(self, session_id: str, turns: list) -> int:
        if not turns:
            return 0
        self._post({
            "type": "turns",
            "session_id": session_id,
            "turns": [dict(t.to_dict(), fingerprint=t.fingerprint()) for t in turns],
        })
        return len(turns)

    def save_summary(self, summary: Summary) -> None:
        self._post({"type": "summary", "summary": summary.to_dict()})

    # ── read side: not available over a write-only webhook ────────────────

    def fingerprints(self, session_id: str) -> list:
        return []

    def get_turns(self, session_id, limit=None, offset=0) -> list:
        return []

    def list_sessions(self, task_key=None, limit=50) -> list:
        return []

    def search(self, query, limit=20, task_key=None) -> list:
        return []

    def get_summaries(self, session_id=None, task_key=None, level=None) -> list:
        return []
