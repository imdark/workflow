"""Tests for the device half of remote agent execution.

Runs against a fake inventory, so nothing reaches the network. What is
tested is the runner's contract: it claims, it reports exactly once per
outcome, and a failing job never takes the loop down.
"""

import json

import pytest

from workflow.deploy.jobs import Job, JobClient, serve
from workflow.deploy.registry.notesgraph import NotesGraphError


def payload(job_id="j1", **overrides):
    base = {"id": job_id, "agentId": "a1", "agentName": "summarize",
            "instructions": "Summarize the repo", "context": "some context",
            "maxSteps": 4}
    base.update(overrides)
    return base


class FakeClient(JobClient):
    """A JobClient with the HTTP layer replaced."""

    def __init__(self, queue=None, claim_error=None):
        self.queue = list(queue or [])
        self.claim_error = claim_error
        self.reports = []
        self.claims = 0

    def claim(self, device_key, runner_id):
        self.claims += 1
        if self.claim_error:
            raise NotesGraphError(self.claim_error)
        return Job.from_payload(self.queue.pop(0)) if self.queue else None

    def report(self, job_id, **fields):
        self.reports.append({"job": job_id, **fields})


def test_job_parses_from_the_wire():
    job = Job.from_payload(payload(model="claude-opus-5", tools=["read_file"]))
    assert job.agent_name == "summarize"
    assert job.max_steps == 4
    assert job.tools == ["read_file"]


def test_a_successful_run_is_reported_done(monkeypatch):
    monkeypatch.setattr("workflow.deploy.jobs.run_job",
                        lambda job, cfg=None, **kw: {"result": "the summary",
                                                     "steps": 3, "session": None})
    client = FakeClient([payload()])

    assert serve(client, "laptop", once=True) == 1
    assert client.reports == [{"job": "j1", "status": "done",
                               "result": "the summary", "steps": 3}]


def test_a_failing_run_is_reported_as_an_error(monkeypatch):
    def boom(job, cfg=None, **kw):
        raise RuntimeError("claude is not on PATH on this device")
    monkeypatch.setattr("workflow.deploy.jobs.run_job", boom)
    client = FakeClient([payload()])

    assert serve(client, "laptop", once=True) == 0
    assert client.reports[0]["status"] == "error"
    assert "not on PATH" in client.reports[0]["error"]


def test_an_idle_device_reports_nothing():
    client = FakeClient([])
    assert serve(client, "laptop", once=True) == 0
    assert client.reports == []


def test_an_unreachable_inventory_does_not_crash_the_runner():
    """A server blip must not end the serve loop."""
    client = FakeClient(claim_error="inventory unreachable")
    assert serve(client, "laptop", once=True) == 0


class _Stop(Exception):
    """Ends a test's serve loop: claim raising it propagates out."""


def serve_until(client, done, **kw):
    """Run serve on a thread until `done` is set, then stop it."""
    import threading

    claim = client.claim

    def claim_or_stop(device_key, runner_id):
        if done.is_set():
            raise _Stop()
        return claim(device_key, runner_id)

    client.claim = claim_or_stop
    errors = []

    def run():
        try:
            serve(client, "laptop", poll_seconds=0.01, **kw)
        except _Stop:
            pass
        except Exception as e:  # surfaced to the test below
            errors.append(e)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    assert done.wait(5), "the jobs never finished"
    thread.join(5)
    assert not errors


def test_queued_jobs_run_side_by_side(monkeypatch):
    """Each job has its own worktree, so one need not wait for another."""
    import threading

    both_running = threading.Barrier(2, timeout=5)
    finished, done = [], threading.Event()

    def run(job, cfg=None, **kw):
        # Only gets past here once the other job is running too.
        both_running.wait()
        finished.append(job.id)
        if len(finished) == 2:
            done.set()
        return {"result": job.id, "steps": 1, "session": None}

    monkeypatch.setattr("workflow.deploy.jobs.run_job", run)
    client = FakeClient([payload("j1"), payload("j2")])

    serve_until(client, done)
    assert sorted(r["job"] for r in client.reports if r["status"] == "done") == ["j1", "j2"]


def test_no_more_than_max_jobs_run_at_once(monkeypatch):
    import threading
    import time

    lock, running, peak = threading.Lock(), [0], [0]
    finished, done = [], threading.Event()

    def run(job, cfg=None, **kw):
        with lock:
            running[0] += 1
            peak[0] = max(peak[0], running[0])
        time.sleep(0.05)
        with lock:
            running[0] -= 1
            finished.append(job.id)
            if len(finished) == 3:
                done.set()
        return {"result": job.id, "steps": 1, "session": None}

    monkeypatch.setattr("workflow.deploy.jobs.run_job", run)
    client = FakeClient([payload("j1"), payload("j2"), payload("j3")])

    serve_until(client, done, max_jobs=2)
    assert peak[0] == 2
    assert len([r for r in client.reports if r["status"] == "done"]) == 3


def test_the_prompt_carries_instructions_and_context():
    from workflow.deploy.jobs import _provider_argv

    provider, argv = _provider_argv(Job.from_payload(payload()),
                                    {"ai": {"provider": "claude"}})
    assert provider == "claude"
    assert "stream-json" in argv
    assert "Summarize the repo" in argv[-1]
    assert "some context" in argv[-1]


# --- running a job for real, against a fake provider ----------------------

FAKE_CLAUDE = r"""
import json, sys, time
def emit(e): print(json.dumps(e), flush=True)
emit({"type": "system", "subtype": "init", "model": "fake-1", "cwd": "/w"})
emit({"type": "assistant", "message": {"content": [
    {"type": "tool_use", "name": "Read", "input": {"file_path": "README.md"}}]}})
emit({"type": "user", "message": {"content": [
    {"type": "tool_result", "content": "# Project ✅"}]}})
time.sleep(float(sys.argv[1]) if len(sys.argv) > 1 else 0)
emit({"type": "assistant", "message": {"content": [
    {"type": "text", "text": "It is a notes app."}]}})
emit({"type": "result", "subtype": "success", "result": "It is a notes app.",
      "num_turns": 2, "total_cost_usd": 0.0123})
"""


@pytest.fixture
def fake_provider(tmp_path, monkeypatch):
    """Point run_job at a scripted stand-in for claude's stream-json."""
    import sys

    script = tmp_path / "fake_claude.py"
    script.write_text(FAKE_CLAUDE)
    monkeypatch.setattr("workflow.deploy.jobs.JOB_ROOT", tmp_path / "jobs")

    def use(*extra):
        monkeypatch.setattr(
            "workflow.deploy.jobs._provider_argv",
            lambda job, cfg: ("claude", [sys.executable, str(script), *extra]),
        )
    use()
    return use


def test_a_run_narrates_its_transcript_and_returns_the_result(fake_provider):
    from workflow.deploy.jobs import run_job

    ticks = []
    outcome = run_job(Job.from_payload(payload()), config={}, use_tmux=False,
                      on_tick=lambda text: ticks.append(text) or False,
                      tick_seconds=0.05)

    assert outcome == {"result": "It is a notes app.", "steps": 2, "session": None}
    transcript = "".join(ticks)
    assert "▶ started · model fake-1" in transcript
    assert '→ Read  {"file_path": "README.md"}' in transcript
    assert "← # Project ✅" in transcript
    assert "✓ finished in 2 turns · $0.0123" in transcript


def test_returning_true_from_a_tick_cancels_the_run(fake_provider):
    from workflow.deploy.jobs import JobCancelled, run_job

    fake_provider("30")   # would take 30s if not stopped
    with pytest.raises(JobCancelled):
        run_job(Job.from_payload(payload()), config={}, use_tmux=False,
                on_tick=lambda text: True, tick_seconds=0.05)


def test_a_failed_provider_run_raises_with_its_output(tmp_path, monkeypatch):
    import sys

    from workflow.deploy.jobs import run_job

    monkeypatch.setattr("workflow.deploy.jobs.JOB_ROOT", tmp_path / "jobs")
    monkeypatch.setattr(
        "workflow.deploy.jobs._provider_argv",
        lambda job, cfg: ("claude", [sys.executable, "-c",
                                     "print('rate limited'); raise SystemExit(2)"]),
    )
    with pytest.raises(RuntimeError, match="rate limited"):
        run_job(Job.from_payload(payload()), config={}, use_tmux=False,
                tick_seconds=0.05)


def test_a_missing_provider_binary_is_a_clear_error(tmp_path, monkeypatch):
    from workflow.deploy.jobs import run_job

    monkeypatch.setattr("workflow.deploy.jobs.JOB_ROOT", tmp_path / "jobs")
    monkeypatch.setattr("workflow.deploy.jobs._provider_argv",
                        lambda job, cfg: ("claude", ["no-such-provider-xyz", "go"]))
    with pytest.raises(RuntimeError, match="not on PATH"):
        run_job(Job.from_payload(payload()), config={}, use_tmux=False,
                tick_seconds=0.05)


@pytest.mark.skipif(not __import__("shutil").which("tmux"), reason="tmux not installed")
def test_with_tmux_the_run_happens_in_an_attachable_session(fake_provider):
    import subprocess

    from workflow.deploy.jobs import run_job, tmux_session_name

    fake_provider("1")
    job = Job.from_payload(payload(job_id="tmuxtest-0001"))
    session = tmux_session_name(job.id)
    seen = []

    def tick(text):
        # While it runs, the session must exist for someone to attach to.
        alive = subprocess.run(["tmux", "has-session", "-t", session],
                               capture_output=True).returncode == 0
        seen.append(alive)
        return False

    try:
        outcome = run_job(job, config={}, use_tmux=True, on_tick=tick,
                          tick_seconds=0.1)
        assert outcome["session"] == session
        assert outcome["result"] == "It is a notes app."
        assert any(seen)
    finally:
        subprocess.run(["tmux", "kill-session", "-t", session], capture_output=True)


def test_the_reporter_streams_text_and_sees_a_cancel():
    from workflow.deploy.jobs import _Reporter

    class C(FakeClient):
        status = "running"

        def report(self, job_id, **fields):
            super().report(job_id, **fields)
            return {"id": job_id, "status": self.status}

    client = C()
    reporter = _Reporter(client, "j1", "wf-job-j1")

    assert reporter("→ Read\n") is False
    # Nothing new and the heartbeat isn't due: no report at all.
    assert reporter("") is False
    client.status = "cancelled"
    assert reporter("more\n") is True

    assert client.reports[0] == {"job": "j1", "status": "running",
                                 "logAppend": "→ Read\n", "tmuxSession": "wf-job-j1"}
    assert len(client.reports) == 2
    assert "tmuxSession" not in client.reports[1]


def test_text_that_fails_to_send_is_kept_for_the_next_tick():
    from workflow.deploy.jobs import _Reporter

    class Flaky(FakeClient):
        fail = True

        def report(self, job_id, **fields):
            if self.fail:
                raise NotesGraphError("blip")
            super().report(job_id, **fields)
            return {"status": "running"}

    client = Flaky()
    reporter = _Reporter(client, "j1", None)
    reporter("line 1\n")
    client.fail = False
    reporter("line 2\n")
    assert client.reports[0]["logAppend"] == "line 1\nline 2\n"


def test_client_builds_the_documented_routes(monkeypatch):
    calls = []

    def fake_request(self, method, path, body=None):
        calls.append((method, path, body))
        return {"job": payload()} if "claim" in path else {}

    monkeypatch.setattr(JobClient, "_request", fake_request)
    client = JobClient(url="https://app.notesgraph.com", workspace="ws-1", token="pat")

    client.claim("laptop", "runner-1")
    client.report("j1", status="done", result="x")

    assert calls[0][0] == "POST"
    assert calls[0][1] == "/api/inventory/workspaces/ws-1/devices/laptop/jobs/claim"
    assert calls[0][2]["runnerId"] == "runner-1"
    assert calls[1][1] == "/api/inventory/workspaces/ws-1/jobs/j1/report"
    # every report renews the lease, so a long run is not re-claimed
    assert calls[1][2]["leaseSeconds"] > 0


def test_a_missing_token_is_a_clear_error():
    client = JobClient(url="https://app.notesgraph.com", workspace="ws-1", token=None)
    client.token = None
    with pytest.raises(NotesGraphError, match="notes-login"):
        client.claim("laptop", "runner-1")


# --- narrating background agents ------------------------------------------

def test_the_last_result_is_the_outcome_and_subagents_are_marked():
    import io

    from workflow.deploy.job_exec import Narrator

    out, log = io.StringIO(), io.StringIO()
    n = Narrator(out, log)
    for event in [
        {"type": "system", "subtype": "init", "model": "m", "cwd": "/w"},
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Agent", "input": {"description": "look"}}]}},
        {"type": "system", "subtype": "init", "model": "m", "cwd": "/w",
         "parent_tool_use_id": "t1"},
        {"type": "assistant", "parent_tool_use_id": "t1", "message": {"content": [
            {"type": "tool_use", "name": "Grep", "input": {"pattern": "x"}}]}},
        {"type": "result", "subtype": "success", "result": "early", "num_turns": 16},
        {"type": "system", "subtype": "init", "model": "m", "cwd": "/w"},
        {"type": "result", "subtype": "error_max_turns", "is_error": True,
         "num_turns": 25, "total_cost_usd": 4.0},
    ]:
        n.feed(json.dumps(event))
    n.finish()

    text = out.getvalue()
    assert text.count("▶ started") == 1 and "▶ continuing" in text
    assert '    ↳ → Grep  {"pattern": "x"}' in text
    assert "✓ finished" not in text
    assert text.rstrip().endswith("✗ failed after 25 turns · $4.0000: error_max_turns")
    assert n.result is None and n.error == "error_max_turns"
    assert n.steps == 25
