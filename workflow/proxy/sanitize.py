"""Strip harness scaffolding from turns before they are summarized.

This never touches what is stored. The transcript keeps every byte that
crossed the wire; these helpers produce a cleaned copy for the summarizer,
so extracted facts are about the work rather than about the tooling that
wrapped it.
"""

import re

# Wrapper tags coding agents inject around their own context.
WRAPPERS = (
    "system-reminder", "additional_data", "user_info", "question_answer",
    "local-command-stdout", "local-command-stderr", "persisted-output",
    "tool_use_error", "open_and_recently_viewed_files", "environment_details",
)

_WRAPPER_RE = re.compile(
    r"<(" + "|".join(WRAPPERS) + r")\b[^>]*>.*?</\1>",
    re.DOTALL | re.IGNORECASE,
)

# Whole messages that are the harness talking to itself, not the user.
_INTERNAL_PROMPT_RES = (
    re.compile(r"^\s*\[(?:SUGGESTION|TITLE|SUMMARY|COMPACT|RECAP|ANALYSIS)\s+MODE[:\s]", re.I),
    re.compile(r"^\s*The user stepped away and is coming back\.\s*Recap", re.I),
    re.compile(r"^\s*Your questions have been answered:\s*\"", re.I),
    re.compile(r"^\s*\{\"parentUuid\"", re.I),
    re.compile(r"^\s*\[\d{4}-\d{2}-\d{2}T[\d:]+[^\]]*\]\[(?:user|assistant|system)\]"),
)

_DATA_URI_RE = re.compile(r"data:[a-z]+/[a-z0-9.+-]+;base64,[A-Za-z0-9+/=]+", re.I)
_OUR_INJECTION_RE = re.compile(
    r"<(wf-memory|wf-summary|wf-facts)\b[^>]*>.*?</\1>", re.DOTALL | re.IGNORECASE
)


def is_internal_prompt(text: str) -> bool:
    """True when a whole message is agent scaffolding rather than a person."""
    stripped = (text or "").strip()
    if not stripped:
        return True
    return any(rx.search(stripped) for rx in _INTERNAL_PROMPT_RES)


def clean_text(text: str) -> str:
    """Remove wrappers, our own injected blocks, and inline base64 blobs."""
    if not text:
        return ""
    cleaned = _OUR_INJECTION_RE.sub("", text)  # first: never re-learn our own output
    cleaned = _WRAPPER_RE.sub("", cleaned)
    cleaned = _DATA_URI_RE.sub("[data-uri]", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def clean_turns(turns, drop_tools: bool = False, max_tool_chars: int = 800) -> list:
    """Return (role, content) pairs suitable for feeding a summarizer.

    Tool results are the bulk of a coding session's tokens and the least
    durable part of it, so they are truncated rather than dropped: the fact
    that a command ran and roughly what it said is usually the useful bit.
    """
    out = []
    for turn in turns:
        if turn.role in ("tool_call", "tool_result"):
            if drop_tools:
                continue
            body = clean_text(turn.content)
            if len(body) > max_tool_chars:
                body = body[:max_tool_chars] + f"\n… [{len(body) - max_tool_chars} chars truncated]"
            label = turn.tool_name or turn.role
            if body:
                out.append((turn.role, f"[{label}] {body}"))
            continue

        if turn.role == "user" and is_internal_prompt(turn.content):
            continue

        body = clean_text(turn.content)
        if body:
            out.append((turn.role, body))
    return out


def render_transcript(turns, drop_tools: bool = False, max_chars: int = 120_000) -> str:
    """Flatten turns into a plain transcript, newest content preserved.

    When the session exceeds `max_chars` the middle is elided rather than
    the tail: the beginning frames the task and the end holds the outcome.
    """
    lines = [f"{role.upper()}: {body}" for role, body in clean_turns(turns, drop_tools)]
    text = "\n\n".join(lines)
    if len(text) <= max_chars:
        return text
    head = text[: max_chars // 2]
    tail = text[-max_chars // 2:]
    return f"{head}\n\n… [{len(text) - max_chars} chars elided] …\n\n{tail}"
