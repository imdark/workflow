"""Read notes out of NotesGraph from the runner's own code.

The notes REST API (`conversation_store.notesgraph`) writes documents but has
no call that reads one back. NotesGraph's MCP endpoint does: it is a plain
stateless JSON-RPC POST, authenticated with the same Personal Access Token
the runner already holds, so the runner can search for a note by title and
read its markdown without an MCP client library.

Used where a run needs something the user wrote down before it starts: the
"Agent rules" note (`jobs.load_agent_rules`) and the "Skill: …" notes that
`wf skill from-notes` turns into SKILL.md files.
"""

import json
import re
import urllib.error
import urllib.request
from typing import Optional

from workflow.deploy.registry.notesgraph import NotesGraphError, load_token

DEFAULT_URL = "https://app.notesgraph.com"

# Every write through the MCP server leaves this trailer on the document.
# It is provenance for a person reading the note, not part of what the note
# says, so it is dropped from text handed to an agent or written to a file.
_MCP_STAMP = re.compile(r"\n*>\s*Edited via MCP by [^\n]*(\n>\s*)*\s*$")


def strip_mcp_stamp(markdown: str) -> str:
    """The note's text without the "Edited via MCP by …" trailer."""
    return _MCP_STAMP.sub("", markdown or "").rstrip()


def _json_objects(text: str) -> list:
    """Every JSON value in `text`, which NotesGraph's search tools return as
    one object per line, or run together, or as one array."""
    text = (text or "").strip()
    if not text:
        return []
    try:
        whole = json.loads(text)
        return whole if isinstance(whole, list) else [whole]
    except json.JSONDecodeError:
        pass
    decoder = json.JSONDecoder()
    found, at = [], 0
    while at < len(text):
        if text[at].isspace():
            at += 1
            continue
        try:
            value, at = decoder.raw_decode(text, at)
        except json.JSONDecodeError:
            break
        found.append(value)
    return found


def _same_title(a: str, b: str) -> bool:
    return " ".join((a or "").split()).casefold() == " ".join((b or "").split()).casefold()


class NotesReader:
    """Search and read a workspace's notes over its MCP endpoint."""

    def __init__(self, url: str = DEFAULT_URL, workspace: str = "",
                 token: Optional[str] = None, timeout: float = 20.0):
        if not workspace:
            raise ValueError("reading notes needs a workspace id (deploy.notesgraph.workspace)")
        self.url = (url or DEFAULT_URL).rstrip("/")
        self.workspace = workspace
        self.token = token or load_token()
        self.timeout = timeout
        self._next_id = 0

    @property
    def endpoint(self) -> str:
        return f"{self.url}/api/workspaces/{self.workspace}/mcp"

    def call(self, tool: str, arguments: Optional[dict] = None) -> str:
        """Call one MCP tool; returns the text it answered with."""
        if not self.token:
            raise NotesGraphError(
                "No NotesGraph token. Run 'wf deploy notes-login <token>' first.")
        self._next_id += 1
        body = json.dumps({"jsonrpc": "2.0", "id": self._next_id, "method": "tools/call",
                           "params": {"name": tool, "arguments": arguments or {}}}).encode("utf-8")
        request = urllib.request.Request(
            self.endpoint, data=body, method="POST",
            headers={"Authorization": f"Bearer {self.token}",
                     "Content-Type": "application/json", "Accept": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8")[:300]
            except Exception:
                pass
            raise NotesGraphError(f"NotesGraph {tool} failed: {e.code} {detail}") from e
        except (urllib.error.URLError, OSError) as e:
            raise NotesGraphError(f"NotesGraph unreachable at {self.url}: {e}") from e
        try:
            reply = json.loads(raw)
        except json.JSONDecodeError as e:
            if raw.lstrip()[:1] == "<":
                raise NotesGraphError(
                    "the MCP endpoint returned a web page, not JSON — it is not "
                    "available on this server.") from e
            raise NotesGraphError(f"NotesGraph {tool} returned invalid JSON: {e}") from e
        if reply.get("error"):
            raise NotesGraphError(f"NotesGraph {tool} failed: {reply['error'].get('message')}")
        result = reply.get("result") or {}
        if result.get("isError"):
            raise NotesGraphError(f"NotesGraph {tool} failed: {_text_of(result)[:300]}")
        return _text_of(result)

    def search(self, query: str) -> list:
        """Notes matching `query` by title or text: [{docId, title, …}]."""
        return [hit for hit in _json_objects(self.call("keyword_search", {"query": query}))
                if isinstance(hit, dict) and hit.get("docId")]

    def find(self, title: str) -> Optional[dict]:
        """The note titled exactly `title` (case and spacing aside), or None."""
        for hit in self.search(" ".join(title.split())):
            if _same_title(hit.get("title", ""), title):
                return hit
        return None

    def read(self, doc_id: str) -> str:
        """A note's markdown, without the MCP edit stamp."""
        return strip_mcp_stamp(self.call("read_document", {"docId": doc_id}))


def _text_of(result: dict) -> str:
    return "".join(item.get("text", "") for item in result.get("content") or []
                   if isinstance(item, dict) and item.get("type") == "text")


def reader_from_config(cfg: dict) -> NotesReader:
    """A reader for the workspace in `deploy.notesgraph`, as the runner uses."""
    ng = (cfg.get("deploy") or {}).get("notesgraph") or {}
    if not ng.get("workspace"):
        raise NotesGraphError(
            "No NotesGraph workspace configured. Set deploy.notesgraph.workspace, "
            "then 'wf deploy notes-login <token>'.")
    return NotesReader(url=ng.get("url") or DEFAULT_URL, workspace=ng["workspace"])
