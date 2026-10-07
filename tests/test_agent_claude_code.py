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
from workflow.backends.base import Issue
from workflow.deploy.jobs import (CLAUDE_CODE_SYSTEM_PROMPT, RESEARCH_SYSTEM_PROMPT,
                                  Job, JobClient, claude_code_argv, research_argv,
                                  run_job, start_job_task)
from workflow.session import task_branch_name


class FakeInventory:
    """Just enough of the questions API: ask, poll, and an answer to give."""

    def __init__(self, answer=None, allowed=None, answer_after=0.0, job_status="running"):
        self.answer, self.allowed = answer, allowed
        self.answer_after, self.job_status = answer_after, job_status
        self.asked = []
        self.titles = []
        self.answers = []
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
                if self.path.endswith("/title"):
                    inventory.titles.append((self.path, body))
                    self._send({"job": {"id": "job-1", "title": body["title"]}})
                    return
                if self.path.endswith("/answer"):
                    # Answered from the Mac's notification.
                    inventory.answers.append((self.path, body))
                    inventory.answer, inventory.allowed = body.get("answer"), body.get("allowed")
                    inventory.answer_after = 0
                    self._send({"question": {"id": "q1", "answeredAt": 1.0, **body}})
                    return
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


REAL_NOTIFY = agent_mcp.notify
REAL_SHOW_QUESTION = agent_mcp.show_question


@pytest.fixture(autouse=True)
def notified(monkeypatch):
    """Keep tests from popping real notifications; record what would show."""
    shown = []
    monkeypatch.setattr(agent_mcp, "notify", lambda *a: shown.append(a))
    monkeypatch.setattr(agent_mcp, "show_question",
                        lambda qid, kind, heading, title, text, *rest: shown.append((heading, title, text)))
    return shown


class FakeNotifier:
    """Stands in for MacQuestion: gives its reply, or waits to be taken away."""

    def __init__(self, reply=None):
        self.reply = reply
        self.gone = threading.Event()

    def answer(self):
        if self.reply is None:
            self.gone.wait()
        return self.reply

    def take_away(self):
        self.gone.set()


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
    assert {t["name"] for t in tools["result"]["tools"]} == {"ask_user", "approve", "set_title"}


def test_set_title_renames_the_job_without_waiting(inventory):
    inv = inventory()
    result = agent_mcp.call_tool(inv.questions(), "set_title",
                                 {"title": "  Pick a show\nto watch with Cosmo "})
    assert result["isError"] is False
    assert result_text(result) == "Run renamed to: Pick a show to watch with Cosmo"
    assert inv.titles == [("/api/inventory/workspaces/ws-1/jobs/job-1/title",
                           {"title": "Pick a show to watch with Cosmo"})]
    assert inv.asked == [], "naming the run is not a question"


def test_set_title_needs_a_title(inventory):
    inv = inventory()
    result = agent_mcp.call_tool(inv.questions(), "set_title", {"title": "  "})
    assert result["isError"] is True
    assert inv.titles == []


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
    assert inv.asked == [{"kind": "question", "text": "Who is Cosmo, and how old?",
                          "detail": None, "options": []}]
    assert inv.polls >= 2
    assert "waiting-q1" in seen_marker
    assert not list(tmp_path.glob("waiting-*")), "marker must go once answered"


def test_a_cancelled_run_stops_waiting(inventory, tmp_path):
    inv = inventory(answer_after=60, job_status="cancelled")
    result = agent_mcp.call_tool(inv.questions(tmp_path), "ask_user", {"question": "hello?"})
    assert result["isError"] is True
    assert "Do not continue" in result_text(result)
    assert not list(tmp_path.glob("waiting-*"))


def test_asking_shows_a_notification_named_after_the_run(inventory, notified):
    inv = inventory(answer="yes", allowed=True)
    q = inv.questions()
    agent_mcp.call_tool(q, "set_title", {"title": "Pick a show"})
    agent_mcp.call_tool(q, "ask_user", {"question": "Who is Cosmo?"})
    agent_mcp.call_tool(q, "approve", {"tool_name": "Bash", "input": {"command": "ls"}})
    assert notified == [("Agent has a question", "Pick a show", "Who is Cosmo?"),
                        ("Agent needs permission", "Pick a show", "Allow Bash?")]


def test_notify_passes_text_as_arguments_and_does_not_wait(monkeypatch):
    real = REAL_NOTIFY
    started = []
    monkeypatch.setattr(agent_mcp.sys, "platform", "darwin")
    monkeypatch.delenv("NG_NOTIFY", raising=False)
    monkeypatch.setattr(agent_mcp.subprocess, "Popen", lambda argv, **kw: started.append((argv, kw)))

    real("Agent has a question", "", 'Say "hi" \\ bye')
    argv, kw = started[0]
    assert argv[0] == "osascript"
    assert argv[-3:] == ["Agent has a question", "", 'Say "hi" \\ bye']
    assert kw["stdout"] is agent_mcp.subprocess.DEVNULL, "stdout is the MCP protocol"

    monkeypatch.setenv("NG_NOTIFY", "0")
    real("t", "", "m")
    monkeypatch.setattr(agent_mcp.sys, "platform", "linux")
    monkeypatch.delenv("NG_NOTIFY")
    real("t", "", "m")
    assert len(started) == 1


# --- answering from the Mac's notification ---------------------------------

def test_an_answer_given_on_the_mac_is_sent_to_notesgraph(inventory, monkeypatch):
    inv = inventory(answer_after=60)  # nobody answers in NotesGraph
    monkeypatch.setattr(agent_mcp, "show_question",
                        lambda *a: FakeNotifier({"answer": "Breaking Bad"}))
    result = agent_mcp.call_tool(inv.questions(), "ask_user",
                                 {"question": "Which show?", "options": ["Breaking Bad", "Bluey"]})
    assert result_text(result) == "Breaking Bad"
    assert inv.answers == [("/api/inventory/workspaces/ws-1/jobs/job-1/questions/q1/answer",
                            {"answer": "Breaking Bad"})]


def test_a_permission_allowed_on_the_mac_is_sent_as_allowed(inventory, monkeypatch):
    inv = inventory(answer_after=60)
    monkeypatch.setattr(agent_mcp, "show_question",
                        lambda *a: FakeNotifier({"allowed": True}))
    result = agent_mcp.call_tool(inv.questions(), "approve",
                                 {"tool_name": "Bash", "input": {"command": "ls"}})
    assert json.loads(result_text(result))["behavior"] == "allow"
    assert inv.answers[0][1] == {"allowed": True}


def test_answered_in_notesgraph_takes_the_mac_notification_away(inventory, monkeypatch):
    inv = inventory(answer="yes", answer_after=0.1)
    notifier = FakeNotifier()
    monkeypatch.setattr(agent_mcp, "show_question", lambda *a: notifier)
    result = agent_mcp.call_tool(inv.questions(), "ask_user", {"question": "Go on?"})
    assert result_text(result) == "yes"
    assert notifier.gone.is_set()
    assert inv.answers == []


def test_notifications_off_for_the_app_falls_back_to_a_plain_one(inventory, monkeypatch, notified):
    inv = inventory(answer="yes", answer_after=0.3)
    monkeypatch.setattr(agent_mcp, "show_question",
                        lambda *a: FakeNotifier({"shown": False, "error": "denied"}))
    agent_mcp.call_tool(inv.questions(), "ask_user", {"question": "Go on?"})
    assert notified == [("Agent has a question", "", "Go on?")]
    assert inv.answers == []


class FakeOpen:
    """`open -W` of the notifier app: writes what the app would say, on exit."""

    def __init__(self, argv, **kw):
        self.argv, self.kw = argv, kw
        self.out = argv[argv.index("--stdout") + 1]
        self.ask = json.loads(argv[-1])
        self.code = None

    def say(self, line):
        with open(self.out, "w") as f:
            f.write(line)
        self.code = 0

    def poll(self):
        return self.code

    def wait(self):
        while self.code is None:
            cancel = os.path.join(self.ask["dir"], self.ask["id"] + ".cancel")
            if os.path.exists(cancel):
                self.code = 0
            time.sleep(0.01)
        return self.code


@pytest.fixture
def mac(monkeypatch, tmp_path):
    started = []
    monkeypatch.setattr(agent_mcp.sys, "platform", "darwin")
    monkeypatch.delenv("NG_NOTIFY", raising=False)
    monkeypatch.setattr(agent_mcp, "NOTIFIER_HOME", tmp_path)
    monkeypatch.setattr(agent_mcp, "notifier_app", lambda: tmp_path / "NotesGraph Agent.app")
    monkeypatch.setattr(agent_mcp.subprocess, "Popen",
                        lambda argv, **kw: started.append(FakeOpen(argv, **kw)) or started[-1])
    return started


def test_show_question_opens_the_app_with_the_whole_question(mac, tmp_path):
    shown = REAL_SHOW_QUESTION("q/1", "permission", "Agent needs permission", "Pick a show",
                               "Allow Bash?", '{"command":"ls"}', [])
    proc = mac[0]
    assert proc.argv[:3] == ["open", "-n", "-W"], "LaunchServices, or macOS won't let it notify"
    assert proc.argv[-3:-1] == [str(tmp_path / "NotesGraph Agent.app"), "--args"]
    assert proc.ask["id"] == "q_1", "the id names files"
    assert proc.ask["kind"] == "permission" and proc.ask["title"] == "Pick a show"
    assert proc.ask["detail"] == '{"command":"ls"}'
    assert proc.ask["parentPid"] == os.getpid()
    assert proc.kw["stdout"] is agent_mcp.subprocess.DEVNULL, "stdout is the MCP protocol"

    proc.say('{"allowed": true}\n')
    assert shown.answer() == {"allowed": True}
    assert not os.path.exists(proc.out)


def test_taking_a_mac_question_away_closes_the_app(mac):
    shown = REAL_SHOW_QUESTION("q1", "question", "Agent has a question", "", "Hi?", None, [])
    shown.take_away()
    assert shown.answer() is None
    assert not list((agent_mcp.NOTIFIER_HOME / "questions").iterdir()), "nothing left behind"


def test_show_question_without_swiftc_shows_a_plain_notification(monkeypatch, notified):
    monkeypatch.setattr(agent_mcp.sys, "platform", "darwin")
    monkeypatch.delenv("NG_NOTIFY", raising=False)
    monkeypatch.setattr(agent_mcp, "notifier_app", lambda: None)
    assert REAL_SHOW_QUESTION("q1", "question", "Agent has a question", "", "Hi?", None, []) is None
    assert notified == [("Agent has a question", "", "Hi?")]


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
           "NG_JOB": "job-1", "NG_TOKEN": "pat", "NG_JOB_DIR": str(tmp_path),
           "NG_NOTIFY": "0"}
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


class FakeOmniSeek:
    """OmniSeek's open liveness route, on a real local port."""

    def __enter__(self):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200 if self.path == "/healthz" else 404)
                self.end_headers()
                self.wfile.write(b'{"ok":true}')

            def log_message(self, *a):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        return self

    def __exit__(self, *a):
        self.server.shutdown()


@pytest.fixture
def omniseek_token(tmp_path, monkeypatch):
    path = tmp_path / "omniseek_http.json"
    path.write_text(json.dumps({"token": "seek-token"}))
    monkeypatch.setattr("workflow.deploy.jobs.OMNISEEK_TOKEN_PATH", path)
    return path


def test_research_gets_omniseek_on_top_of_claude_code(tmp_path, omniseek_token):
    job_dir = tmp_path / "job"
    job_dir.mkdir()
    with FakeOmniSeek() as seek:
        argv, env = research_argv(job(model="research"), "Survey X", job_dir, client(),
                                  {"agent": {"omniseek": {"url": seek.url}}})

    prompt = argv[argv.index("--append-system-prompt") + 1]
    assert prompt.startswith(CLAUDE_CODE_SYSTEM_PROMPT) and RESEARCH_SYSTEM_PROMPT in prompt
    allowed = next(a for a in argv if a.startswith("--allowedTools="))
    assert "mcp__omniseek" in allowed and "mcp__notesgraph" in allowed
    assert "Bash" not in allowed
    assert argv[-1] == "Survey X"

    servers = json.loads((job_dir / "mcp.json").read_text())["mcpServers"]
    assert servers["omniseek"] == {"type": "http", "url": f"{seek.url}/mcp",
                                   "headers": {"Authorization": "Bearer seek-token"}}
    assert {"notesgraph", "run"} <= servers.keys()
    assert stat.S_IMODE((job_dir / "mcp.json").stat().st_mode) == 0o600


def test_research_says_how_to_fix_a_missing_omniseek(tmp_path, monkeypatch):
    monkeypatch.setattr("workflow.deploy.jobs.OMNISEEK_TOKEN_PATH", tmp_path / "none.json")
    with pytest.raises(RuntimeError, match="isn't set up"):
        research_argv(job(model="research"), "Survey X", tmp_path, client(), {})


def test_research_says_how_to_fix_a_stopped_omniseek(tmp_path, omniseek_token):
    with FakeOmniSeek() as seek:
        stopped = seek.url
    with pytest.raises(RuntimeError, match="isn't running"):
        research_argv(job(model="research"), "Survey X", tmp_path, client(),
                      {"agent": {"omniseek": {"url": stopped}}})


def test_the_system_prompt_asks_before_guessing_and_saves_answers():
    prompt = CLAUDE_CODE_SYSTEM_PROMPT
    assert "Who is Cosmo" in prompt
    assert "mcp__run__ask_user" in prompt
    assert "Agent memory" in prompt
    assert "keyword_search" in prompt


class FakeBackend:
    """A task backend that records what `wf start` would do to a ticket."""

    def __init__(self):
        self.issues, self.created, self.in_progress = {}, [], []

    def create_issue(self, summary, description, issue_type="Task", project_key=None):
        issue = Issue(f"NG-{len(self.issues) + 1}", summary, description)
        self.issues[issue.key] = issue
        self.created.append(issue)
        return issue

    def get(self, key):
        return self.issues[key]

    def move_to_in_progress(self, issue, custom_fields=None):
        self.in_progress.append(issue.key)


@pytest.fixture
def started(tmp_path, monkeypatch):
    """Stand in for the wf task setup, so run_job tests run just the job."""
    monkeypatch.setattr("workflow.deploy.jobs.JOB_ROOT", tmp_path)
    issue = Issue("NG-1", "Find a show", "")
    calls = {"saved": 0}
    monkeypatch.setattr("workflow.deploy.jobs.start_job_task",
                        lambda j, d, c: (issue, d, None))
    monkeypatch.setattr("workflow.deploy.jobs._job_context", lambda i, j, w: "go")
    monkeypatch.setattr("workflow.deploy.jobs._save_session",
                        lambda i, d, w: calls.__setitem__("saved", calls["saved"] + 1))
    monkeypatch.setattr("workflow.ai_providers.claude.skills_plugin_dir", lambda *a: None)
    monkeypatch.setattr("workflow.session.apply_claude_env", lambda env=None: {})
    return calls


def test_a_claude_code_job_needs_a_connection(tmp_path, monkeypatch):
    monkeypatch.setattr("workflow.deploy.jobs.JOB_ROOT", tmp_path)
    with pytest.raises(RuntimeError, match="NotesGraph connection"):
        run_job(job(), config={}, use_tmux=False)


def test_the_token_file_is_removed_after_the_run(tmp_path, monkeypatch, started):
    def fake_argv(j, prompt, job_dir, c, plugin_dir=None):
        (job_dir / "mcp.json").write_text("{}")
        return [sys.executable, "-c", "print('done')"], {}
    monkeypatch.setattr("workflow.deploy.jobs.claude_code_argv", fake_argv)

    out = run_job(job(model="workflow"), config={}, use_tmux=False, client=client(), tick_seconds=0.05)
    assert out["result"] == "done"
    assert not (tmp_path / "job-1234abcd" / "mcp.json").exists()
    assert started["saved"] == 1, "the run is filed in the task's memory"


def test_time_spent_waiting_for_the_user_does_not_count(tmp_path, monkeypatch, started):
    """A 1s limit, a run that spends 2s waiting on a question: it finishes."""
    marker = tmp_path / "job-1234abcd" / "waiting-q1"
    script = (f"import pathlib, time; m = pathlib.Path({str(marker)!r}); "
              "m.write_text('q'); time.sleep(2); m.unlink(); print('answered')")
    monkeypatch.setattr("workflow.deploy.jobs.claude_code_argv",
                        lambda *a: ([sys.executable, "-c", script], {}))

    out = run_job(job(model="workflow"), config={}, timeout=1, use_tmux=False, client=client(),
                  tick_seconds=0.1)
    assert out["result"] == "answered"


def test_without_a_question_the_limit_still_applies(tmp_path, monkeypatch, started):
    monkeypatch.setattr("workflow.deploy.jobs.claude_code_argv",
                        lambda *a: ([sys.executable, "-c", "import time; time.sleep(5)"], {}))
    with pytest.raises(RuntimeError, match="exceeded 1s"):
        run_job(job(model="workflow"), config={}, timeout=1, use_tmux=False, client=client(),
                tick_seconds=0.1)


def test_claude_code_has_no_turn_limit(tmp_path):
    """The step limit is for the in-tab loop; Claude Code counts each tool call."""
    argv, _ = claude_code_argv(job(maxSteps=8), "go", tmp_path, client())
    assert not any(a.startswith("--max-turns") for a in argv)


def test_the_tasks_skills_go_in_as_a_plugin(tmp_path):
    argv, _ = claude_code_argv(job(), "go", tmp_path, client(), tmp_path / "plugin")
    assert argv[argv.index("--plugin-dir") + 1] == str(tmp_path / "plugin")
    assert argv[-1] == "go"


# --- starting it as a wf task ---------------------------------------------

def git(cwd, *args):
    import subprocess
    return subprocess.run(["git", "-C", str(cwd), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "code" / "notes"
    root.mkdir(parents=True)
    git(root, "init", "-q", "-b", "main")
    git(root, "-c", "user.email=t@t", "-c", "user.name=t",
        "commit", "-q", "--allow-empty", "-m", "start")
    return root


@pytest.fixture
def wf(repo, monkeypatch):
    """wf configured with one repo and the fake backend."""
    backend = FakeBackend()
    monkeypatch.setattr("workflow.backends.get_backend", lambda cfg, force_type=None: backend)
    monkeypatch.setattr("workflow.config.load_effective_config", lambda: {})
    monkeypatch.setattr("workflow.config.is_git_enabled", lambda: True)
    monkeypatch.setattr("workflow.session.configured_repos",
                        lambda: {str(repo): {"base_branch": "main"}})
    monkeypatch.setattr("workflow.projects.get_current_project_repositories",
                        lambda: {str(repo): {}})
    monkeypatch.setattr("workflow.projects.get_default_repo", lambda project_name=None: None)
    monkeypatch.setattr("workflow.git_utils.add_claude_trust", lambda path: None)
    return backend


def test_a_job_becomes_a_task_in_progress_on_its_own_branch(repo, wf, tmp_path):
    job_dir = tmp_path / "jobs" / "j1"
    job_dir.mkdir(parents=True)

    issue, workdir, repo_path = start_job_task(job(), job_dir, repo)

    assert [i.title for i in wf.created] == ["Find a show to watch with Cosmo"]
    assert wf.in_progress == [issue.key]
    # wf start's branch and worktree: beside the repo, never in it.
    assert git(workdir, "branch", "--show-current") == "ng-1-Find-a-show-to-watch-with-Cosmo"
    assert workdir.parent == repo.parent and workdir.name.startswith("notes-ng-1-")
    assert repo_path == str(workdir)
    assert git(workdir, "config", "commit.template") == "[NG-1]"
    # The checkout the runner started in has not moved.
    assert git(repo, "branch", "--show-current") == "main"


def test_a_reclaimed_job_keeps_its_ticket_and_worktree(repo, wf, tmp_path):
    job_dir = tmp_path / "jobs" / "j1"
    job_dir.mkdir(parents=True)

    first = start_job_task(job(), job_dir, repo)
    again = start_job_task(job(), job_dir, repo)

    assert len(wf.created) == 1
    assert again[0].key == first[0].key and again[1] == first[1]


def test_outside_a_configured_repo_it_runs_in_the_job_directory(wf, tmp_path, monkeypatch):
    monkeypatch.setattr("workflow.session.configured_repos", lambda: {})
    job_dir = tmp_path / "jobs" / "j1"
    job_dir.mkdir(parents=True)
    issue, workdir, repo_path = start_job_task(job(), job_dir, tmp_path)
    assert workdir == job_dir and repo_path is None
    assert wf.in_progress == [issue.key]


def test_branch_names_drop_what_git_refuses():
    issue = Issue("NG-7", "Fix: the ~board? [mobile]", "")
    assert task_branch_name(issue) == "ng-7-Fix-the-board-mobile"


def test_wf_start_and_jobs_name_branches_the_same(monkeypatch):
    """create_branch (wf start's normal path) uses the shared name."""
    import workflow.git_utils as g
    names = []

    class FakeGit:
        def checkout(self, *args):
            names.append(args[-1])

    class FakeRepo:
        git = FakeGit()
    monkeypatch.setattr(g, "get_repo", lambda path=None: FakeRepo())
    monkeypatch.setattr(g, "select_base_branch", lambda base, path=None: base)
    monkeypatch.setattr(g, "checkout_branch", lambda name, path=None: True)
    issue = Issue("NG-7", "Fix: the board", "")
    g.create_branch(issue, "main", "/r", skip_uncommitted_check=True)
    assert names == [task_branch_name(issue)]


# --- a plain claude-code job ----------------------------------------------

def run_claude_code_in(repo, tmp_path, monkeypatch, script):
    """A plain claude-code job: its own worktree under the job directory."""
    monkeypatch.setattr("workflow.deploy.jobs.JOB_ROOT", tmp_path / "jobs")
    monkeypatch.setattr("workflow.deploy.jobs.claude_code_argv",
                        lambda *a: ([sys.executable, "-c", script], {}))
    return run_job(job(), config={}, use_tmux=False, client=client(),
                   tick_seconds=0.05, cwd=repo)


def test_it_branches_and_commits_in_its_own_worktree(repo, tmp_path, monkeypatch):
    script = ("import os, subprocess as s; print(os.getcwd()); "
              "s.run(['git', 'checkout', '-q', '-b', 'agent-work'], check=True); "
              "s.run(['git', '-c', 'user.email=a@a', '-c', 'user.name=a', 'commit', "
              "'-q', '--allow-empty', '-m', 'agent'], check=True)")
    out = run_claude_code_in(repo, tmp_path, monkeypatch, script)

    assert out["result"].endswith("job-1234abcd/repo")
    # The checkout someone is working in has not moved...
    assert git(repo, "branch", "--show-current") == "main"
    # ...and the agent's branch is a branch of the same repo.
    assert git(repo, "log", "-1", "--format=%s", "agent-work") == "agent"
    # Everything is on a branch, so the worktree itself is gone.
    assert not (tmp_path / "jobs" / "job-1234abcd" / "repo").exists()


def test_a_worktree_with_uncommitted_changes_is_kept(repo, tmp_path, monkeypatch):
    run_claude_code_in(repo, tmp_path, monkeypatch,
                       "open('half-done.txt', 'w').write('x'); print('stopped')")
    tree = tmp_path / "jobs" / "job-1234abcd" / "repo"
    assert (tree / "half-done.txt").exists()
    assert not (repo / "half-done.txt").exists()


def test_outside_a_repo_it_runs_in_the_job_directory(tmp_path, monkeypatch):
    plain = tmp_path / "plain"
    plain.mkdir()
    out = run_claude_code_in(plain, tmp_path, monkeypatch, "import os; print(os.getcwd())")
    assert out["result"].endswith("job-1234abcd")


def test_a_claude_code_job_is_not_a_wf_task(repo, tmp_path, monkeypatch):
    """Only the workflow model makes tickets and wf branches."""
    def no_task(*a):
        raise AssertionError("a claude-code job must not start a wf task")
    monkeypatch.setattr("workflow.deploy.jobs.start_job_task", no_task)
    out = run_claude_code_in(repo, tmp_path, monkeypatch, "print('ok')")
    assert out["result"] == "ok"


def test_a_workflow_job_needs_a_connection(tmp_path, monkeypatch):
    monkeypatch.setattr("workflow.deploy.jobs.JOB_ROOT", tmp_path)
    with pytest.raises(RuntimeError, match="workflow job needs a NotesGraph connection"):
        run_job(job(model="workflow"), config={}, use_tmux=False)


def test_a_workflow_job_asks_and_remembers_like_claude_code(tmp_path, monkeypatch, started):
    """Workflow is Claude Code started as a wf task: same ask/approve tools,
    same ask-before-guessing and save-the-answer system prompt."""
    monkeypatch.setattr("workflow.deploy.jobs._watch",
                        lambda *a: {"result": "", "steps": 1, "session": None})
    run_job(job(model="workflow"), config={}, use_tmux=False, client=client())

    spec = json.loads((tmp_path / "job-1234abcd" / "cmd.json").read_text())
    argv = spec["argv"]
    assert spec["provider"] == "workflow"
    assert argv[argv.index("--permission-prompt-tool") + 1] == "mcp__run__approve"
    assert argv[argv.index("--append-system-prompt") + 1] == CLAUDE_CODE_SYSTEM_PROMPT
    allowed = next(a for a in argv if a.startswith("--allowedTools="))
    assert "mcp__run__ask_user" in allowed and "mcp__notesgraph" in allowed
    assert int(spec["env"]["MCP_TOOL_TIMEOUT"]) >= 60 * 60 * 1000


def test_the_servers_profile_is_the_agent_when_it_sends_one(tmp_path):
    profile = {"systemPrompt": "Be the agent the server says.",
               "allowedTools": ["mcp__notesgraph", "mcp__run__ask_user", "WebSearch"],
               "mcpServers": ["notesgraph", "run"], "workdir": "repo",
               "toolTimeoutMs": 1234}
    argv, env = claude_code_argv(job(profile=profile), "go", tmp_path, client())

    assert argv[argv.index("--append-system-prompt") + 1] == "Be the agent the server says."
    assert next(a for a in argv if a.startswith("--allowedTools=")) == \
        "--allowedTools=mcp__notesgraph,mcp__run__ask_user,WebSearch"
    assert env["MCP_TOOL_TIMEOUT"] == "1234"


def test_research_takes_the_whole_prompt_and_tools_from_the_profile(tmp_path, omniseek_token):
    profile = {"systemPrompt": "notes part\nresearch part",
               "allowedTools": ["mcp__notesgraph", "mcp__omniseek"]}
    job_dir = tmp_path / "job"
    job_dir.mkdir()
    with FakeOmniSeek() as seek:
        argv, _ = research_argv(job(model="research", profile=profile), "Survey X", job_dir,
                                client(), {"agent": {"omniseek": {"url": seek.url}}})

    # Not the built-in research prompt added on top: the server's already has it.
    assert argv[argv.index("--append-system-prompt") + 1] == "notes part\nresearch part"
    assert next(a for a in argv if a.startswith("--allowedTools=")) == \
        "--allowedTools=mcp__notesgraph,mcp__omniseek"
    assert "omniseek" in json.loads((job_dir / "mcp.json").read_text())["mcpServers"]


def test_prepare_task_hands_a_cloud_runner_the_task_its_agent_works_in(repo, wf, tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from workflow.cli_agent import agent_app

    monkeypatch.setattr("workflow.deploy.jobs._job_context",
                        lambda issue, job, workdir: f"wf ai context for {issue.key}")
    monkeypatch.setattr("workflow.ai_providers.claude.skills_plugin_dir",
                        lambda issue, key, task_dir, repo_path: tmp_path / "plugin")
    job_dir = tmp_path / "jobs" / "job-1234"
    job_dir.mkdir(parents=True)
    prompt = tmp_path / "prompt.md"
    prompt.write_text("Find a show to watch with Cosmo\n\n--- context ---\nnote")
    out = job_dir / "automation.json"

    result = CliRunner().invoke(agent_app, [
        "prepare-task", "--job-dir", str(job_dir), "--repo", str(repo),
        "--prompt-file", str(prompt), "--out", str(out)])

    assert result.exit_code == 0, result.output
    handed = json.loads(out.read_text())
    assert handed["key"] == "NG-1" and wf.in_progress == ["NG-1"]
    assert git(handed["cwd"], "branch", "--show-current") == "ng-1-Find-a-show-to-watch-with-Cosmo"
    assert handed["prompt"] == "wf ai context for NG-1"
    assert handed["plugins"] == [str(tmp_path / "plugin")]
