"""Agent runs and what they are started from, read from NotesGraph.

`wf agent jobs` lists one device's jobs and needs this machine registered
as that device. These read the whole workspace:

    agent_runs       every run, on any device: agents and automations,
                     narrowed by status, device, kind, agent or age
    agent_templates  what a run can be started from: the ready-made
                     automation templates, the automations saved from them,
                     and the agents recent runs used

Both are read-only. A run's instructions, context, result and an
automation's code and secrets are left out: a listing says what ran and how
it ended, not what it was told.
"""
from __future__ import annotations

import re
import time
import urllib.parse
from datetime import datetime, timezone
from typing import List, Optional

# How many runs to read when narrowing by agent (the server can't) or
# counting which agents are in use.
SCAN_LIMIT = 200

RUN_MARKS = {"done": "✓", "error": "✗", "running": "…", "queued": "·", "cancelled": "–"}

_AGE = re.compile(r"^(\d+)\s*([mhdw])$")
_AGE_SECONDS = {"m": 60, "h": 3600, "d": 86400, "w": 604800}


class RunsError(RuntimeError):
    pass


def client_from_config():
    """The NotesGraph client for the configured workspace."""
    from workflow.config import load_effective_config
    from workflow.deploy.jobs import JobClient

    ng = (load_effective_config().get("deploy") or {}).get("notesgraph") or {}
    if not ng.get("workspace"):
        raise RunsError("No NotesGraph workspace configured. Set deploy.notesgraph.workspace, "
                        "then 'wf deploy notes-login <token>'.")
    return JobClient(url=ng.get("url", "https://app.notesgraph.com"), workspace=ng["workspace"])


def since_iso(since: Optional[str], now: Optional[float] = None) -> Optional[str]:
    """An ISO time from `24h`, `7d`, `30m`, `2w`, or an ISO date as given."""
    if not since:
        return None
    age = _AGE.match(since.strip().lower())
    if not age:
        return since.strip()
    at = (time.time() if now is None else now) - int(age.group(1)) * _AGE_SECONDS[age.group(2)]
    return datetime.fromtimestamp(at, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _shape_run(job: dict) -> dict:
    started, finished = job.get("startedAt"), job.get("finishedAt")
    return {
        "id": job.get("id"),
        "kind": job.get("kind") or "agent",
        "agent": job.get("agentName"),
        "title": job.get("title") or None,
        "status": job.get("status"),
        "device": job.get("deviceKey"),
        "model": job.get("model"),
        "created_at": job.get("createdAt"),
        "started_at": started,
        "finished_at": finished,
        "seconds": round(finished - started) if started and finished else None,
        "steps": job.get("steps"),
        "cost_usd": job.get("costUsd"),
        "error": job.get("error") or None,
        # The note item the run was started on, when there is one.
        "note": {"docId": job["docId"], "blockId": job.get("blockId")} if job.get("docId") else None,
        "continues": job.get("continues") or None,
    }


def agent_runs(client=None, device: Optional[str] = None, status: Optional[str] = None,
               kind: Optional[str] = None, agent: Optional[str] = None, since: Optional[str] = None,
               limit: int = 20) -> List[dict]:
    """Runs in the workspace, newest first. `since` counts from when a run
    ended, so it leaves out ones still running."""
    client = client or client_from_config()
    query = {"device": device, "status": status, "kind": kind, "since": since_iso(since),
             # The server can't match an agent's name: read more, cut here.
             "limit": SCAN_LIMIT if agent else limit}
    path = f"{client._base()}/jobs?" + urllib.parse.urlencode({k: v for k, v in query.items() if v})
    runs = [_shape_run(job) for job in client._request("GET", path).get("jobs") or []]
    if agent:
        needle = agent.lower()
        runs = [r for r in runs if needle in (r["agent"] or "").lower() or needle in (r["title"] or "").lower()]
    return runs[:limit]


def _trigger(trigger: dict) -> str:
    kind = trigger.get("type")
    if kind == "cron":
        return f"cron {trigger.get('expr')}" + (f" {trigger['tz']}" if trigger.get("tz") else "")
    if kind == "interval":
        return f"every {trigger.get('minutes')} min"
    if kind == "event":
        return f"on {trigger.get('event') or trigger.get('name') or 'event'}"
    return str(kind or "manual")


def _triggers(triggers) -> List[str]:
    return [_trigger(t) for t in triggers or [] if isinstance(t, dict)] or ["manual"]


def agent_templates(client=None) -> dict:
    """What runs are started from: templates, saved automations, agents."""
    client = client or client_from_config()
    base = f"/api/workspaces/{client.workspace}/automations"
    automations = [{
        "id": a.get("id"), "name": a.get("name"), "runtime": a.get("runtime"),
        "enabled": bool(a.get("enabled")), "triggers": _triggers(a.get("triggers")),
        "webhook": bool(a.get("hasWebhook")),
        "deployed_to": [d.get("target") for d in a.get("deployments") or [] if isinstance(d, dict)],
        "last_run_at": a.get("lastRunAt"), "next_run_at": a.get("nextRunAt"),
        "note": {"docId": a["docId"], "blockId": a.get("blockId")} if a.get("docId") else None,
    } for a in client._request("GET", base + "/").get("automations") or []]
    saved = {a["name"] for a in automations}
    templates = [{
        "id": t.get("id"), "name": t.get("name"), "description": t.get("description"),
        "triggers": _triggers(t.get("triggers")), "timeout_seconds": t.get("timeoutSeconds"),
        "saved": t.get("name") in saved,
    } for t in client._request("GET", base + "/templates").get("templates") or []]

    # Agents are notes, with no list of their own here: the ones recent runs used.
    agents: dict = {}
    recent = client._request("GET", f"{client._base()}/jobs?kind=agent&limit={SCAN_LIMIT}").get("jobs") or []
    for job in recent:
        name = job.get("agentName") or "(unnamed)"
        seen = agents.setdefault(name, {"name": name, "id": job.get("agentId"), "runs": 0,
                                        "last_run_at": job.get("createdAt"), "last_status": job.get("status"),
                                        "devices": []})
        seen["runs"] += 1
        if job.get("deviceKey") and job["deviceKey"] not in seen["devices"]:
            seen["devices"].append(job["deviceKey"])
    return {"templates": templates, "automations": automations,
            "agents": sorted(agents.values(), key=lambda a: -a["runs"]), "agents_from_runs": len(recent)}


# --- how the CLI prints them ---------------------------------------------

def _when(at) -> str:
    """`11-27 19:41` in local time, from epoch seconds or an ISO string."""
    if not at:
        return "never"
    if isinstance(at, (int, float)):
        return time.strftime("%m-%d %H:%M", time.localtime(at))
    try:
        return datetime.fromisoformat(str(at).replace("Z", "+00:00")).astimezone().strftime("%m-%d %H:%M")
    except ValueError:
        return str(at)


def _took(seconds: Optional[int]) -> str:
    if seconds is None:
        return ""
    return f"{seconds}s" if seconds < 90 else f"{round(seconds / 60)}m"


def format_runs(runs: List[dict]) -> str:
    if not runs:
        return "No runs."
    lines = []
    for r in runs:
        name = r["title"] or r["agent"] or "?"
        bits = [r["status"], r["device"], _when(r["created_at"]), _took(r["seconds"]),
                f"${r['cost_usd']:.2f}" if r.get("cost_usd") else "",
                "automation" if r["kind"] == "automation" else ""]
        lines.append(f"{RUN_MARKS.get(r['status'], '?')} {(r['id'] or '')[:8]} {name} · "
                     + " · ".join(b for b in bits if b))
        if r["title"] and r["agent"] and r["agent"] != r["title"]:
            lines.append(f"    agent: {r['agent']}")
        if r["error"]:
            lines.append(f"    {r['error'].strip().splitlines()[0][:160]}")
    return "\n".join(lines)


def format_templates(result: dict) -> str:
    lines = ["Automation templates"]
    for t in result["templates"]:
        lines.append(f"  {'✓' if t['saved'] else '·'} {t['name']} ({t['id']}) · {', '.join(t['triggers'])}"
                     + ("" if t["saved"] else " · not saved"))
    lines.append("\nSaved automations")
    for a in result["automations"]:
        where = f" · on {', '.join(a['deployed_to'])}" if a["deployed_to"] else ""
        lines.append(f"  {'✓' if a['enabled'] else '–'} {a['name']} · {a['runtime']} · {', '.join(a['triggers'])}"
                     + ("" if a["enabled"] else " · off") + where)
        lines.append(f"      last {_when(a['last_run_at'])}, next {_when(a['next_run_at']) if a['enabled'] else 'never'}"
                     f" · {a['id']}")
    if not result["automations"]:
        lines.append("  none")
    lines.append(f"\nAgents (from the last {result['agents_from_runs']} agent runs)")
    for agent in result["agents"]:
        lines.append(f"  {RUN_MARKS.get(agent['last_status'], '?')} {agent['name']} · {agent['runs']} run"
                     f"{'' if agent['runs'] == 1 else 's'} · last {_when(agent['last_run_at'])}"
                     f" · {', '.join(agent['devices'])}")
    if not result["agents"]:
        lines.append("  none")
    return "\n".join(lines)
