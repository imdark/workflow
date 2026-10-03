"""Config-driven factory for conversation stores.

Mirrors `workflow.backends.get_backend`. Configuration lives under
`proxy.capture` in ~/.wf/config.yaml:

    proxy:
      capture:
        store: sqlite          # primary -- reads and writes
        mirrors: [markdown]    # additional write-only destinations
        sqlite:
          path: ~/.wf/conversations.db
        markdown:
          dir: ~/code/notes-graph/vault/sessions
        jsonl:
          dir: ~/.wf/memory/conversations
        http:
          url: http://127.0.0.1:3000/api/ingest

A store named in `mirrors` receives every write; reads always go to the
primary, so it should be one of the local backends.
"""

from workflow.conversation_store.base import (
    ConversationStore,
    SessionMeta,
    Summary,
    Turn,
    reconcile,
)

STORE_TYPES = ("sqlite", "jsonl", "markdown", "http")

__all__ = [
    "ConversationStore", "SessionMeta", "Summary", "Turn", "reconcile",
    "STORE_TYPES", "get_store", "build_store", "MultiStore",
]


def build_store(kind: str, options: dict) -> ConversationStore:
    """Construct one store by name. Raises ValueError on an unknown kind."""
    options = options or {}
    if kind == "sqlite":
        from workflow.conversation_store.sqlite import SqliteStore
        return SqliteStore(path=options.get("path"))
    if kind == "jsonl":
        from workflow.conversation_store.jsonl import JsonlStore
        return JsonlStore(directory=options.get("dir"))
    if kind == "markdown":
        from workflow.conversation_store.markdown import MarkdownStore
        return MarkdownStore(directory=options.get("dir"))
    if kind == "http":
        from workflow.conversation_store.http import HttpStore
        return HttpStore(
            url=options.get("url", ""),
            headers=options.get("headers"),
            timeout=float(options.get("timeout", 5)),
        )
    raise ValueError(f"Unknown conversation store '{kind}'. Supported: {', '.join(STORE_TYPES)}")


class MultiStore(ConversationStore):
    """Fan writes out to several stores; read from the primary only."""

    name = "multi"

    def __init__(self, primary: ConversationStore, mirrors=None):
        self.primary = primary
        self.mirrors = list(mirrors or [])

    @property
    def all(self) -> list:
        return [self.primary] + self.mirrors

    def _fanout(self, method: str, *args, **kwargs):
        result = getattr(self.primary, method)(*args, **kwargs)
        for mirror in self.mirrors:
            try:
                getattr(mirror, method)(*args, **kwargs)
            except Exception as e:
                # A failing mirror must never cost us the primary write.
                print(f"⚠️  conversation mirror '{mirror.name}' failed on {method}: {e}")
        return result

    def ensure_session(self, meta): return self._fanout("ensure_session", meta)
    def append_turns(self, session_id, turns): return self._fanout("append_turns", session_id, turns)
    def save_summary(self, summary): return self._fanout("save_summary", summary)

    def fingerprints(self, session_id): return self.primary.fingerprints(session_id)
    def get_turns(self, session_id, limit=None, offset=0):
        return self.primary.get_turns(session_id, limit, offset)
    def list_sessions(self, task_key=None, limit=50):
        return self.primary.list_sessions(task_key, limit)
    def search(self, query, limit=20, task_key=None):
        return self.primary.search(query, limit, task_key)
    def get_summaries(self, session_id=None, task_key=None, level=None):
        return self.primary.get_summaries(session_id, task_key, level)

    def close(self):
        for store in self.all:
            try:
                store.close()
            except Exception:
                pass


def get_store(cfg=None) -> ConversationStore:
    """Build the configured store (plus mirrors) from workflow config."""
    if cfg is None:
        from workflow.config import load_effective_config
        cfg = load_effective_config()

    capture = (cfg.get("proxy") or {}).get("capture") or {}
    kind = capture.get("store", "sqlite")
    primary = build_store(kind, capture.get(kind, {}))

    mirrors = []
    for mirror_kind in capture.get("mirrors") or []:
        if mirror_kind == kind:
            continue  # already the primary
        try:
            mirrors.append(build_store(mirror_kind, capture.get(mirror_kind, {})))
        except (ValueError, Exception) as e:
            print(f"⚠️  Skipping conversation mirror '{mirror_kind}': {e}")

    return MultiStore(primary, mirrors) if mirrors else primary
