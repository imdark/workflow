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

    def __init__(self, workspace="ws-1", fail_update=False):
        self.workspace = workspace
        self.docs = {}
        self.creates = 0
        self.updates = 0
        self.fail_update = fail_update

    def create_doc(self, title, markdown):
        self.creates += 1
        doc_id = f"doc-{self.creates}"
        self.docs[doc_id] = {"title": title, "markdown": markdown}
        return doc_id

    def update_doc(self, doc_id, markdown):
        if self.fail_update:
            raise ng.NotesGraphError("doc was deleted")
        self.updates += 1
        self.docs[doc_id]["markdown"] = markdown
        return True


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

def test_the_title_is_the_breadcrumb_path():
    assert ng.doc_title("work", "65c43237-b873-4e45") == \
        "work / ai / conversations / 65c43237"
    assert ng.index_title("work") == "work / ai / conversations"
    # a session captured outside any project still has somewhere to go
    assert ng.doc_title(None, "abc123") == "unscoped / ai / conversations / abc123"


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
    # one conversation doc + one index doc, created once each
    assert client.creates == 2
    assert client.updates >= 2


def test_a_deleted_document_is_recreated(ledger):
    store = FakeStore(session(), turns())
    sid = store.meta.session_id

    ng.publish_session(sid, store, cfg={}, client=FakeClient())
    # someone deleted it in the app; the conversation must not be lost
    recreating = FakeClient(fail_update=True)
    doc_id = ng.publish_session(sid, store, cfg={}, client=recreating)

    assert doc_id in recreating.docs


def test_the_index_links_every_conversation_in_the_project(ledger):
    client = FakeClient()
    for n in (1, 2):
        sid = f"aaaa{n}-b873-4e45-bbbb-111111111111"
        ng.publish_session(sid, FakeStore(session(session_id=sid), turns()),
                           cfg={}, client=client)

    index = next(d for d in client.docs.values()
                 if d["title"] == "work / ai / conversations")
    assert "aaaa1" in index["markdown"] and "aaaa2" in index["markdown"]
    assert index["markdown"].count("/workspace/ws-1/") == 2


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
