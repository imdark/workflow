"""Resolve who and what a proxied request belongs to.

Every captured session is tagged with the workflow task, project and repo
that were active when it ran -- the scoping this CLI already has, so the
proxy never needs its own notion of teams or users.
"""

import hashlib
import os
import re

# Headers agents use to carry their own conversation id, most specific first.
SESSION_HEADERS = (
    "x-conversation-id",
    "x-session-id",
    "x-claude-code-session-id",
    "x-deepseek-harness-session-id",
    "x-chat-id",
    "x-thread-id",
)

# User-agent fragments that identify a client when the path carries no prefix.
AGENT_HINTS = (
    ("claude-cli", "claude-code"),
    ("claude-code", "claude-code"),
    ("codex", "codex"),
    ("opencode", "opencode"),
    ("openai", "openai-sdk"),
)


def resolve_session_id(headers, turns, agent: str) -> str:
    """Prefer the agent's own session id; otherwise derive a stable one.

    The fallback hashes the first user turn plus the agent name, so every
    request in one conversation lands in the same session even though the
    client never sent an id. A conversation that starts with an identical
    first message on the same day collides deliberately -- that is almost
    always a resumed session.
    """
    for header in SESSION_HEADERS:
        value = headers.get(header)
        if value:
            return value.strip()

    seed = next((t.content for t in turns if t.role == "user" and t.content.strip()), "")
    if not seed:
        seed = next((t.content for t in turns if t.content.strip()), "")
    digest = hashlib.sha256(f"{agent}\x00{seed[:4000]}".encode("utf-8", "replace")).hexdigest()
    return f"derived-{digest[:16]}"


def resolve_agent(path: str, headers) -> str:
    """Identify the client from a URL prefix, then a user-agent fallback."""
    # /claude-code/v1/messages -> claude-code
    m = re.match(r"^/([a-z0-9][a-z0-9_-]{1,30})/(?:v1/)?(?:messages|chat|responses)", path)
    if m and m.group(1) != "v1":
        return m.group(1)

    ua = (headers.get("user-agent") or "").lower()
    for fragment, name in AGENT_HINTS:
        if fragment in ua:
            return name
    return "unknown"


def resolve_workflow_context() -> dict:
    """Current task / project / repo, best-effort.

    Runs inside the proxy process, which is long-lived, so nothing is
    cached: the user switches tasks while the daemon keeps running.
    """
    context = {"task_key": None, "project": None, "repo": None}

    try:
        from workflow.state import get_current_task
        task = get_current_task()
        if task is not None:
            context["task_key"] = getattr(task, "key", None) or (
                task if isinstance(task, str) else None
            )
    except Exception:
        pass

    try:
        from workflow.projects import get_current_project
        context["project"] = get_current_project()
    except Exception:
        pass

    try:
        from workflow.projects import get_cwd_repo
        context["repo"] = get_cwd_repo() or os.getcwd()
    except Exception:
        context["repo"] = os.getcwd()

    return context
