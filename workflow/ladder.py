"""The summarization ladder over captured conversations.

Three levels, each derived from the one below:

  session  one prose summary of a single conversation -- what was done,
           what was decided, what is still open
  facts    structured, durable statements extracted from that session
           (preference / decision / constraint / fact), deduplicated
           against the facts already known for the task
  rollup   a per-task digest regenerated from recent session summaries and
           the accumulated fact set, so a new session can be primed with
           one block instead of a whole history

Summaries are produced by the configured AI provider. Every level degrades
to a no-op if the provider is unavailable -- the raw transcript is the
durable artifact, summaries are a convenience on top.
"""

import json
import re
import subprocess
import time
from typing import Optional

from workflow.conversation_store.base import Summary
from workflow.proxy.sanitize import render_transcript

# How many sessions must accumulate before the task rollup is rebuilt.
DEFAULT_ROLLUP_EVERY = 5

FACT_TYPES = ("decision", "preference", "constraint", "fact")

SESSION_PROMPT = """\
Summarize this agent coding session for a teammate who will pick the work up later.

Cover only what is durable:
- what the session set out to do, and whether it got there
- decisions made, and the reasoning behind them
- files, commands, or interfaces that turned out to matter
- anything left broken, unverified, or explicitly deferred

Skip pleasantries, tool mechanics, and anything already obvious from the code.
Write 5-15 lines of plain prose. No preamble.

TRANSCRIPT:
{transcript}
"""

FACTS_PROMPT = """\
Extract durable facts from this agent coding session.

A fact must survive outside this conversation: it stays true next week, in a
different session, without the surrounding context. Prefer omission to noise --
returning two solid facts beats returning ten weak ones.

Types:
- decision:   a choice that was made and should be honored later
- preference: how this person wants work done
- constraint: a limit of the system, environment, or project
- fact:       a durable property of the codebase

Skip: one-off commands, transient state, anything specific to this session only.

{known_block}
Return ONLY a JSON array, no prose:
[{{"type": "decision", "content": "...", "confidence": 0.0-1.0}}]

TRANSCRIPT:
{transcript}
"""

ROLLUP_PROMPT = """\
Write a single durable briefing for task {task_key}, to be given to an agent
starting fresh on it.

Merge the session summaries and facts below into one coherent picture. Resolve
contradictions in favor of the most recent input and say so when something
changed. Drop anything superseded. Do not repeat the same point twice.

Structure it as:
- Goal: what this task is
- State: where it actually stands now
- Decisions: what is settled, and why
- Watch out: constraints, gotchas, unfinished edges

Aim for 10-25 lines. No preamble.

{body}
"""


# ── provider plumbing ────────────────────────────────────────────────────────

def _run_llm(prompt: str, config: Optional[dict] = None, timeout: int = 180) -> Optional[str]:
    """Run one non-interactive completion through the configured provider.

    Deliberately shells out to the provider CLI rather than reusing
    `AIProvider.run`: those launch an interactive session with a pty, which
    is not what a background summarizer wants.
    """
    if config is None:
        from workflow.config import load_effective_config
        config = load_effective_config()

    provider = ((config.get("ai") or {}).get("provider") or "claude").lower()
    commands = {
        "claude": ["claude", "--print", prompt],
        "opencode": ["opencode", "run", prompt],
        "flow": ["flow", "--print", prompt],
    }
    cmd = commands.get(provider, commands["claude"])

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        print(f"⚠️  Summarizer: '{cmd[0]}' not on PATH; skipping")
        return None
    except subprocess.TimeoutExpired:
        print(f"⚠️  Summarizer timed out after {timeout}s")
        return None

    if result.returncode != 0:
        print(f"⚠️  Summarizer exited {result.returncode}: {(result.stderr or '').strip()[:200]}")
        return None
    return (result.stdout or "").strip() or None


def _model_name(config: dict) -> str:
    return ((config.get("ai") or {}).get("provider") or "claude")


# ── level 1: session summary ─────────────────────────────────────────────────

def summarize_session(session_id: str, store, config: Optional[dict] = None,
                      force: bool = False) -> Optional[str]:
    """Produce the session summary, then the facts, then maybe the rollup.

    Called by the proxy when a session goes idle, and by `wf mem summarize`.
    Returns the summary text, or None when nothing was produced.
    """
    if config is None:
        from workflow.config import load_effective_config
        config = load_effective_config()

    ladder_cfg = (config.get("proxy") or {}).get("ladder") or {}
    if ladder_cfg.get("enabled") is False:
        return None

    if not force and store.get_summaries(session_id=session_id, level="session"):
        return None  # already summarized

    turns = store.get_turns(session_id)
    if len(turns) < int(ladder_cfg.get("min_turns", 4)):
        return None

    meta = next((m for m in store.list_sessions(limit=1000) if m.session_id == session_id), None)
    task_key = meta.task_key if meta else None

    transcript = render_transcript(turns)
    if not transcript.strip():
        return None

    summary_text = _run_llm(SESSION_PROMPT.format(transcript=transcript), config)
    if not summary_text:
        return None

    store.save_summary(Summary(
        level="session", content=summary_text, session_id=session_id,
        task_key=task_key, model=_model_name(config),
    ))
    print(f"📝 Session summary saved for {session_id}")

    # Mirror into the existing per-task memory dir so `wf ai` context and
    # anything else reading ~/.wf/memory keeps working unchanged.
    _mirror_to_task_memory(task_key, summary_text)

    if ladder_cfg.get("facts", True):
        extract_facts(session_id, store, config, transcript=transcript, task_key=task_key)

    if ladder_cfg.get("rollup", True) and task_key:
        every = int(ladder_cfg.get("rollup_every", DEFAULT_ROLLUP_EVERY))
        done = len(store.get_summaries(task_key=task_key, level="session"))
        if done % every == 0:
            build_rollup(task_key, store, config)

    # Publish the finished conversation last, so the document carries the
    # summary and facts rather than just the transcript.
    _publish(session_id, store, config)

    return summary_text


def _publish(session_id: str, store, config: dict) -> None:
    """Mirror a finished conversation into NotesGraph, if configured.

    Never raises: publishing is a convenience on top of a transcript that is
    already safely in the primary store.
    """
    try:
        from workflow.conversation_store.notesgraph import is_enabled, publish_session
    except ImportError:
        return
    if not is_enabled(config):
        return
    try:
        doc_id = publish_session(session_id, store, config)
        if doc_id:
            print(f"📓 Published conversation to NotesGraph ({doc_id})")
    except Exception as e:
        print(f"⚠️  Could not publish the conversation to NotesGraph: {e}")


def _mirror_to_task_memory(task_key: Optional[str], text: str) -> None:
    if not task_key:
        return
    try:
        from pathlib import Path
        d = Path.home() / ".wf" / "memory" / task_key
        d.mkdir(parents=True, exist_ok=True)
        with (d / "summary.md").open("a", encoding="utf-8") as f:
            f.write(f"\n\n--- {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n{text}\n")
    except OSError as e:
        print(f"⚠️  Could not mirror summary into ~/.wf/memory: {e}")


# ── level 2: extracted facts ─────────────────────────────────────────────────

def extract_facts(session_id: str, store, config: Optional[dict] = None,
                  transcript: Optional[str] = None,
                  task_key: Optional[str] = None) -> list:
    """Extract durable facts, deduplicated against what the task already knows."""
    if config is None:
        from workflow.config import load_effective_config
        config = load_effective_config()

    if transcript is None:
        transcript = render_transcript(store.get_turns(session_id))
    if not transcript.strip():
        return []

    known = _known_facts(store, task_key)
    known_block = ""
    if known:
        listed = "\n".join(f"- {f['content']}" for f in known[:40])
        known_block = (
            "Already known for this task -- do NOT repeat these, and only "
            "contradict one if this session genuinely supersedes it:\n"
            f"{listed}\n\n"
        )

    raw = _run_llm(
        FACTS_PROMPT.format(known_block=known_block, transcript=transcript), config
    )
    if not raw:
        return []

    facts = _parse_facts(raw)
    if not facts:
        return []

    known_texts = {_normalize(f["content"]) for f in known}
    fresh = [f for f in facts if _normalize(f["content"]) not in known_texts]
    if not fresh:
        return []

    store.save_summary(Summary(
        level="facts", content=json.dumps(fresh, ensure_ascii=False, indent=2),
        session_id=session_id, task_key=task_key, model=_model_name(config),
    ))
    print(f"🧠 Extracted {len(fresh)} fact(s) from {session_id}")
    return fresh


def _known_facts(store, task_key: Optional[str]) -> list:
    out = []
    for summary in store.get_summaries(task_key=task_key, level="facts"):
        out.extend(_parse_facts(summary.content))
    return out


def _parse_facts(raw: str) -> list:
    """Parse a fact array, tolerating fenced or prose-wrapped model output."""
    text = raw.strip()
    fence = re.search(r"```(?:json)?\s*(.+?)```", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    if not text.startswith("["):
        start, end = text.find("["), text.rfind("]")
        if start == -1 or end <= start:
            return []
        text = text[start:end + 1]

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return []
    if not isinstance(parsed, list):
        return []

    facts = []
    for item in parsed:
        if not isinstance(item, dict):
            continue
        content = str(item.get("content") or "").strip()
        if not content:
            continue
        ftype = str(item.get("type") or "fact").lower()
        facts.append({
            "type": ftype if ftype in FACT_TYPES else "fact",
            "content": content,
            "confidence": _confidence(item.get("confidence")),
        })
    return facts


def _confidence(value) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return 0.5


def _normalize(text: str) -> str:
    return re.sub(r"\W+", " ", text.lower()).strip()


# ── level 3: per-task rollup ─────────────────────────────────────────────────

def build_rollup(task_key: str, store, config: Optional[dict] = None,
                 max_sessions: int = 12) -> Optional[str]:
    """Regenerate the task briefing from recent summaries and known facts."""
    if config is None:
        from workflow.config import load_effective_config
        config = load_effective_config()

    sessions = store.get_summaries(task_key=task_key, level="session")[:max_sessions]
    facts = _known_facts(store, task_key)
    if not sessions and not facts:
        return None

    parts = []
    if sessions:
        rendered = "\n\n".join(
            f"[session {time.strftime('%Y-%m-%d', time.localtime(s.ts))}]\n{s.content}"
            for s in sessions
        )
        parts.append(f"SESSION SUMMARIES (newest first):\n{rendered}")
    if facts:
        rendered = "\n".join(f"- ({f['type']}) {f['content']}" for f in facts)
        parts.append(f"KNOWN FACTS:\n{rendered}")

    text = _run_llm(
        ROLLUP_PROMPT.format(task_key=task_key, body="\n\n".join(parts)), config
    )
    if not text:
        return None

    store.save_summary(Summary(
        level="rollup", content=text, session_id=None,
        task_key=task_key, model=_model_name(config),
    ))
    print(f"📚 Rollup rebuilt for {task_key}")
    return text


def get_rollup(task_key: str, store) -> Optional[str]:
    """Most recent rollup for a task, for priming a new session."""
    rollups = store.get_summaries(task_key=task_key, level="rollup")
    return rollups[0].content if rollups else None
