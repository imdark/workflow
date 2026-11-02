"""Propose harness changes from mistakes repeated across captured sessions.

Step 1 of the self-improving harness loop: mine, propose, stop. A mechanical
pass finds loops in the captured transcripts (the same call failing three or
more times, the same call repeated, the same question asked in two sessions).
The provider then turns those findings into at most a handful of small deltas
-- one line to add or patch in Agent rules, a `Skill:` note, or an Agent memory
fact -- each quoting its evidence. Nothing is applied; the output is a
"Proposed harness changes" note for the user to accept or reject.

Sessions that fetched web content are flagged: a hostile page could have
steered the run, so a proposal drawn from one needs a closer read.

Proposals live in ~/.wf/learn/proposed-<date>.md and, when NotesGraph
publishing is on, are mirrored under `<project> / ai / proposals`.
"""

import json
import re
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Optional

PROPOSALS_DIR = Path.home() / ".wf" / "learn"

FAIL_RE = re.compile(r"\b(error|failed|not found|no such file|denied|traceback|exception)\b", re.I)
FETCH_TOOLS = {"WebFetch", "WebSearch"}
TARGETS = ("agent_rules", "skill", "agent_memory")

PROPOSE_PROMPT = """\
Below are findings from a mechanical scan of agent sessions: loops where the
agent kept repeating a failing or redundant step, and questions asked in more
than one session. Turn them into small, concrete harness changes for the next
run. Each change is ONE line to add to the Agent rules, one edit to a skill,
or one dated fact for Agent memory. Quote the evidence. Prefer omission to
noise: if a finding is a one-off, skip it. Return at most {limit}.

{known_block}Return ONLY a JSON array, no prose:
[{{"target": "agent_rules" | "skill" | "agent_memory",
   "change": "the exact line to add or the patch to make",
   "why": "the mistake it prevents, in one sentence",
   "evidence": "which findings and sessions, quoted briefly"}}]

FINDINGS:
{body}
"""


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def _short(text: str, n: int = 100) -> str:
    text = re.sub(r"\s+", " ", (text or "").strip())
    return text if len(text) <= n else text[: n - 1] + "…"


def find_loops(turns: list, min_repeat: int = 3) -> list:
    """Repeated tool calls in one session; `failing` when their results look like errors."""
    results = {t.tool_call_id: t.content for t in turns
               if t.role == "tool_result" and t.tool_call_id}
    calls = defaultdict(list)
    for t in turns:
        if t.role == "tool_call":
            calls[(t.tool_name or "", _norm(t.content))].append(t)
    out = []
    for (tool, _), group in calls.items():
        if len(group) < min_repeat:
            continue
        failed = sum(1 for t in group if FAIL_RE.search(results.get(t.tool_call_id, "")[:400]))
        out.append({"kind": "failing-loop" if failed >= min_repeat else "repeat-loop",
                    "tool": tool, "times": len(group), "failed": failed,
                    "call": _short(group[0].content)})
    return out


def user_questions(turns: list) -> set:
    """Short user messages, normalised, for cross-session comparison."""
    return {_norm(t.content) for t in turns
            if t.role == "user" and 12 <= len(t.content.strip()) <= 200}


def scan(store, task_key: Optional[str] = None, max_sessions: int = 40) -> dict:
    """Run the detectors over recent sessions. No model involved."""
    sessions = store.list_sessions(task_key=task_key, limit=max_sessions)
    loops, asked, untrusted = [], defaultdict(set), set()
    for s in sessions:
        turns = store.get_turns(s.session_id)
        for loop in find_loops(turns):
            loops.append({**loop, "session": s.session_id, "task": s.task_key})
        for q in user_questions(turns):
            asked[q].add(s.session_id)
        if any(t.role == "tool_call" and t.tool_name in FETCH_TOOLS for t in turns):
            untrusted.add(s.session_id)
    repeated = [{"question": q, "sessions": sorted(ids)}
                for q, ids in asked.items() if len(ids) >= 2]
    return {"sessions": len(sessions), "loops": loops,
            "repeated_questions": repeated, "untrusted": sorted(untrusted)}


def has_findings(found: dict) -> bool:
    return bool(found["loops"] or found["repeated_questions"])


def format_findings(found: dict) -> str:
    lines = []
    for l in found["loops"]:
        tag = " [session fetched web content]" if l["session"] in found["untrusted"] else ""
        detail = f"failed {l['failed']} of {l['times']}" if l["kind"] == "failing-loop" else f"{l['times']} times"
        lines.append(f"- {l['kind']}: {l['tool']} `{l['call']}` {detail} "
                     f"(session {l['session']}, {l['task'] or 'no task'}){tag}")
    for q in found["repeated_questions"]:
        lines.append(f"- asked in {len(q['sessions'])} sessions: \"{_short(q['question'])}\"")
    return "\n".join(lines)


def propose_changes(store, config: Optional[dict] = None, task_key: Optional[str] = None,
                    max_sessions: int = 40, limit: int = 10) -> dict:
    """Scan, then ask the provider for proposals. Returns {found, proposals}."""
    from workflow.ladder import _run_llm, _parse_json_array

    if config is None:
        from workflow.config import load_effective_config
        config = load_effective_config()

    found = scan(store, task_key, max_sessions)
    if not has_findings(found):
        return {"found": found, "proposals": []}
    raw = _run_llm(PROPOSE_PROMPT.format(limit=limit, known_block="",
                                         body=format_findings(found)), config)
    proposals = []
    for item in _parse_json_array(raw or "")[:limit]:
        if not isinstance(item, dict) or item.get("target") not in TARGETS or not item.get("change"):
            continue
        evidence = str(item.get("evidence", ""))
        proposals.append({
            "target": item["target"], "change": str(item["change"]).strip(),
            "why": str(item.get("why", "")).strip(), "evidence": evidence.strip(),
            "untrusted": any(sid in evidence for sid in found["untrusted"]),
        })
    return {"found": found, "proposals": proposals}


def render_proposals(proposals: list, found: dict) -> str:
    """The "Proposed harness changes" note body."""
    out = [f"Proposed harness changes, {time.strftime('%Y-%m-%d')}. "
           f"Mined {found['sessions']} sessions. Nothing here is applied; "
           "accept a line by copying it to its target.", ""]
    for i, p in enumerate(proposals, 1):
        flag = "  **review closely: evidence comes from a session that fetched web content**" \
            if p["untrusted"] else ""
        out += [f"## {i}. {p['target'].replace('_', ' ')}", f"* **Change:** {p['change']}",
                f"* **Why:** {p['why']}", f"* **Evidence:** {p['evidence']}{flag}", ""]
    return "\n".join(out)


def save_proposals(text: str, directory: Path = PROPOSALS_DIR) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"proposed-{time.strftime('%Y-%m-%d')}.md"
    path.write_text(text)
    return path


def publish_proposals(text: str, project: Optional[str], config: dict) -> Optional[str]:
    """Mirror the proposals into NotesGraph under `<project> / ai / proposals`. Never raises."""
    try:
        from workflow.conversation_store import notesgraph as ng
        if not ng.is_enabled(config):
            return None
        client = ng.client_from_config(config)
        folder = [project or "unscoped", "ai", "proposals"]
        doc_id = client.create_doc(
            f"Proposed harness changes {time.strftime('%Y-%m-%d')}", text, folder=folder)
        try:
            client.link_doc(ng.ensure_path(client, folder, ng._load_ledger()), doc_id)
        except ng.NotesGraphError as e:
            print(f"⚠️  Published the proposals but could not link them: {e}")
        return doc_id
    except Exception as e:
        print(f"⚠️  Could not publish proposals to NotesGraph: {e}")
        return None
