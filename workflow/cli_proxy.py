"""CLI commands for the capture proxy and the conversation memory it builds.

Registered onto the main app in `workflow.cli` as `wf proxy` and `wf mem`.
"""

import json
import time
from typing import Optional

import typer
from rich.console import Console

console = Console()

proxy_app = typer.Typer(help="Run the capturing model proxy", no_args_is_help=True)
mem_app = typer.Typer(help="Search and summarize captured conversations", no_args_is_help=True)


def _store():
    from workflow.conversation_store import get_store
    return get_store()


def _config():
    from workflow.config import load_effective_config
    return load_effective_config()


# ── wf proxy ─────────────────────────────────────────────────────────────────

@proxy_app.command("start")
def proxy_start(
    foreground: bool = typer.Option(False, "--foreground", "-f",
                                    help="Run in this terminal instead of detaching"),
):
    """Start the proxy that captures agent conversations."""
    from workflow.proxy import daemon

    if foreground:
        console.print(f"🎧 Proxy listening on {daemon.base_url()} (Ctrl-C to stop)")
        daemon.start(foreground=True)
        return

    info = daemon.start()
    if not info.get("running"):
        console.print(f"❌ Proxy failed to start: {info.get('error', 'unknown error')}")
        raise typer.Exit(1)

    url = f"http://{info['host']}:{info['port']}"
    console.print(f"✅ Proxy running on {url} (pid {info['pid']})")
    health = info.get("health") or {}
    if health:
        console.print(f"   store: {health.get('store')}")
    console.print("\n💡 Point an agent at it:")
    console.print(f"   export ANTHROPIC_BASE_URL={url}")
    console.print(f"   export OPENAI_BASE_URL={url}/v1")
    console.print("   ...or just run 'wf ai', which sets these for you.")


@proxy_app.command("stop")
def proxy_stop():
    """Stop the proxy, summarizing any sessions still open."""
    from workflow.proxy import daemon

    if daemon.stop():
        console.print("🛑 Proxy stopped")
    else:
        console.print("ℹ️  Proxy was not running")


@proxy_app.command("status")
def proxy_status():
    """Show whether the proxy is running and what it is capturing to."""
    from workflow.proxy import daemon

    info = daemon.status()
    if not info.get("running"):
        console.print("⚪ Proxy not running")
        console.print("💡 Start it with 'wf proxy start'")
        return

    url = f"http://{info['host']}:{info['port']}"
    uptime = time.time() - info.get("started_at", time.time())
    console.print(f"🟢 Proxy running on {url} (pid {info['pid']}, up {_duration(uptime)})")

    health = info.get("health")
    if health:
        console.print(f"   store:    {health.get('store')}")
        console.print(f"   active:   {health.get('active_sessions')} session(s)")
        for name, target in (health.get("upstream") or {}).items():
            console.print(f"   upstream: {name} → {target}")
    else:
        console.print("   ⚠️  process is up but not answering /_wf/health yet")


@proxy_app.command("env")
def proxy_env():
    """Print the shell exports that route agents through the proxy."""
    from workflow.proxy import daemon

    for key, value in daemon.env_for_agents().items():
        typer.echo(f"export {key}={value}")


@proxy_app.command("logs")
def proxy_logs(lines: int = typer.Option(40, "--lines", "-n", help="How many lines to show")):
    """Tail the proxy's log file."""
    from workflow.proxy.daemon import LOG_FILE

    if not LOG_FILE.exists():
        console.print(f"ℹ️  No log yet at {LOG_FILE}")
        return
    tail = LOG_FILE.read_text(errors="replace").splitlines()[-lines:]
    console.print("\n".join(tail) or "(empty)")


# ── wf mem ───────────────────────────────────────────────────────────────────

@mem_app.command("sessions")
def mem_sessions(
    task: Optional[str] = typer.Option(None, "--task", "-t", help="Filter by task key"),
    limit: int = typer.Option(20, "--limit", "-l"),
):
    """List captured conversations, most recent first."""
    store = _store()
    sessions = store.list_sessions(task_key=task, limit=limit)
    if not sessions:
        console.print("No conversations captured yet.")
        console.print("💡 Start the proxy with 'wf proxy start', then run an agent through it.")
        return

    for meta in sessions:
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(meta.updated_at))
        turns = len(store.get_turns(meta.session_id))
        task_label = meta.task_key or "—"
        console.print(
            f"  [bold]{meta.session_id[:20]:<20}[/bold] {when}  "
            f"{task_label:<12} {meta.agent:<12} {turns:>4} turns"
        )


@mem_app.command("show")
def mem_show(
    session_id: str = typer.Argument(..., help="Session id (prefix is enough)"),
    limit: int = typer.Option(0, "--limit", "-l", help="Only the last N turns (0 = all)"),
    tools: bool = typer.Option(False, "--tools", help="Include tool calls and results"),
):
    """Print a captured conversation."""
    store = _store()
    resolved = _resolve_session(store, session_id)
    if not resolved:
        raise typer.Exit(1)

    turns = store.get_turns(resolved)
    if not tools:
        turns = [t for t in turns if t.role in ("user", "assistant", "system")]
    if limit:
        turns = turns[-limit:]

    colors = {"user": "cyan", "assistant": "green", "system": "yellow",
              "tool_call": "magenta", "tool_result": "blue"}
    for turn in turns:
        label = turn.role.upper()
        if turn.tool_name:
            label += f" ({turn.tool_name})"
        console.print(f"\n[{colors.get(turn.role, 'white')}]── {label} ──[/]")
        console.print(turn.content)


@mem_app.command("search")
def mem_search(
    query: str = typer.Argument(..., help="Text to search for"),
    task: Optional[str] = typer.Option(None, "--task", "-t", help="Restrict to one task"),
    limit: int = typer.Option(10, "--limit", "-l"),
):
    """Full-text search across every captured conversation."""
    store = _store()
    hits = store.search(query, limit=limit, task_key=task)
    if not hits:
        console.print(f"No matches for '{query}'")
        return

    for turn in hits:
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(turn.ts))
        snippet = _snippet(turn.content, query)
        console.print(f"\n[dim]{turn.session_id[:16]} · {turn.role} · {when}[/dim]")
        console.print(f"  {snippet}")


@mem_app.command("summarize")
def mem_summarize(
    session_id: Optional[str] = typer.Argument(None, help="Session to summarize (default: most recent)"),
    force: bool = typer.Option(False, "--force", help="Re-summarize even if one exists"),
):
    """Run the summary + fact extraction ladder over a session."""
    from workflow.ladder import summarize_session

    store = _store()
    if session_id:
        resolved = _resolve_session(store, session_id)
        if not resolved:
            raise typer.Exit(1)
    else:
        sessions = store.list_sessions(limit=1)
        if not sessions:
            console.print("No conversations captured yet.")
            raise typer.Exit(1)
        resolved = sessions[0].session_id
        console.print(f"Summarizing most recent session: {resolved}")

    text = summarize_session(resolved, store, _config(), force=force)
    if text:
        console.print(f"\n{text}")
    else:
        console.print("ℹ️  Nothing produced (too short, already summarized, or provider unavailable)")


@mem_app.command("facts")
def mem_facts(task: Optional[str] = typer.Option(None, "--task", "-t", help="Task key")):
    """Show durable facts extracted for a task."""
    store = _store()
    task_key = task or _current_task_key()
    summaries = store.get_summaries(task_key=task_key, level="facts")
    if not summaries:
        console.print(f"No facts recorded{f' for {task_key}' if task_key else ''} yet.")
        return

    seen = set()
    for summary in summaries:
        try:
            facts = json.loads(summary.content)
        except json.JSONDecodeError:
            continue
        for fact in facts:
            content = fact.get("content", "")
            if content in seen:
                continue
            seen.add(content)
            console.print(f"  [dim]({fact.get('type', 'fact')})[/dim] {content}")


@mem_app.command("rollup")
def mem_rollup(
    task: Optional[str] = typer.Option(None, "--task", "-t", help="Task key"),
    rebuild: bool = typer.Option(False, "--rebuild", help="Regenerate instead of showing the cached one"),
):
    """Show (or rebuild) the durable briefing for a task."""
    from workflow.ladder import build_rollup, get_rollup

    store = _store()
    task_key = task or _current_task_key()
    if not task_key:
        console.print("❌ No task specified and no current task set.")
        raise typer.Exit(1)

    text = build_rollup(task_key, store, _config()) if rebuild else get_rollup(task_key, store)
    if text:
        console.print(text)
    else:
        console.print(f"No rollup for {task_key} yet. Build one with '--rebuild'.")


# ── helpers ──────────────────────────────────────────────────────────────────

def _current_task_key() -> Optional[str]:
    try:
        from workflow.state import get_current_task
        task = get_current_task()
        return getattr(task, "key", None) if task else None
    except Exception:
        return None


def _resolve_session(store, prefix: str) -> Optional[str]:
    """Accept a session-id prefix, the way git accepts a short sha."""
    matches = [m.session_id for m in store.list_sessions(limit=1000)
               if m.session_id.startswith(prefix)]
    if not matches:
        console.print(f"❌ No session matching '{prefix}'")
        return None
    if len(matches) > 1:
        console.print(f"❌ '{prefix}' is ambiguous ({len(matches)} sessions):")
        for sid in matches[:10]:
            console.print(f"   {sid}")
        return None
    return matches[0]


def _snippet(content: str, query: str, width: int = 160) -> str:
    idx = content.lower().find(query.lower())
    if idx == -1:
        return content[:width].replace("\n", " ")
    start = max(0, idx - width // 3)
    chunk = content[start:start + width].replace("\n", " ")
    return ("…" if start else "") + chunk + ("…" if start + width < len(content) else "")


def _duration(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}m"


@mem_app.command("compact")
def mem_compact(
    session_id: Optional[str] = typer.Argument(None, help="Session to compact (default: all)"),
    apply: bool = typer.Option(False, "--apply", help="Write the changes (default: dry run)"),
):
    """Collapse duplicate turns left by the pre-fix reconciliation bug.

    Until the multiset fix, a client that varied its system prompt per
    request caused the whole history to re-append every turn. This removes
    the repeats, keeping the first occurrence of each so ordering survives.
    """
    from workflow.conversation_store.sqlite import SqliteStore

    store = _store()
    target = getattr(store, "primary", store)
    if not isinstance(target, SqliteStore):
        console.print(f"❌ compact needs the sqlite store; primary is '{target.name}'")
        raise typer.Exit(1)

    sessions = ([_resolve_session(target, session_id)] if session_id
                else [m.session_id for m in target.list_sessions(limit=10_000)])
    if session_id and not sessions[0]:
        raise typer.Exit(1)

    total_before = total_after = 0
    for sid in sessions:
        rows = target._db.execute(
            "SELECT seq, fingerprint FROM messages WHERE session_id = ? ORDER BY seq", (sid,)
        ).fetchall()
        seen, drop = set(), []
        for row in rows:
            if row["fingerprint"] in seen:
                drop.append(row["seq"])
            else:
                seen.add(row["fingerprint"])

        total_before += len(rows)
        total_after += len(rows) - len(drop)
        if not drop:
            continue

        console.print(f"  {sid[:20]:<22} {len(rows):>7} → {len(rows) - len(drop):>6} turns "
                      f"[dim](-{len(drop)})[/dim]")
        if apply:
            target._db.executemany(
                "DELETE FROM messages WHERE session_id = ? AND seq = ?",
                [(sid, seq) for seq in drop],
            )
            # The FTS index mirrors messages by (session_id, seq); leaving
            # rows there would surface deleted turns in search results.
            if target._fts:
                target._db.executemany(
                    "DELETE FROM messages_fts WHERE session_id = ? AND seq = ?",
                    [(sid, seq) for seq in drop],
                )
            target._db.commit()

    if total_before == total_after:
        console.print("✅ Nothing to compact.")
        return

    saved = total_before - total_after
    verb = "Removed" if apply else "Would remove"
    console.print(f"\n{verb} {saved:,} duplicate turn(s) "
                  f"({total_before:,} → {total_after:,})")
    if not apply:
        console.print("💡 Re-run with --apply to write the changes.")


@mem_app.command("publish")
def mem_publish(
    session_id: Optional[str] = typer.Argument(None, help="Session (default: most recent)"),
    all_sessions: bool = typer.Option(False, "--all", help="Publish every captured session"),
    task: Optional[str] = typer.Option(None, "--task", "-t", help="Only this task's sessions"),
):
    """Publish captured conversations to NotesGraph.

    Runs automatically when a session is summarized; this is for backfilling
    what was captured before publishing was switched on, or for republishing
    after an edit.
    """
    from workflow.conversation_store.notesgraph import (
        client_from_config,
        is_enabled,
        publish_session,
    )

    config = _config()
    if not is_enabled(config):
        console.print("❌ NotesGraph publishing is not configured.")
        console.print("💡 Set proxy.capture.notesgraph.enabled and .workspace, then")
        console.print("   'wf deploy notes-login <token>' if you haven't already.")
        raise typer.Exit(1)

    store = _store()
    if all_sessions or task:
        sessions = [m.session_id for m in store.list_sessions(task_key=task, limit=1000)]
    elif session_id:
        resolved = _resolve_session(store, session_id)
        if not resolved:
            raise typer.Exit(1)
        sessions = [resolved]
    else:
        recent = store.list_sessions(limit=1)
        if not recent:
            console.print("No conversations captured yet.")
            raise typer.Exit(1)
        sessions = [recent[0].session_id]

    client = client_from_config(config)
    published = 0
    for sid in sessions:
        try:
            doc_id = publish_session(sid, store, config, client=client)
        except Exception as e:
            console.print(f"  [red]✗[/red] {sid[:16]}: {e}")
            continue
        if doc_id:
            published += 1
            console.print(f"  [green]✓[/green] {sid[:16]} → {doc_id}")

    console.print(f"\n📓 Published {published}/{len(sessions)} conversation(s)")
