"""Publish finished conversations into NotesGraph.

`wf ai` sessions land in the primary store turn by turn while they run.
This publishes the finished article: one document per conversation, written
once the session has gone quiet, under a breadcrumb path

    <project> / ai / conversations / <chat id>

plus an index document at `<project> / ai / conversations` linking to them.

The notes API creates flat documents with a title -- there is no folder
model server-side, and NotesGraph's own hierarchy lives in the frontend's
CRDT rather than anywhere this can reach. Breadcrumb titles and a linked
index are the navigable structure the available API supports.
"""

import json
import re
import time
import urllib.error
import urllib.request
from typing import Optional

from workflow.conversation_store.base import Summary
from workflow.deploy.registry.notesgraph import NotesGraphError, load_token

DEFAULT_URL = "https://app.notesgraph.com"

# Tool payloads are the bulk of a coding session and the least readable part
# of it; keep enough to see what ran without burying the conversation.
TOOL_PREVIEW_CHARS = 400

# The notes API runs on NestJS's default body parser, which is Express's
# 100KB. A long session renders well past that, so the transcript is elided
# to fit rather than the publish failing with a 413.
DEFAULT_MAX_DOC_BYTES = 90_000


class NotesGraphDocClient:
    """The notes API: create and update documents from markdown."""

    def __init__(self, url: str = DEFAULT_URL, workspace: str = "",
                 token: Optional[str] = None, timeout: float = 30.0):
        if not workspace:
            raise ValueError("notesgraph publishing needs a workspace id")
        self.url = (url or DEFAULT_URL).rstrip("/")
        self.workspace = workspace
        self.token = token or load_token()
        self.timeout = timeout

    def _request(self, method: str, path: str, payload: Optional[dict] = None) -> dict:
        if not self.token:
            raise NotesGraphError(
                "No NotesGraph token. Run 'wf deploy notes-login <token>' first."
            )
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = urllib.request.Request(
            f"{self.url}{path}", data=body, method=method,
            headers={"Authorization": f"Bearer {self.token}",
                     "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read().decode("utf-8")
                if not raw.strip():
                    return {}
                try:
                    return json.loads(raw)
                except json.JSONDecodeError as e:
                    # An unknown /api path falls through to the SPA, which
                    # answers 200 with index.html.
                    if raw.lstrip()[:1] == "<":
                        raise NotesGraphError(
                            f"{path} returned a web page, not JSON — the notes "
                            "API is not available on this server."
                        ) from e
                    raise NotesGraphError(f"{path} returned invalid JSON: {e}") from e
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8")[:300]
            except Exception:
                pass
            raise NotesGraphError(f"{method} {path} failed: {e.code} {detail}") from e
        except (urllib.error.URLError, OSError) as e:
            raise NotesGraphError(f"NotesGraph unreachable at {self.url}: {e}") from e

    def create_doc(self, title: str, markdown: str) -> str:
        result = self._request(
            "POST", f"/api/notes/workspaces/{self.workspace}/docs",
            {"title": title, "markdown": markdown},
        )
        return result.get("docId", "")

    def update_doc(self, doc_id: str, markdown: str) -> bool:
        result = self._request(
            "PUT", f"/api/notes/workspaces/{self.workspace}/docs/{doc_id}",
            {"markdown": markdown},
        )
        return bool(result.get("ok"))


# ── rendering ────────────────────────────────────────────────────────────────

ROLE_HEADING = {
    "user": "User",
    "assistant": "Assistant",
    "system": "System prompt",
    "tool_call": "Tool call",
    "tool_result": "Tool result",
}


def doc_title(project: Optional[str], session_id: str) -> str:
    """Breadcrumb title: `<project> / ai / conversations / <chat id>`."""
    return f"{project or 'unscoped'} / ai / conversations / {short_id(session_id)}"


def index_title(project: Optional[str]) -> str:
    return f"{project or 'unscoped'} / ai / conversations"


def short_id(session_id: str) -> str:
    """Session ids are uuids; the first segment is enough to recognize one."""
    return session_id.split("-")[0][:12] or session_id[:12]


def render_conversation(meta, turns, summaries=None,
                        max_bytes: int = DEFAULT_MAX_DOC_BYTES) -> str:
    """The document body: front matter, the ending, then the transcript.

    Kept under `max_bytes`. When a session does not fit, the transcript is
    elided from the middle outward: the opening frames the task and the
    close holds the outcome, so the turns in between are the ones worth
    losing. The header, the ending and the facts are never dropped -- they
    are the part someone opens the document for.
    """
    started = time.strftime("%Y-%m-%d %H:%M", time.localtime(meta.started_at))
    lines = [
        f"# {meta.task_key or 'Session'} — {short_id(meta.session_id)}",
        "",
        f"- **Project**: {meta.project or '—'}",
        f"- **Task**: {meta.task_key or '—'}",
        f"- **Repo**: {meta.repo or '—'}",
        f"- **Agent**: {meta.agent} ({meta.model or 'unknown model'})",
        f"- **Started**: {started}",
        f"- **Turns**: {len(turns)}",
        f"- **Session**: `{meta.session_id}`",
        "",
    ]

    # The ending first: whoever opens this months later wants the outcome,
    # not to scroll a hundred tool calls to find it.
    for summary in summaries or []:
        if summary.level == "session":
            lines += ["## Ending", "", escape_taglike(summary.content.strip()), ""]
            break

    facts = [s for s in (summaries or []) if s.level == "facts"]
    if facts:
        lines += ["## Durable facts", ""]
        for summary in facts:
            try:
                for fact in json.loads(summary.content):
                    lines.append(
                        f"- *({fact.get('type', 'fact')})* "
                        f"{escape_taglike(fact.get('content', ''))}"
                    )
            except json.JSONDecodeError:
                continue
        lines.append("")

    header = "\n".join(lines)
    rendered = [_render_turn(turn) for turn in turns]
    transcript, elided = _fit(rendered, max_bytes - len(header.encode()) - 200)

    parts = [header, "## Transcript", ""]
    if elided:
        parts.append(
            f"> {elided} turn(s) elided from the middle to fit the document "
            f"limit. The full transcript is in the local store: "
            f"`wf mem show {short_id(meta.session_id)}`.\n"
        )
    parts.extend(transcript)
    return "\n".join(parts)


def _render_turn(turn) -> str:
    heading = ROLE_HEADING.get(turn.role, turn.role)
    if turn.tool_name:
        heading = f"{heading}: `{turn.tool_name}`"
    return f"### {heading}\n\n{_body(turn)}\n"


def _fit(rendered: list, budget: int) -> tuple:
    """Keep as much of the transcript as fits, dropping from the middle.

    Returns the kept blocks and how many were dropped. Takes from the front
    and the back alternately so the opening and the outcome both survive.
    """
    total = sum(len(block.encode()) for block in rendered)
    if budget <= 0:
        return [], len(rendered)
    if total <= budget:
        return rendered, 0

    head, tail, used = [], [], 0
    front, back = 0, len(rendered) - 1
    take_front = True
    while front <= back:
        index = front if take_front else back
        size = len(rendered[index].encode())
        if used + size > budget:
            break
        used += size
        if take_front:
            head.append(rendered[index])
            front += 1
        else:
            tail.insert(0, rendered[index])
            back -= 1
        take_front = not take_front

    dropped = len(rendered) - len(head) - len(tail)
    marker = ["\n---\n"] if dropped else []
    return head + marker + tail, dropped


# NotesGraph's markdown importer treats `<word>` as an HTML tag and returns
# 500 on one it cannot parse. Agent transcripts are full of them --
# <system-reminder>, <user_query>, generics like Vec<T> -- so anything
# outside a code fence has its angle brackets entity-escaped on the way out.
# Inside a fence they are safe and left alone, which keeps tool payloads
# readable.
_TAGLIKE = re.compile(r"<(?=[/!?a-zA-Z])")


def escape_taglike(text: str) -> str:
    """Escape `<` where it could be read as an HTML tag."""
    return _TAGLIKE.sub("&lt;", text or "")


def _body(turn) -> str:
    content = (turn.content or "").strip()
    if turn.role in ("tool_call", "tool_result"):
        if len(content) > TOOL_PREVIEW_CHARS:
            content = content[:TOOL_PREVIEW_CHARS] + \
                f"\n… [{len(content) - TOOL_PREVIEW_CHARS} chars truncated]"
        # A fence protects angle brackets, but not a fence the content
        # closes early with its own backticks.
        return f"```\n{content.replace('```', '`­``')}\n```"
    return escape_taglike(content)


def render_index(project: Optional[str], entries: list) -> str:
    """The index document, newest conversation first."""
    lines = [f"# {project or 'unscoped'} / ai / conversations", ""]
    if not entries:
        lines.append("_No conversations published yet._")
        return "\n".join(lines)

    lines += ["| When | Task | Turns | Conversation |", "| --- | --- | --- | --- |"]
    for entry in sorted(entries, key=lambda e: e.get("updated", 0), reverse=True):
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(entry.get("updated", 0)))
        link = f"[{entry['short']}](/workspace/{entry['workspace']}/{entry['doc_id']})"
        lines.append(f"| {when} | {entry.get('task') or '—'} | "
                     f"{entry.get('turns', 0)} | {link} |")
    return "\n".join(lines)


# ── published-document bookkeeping ───────────────────────────────────────────

# Which NotesGraph document holds which session. The notes API has no way to
# look a document up by title, so republishing a session would otherwise
# create a second copy every time.
LEDGER = None  # resolved lazily so tests can point it elsewhere


def _ledger_path():
    from pathlib import Path
    return LEDGER or (Path.home() / ".wf" / "state" / "notesgraph_docs.json")


def _load_ledger() -> dict:
    path = _ledger_path()
    if not path.exists():
        return {"sessions": {}, "indexes": {}}
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        # A corrupt ledger costs duplicate documents, not data. Start clean
        # rather than refusing to publish.
        return {"sessions": {}, "indexes": {}}
    data.setdefault("sessions", {})
    data.setdefault("indexes", {})
    return data


def _save_ledger(data: dict) -> None:
    path = _ledger_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2))


# ── publishing ───────────────────────────────────────────────────────────────

def is_enabled(cfg) -> bool:
    publish = ((cfg.get("proxy") or {}).get("capture") or {}).get("notesgraph") or {}
    return bool(publish.get("enabled")) and bool(publish.get("workspace"))


def client_from_config(cfg) -> NotesGraphDocClient:
    publish = ((cfg.get("proxy") or {}).get("capture") or {}).get("notesgraph") or {}
    return NotesGraphDocClient(
        url=publish.get("url", DEFAULT_URL),
        workspace=publish["workspace"],
        timeout=float(publish.get("timeout", 30)),
    )


def publish_session(session_id: str, store, cfg=None,
                    client: Optional[NotesGraphDocClient] = None) -> Optional[str]:
    """Publish one finished conversation. Returns the document id.

    Idempotent: a session already published is updated in place, so a
    re-summarized or resumed session does not spawn a second document.
    """
    if cfg is None:
        from workflow.config import load_effective_config
        cfg = load_effective_config()

    if client is None:
        if not is_enabled(cfg):
            return None
        client = client_from_config(cfg)

    meta = next((m for m in store.list_sessions(limit=5000)
                 if m.session_id == session_id), None)
    if meta is None:
        return None

    turns = store.get_turns(session_id)
    if not turns:
        return None
    summaries = store.get_summaries(session_id=session_id)

    publish_cfg = ((cfg.get("proxy") or {}).get("capture") or {}).get("notesgraph") or {}
    markdown = render_conversation(
        meta, turns, summaries,
        max_bytes=int(publish_cfg.get("max_doc_bytes", DEFAULT_MAX_DOC_BYTES)),
    )
    title = doc_title(meta.project, session_id)

    ledger = _load_ledger()
    record = ledger["sessions"].get(session_id)

    if record and record.get("doc_id"):
        try:
            client.update_doc(record["doc_id"], markdown)
            doc_id = record["doc_id"]
        except NotesGraphError:
            # The document was deleted in the app; publish a fresh one rather
            # than losing the conversation.
            doc_id = client.create_doc(title, markdown)
    else:
        doc_id = client.create_doc(title, markdown)

    ledger["sessions"][session_id] = {
        "doc_id": doc_id,
        "title": title,
        "short": short_id(session_id),
        "project": meta.project,
        "task": meta.task_key,
        "turns": len(turns),
        "updated": time.time(),
        "workspace": client.workspace,
    }
    _save_ledger(ledger)

    _publish_index(client, meta.project, ledger)
    return doc_id


def _publish_index(client: NotesGraphDocClient, project: Optional[str],
                   ledger: dict) -> None:
    """Rewrite the project's conversation index."""
    entries = [record for record in ledger["sessions"].values()
               if record.get("project") == project]
    markdown = render_index(project, entries)
    title = index_title(project)

    key = project or "unscoped"
    doc_id = ledger["indexes"].get(key)
    if doc_id:
        try:
            client.update_doc(doc_id, markdown)
            return
        except NotesGraphError:
            doc_id = None   # deleted in the app; fall through and recreate

    # An index that cannot be written is not worth failing a publish over:
    # the conversation document itself is the artifact.
    try:
        ledger["indexes"][key] = client.create_doc(title, markdown)
        _save_ledger(ledger)
    except NotesGraphError as e:
        print(f"⚠️  Could not update the conversation index: {e}")
