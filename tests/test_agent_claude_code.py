"""Tests for claude-code agent jobs: asking the user, permissions, and the
Claude Code command line the runner builds.

The questions API is served by a real local HTTP server, so the polling,
the waiting marker and cancellation are exercised over actual requests.
"""

import json
import os
import stat
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from workflow.deploy import agent_mcp
from workflow.deploy.jobs import (CLAUDE_CODE_SYSTEM_PROMPT, Job, JobClient,
                                  claude_code_argv, run_job)


class FakeInventory:
    """Just enough of the questions API: ask, poll, and an answer to give."""

    def __init__(self, answer=None, allowed=None, answer_after=0.0, job_status="running"):
        self.answer, self.allowed = answer, allowed
        self.answer_after, self.job_status = answer_after, job_status
        self.asked = []
        self.polls = 0
        inventory = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, payload):
                body = json.dumps(payload).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                assert self.headers["Authorization"] == "Bearer pat"
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                inventory.asked.append(body)
                inventory.asked_at = time.monotonic()
                self._send({"question": {"id": "q1", "answeredAt": None, **body}})

            def do_GET(self):
                inventory.polls += 1
                answered = time.monotonic() - inventory.asked_at >= inventory.answer_after
                question = {"id": "q1", "answeredAt": 1.0 if answered else None,
                            "answer": inventory.answer if answered else None,
                            "allowed": inventory.allowed if answered else None}
                self._send({"question": question, "jobStatus": inventory.job_status})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def questions(self, job_dir=None):
        return agent_mcp.Questions(self.url, "ws-1", "job-1", "pat", job_dir,
                                   poll_seconds=0.05)

    def close(self):
        self.server.shutdown()


@pytest.fixture
def inventory():
    made = []

    def make(**kw):
        made.append(FakeInventory(**kw))
        return made[-1]
    yield make
    for inv in made:
        inv.close()


def result_text(result):
    return result["content"][0]["text"]


# --- the MCP server -------------------------------------------------------

def test_it_speaks_enough_mcp_to_be_loaded():
    q = agent_mcp.Questions("http://x", "ws", "job", "pat")
    init = agent_mcp.handle(q, {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                "params": {"protocolVersion": "2025-06-18"}})
    assert init["result"]["capabilities"] == {"tools": {}}
    assert init["result"]["protocolVersion"] == "2025-06-18"

    assert agent_mcp.handle(q, {"jsonrpc": "2.0", "method": "notifications/initialized"}) is None

    tools = agent_mcp.handle(q, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    assert {t["name"] for t in tools["result"]["tools"]} == {"ask_user", "approve"}


def test_ask_user_waits_for_the_answer_and_returns_it(inventory, tmp_path):
    inv = inventory(answer="My son, 7 years old", answer_after=0.3)
    seen_marker = []

    def watch():
        # While the question is open the runner must be able to see it.
        for _ in range(40):
            seen_marker.extend(p.name for p in tmp_path.glob("waiting-*"))
            time.sleep(0.02)
    threading.Thread(target=watch, daemon=True).start()

    result = agent_mcp.call_tool(inv.questions(tmp_path), "ask_user",
                                 {"question": "Who is Cosmo, and how old?"})

    assert result_text(result) == "My son, 7 years old"
    assert result["isError"] is False
    assert inv.asked == [{"kind": "question", "text": "Who is Cosmo, and how old?", "detail": None}]
    assert inv.polls >= 2
    assert "waiting-q1" in seen_marker
    assert not list(tmp_path.glob("waiting-*")), "marker must go once answered"


def test_a_cancelled_run_stops_waiting(inventory, tmp_path):
    inv = inventory(answer_after=60, job_status="cancelled")
    result = agent_mcp.call_tool(inv.questions(tmp_path), "ask_user", {"question": "hello?"})
    assert result["isError"] is True
    assert "Do not continue" in result_text(result)
    assert not list(tmp_path.glob("waiting-*"))


def test_an_allowed_permission_returns_claude_codes_allow_shape(inventory):
    inv = inventory(allowed=True)
    result = agent_mcp.call_tool(inv.questions(), "approve",
                                 {"tool_name": "Bash", "input": {"command": "ls"}})
    assert json.loads(result_text(result)) == {"behavior": "allow",
                                               "updatedInput": {"command": "ls"}}
    assert inv.asked[0]["kind"] == "permission"
    assert inv.asked[0]["text"] == "Allow Bash?"
    assert '"command": "ls"' in inv.asked[0]["detail"]


def test_a_denied_permission_passes_on_what_the_user_said(inventory):
    inv = inventory(allowed=False, answer="not on my laptop")
    result = agent_mcp.call_tool(inv.questions(), "approve",
                                 {"tool_name": "Bash", "input": {"command": "rm -rf build"}})
    decision = json.loads(result_text(result))
    assert decision["behavior"] == "deny"
    assert "not on my laptop" in decision["message"]


def test_the_server_answers_over_stdio(inventory, tmp_path):
    """Run the module as Claude Code would, and ask through it."""
    import subprocess

    inv = inventory(answer="Cosmo is my son", answer_after=0.5)
    env = {**os.environ, "NG_URL": inv.url, "NG_WORKSPACE": "ws-1",
           "NG_JOB": "job-1", "NG_TOKEN": "pat", "NG_JOB_DIR": str(tmp_path)}
    proc = subprocess.Popen([sys.executable, "-m", "workflow.deploy.agent_mcp"],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            text=True, env=env)
    def send(message):
        proc.stdin.write(json.dumps(message) + "\n")
        proc.stdin.flush()

    try:
        send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        assert json.loads(proc.stdout.readline())["id"] == 1
        send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        send({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
              "params": {"name": "ask_user", "arguments": {"question": "Who is Cosmo?"}}})
        # A ping sent while the question is open is answered straight away.
        send({"jsonrpc": "2.0", "id": 3, "method": "ping"})
        replies = {}
        while 2 not in replies:
            reply = json.loads(proc.stdout.readline())
            replies[reply["id"]] = reply
        assert replies[2]["result"]["content"][0]["text"] == "Cosmo is my son"
        assert 3 in replies
    finally:
        proc.stdin.close()
        proc.wait(timeout=10)


# --- the command line -----------------------------------------------------

def job(**kw):
    return Job.from_payload({"id": "job-1234abcd", "agentId": "a", "agentName": "shows",
                             "instructions": "Find a show to watch with Cosmo",
                             "context": "", "model": "claude-code", **kw})


def client():
    return JobClient(url="https://app.notesgraph.com", workspace="ws-1", token="pat")


def test_claude_code_gets_notesgraph_and_a_way_to_ask(tmp_path):
    argv, env = claude_code_argv(job(), "Find a show to watch with Cosmo", tmp_path, client())

    assert argv[0] == "claude" and "--print" in argv
    assert argv[-1] == "Find a show to watch with Cosmo"
    assert argv[argv.index("--permission-prompt-tool") + 1] == "mcp__run__approve"
    assert argv[argv.index("--append-system-prompt") + 1] == CLAUDE_CODE_SYSTEM_PROMPT
    allowed = next(a for a in argv if a.startswith("--allowedTools="))
    assert "mcp__notesgraph" in allowed and "mcp__run__ask_user" in allowed
    assert "Bash" not in allowed, "the shell must go through the permission prompt"
    assert int(env["MCP_TOOL_TIMEOUT"]) >= 60 * 60 * 1000

    config = json.loads((tmp_path / "mcp.json").read_text())["mcpServers"]
    assert config["notesgraph"]["url"] == "https://app.notesgraph.com/api/workspaces/ws-1/mcp"
    assert config["notesgraph"]["headers"]["Authorization"] == "Bearer pat"
    assert config["run"]["env"]["NG_JOB"] == "job-1234abcd"
    # It holds the token: owner-only.
    assert stat.S_IMODE((tmp_path / "mcp.json").stat().st_mode) == 0o600


def test_the_system_prompt_asks_before_guessing_and_saves_answers():
    prompt = CLAUDE_CODE_SYSTEM_PROMPT
    assert "Who is Cosmo" in prompt
    assert "mcp__run__ask_user" in prompt
    assert "Agent memory" in prompt
    assert "keyword_search" in prompt


def test_a_claude_code_job_needs_a_connection(tmp_path, monkeypatch):
    monkeypatch.setattr("workflow.deploy.jobs.JOB_ROOT", tmp_path)
    with pytest.raises(RuntimeError, match="NotesGraph connection"):
        run_job(job(), config={}, use_tmux=False)


def test_the_token_file_is_removed_after_the_run(tmp_path, monkeypatch):
    monkeypatch.setattr("workflow.deploy.jobs.JOB_ROOT", tmp_path)

    def fake_argv(j, prompt, job_dir, c):
        (job_dir / "mcp.json").write_text("{}")
        return [sys.executable, "-c", "print('done')"], {}
    monkeypatch.setattr("workflow.deploy.jobs.claude_code_argv", fake_argv)

    out = run_job(job(), config={}, use_tmux=False, client=client(), tick_seconds=0.05)
    assert out["result"] == "done"
    assert not (tmp_path / "job-1234abcd" / "mcp.json").exists()


def test_time_spent_waiting_for_the_user_does_not_count(tmp_path, monkeypatch):
    """A 1s limit, a run that spends 2s waiting on a question: it finishes."""
    monkeypatch.setattr("workflow.deploy.jobs.JOB_ROOT", tmp_path)
    marker = tmp_path / "job-1234abcd" / "waiting-q1"
    script = (f"import pathlib, time; m = pathlib.Path({str(marker)!r}); "
              "m.write_text('q'); time.sleep(2); m.unlink(); print('answered')")
    monkeypatch.setattr("workflow.deploy.jobs.claude_code_argv",
                        lambda j, p, d, c: ([sys.executable, "-c", script], {}))

    out = run_job(job(), config={}, timeout=1, use_tmux=False, client=client(),
                  tick_seconds=0.1)
    assert out["result"] == "answered"


def test_without_a_question_the_limit_still_applies(tmp_path, monkeypatch):
    monkeypatch.setattr("workflow.deploy.jobs.JOB_ROOT", tmp_path)
    monkeypatch.setattr("workflow.deploy.jobs.claude_code_argv",
                        lambda j, p, d, c: ([sys.executable, "-c", "import time; time.sleep(5)"], {}))
    with pytest.raises(RuntimeError, match="exceeded 1s"):
        run_job(job(), config={}, timeout=1, use_tmux=False, client=client(),
                tick_seconds=0.1)
