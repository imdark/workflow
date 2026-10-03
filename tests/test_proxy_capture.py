"""Tests for the capturing model proxy and its conversation stores.

Everything runs against a fake upstream on localhost and temporary store
paths, so no test reaches the network or touches ~/.wf.
"""

import asyncio
import json

import pytest
from aiohttp import ClientSession, web

from workflow.conversation_store import build_store, reconcile
from workflow.conversation_store.base import SessionMeta, Summary, Turn
from workflow.proxy import protocols
from workflow.proxy.sanitize import clean_text, is_internal_prompt, render_transcript


# ---------------------------------------------------------------------------
# protocol normalization
# ---------------------------------------------------------------------------

def test_detect_protocol_ignores_agent_prefix():
    assert protocols.detect_protocol("/v1/messages") == protocols.ANTHROPIC
    assert protocols.detect_protocol("/claude-code/v1/messages") == protocols.ANTHROPIC
    assert protocols.detect_protocol("/v1/chat/completions") == protocols.OPENAI_CHAT
    assert protocols.detect_protocol("/v1/responses") == protocols.OPENAI_RESPONSES
    assert protocols.detect_protocol("/v1/models") is None


def test_anthropic_splits_mixed_blocks_in_wire_order():
    """Text must precede tool blocks, matching what the assembler emits.

    If the two disagree the de-duplication prefix breaks and every turn
    re-appends the tail of the conversation.
    """
    body = {
        "model": "claude-opus-5",
        "system": "be brief",
        "messages": [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": [
                {"type": "text", "text": "looking"},
                {"type": "tool_use", "id": "t1", "name": "Read", "input": {"f": "a.py"}},
            ]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "t1", "content": "contents"},
                {"type": "text", "text": "now fix it"},
            ]},
        ],
    }
    turns = protocols.normalize_request(body, protocols.ANTHROPIC, "s1").turns
    assert [(t.role, t.content) for t in turns] == [
        ("system", "be brief"),
        ("user", "hi"),
        ("assistant", "looking"),
        ("tool_call", '{"f": "a.py"}'),
        ("tool_result", "contents"),
        ("user", "now fix it"),
    ]


def test_openai_chat_tool_messages():
    body = {"messages": [
        {"role": "user", "content": "run tests"},
        {"role": "assistant", "content": "ok", "tool_calls": [
            {"id": "c1", "function": {"name": "bash", "arguments": '{"cmd":"pytest"}'}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "2 passed"},
    ]}
    turns = protocols.normalize_request(body, protocols.OPENAI_CHAT, "s1").turns
    assert [t.role for t in turns] == ["user", "assistant", "tool_call", "tool_result"]
    assert turns[2].tool_name == "bash"
    assert turns[3].tool_call_id == "c1"


def test_openai_responses_items():
    body = {"instructions": "be terse", "input": [
        {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hello"}]},
        {"type": "reasoning", "summary": []},
        {"type": "function_call", "call_id": "f1", "name": "grep", "arguments": '{"q":"x"}'},
        {"type": "function_call_output", "call_id": "f1", "output": "no matches"},
    ]}
    turns = protocols.normalize_request(body, protocols.OPENAI_RESPONSES, "s1").turns
    assert [t.role for t in turns] == ["system", "user", "tool_call", "tool_result"]


def test_assembler_rebuilds_anthropic_stream():
    a = protocols.ResponseAssembler(protocols.ANTHROPIC, "s1")
    events = [
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "he"}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "llo"}},
        {"type": "content_block_start", "index": 1,
         "content_block": {"type": "tool_use", "id": "t9", "name": "Edit"}},
        {"type": "content_block_delta", "index": 1,
         "delta": {"type": "input_json_delta", "partial_json": '{"a":1}'}},
    ]
    for e in events:
        a.feed(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n".encode())
    a.finish()
    turns = a.turns()
    assert turns[0].role == "assistant" and turns[0].content == "hello"
    assert turns[1].role == "tool_call" and turns[1].tool_name == "Edit"


def test_assembler_handles_split_sse_frames():
    """A chunk boundary mid-line must not lose the event."""
    a = protocols.ResponseAssembler(protocols.ANTHROPIC, "s1")
    event = json.dumps({"type": "content_block_delta", "index": 0,
                        "delta": {"type": "text_delta", "text": "split"}})
    payload = f"data: {event}\n\n".encode()
    a.feed(payload[:12])
    a.feed(payload[12:])
    a.finish()
    assert a.turns()[0].content == "split"


def test_assembler_handles_non_streaming_json():
    a = protocols.ResponseAssembler(protocols.OPENAI_CHAT, "s1")
    a.feed(json.dumps({"choices": [{"message": {"content": "buffered reply"}}]}).encode())
    a.finish()
    assert a.turns()[0].content == "buffered reply"


# ---------------------------------------------------------------------------
# de-duplication
# ---------------------------------------------------------------------------

def test_fingerprint_canonicalizes_tool_json():
    """Streamed and echoed spellings of one tool call must hash alike."""
    streamed = Turn("s", 0, "tool_call", '{"file":"a.py","n":1}', tool_name="Read", tool_call_id="t1")
    echoed = Turn("s", 0, "tool_call", '{"n": 1, "file": "a.py"}', tool_name="Read", tool_call_id="t1")
    assert streamed.fingerprint() == echoed.fingerprint()


def test_fingerprint_ignores_timestamp_and_seq():
    a = Turn("s", 0, "user", "same", ts=1.0)
    b = Turn("s", 7, "user", "same", ts=999.0)
    assert a.fingerprint() == b.fingerprint()


def test_reconcile_returns_only_the_tail():
    history = [Turn("s", 0, "user", "one"), Turn("s", 0, "assistant", "two")]
    stored = [t.fingerprint() for t in history]
    incoming = history + [Turn("s", 0, "user", "three")]
    assert [t.content for t in reconcile(stored, incoming)] == ["three"]


def test_reconcile_treats_divergence_as_new():
    """After a client-side compaction the prefix no longer matches."""
    stored = [Turn("s", 0, "user", "one").fingerprint(),
              Turn("s", 0, "assistant", "two").fingerprint()]
    incoming = [Turn("s", 0, "user", "one"), Turn("s", 0, "assistant", "COMPACTED"),
                Turn("s", 0, "user", "next")]
    assert [t.content for t in reconcile(stored, incoming)] == ["COMPACTED", "next"]


# ---------------------------------------------------------------------------
# stores
# ---------------------------------------------------------------------------

@pytest.fixture(params=["sqlite", "jsonl", "markdown"])
def store(request, tmp_path):
    options = {"sqlite": {"path": str(tmp_path / "c.db")},
               "jsonl": {"dir": str(tmp_path / "jsonl")},
               "markdown": {"dir": str(tmp_path / "md")}}[request.param]
    s = build_store(request.param, options)
    yield s
    s.close()


def test_store_roundtrip(store):
    meta = SessionMeta(session_id="s1", agent="claude-code", model="claude-opus-5",
                       task_key="PAN-1", project="work", repo="/tmp/r")
    store.ensure_session(meta)
    turns = [Turn("s1", 0, "user", "how do I run tests"),
             Turn("s1", 0, "assistant", "use pytest")]
    assert store.append_turns("s1", turns) == 2

    stored = store.get_turns("s1")
    assert [t.content for t in stored] == ["how do I run tests", "use pytest"]
    assert store.fingerprints("s1") == [t.fingerprint() for t in turns]

    sessions = store.list_sessions()
    assert any(m.session_id == "s1" for m in sessions)
    assert store.search("pytest")


def test_store_append_is_incremental(store):
    store.ensure_session(SessionMeta(session_id="s1", task_key="PAN-1"))
    first = [Turn("s1", 0, "user", "a"), Turn("s1", 0, "assistant", "b")]
    store.append_turns("s1", first)

    incoming = first + [Turn("s1", 0, "user", "c")]
    new = reconcile(store.fingerprints("s1"), incoming)
    store.append_turns("s1", new)

    assert [t.content for t in store.get_turns("s1")] == ["a", "b", "c"]


def test_store_summaries(store):
    store.ensure_session(SessionMeta(session_id="s1", task_key="PAN-1"))
    store.append_turns("s1", [Turn("s1", 0, "user", "x")])
    store.save_summary(Summary(level="session", content="did a thing",
                               session_id="s1", task_key="PAN-1"))
    if store.name == "markdown":
        # The markdown store writes session summaries into the session file
        # itself; only task-level rollups are queryable as documents.
        store.save_summary(Summary(level="rollup", content="task state", task_key="PAN-1"))
        assert store.get_summaries(task_key="PAN-1", level="rollup")
    else:
        found = store.get_summaries(session_id="s1", level="session")
        assert found and found[0].content == "did a thing"


def test_unknown_store_is_rejected():
    with pytest.raises(ValueError, match="Unknown conversation store"):
        build_store("redis", {})


# ---------------------------------------------------------------------------
# sanitization (summarizer input only -- never what is stored)
# ---------------------------------------------------------------------------

def test_clean_text_strips_harness_wrappers():
    raw = "<system-reminder>ignore me</system-reminder>\nreal question"
    assert clean_text(raw) == "real question"


def test_clean_text_strips_our_own_injections():
    raw = "<wf-memory>previous summary</wf-memory>\nactual text"
    assert clean_text(raw) == "actual text"


def test_internal_prompts_are_recognized():
    assert is_internal_prompt("[SUGGESTION MODE: pick one]")
    assert is_internal_prompt("The user stepped away and is coming back. Recap")
    assert not is_internal_prompt("why does wf cd fail?")


def test_render_transcript_truncates_tool_noise():
    turns = [Turn("s", 0, "user", "go"),
             Turn("s", 1, "tool_result", "x" * 5000, tool_name="Bash")]
    out = render_transcript(turns)
    assert "truncated" in out
    assert len(out) < 2000


# ---------------------------------------------------------------------------
# end-to-end through the proxy
# ---------------------------------------------------------------------------

async def _fake_upstream(request):
    body = await request.json()
    assert body.get("messages"), "upstream received no messages"
    response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
    await response.prepare(request)
    for event in [
        {"type": "content_block_delta", "index": 0,
         "delta": {"type": "text_delta", "text": "the answer"}},
        {"type": "message_stop"},
    ]:
        await response.write(f"data: {json.dumps(event)}\n\n".encode())
    await response.write_eof()
    return response


async def _run_proxy_capture(tmp_path):
    upstream = web.Application()
    upstream.router.add_post("/v1/messages", _fake_upstream)
    up_runner = web.AppRunner(upstream)
    await up_runner.setup()
    await web.TCPSite(up_runner, "127.0.0.1", 18321).start()

    from workflow.proxy.server import CaptureProxy
    proxy = CaptureProxy({
        "proxy": {
            "host": "127.0.0.1", "port": 18320,
            "upstream": {"anthropic": "http://127.0.0.1:18321",
                         "openai": "http://127.0.0.1:18321"},
            "ladder": {"enabled": False},
            "capture": {"store": "sqlite", "sqlite": {"path": str(tmp_path / "c.db")}},
        },
    })
    px_runner = web.AppRunner(proxy.build_app())
    await px_runner.setup()
    await web.TCPSite(px_runner, "127.0.0.1", 18320).start()

    try:
        async with ClientSession() as client:
            async with client.post(
                "http://127.0.0.1:18320/v1/messages",
                json={"model": "m", "stream": True,
                      "messages": [{"role": "user", "content": "question"}]},
                headers={"x-session-id": "sess-1", "x-api-key": "sk-do-not-store"},
            ) as response:
                streamed = await response.text()
        await asyncio.sleep(0.3)
        return streamed, proxy.store.get_turns("sess-1")
    finally:
        await px_runner.cleanup()
        await up_runner.cleanup()


def test_proxy_forwards_and_captures(tmp_path):
    streamed, turns = asyncio.run(_run_proxy_capture(tmp_path))

    # the client still sees the upstream stream
    assert "the answer" in streamed
    # and both sides of the exchange were recorded
    assert [(t.role, t.content) for t in turns] == [
        ("user", "question"), ("assistant", "the answer"),
    ]
    # credentials never reach the store
    assert "sk-do-not-store" not in json.dumps([t.to_dict() for t in turns])


# ---------------------------------------------------------------------------
# regressions from real captured traffic
# ---------------------------------------------------------------------------

def test_a_volatile_system_prompt_does_not_reappend_the_history():
    """The failure that produced 16,901 turns from 352 distinct ones.

    Claude Code stamps a per-request build id into its system prompt, so
    position 0 differs on every request. Prefix-based reconciliation matched
    nothing and re-appended the whole conversation each turn.
    """
    history = [Turn("s", 0, "user", "one"), Turn("s", 0, "assistant", "two")]
    stored = [t.fingerprint() for t in history]

    incoming = [Turn("s", 0, "system", "cc_version=2.1.270.6b8")] + history
    fresh = reconcile(stored, incoming)

    # only the changed system prompt is new; the conversation is not resent
    assert [t.content for t in fresh] == ["cc_version=2.1.270.6b8"]


def test_reconcile_keeps_a_genuinely_repeated_message():
    """Counting occurrences, not membership: 'do it' typed twice is two turns."""
    stored = [Turn("s", 0, "user", "do it").fingerprint()]
    incoming = [Turn("s", 0, "user", "do it"), Turn("s", 0, "assistant", "done"),
                Turn("s", 0, "user", "do it")]

    fresh = reconcile(stored, incoming)
    assert [t.content for t in fresh] == ["done", "do it"]


def test_reconcile_is_stable_when_nothing_changed():
    """A retried request must add nothing at all."""
    history = [Turn("s", 0, "system", "sys"), Turn("s", 0, "user", "hi"),
               Turn("s", 0, "assistant", "hello")]
    stored = [t.fingerprint() for t in history]
    assert reconcile(stored, history) == []


def test_reconcile_survives_a_reordered_history():
    """Alignment must not depend on position.

    A client that compacts or reorders its history should still only
    contribute what is actually new.
    """
    history = [Turn("s", 0, "user", "a"), Turn("s", 0, "assistant", "b")]
    stored = [t.fingerprint() for t in history]
    incoming = list(reversed(history)) + [Turn("s", 0, "user", "c")]

    assert [t.content for t in reconcile(stored, incoming)] == ["c"]


async def _run_volatile_system_capture(tmp_path):
    """Two requests whose system prompt differs, as a real client's does."""
    upstream = web.Application()
    upstream.router.add_post("/v1/messages", _fake_upstream)
    up_runner = web.AppRunner(upstream)
    await up_runner.setup()
    await web.TCPSite(up_runner, "127.0.0.1", 18331).start()

    from workflow.proxy.server import CaptureProxy
    proxy = CaptureProxy({
        "proxy": {
            "host": "127.0.0.1", "port": 18330,
            "upstream": {"anthropic": "http://127.0.0.1:18331",
                         "openai": "http://127.0.0.1:18331"},
            "ladder": {"enabled": False},
            "capture": {"store": "sqlite", "sqlite": {"path": str(tmp_path / "c.db")}},
        },
    })
    px_runner = web.AppRunner(proxy.build_app())
    await px_runner.setup()
    await web.TCPSite(px_runner, "127.0.0.1", 18330).start()

    try:
        async with ClientSession() as client:
            for build in ("f64", "6b8", "a11"):
                body = {
                    "model": "m", "stream": True,
                    "system": f"x-anthropic-billing-header: cc_version=2.1.270.{build}",
                    "messages": [{"role": "user", "content": "question"}],
                }
                async with client.post("http://127.0.0.1:18330/v1/messages", json=body,
                                       headers={"x-session-id": "volatile"}) as r:
                    await r.text()
                await asyncio.sleep(0.2)
        await asyncio.sleep(0.3)
        return proxy.store.get_turns("volatile")
    finally:
        await px_runner.cleanup()
        await up_runner.cleanup()


def test_three_requests_do_not_multiply_the_transcript(tmp_path):
    turns = asyncio.run(_run_volatile_system_capture(tmp_path))
    roles = [t.role for t in turns]

    # one system prompt, kept once despite changing on every request
    assert roles.count("system") == 1
    # the user asked once; it must not be stored three times
    assert [t.content for t in turns if t.role == "user"] == ["question"]
    # the assistant replied three times with identical text -- all three are
    # real turns, so they are all kept
    assert roles.count("assistant") == 3
    assert len(turns) == 5
