"""wf agent runs / templates: the workspace's runs and what they are started from."""
import json

from typer.testing import CliRunner

from workflow import agent_runs as runs
from workflow import cli_agent

T0 = 1795750000.0


def job(id="a1b2c3d4-0000", **over):
    return {"id": id, "kind": "agent", "agentName": "do the work", "agentId": "ag-1", "title": "Fix the sidebar",
            "status": "done", "deviceKey": "laptop", "model": "workflow", "createdAt": T0, "startedAt": T0 + 5,
            "finishedAt": T0 + 125, "steps": 9, "costUsd": 0.456, "error": None, "docId": "doc1", "blockId": "blk1",
            "continues": None,
            # What a listing must not carry.
            "instructions": "SECRET PROMPT", "context": "SECRET CONTEXT", "result": "SECRET RESULT",
            "payload": {"code": "SECRET CODE", "secrets": {"KEY": "SECRET VALUE"}}, **over}


class Client:
    workspace = "ws1"

    def __init__(self, jobs=(), automations=(), templates=()):
        self.jobs, self.automations, self.templates, self.paths = list(jobs), list(automations), list(templates), []

    def _base(self):
        return "/api/registry/workspaces/ws1"

    def _request(self, method, path, payload=None):
        self.paths.append(path)
        if path.endswith("/automations/templates"):
            return {"templates": self.templates}
        if path.endswith("/automations/"):
            return {"automations": self.automations}
        return {"jobs": self.jobs}


def test_runs_carry_how_they_went_and_nothing_they_were_told():
    client = Client([job(), job("e5f6", status="error", error="Claude exited 1\nmore", kind="automation",
                             title=None, agentName="Nightly", costUsd=None, finishedAt=None, docId=None)])
    out = runs.agent_runs(client)
    assert "SECRET" not in json.dumps(out)
    first, second = out
    assert (first["seconds"], first["cost_usd"], first["note"]) == (120, 0.456, {"docId": "doc1", "blockId": "blk1"})
    assert (second["seconds"], second["note"], second["kind"]) == (None, None, "automation")
    text = runs.format_runs(out)
    assert "✓ a1b2c3d4 Fix the sidebar · done · laptop · " in text and " · 2m · $0.46" in text
    assert "    agent: do the work" in text
    assert "✗ e5f6 Nightly · error · laptop" in text and text.endswith("    Claude exited 1")
    assert runs.format_runs([]) == "No runs."


def test_the_server_narrows_by_device_status_kind_and_age_and_wf_by_agent():
    client = Client([job(), job("b2", agentName="Health check", title="Health check: prod"), job("c3", title="Other")])
    runs.agent_runs(client, device="prod", status="error", kind="agent", since="2026-11-01", limit=5)
    assert client.paths[-1] == ("/api/registry/workspaces/ws1/jobs?"
                                "device=prod&status=error&kind=agent&since=2026-11-01&limit=5")
    # The server can't match a name: wf reads more and cuts.
    found = runs.agent_runs(client, agent="health", limit=5)
    assert client.paths[-1].endswith(f"limit={runs.SCAN_LIMIT}") and [r["id"] for r in found] == ["b2"]
    assert [r["id"] for r in runs.agent_runs(client, agent="do the work", limit=1)] == ["a1b2c3d4-0000"]


def test_since_takes_an_age_or_a_date():
    assert runs.since_iso("24h", now=86400 * 2) == "1970-01-02T00:00:00Z"
    assert runs.since_iso("2w", now=86400 * 15) == "1970-01-02T00:00:00Z"
    assert runs.since_iso("2026-11-01") == "2026-11-01"
    assert runs.since_iso(None) is None


def test_templates_list_what_is_saved_what_is_not_and_the_agents_in_use():
    client = Client(
        jobs=[job(), job("b2"), job("c3", agentName="Health check", deviceKey="prod", status="error")],
        templates=[{"id": "nightly", "name": "Title notes", "description": "d", "timeoutSeconds": 600,
                    "triggers": [{"type": "cron", "expr": "0 3 * * *", "tz": "UTC"}], "code": "SECRET CODE"},
                   {"id": "fail", "name": "Mark tasks FAILED", "description": "d", "triggers": [{"type": "hook"}]}],
        automations=[{"id": "au-1", "name": "Title notes", "runtime": "node", "enabled": False,
                      "triggers": [{"type": "interval", "minutes": 30}], "hasWebhook": False,
                      "deployments": [{"target": "cloud"}], "lastRunAt": "2026-11-27T05:00:00.000Z",
                      "nextRunAt": None, "docId": None, "secrets": ["KEY"]}])
    out = runs.agent_templates(client)
    assert "SECRET" not in json.dumps(out)
    assert [(t["id"], t["saved"], t["triggers"]) for t in out["templates"]] == [
        ("nightly", True, ["cron 0 3 * * * UTC"]), ("fail", False, ["hook"])]
    assert out["automations"][0]["deployed_to"] == ["cloud"] and out["automations"][0]["triggers"] == ["every 30 min"]
    assert [(a["name"], a["runs"], a["devices"]) for a in out["agents"]] == [
        ("do the work", 2, ["laptop"]), ("Health check", 1, ["prod"])]
    text = runs.format_templates(out)
    assert "  ✓ Title notes (nightly) · cron 0 3 * * * UTC" in text
    assert "  · Mark tasks FAILED (fail) · hook · not saved" in text
    assert "  – Title notes · node · every 30 min · off · on cloud" in text and "next never · au-1" in text
    assert "  ✓ do the work · 2 runs" in text and "  ✗ Health check · 1 run ·" in text


def test_the_commands_print_text_or_json_and_fail_clearly(monkeypatch):
    client = Client([job()])
    monkeypatch.setattr(runs, "client_from_config", lambda: client)
    runner = CliRunner()
    r = runner.invoke(cli_agent.agent_app, ["runs", "--json", "--device", "laptop"])
    assert r.exit_code == 0 and json.loads(r.output)[0]["id"] == "a1b2c3d4-0000"
    assert "device=laptop" in client.paths[-1]
    assert "Automation templates" in runner.invoke(cli_agent.agent_app, ["templates"]).output

    def no_workspace():
        raise runs.RunsError("No NotesGraph workspace configured.")
    monkeypatch.setattr(runs, "client_from_config", no_workspace)
    r = runner.invoke(cli_agent.agent_app, ["runs"])
    assert r.exit_code == 2


def test_agent_runs_may_list_runs_and_workflows_without_asking():
    from workflow.deploy.jobs import WF_TOOLS_ALLOWED
    for name in ("ci_workflows", "agent_runs", "agent_templates"):
        assert f"mcp__wf-tools__{name}" in WF_TOOLS_ALLOWED
