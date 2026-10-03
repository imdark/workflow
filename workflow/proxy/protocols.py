"""Normalize the three agent wire protocols into one turn shape.

Supported:
  anthropic         POST /v1/messages            (Claude Code)
  openai-chat       POST /v1/chat/completions    (opencode, flow, most SDKs)
  openai-responses  POST /v1/responses           (Codex CLI)

Both directions are handled: `normalize_request` turns a request body into
turns, and `ResponseAssembler` rebuilds the assistant turn from a streamed
or buffered response.

Content is carried through verbatim. Harness noise is stripped later, and
only on the copy fed to the summarizer -- the stored transcript stays a
faithful record of what actually crossed the wire.
"""

import json
import time
from dataclasses import dataclass, field
from typing import Optional

from workflow.conversation_store.base import Turn

ANTHROPIC = "anthropic"
OPENAI_CHAT = "openai-chat"
OPENAI_RESPONSES = "openai-responses"

PROTOCOLS = (ANTHROPIC, OPENAI_CHAT, OPENAI_RESPONSES)


def detect_protocol(path: str) -> Optional[str]:
    """Map a request path to a protocol, ignoring any agent/space prefix."""
    p = path.rstrip("/")
    if p.endswith("/v1/messages") or p.endswith("/messages"):
        return ANTHROPIC
    if p.endswith("/chat/completions"):
        return OPENAI_CHAT
    if p.endswith("/v1/responses") or p.endswith("/responses"):
        return OPENAI_RESPONSES
    return None


@dataclass
class NormalizedRequest:
    protocol: str
    model: str
    turns: list = field(default_factory=list)
    stream: bool = False


# ── request side ─────────────────────────────────────────────────────────────

def normalize_request(body: dict, protocol: str, session_id: str = "") -> NormalizedRequest:
    model = str(body.get("model") or "")
    stream = bool(body.get("stream"))
    if protocol == ANTHROPIC:
        turns = _anthropic_turns(body, session_id)
    elif protocol == OPENAI_CHAT:
        turns = _openai_chat_turns(body, session_id)
    elif protocol == OPENAI_RESPONSES:
        turns = _openai_responses_turns(body, session_id)
    else:
        turns = []
    return NormalizedRequest(protocol=protocol, model=model, turns=turns, stream=stream)


def _turn(session_id, role, content, tool_name=None, tool_call_id=None, raw=None) -> Turn:
    # seq is assigned by the store at append time; 0 is a placeholder.
    return Turn(
        session_id=session_id, seq=0, role=role, content=content,
        ts=time.time(), tool_name=tool_name, tool_call_id=tool_call_id, raw=raw,
    )


def _anthropic_turns(body: dict, session_id: str) -> list:
    turns = []
    system = body.get("system")
    if system:
        turns.append(_turn(session_id, "system", _anthropic_text(system)))

    for message in body.get("messages") or []:
        role = message.get("role", "user")
        content = message.get("content")

        if isinstance(content, str):
            if content.strip():
                turns.append(_turn(session_id, role, content))
            continue

        # Block arrays mix text with tool traffic. Consecutive text blocks
        # coalesce into one turn; every other block becomes its own turn, in
        # wire order.
        #
        # Order matters and is not cosmetic. The same assistant message
        # reaches the store twice -- once assembled from the response stream,
        # once echoed back in the next request's history -- and the two must
        # normalize identically or the de-duplication prefix breaks and the
        # whole tail of the conversation is re-appended every turn. The
        # assembler emits text before tool calls, so this must too, which
        # means flushing pending text before each non-text block rather than
        # deferring it to the end of the message.
        text_parts = []

        def flush_text():
            joined = "\n".join(p for p in text_parts if p)
            text_parts.clear()
            if joined.strip():
                turns.append(_turn(session_id, role, joined))

        for block in content or []:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "text":
                text_parts.append(block.get("text", ""))
                continue

            flush_text()
            if btype == "thinking":
                # Reasoning is kept, tagged, so a transcript can show it
                # without it being mistaken for the model's actual reply.
                thought = block.get("thinking") or block.get("text") or ""
                if thought.strip():
                    turns.append(_turn(session_id, "assistant", thought, tool_name="thinking"))
            elif btype == "tool_use":
                turns.append(_turn(
                    session_id, "tool_call",
                    _stringify(block.get("input")),
                    tool_name=block.get("name"), tool_call_id=block.get("id"), raw=block,
                ))
            elif btype == "tool_result":
                turns.append(_turn(
                    session_id, "tool_result",
                    _anthropic_text(block.get("content")),
                    tool_call_id=block.get("tool_use_id"), raw=block,
                ))
            elif btype == "image":
                turns.append(_turn(session_id, role, "[image]", tool_name="image"))

        flush_text()
    return turns


def _openai_chat_turns(body: dict, session_id: str) -> list:
    turns = []
    for message in body.get("messages") or []:
        role = message.get("role", "user")
        content = message.get("content")
        text = _openai_text(content)

        if role == "tool":
            turns.append(_turn(
                session_id, "tool_result", text,
                tool_call_id=message.get("tool_call_id"),
                tool_name=message.get("name"), raw=message,
            ))
            continue

        if text.strip():
            turns.append(_turn(session_id, role, text))

        for call in message.get("tool_calls") or []:
            fn = call.get("function") or {}
            turns.append(_turn(
                session_id, "tool_call", _stringify(fn.get("arguments")),
                tool_name=fn.get("name"), tool_call_id=call.get("id"), raw=call,
            ))
    return turns


def _openai_responses_turns(body: dict, session_id: str) -> list:
    turns = []
    instructions = body.get("instructions")
    if instructions:
        turns.append(_turn(session_id, "system", _stringify(instructions)))

    # The Responses API uses `input` rather than `messages`, and it may be a
    # bare string for a single-shot call.
    items = body.get("input")
    if isinstance(items, str):
        return turns + [_turn(session_id, "user", items)] if items.strip() else turns

    for item in items or []:
        if not isinstance(item, dict):
            continue
        itype = item.get("type", "message")
        if itype == "message":
            role = item.get("role", "user")
            if role == "developer":
                role = "system"
            text = _responses_text(item.get("content"))
            if text.strip():
                turns.append(_turn(session_id, role, text))
        elif itype == "function_call":
            turns.append(_turn(
                session_id, "tool_call", _stringify(item.get("arguments")),
                tool_name=item.get("name"), tool_call_id=item.get("call_id"), raw=item,
            ))
        elif itype == "function_call_output":
            turns.append(_turn(
                session_id, "tool_result", _stringify(item.get("output")),
                tool_call_id=item.get("call_id"), raw=item,
            ))
        elif itype == "reasoning":
            summary = _responses_text(item.get("summary"))
            if summary.strip():
                turns.append(_turn(session_id, "assistant", summary, tool_name="thinking"))
    return turns


# ── response side ────────────────────────────────────────────────────────────

class ResponseAssembler:
    """Rebuild the assistant's reply from a streamed or buffered response.

    Feed it raw body bytes as they arrive (`feed`), then call `turns()`.
    Handles SSE for all three protocols plus the non-streaming JSON shape.
    """

    def __init__(self, protocol: str, session_id: str):
        self.protocol = protocol
        self.session_id = session_id
        self._buffer = b""
        self._text_parts = []
        self._tool_calls = {}   # index/id -> {"name": str, "args": [str], "id": str}
        self._sse = False
        self._raw_chunks = []

    def feed(self, chunk: bytes) -> None:
        if not chunk:
            return
        self._buffer += chunk
        # SSE frames are newline-delimited; process whole lines and keep the
        # remainder for the next chunk.
        while b"\n" in self._buffer:
            line, self._buffer = self._buffer.split(b"\n", 1)
            self._consume_line(line.decode("utf-8", "replace").strip())

    def finish(self) -> None:
        """Flush a trailing partial line and any non-SSE JSON body."""
        tail = self._buffer.decode("utf-8", "replace").strip()
        self._buffer = b""
        if tail:
            self._consume_line(tail)
        if not self._sse and self._raw_chunks:
            self._consume_json_body("".join(self._raw_chunks))

    def _consume_line(self, line: str) -> None:
        if not line:
            return
        if line.startswith("data:"):
            self._sse = True
            payload = line[5:].strip()
            if payload and payload != "[DONE]":
                try:
                    self._consume_event(json.loads(payload))
                except json.JSONDecodeError:
                    pass
            return
        if line.startswith("event:") or line.startswith(":"):
            self._sse = True
            return
        # Not SSE: accumulate for a single JSON parse at finish().
        self._raw_chunks.append(line)

    def _consume_event(self, event: dict) -> None:
        if self.protocol == ANTHROPIC:
            self._anthropic_event(event)
        elif self.protocol == OPENAI_CHAT:
            self._openai_chat_event(event)
        else:
            self._openai_responses_event(event)

    def _anthropic_event(self, event: dict) -> None:
        etype = event.get("type")
        if etype == "content_block_start":
            block = event.get("content_block") or {}
            if block.get("type") == "tool_use":
                self._tool_calls[event.get("index", 0)] = {
                    "name": block.get("name"), "id": block.get("id"), "args": [],
                }
        elif etype == "content_block_delta":
            delta = event.get("delta") or {}
            if delta.get("type") == "text_delta":
                self._text_parts.append(delta.get("text", ""))
            elif delta.get("type") == "thinking_delta":
                self._text_parts.append(delta.get("thinking", ""))
            elif delta.get("type") == "input_json_delta":
                call = self._tool_calls.get(event.get("index", 0))
                if call is not None:
                    call["args"].append(delta.get("partial_json", ""))

    def _openai_chat_event(self, event: dict) -> None:
        for choice in event.get("choices") or []:
            delta = choice.get("delta") or {}
            if delta.get("content"):
                self._text_parts.append(delta["content"])
            for call in delta.get("tool_calls") or []:
                idx = call.get("index", 0)
                slot = self._tool_calls.setdefault(idx, {"name": None, "id": None, "args": []})
                if call.get("id"):
                    slot["id"] = call["id"]
                fn = call.get("function") or {}
                if fn.get("name"):
                    slot["name"] = fn["name"]
                if fn.get("arguments"):
                    slot["args"].append(fn["arguments"])

    def _openai_responses_event(self, event: dict) -> None:
        etype = event.get("type", "")
        if etype.endswith("output_text.delta"):
            self._text_parts.append(event.get("delta", "") or "")
        elif etype.endswith("function_call_arguments.delta"):
            idx = event.get("output_index", 0)
            slot = self._tool_calls.setdefault(idx, {"name": None, "id": None, "args": []})
            slot["args"].append(event.get("delta", "") or "")
        elif etype == "response.output_item.added":
            item = event.get("item") or {}
            if item.get("type") == "function_call":
                self._tool_calls[event.get("output_index", 0)] = {
                    "name": item.get("name"), "id": item.get("call_id"), "args": [],
                }

    def _consume_json_body(self, body: str) -> None:
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            return
        if self.protocol == ANTHROPIC:
            for block in data.get("content") or []:
                if block.get("type") == "text":
                    self._text_parts.append(block.get("text", ""))
                elif block.get("type") == "tool_use":
                    self._tool_calls[len(self._tool_calls)] = {
                        "name": block.get("name"), "id": block.get("id"),
                        "args": [_stringify(block.get("input"))],
                    }
        elif self.protocol == OPENAI_CHAT:
            for choice in data.get("choices") or []:
                message = choice.get("message") or {}
                if message.get("content"):
                    self._text_parts.append(_openai_text(message["content"]))
                for call in message.get("tool_calls") or []:
                    fn = call.get("function") or {}
                    self._tool_calls[len(self._tool_calls)] = {
                        "name": fn.get("name"), "id": call.get("id"),
                        "args": [_stringify(fn.get("arguments"))],
                    }
        else:
            for item in data.get("output") or []:
                if item.get("type") == "message":
                    self._text_parts.append(_responses_text(item.get("content")))
                elif item.get("type") == "function_call":
                    self._tool_calls[len(self._tool_calls)] = {
                        "name": item.get("name"), "id": item.get("call_id"),
                        "args": [_stringify(item.get("arguments"))],
                    }

    def turns(self) -> list:
        out = []
        text = "".join(self._text_parts)
        if text.strip():
            out.append(_turn(self.session_id, "assistant", text))
        for call in self._tool_calls.values():
            out.append(_turn(
                self.session_id, "tool_call", "".join(call["args"]),
                tool_name=call.get("name"), tool_call_id=call.get("id"),
            ))
        return out


# ── shared text extraction ───────────────────────────────────────────────────

def _anthropic_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                parts.append(block.get("text") or block.get("content") or "")
        return "\n".join(p for p in parts if isinstance(p, str) and p)
    return _stringify(content)


def _openai_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            part.get("text", "") for part in content
            if isinstance(part, dict) and part.get("type") in (None, "text", "input_text", "output_text")
        )
    return _stringify(content)


def _responses_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            part.get("text", "") for part in content
            if isinstance(part, dict) and part.get("text")
        )
    return _stringify(content)


def _stringify(value) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(value)
