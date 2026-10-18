"""Tests for publishing finished conversations into NotesGraph.

The notes API is faked throughout; no test reaches the network. What
matters here is the document shape, and that republishing a session updates
the document it already has rather than spawning a second copy.
"""

import json

import pytest

from workflow.conversation_store import notesgraph as ng
from workflow.conversation_store.base import SessionMeta, Summary, Turn


class FakeClient:
    """Records documents instead of sending them."""

    def __init__(self, workspace="ws-1", fail_update=False, old_server=False):
        self.workspace = workspace
        self.docs = {}
        self.links = []
        self.creates = 0
        self.updates = 0
        self.fail_update = fail_update
        self.old_server = old_server

    def create_doc(self, title, markdown, folder=None):
        self.creates += 1
        doc_id = f"doc-{self.creates}"
        self.docs[doc_id] = {"title": title, "markdown": markdown,
                             "folder": None if self.old_server else folder}
        return doc_id

    def file_doc(self, doc_id, folder):
        if self.old_server:
            raise ng.NotesGraphError("404 Not Found")
        self.docs[doc_id]["folder"] = folder
        return True

    def rename_doc(self, doc_id, title):
        if self.old_server:
            raise ng.NotesGraphError("400 markdown is required")
        self.docs[doc_id]["title"] = title
        return True

    def update_doc(self, doc_id, markdown):
        if self.fail_update:
            raise ng.NotesGraphError("doc was deleted")
        self.updates += 1
        self.docs[doc_id]["markdown"] = markdown
        return True

    def link_doc(self, parent_id, child_id):
        if self.old_server:
            raise ng.NotesGraphError("404 Not Found")
        self.links.append((parent_id, child_id))
        return True

    def doc_titled(self, title):
        return next(i for i, d in self.docs.items() if d["title"] == title)

    def parent_of(self, doc_id):
        return [p for p, c in self.links if c == doc_id]


class FakeStore:
    def __init__(self, meta, turns, summaries=()):
        self.meta, self.turns, self.summaries = meta, turns, list(summaries)

    def list_sessions(self, task_key=None, limit=50):
        return [self.meta]

    def get_turns(self, session_id, limit=None, offset=0):
        return self.turns

    def get_summaries(self, session_id=None, task_key=None, level=None):
        return [s for s in self.summaries if level is None or s.level == level]


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    monkeypatch.setattr(ng, "LEDGER", tmp_path / "docs.json")
    return tmp_path / "docs.json"


def session(**overrides):
    base = dict(session_id="65c43237-b873-4e45-bbbb-111111111111",
                agent="claude-code", model="claude-opus-5",
                task_key="PAN-484", project="work", repo="/code/workflow")
    base.update(overrides)
    return SessionMeta(**base)


def turns():
    sid = "65c43237-b873-4e45-bbbb-111111111111"
    return [
        Turn(sid, 0, "system", "you are helpful"),
        Turn(sid, 1, "user", "why does wf cd fail?"),
        Turn(sid, 2, "assistant", "the path was relative"),
        Turn(sid, 3, "tool_call", '{"file":"cli.py"}', tool_name="Read"),
    ]


# ---------------------------------------------------------------------------
# document shape
# ---------------------------------------------------------------------------

def test_conversations_are_filed_in_real_folders_not_titled_with_the_path():
    assert ng.folder_path("work") == ["work", "ai", "conversations"]
    # a session captured outside any project still has somewhere to go
    assert ng.folder_path(None) == ["unscoped", "ai", "conversations"]
    meta = session(started_at=0)
    title = ng.doc_title(meta)
    assert " / " not in title
    assert title.endswith("PAN-484 · 65c43237")


def test_a_conversation_is_created_in_its_folder(ledger):
    store = FakeStore(session(), turns())
    client = FakeClient()
    doc_id = ng.publish_session(store.meta.session_id, store, cfg={}, client=client)
    assert client.docs[doc_id]["folder"] == ["work", "ai", "conversations"]
    assert " / " not in client.docs[doc_id]["title"]


def test_one_published_before_folders_is_filed_and_renamed(ledger):
    store = FakeStore(session(), turns())
    sid = store.meta.session_id
    client = FakeClient()
    client.docs["old-doc"] = {"title": "work / ai / conversations / 65c43237",
                              "markdown": "", "folder": None}
    ledger.write_text(json.dumps({"sessions": {sid: {"doc_id": "old-doc"}},
                                  "indexes": {}}))

    assert ng.publish_session(sid, store, cfg={}, client=client) == "old-doc"
    assert client.docs["old-doc"]["folder"] == ["work", "ai", "conversations"]
    assert client.docs["old-doc"]["title"] == ng.doc_title(store.meta)
    assert json.loads(ledger.read_text())["sessions"][sid]["filed"] is True


def test_an_older_server_leaves_it_unfiled_and_it_is_tried_again(ledger):
    store = FakeStore(session(), turns())
    sid = store.meta.session_id
    client = FakeClient(old_server=True)
    client.docs["old-doc"] = {"title": "work / ai / conversations / 65c43237",
                              "markdown": "", "folder": None}
    ledger.write_text(json.dumps({"sessions": {sid: {"doc_id": "old-doc"}},
                                  "indexes": {}}))

    assert ng.publish_session(sid, store, cfg={}, client=client) == "old-doc"
    assert json.loads(ledger.read_text())["sessions"][sid]["filed"] is False


def test_the_document_leads_with_the_ending(ledger):
    store = FakeStore(session(), turns(), [
        Summary(level="session", content="Fixed the relative path bug.",
                session_id="65c43237-b873-4e45-bbbb-111111111111"),
    ])
    client = FakeClient()
    ng.publish_session(store.meta.session_id, store, cfg={}, client=client)

    body = client.docs["doc-1"]["markdown"]
    # the outcome comes before the transcript: nobody wants to scroll a
    # hundred tool calls to find out how it went
    assert body.index("## Ending") < body.index("## Transcript")
    assert "Fixed the relative path bug." in body


def test_the_document_carries_provenance(ledger):
    store = FakeStore(session(), turns())
    client = FakeClient()
    ng.publish_session(store.meta.session_id, store, cfg={}, client=client)

    body = client.docs["doc-1"]["markdown"]
    for expected in ("PAN-484", "work", "/code/workflow", "claude-code", "claude-opus-5"):
        assert expected in body


def test_facts_are_rendered_when_present(ledger):
    sid = session().session_id
    store = FakeStore(session(), turns(), [
        Summary(level="facts", session_id=sid,
                content=json.dumps([{"type": "decision", "content": "Store absolute paths"}])),
    ])
    client = FakeClient()
    ng.publish_session(sid, store, cfg={}, client=client)

    body = client.docs["doc-1"]["markdown"]
    assert "## Durable facts" in body
    assert "Store absolute paths" in body


def test_tool_payloads_are_fenced_and_truncated(ledger):
    sid = session().session_id
    long_turns = [Turn(sid, 0, "tool_result", "x" * 5000, tool_name="Bash")]
    client = FakeClient()
    ng.publish_session(sid, FakeStore(session(), long_turns), cfg={}, client=client)

    body = client.docs["doc-1"]["markdown"]
    assert "```" in body
    assert "chars truncated" in body
    assert len(body) < 3000


# ---------------------------------------------------------------------------
# idempotence
# ---------------------------------------------------------------------------

def test_republishing_updates_rather_than_duplicating(ledger):
    """The notes API cannot look a doc up by title, so the ledger has to."""
    store = FakeStore(session(), turns())
    client = FakeClient()
    sid = store.meta.session_id

    first = ng.publish_session(sid, store, cfg={}, client=client)
    second = ng.publish_session(sid, store, cfg={}, client=client)

    assert first == second
    # one conversation doc + the work / ai / conversations notes, once each
    assert client.creates == 4
    assert client.updates >= 1


def test_a_deleted_document_is_recreated(ledger):
    store = FakeStore(session(), turns())
    sid = store.meta.session_id

    ng.publish_session(sid, store, cfg={}, client=FakeClient())
    # someone deleted it in the app; the conversation must not be lost
    recreating = FakeClient(fail_update=True)
    doc_id = ng.publish_session(sid, store, cfg={}, client=recreating)

    assert doc_id in recreating.docs


def test_conversations_nest_under_the_project_not_the_inbox(ledger):
    """The note tree follows links, not folders: an unlinked doc is an orphan."""
    client = FakeClient()
    sids = [f"aaaa{n}-b873-4e45-bbbb-111111111111" for n in (1, 2)]
    docs = [ng.publish_session(sid, FakeStore(session(session_id=sid), turns()),
                               cfg={}, client=client) for sid in sids]

    root, ai, index = (client.doc_titled(t) for t in ("work", "ai", "conversations"))
    assert client.parent_of(ai) == [root]
    assert client.parent_of(index) == [ai]
    assert all(client.parent_of(doc) == [index] for doc in docs)
    # only the project note is left without a parent
    assert client.parent_of(root) == []
    assert client.docs[index]["folder"] == ["work", "ai", "conversations"]


def test_republishing_does_not_link_twice(ledger):
    """Each link call appends another reference to the parent."""
    store = FakeStore(session(), turns())
    client = FakeClient()
    for _ in range(3):
        ng.publish_session(store.meta.session_id, store, cfg={}, client=client)

    assert len(client.links) == len(set(client.links)) == 3


def test_a_recreated_conversation_is_linked_again(ledger):
    store = FakeStore(session(), turns())
    sid = store.meta.session_id
    client = FakeClient()
    ng.publish_session(sid, store, cfg={}, client=client)

    client.fail_update = True
    new_doc = ng.publish_session(sid, store, cfg={}, client=client)
    assert client.parent_of(new_doc) == [client.doc_titled("conversations")]


def test_the_old_markdown_index_becomes_the_conversations_note(ledger):
    sid = session().session_id
    client = FakeClient()
    client.docs["old-index"] = {"title": "All conversations",
                                "markdown": "| When | Task |", "folder": None}
    client.docs["old-doc"] = {"title": "t", "markdown": "", "folder": None}
    ledger.write_text(json.dumps({
        "sessions": {
            sid: {"doc_id": "old-doc", "filed": True, "project": "work"},
            "other": {"doc_id": "older-doc", "filed": True, "project": "work"},
        },
        "indexes": {"work": "old-index"},
    }))

    ng.publish_session(sid, FakeStore(session(), turns()), cfg={}, client=client)

    assert client.docs["old-index"]["title"] == "conversations"
    assert "| When" not in client.docs["old-index"]["markdown"]
    # conversations published before the tree get linked too
    assert client.parent_of("older-doc") == ["old-index"]
    assert client.parent_of("old-doc") == ["old-index"]
    assert "work" not in json.loads(ledger.read_text())["indexes"]


def test_an_older_server_without_links_still_publishes_and_retries(ledger):
    store = FakeStore(session(), turns())
    sid = store.meta.session_id
    old = FakeClient(old_server=True)
    assert ng.publish_session(sid, store, cfg={}, client=old)
    assert json.loads(ledger.read_text())["sessions"][sid]["linked"] is False

    new = FakeClient()
    new.docs = old.docs
    ng.publish_session(sid, store, cfg={}, client=new)
    assert json.loads(ledger.read_text())["sessions"][sid]["linked"] is True


def test_an_empty_session_is_not_published(ledger):
    client = FakeClient()
    assert ng.publish_session(session().session_id,
                              FakeStore(session(), []), cfg={}, client=client) is None
    assert client.creates == 0


def test_an_unknown_session_is_not_published(ledger):
    client = FakeClient()
    store = FakeStore(session(), turns())
    assert ng.publish_session("not-a-session", store, cfg={}, client=client) is None


# ---------------------------------------------------------------------------
# configuration
# ---------------------------------------------------------------------------

def test_publishing_is_off_until_configured():
    assert not ng.is_enabled({})
    assert not ng.is_enabled({"proxy": {"capture": {"notesgraph": {"enabled": True}}}})
    assert ng.is_enabled({"proxy": {"capture": {"notesgraph": {
        "enabled": True, "workspace": "ws-1"}}}})


def test_a_corrupt_ledger_does_not_block_publishing(ledger):
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.write_text("{ not json")
    client = FakeClient()
    store = FakeStore(session(), turns())

    assert ng.publish_session(store.meta.session_id, store, cfg={}, client=client)


# ---------------------------------------------------------------------------
# document size
# ---------------------------------------------------------------------------

def test_a_long_session_is_elided_to_fit(ledger):
    """The server's body parser caps at 100KB; a real session blows past it.

    This reproduces the 413 that a 352-turn conversation produced against
    the live API.
    """
    sid = session().session_id
    many = [Turn(sid, i, "assistant", f"turn {i} " + "x" * 2000) for i in range(200)]
    client = FakeClient()

    ng.publish_session(sid, FakeStore(session(), many), cfg={}, client=client)
    body = client.docs["doc-1"]["markdown"]

    assert len(body.encode()) < ng.DEFAULT_MAX_DOC_BYTES
    assert "elided from the middle" in body
    # the opening and the close both survive; the middle is what goes
    assert "turn 0 " in body
    assert "turn 199 " in body
    assert "turn 100 " not in body


def test_the_ending_survives_elision(ledger):
    """Whatever else is dropped, the outcome is the point of the document."""
    sid = session().session_id
    many = [Turn(sid, i, "assistant", "y" * 3000) for i in range(200)]
    client = FakeClient()

    ng.publish_session(sid, FakeStore(session(), many, [
        Summary(level="session", content="THE OUTCOME", session_id=sid),
    ]), cfg={}, client=client)

    body = client.docs["doc-1"]["markdown"]
    assert "THE OUTCOME" in body
    assert "PAN-484" in body


def test_the_cap_is_configurable(ledger):
    sid = session().session_id
    many = [Turn(sid, i, "assistant", "z" * 500) for i in range(50)]
    client = FakeClient()
    cfg = {"proxy": {"capture": {"notesgraph": {"max_doc_bytes": 4000}}}}

    ng.publish_session(sid, FakeStore(session(), many), cfg=cfg, client=client)
    assert len(client.docs["doc-1"]["markdown"].encode()) < 4000


# ---------------------------------------------------------------------------
# markdown the server will accept
# ---------------------------------------------------------------------------

def test_tag_like_angle_brackets_are_escaped():
    """NotesGraph's markdown importer 500s on an HTML tag it can't parse.

    Agent transcripts are full of them, so anything outside a code fence has
    to go out escaped. Reproduced against the live API before this existed.
    """
    assert ng.escape_taglike("see <system-reminder> here") == "see &lt;system-reminder> here"
    assert ng.escape_taglike("Vec<T>") == "Vec&lt;T>"
    assert ng.escape_taglike("</close>") == "&lt;/close>"
    # a bare comparison is not a tag and stays readable
    assert ng.escape_taglike("5 < 6 and 7 > 3") == "5 < 6 and 7 > 3"


def test_prose_in_the_published_document_carries_no_raw_tags(ledger):
    sid = session().session_id
    turns_with_tags = [
        Turn(sid, 0, "user", "<system-reminder>noise</system-reminder> real question"),
        Turn(sid, 1, "assistant", "use Vec<String> here"),
    ]
    client = FakeClient()
    ng.publish_session(sid, FakeStore(session(), turns_with_tags, [
        Summary(level="session", content="Discussed <user_query> handling", session_id=sid),
    ]), cfg={}, client=client)

    body = client.docs["doc-1"]["markdown"]
    prose = "\n".join(
        line for line in body.splitlines() if not line.startswith("```")
    )
    assert "<system-reminder>" not in prose
    assert "<user_query>" not in prose
    assert "&lt;system-reminder>" in prose


def test_a_tool_payload_cannot_break_out_of_its_fence(ledger):
    """Content containing ``` would end the fence early and expose its tags."""
    sid = session().session_id
    nasty = [Turn(sid, 0, "tool_result", "```\n<script>x</script>\n```", tool_name="Bash")]
    client = FakeClient()
    ng.publish_session(sid, FakeStore(session(), nasty), cfg={}, client=client)

    body = client.docs["doc-1"]["markdown"]
    # exactly the fence we opened and closed, nothing the payload introduced
    assert body.count("```") == 2
