"""Automation jobs: code `wf agent serve` runs itself, in node, no model."""

import json
import shutil

import pytest

from workflow.deploy import automations
from workflow.deploy.automations import (
    AutomationCancelled,
    AutomationFailed,
    run_automation,
)
from workflow.deploy.jobs import Job, JobClient, _run_claimed
from workflow.deploy.registry.notesgraph import NotesGraphError

needs_node = pytest.mark.skipif(shutil.which("node") is None,
                                reason="node is not installed")


def code_job(code, **payload):
    return {"code": code, "input": {"n": 2}, "timeoutSeconds": 5, **payload}


@needs_node
def test_the_return_value_is_the_result_and_console_the_log():
    logs = []
    result = run_automation(
        code_job('console.log("doubling"); return {n: input.n * 2, t: secrets.T}'),
        {"T": "t"}, on_tick=lambda text: logs.append(text) and False)
    assert json.loads(result) == {"n": 4, "t": "t"}
    assert "doubling\n" in "".join(logs)


@needs_node
def test_nothing_returned_is_an_empty_result():
    assert run_automation(code_job("const x = 1;")) == ""


@needs_node
def test_the_code_sees_nothing_of_the_runner():
    result = run_automation(code_job(
        "return [typeof process, typeof require]"))
    assert json.loads(result) == ["undefined", "undefined"]


@needs_node
def test_plain_code_has_no_sh():
    assert json.loads(run_automation(code_job("return typeof sh"))) == "undefined"


@needs_node
def test_a_shell_automation_runs_commands_and_logs_their_output(tmp_path):
    logs = []
    result = run_automation(
        code_job(
            'const r = await sh("echo hi; pwd", {cwd: input.cwd});'
            ' return r.output.trim().split("\\n")',
            shell=True, input={"cwd": str(tmp_path)}),
        on_tick=lambda text: logs.append(text) and False)
    assert json.loads(result) == ["hi", str(tmp_path.resolve())]
    assert "hi\n" in "".join(logs)


@needs_node
def test_a_failing_command_fails_a_shell_automation_unless_unchecked():
    with pytest.raises(AutomationFailed, match="exited 3: nope"):
        run_automation(code_job('await sh("echo nope; exit 3")', shell=True))
    result = run_automation(code_job(
        'return (await sh("exit 3", {check: false})).code', shell=True))
    assert json.loads(result) == 3


@needs_node
def test_a_shell_automation_gets_home_but_not_wf_tokens(monkeypatch):
    monkeypatch.setenv("NOTESGRAPH_TOKEN", "secret")
    result = run_automation(code_job(
        'return (await sh("echo $HOME:$NOTESGRAPH_TOKEN")).output.trim()',
        shell=True))
    assert json.loads(result) == f"{automations.os.environ['HOME']}:"


@needs_node
def test_a_timeout_stops_the_commands_a_shell_automation_started(tmp_path):
    marker = tmp_path / "still-running"
    with pytest.raises(AutomationFailed, match="^Timed out"):
        run_automation(code_job(
            f'await sh("sleep 3 && touch {marker}")',
            shell=True, timeoutSeconds=1))
    import time
    time.sleep(3)
    assert not marker.exists()


def actions_job(code, **payload):
    return {"code": code, "runtime": "actions", "timeoutSeconds": 10, **payload}


def test_actions_yaml_runs_commands_in_order_with_inputs_and_earlier_results():
    logs = []
    result = run_automation(actions_job(
        "actions:\n"
        "  - id: greet\n"
        "    type: custom_command\n"
        "    command: echo hello {input.who}\n"
        "  - id: again\n"
        "    type: custom_command\n"
        "    command: echo {steps.greet.stdout}!\n",
        input={"who": "it's me; rm -rf /"}),
        on_tick=lambda text: logs.append(text) and False)
    steps = json.loads(result)
    # Filled-in values are quoted: the input's `;` is text, not a command.
    assert steps["greet"]["stdout"] == "hello it's me; rm -rf /"
    assert steps["again"]["stdout"] == "hello it's me; rm -rf /!"
    assert "▶ greet (custom_command)" in "".join(logs)


def test_a_failing_action_fails_the_run_unless_optional():
    with pytest.raises(AutomationFailed, match="boom: exited 2: nope"):
        run_automation(actions_job(
            "actions:\n  - id: boom\n    type: custom_command\n"
            "    command: echo nope; exit 2\n"))
    result = run_automation(actions_job(
        "actions:\n  - id: boom\n    type: custom_command\n"
        "    command: exit 2\n    optional: true\n"
        "  - type: custom_command\n    command: echo after\n"))
    assert json.loads(result)["step2"]["stdout"] == "after"


def test_malformed_actions_yaml_says_why():
    with pytest.raises(AutomationFailed, match="needs a `type`"):
        run_automation(actions_job("actions:\n  - command: echo hi\n"))
    with pytest.raises(AutomationFailed, match="names nothing"):
        run_automation(actions_job(
            "actions:\n  - type: custom_command\n    command: echo {input.nope}\n"))


def test_a_timeout_stops_an_actions_command(tmp_path):
    marker = tmp_path / "still-running"
    with pytest.raises(AutomationFailed, match="^Timed out"):
        run_automation(actions_job(
            "actions:\n  - type: custom_command\n"
            f"    command: sleep 3 && touch {marker}\n", timeoutSeconds=1))
    import time
    time.sleep(3)
    assert not marker.exists()


@needs_node
def test_a_thrown_error_fails_the_run_with_its_message():
    with pytest.raises(AutomationFailed, match="no luck"):
        run_automation(code_job('throw new Error("no luck")'))


@needs_node
def test_code_that_never_ends_is_stopped_at_its_limit():
    with pytest.raises(AutomationFailed, match="^Timed out after 1s"):
        run_automation(code_job("while (true) {}", timeoutSeconds=1))


@needs_node
def test_a_cancel_from_the_app_stops_it():
    with pytest.raises(AutomationCancelled):
        run_automation(code_job("await new Promise(r => setTimeout(r, 10000))",
                                timeoutSeconds=30),
                       on_tick=lambda text: True)


def test_a_job_without_code_fails():
    with pytest.raises(AutomationFailed, match="no code"):
        run_automation({"code": ""})


class RecordingClient(JobClient):
    def __init__(self):
        self.reports = []
        self.workspace = "ws-1"

    def report(self, job_id, **fields):
        self.reports.append(fields)
        return {"status": fields.get("status", "running")}


@needs_node
def test_a_claimed_automation_is_reported_done_with_its_output():
    client = RecordingClient()
    job = Job.from_payload({
        "id": "j1", "agentId": "a1", "agentName": "Nightly",
        "instructions": "", "context": "", "kind": "automation",
        "payload": code_job("return 42"), "secrets": {},
    })
    assert _run_claimed(client, job, None, lambda *a, **k: None)
    assert client.reports[-1]["status"] == "done"
    assert client.reports[-1]["result"] == "42"


def test_claims_ask_for_automations_and_fall_back_on_a_403(monkeypatch):
    calls = []

    def fake_request(self, method, path, body=None):
        calls.append(body["kinds"])
        if "automation" in body["kinds"]:
            error = NotesGraphError("POST claim failed: 403 forbidden")
            error.status = 403
            raise error
        return {"job": None}

    monkeypatch.setattr(JobClient, "_request", fake_request)
    client = JobClient(url="https://app.notesgraph.com", workspace="ws-1",
                       token="pat")
    client.take_automations = True
    assert client.claim("laptop", "r1") is None
    assert client.claim("laptop", "r1") is None
    assert calls == [["agent", "automation"], ["agent"], ["agent"]]


def test_without_node_only_agent_jobs_are_asked_for(monkeypatch):
    monkeypatch.setattr(automations.shutil, "which", lambda name: None)
    assert not automations.can_run()
