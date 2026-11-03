"""Tests for profile automations on the Mac runner (setup, tools, teardown
around a Claude job, as the cloud runner has them) and for the run tools
that upload artifacts and show them with a question."""

import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from workflow.deploy import agent_mcp, automation_mcp, job_exec, profile_automation as pa
from workflow.deploy.jobs import (Job, JobClient, claude_code_argv, is_profiled_claude_job,
                                  prepare_automation)


# --- rendering and environment ----------------------------------------------

def test_placeholders_render_and_shell_braces_are_left_alone():
    values = pa.placeholders("job-1234abcd-rest", Path("/jobs/j"), Path("/cache"),
                             "git@github.com:a/b.git", "main", Path("/jobs/j/prompt.md"))
    assert values["job_short"] == "job-1234" and values["branch"] == "cloud/job-1234"
    rendered = pa.render("cd {job_dir} && git clone {repo} {cache}/x && echo ${HOME} ${{x}} {unknown}",
                         values)
    assert rendered == ("cd /jobs/j && git clone git@github.com:a/b.git /cache/x && "
                        "echo ${HOME} ${{x}} {unknown}")
    assert pa.render("cat {prompt_file}", values) == "cat /jobs/j/prompt.md"


def test_a_tools_params_become_arg_variables_and_required_ones_are_required():
    spec = {"name": "render", "run": "x",
            "params": {"path": {"required": True}, "max-results": {}, "mode": {}}}
    env = pa.tool_env(spec, {"path": "a.mp4", "max-results": 3, "mode": ""}, {"PATH": "/bin"})
    assert env == {"PATH": "/bin", "ARG_PATH": "a.mp4", "ARG_MAX_RESULTS": "3"}
    with pytest.raises(KeyError) as missing:
        pa.tool_env(spec, {}, {})
    assert missing.value.args[0] == "path"
    assert pa.arg_var("num speakers") == "ARG_NUM_SPEAKERS"


def test_a_tool_runs_with_its_arguments_where_the_agent_works(tmp_path):
    spec = {"name": "where", "run": "echo \"$ARG_PATH in $AGENT_CWD\"; pwd",
            "params": {"path": {"required": True}}}
    text, ok = pa.run_tool(spec, {"path": "clip.mp4"}, {}, str(tmp_path),
                           {**pa.shell_env("j", tmp_path, str(tmp_path))})
    assert ok
    assert text.splitlines()[0] == "exit code 0"
    assert f"clip.mp4 in {tmp_path}" in text and str(tmp_path.resolve()) in text


def test_a_failing_or_slow_tool_says_so():
    text, ok = pa.run_tool({"name": "bad", "run": "echo boom; exit 3"}, {}, {}, None, {"PATH": os.environ["PATH"]})
    assert not ok and text.startswith("exit code 3") and "boom" in text
    text, ok = pa.run_tool({"name": "slow", "run": "sleep 5", "timeout": 1}, {}, {}, None,
                           {"PATH": os.environ["PATH"]})
    assert not ok and text.startswith("timed out after 1s")


def test_steps_stop_at_a_failing_one_unless_it_is_optional(tmp_path):
    said = []
    env = {"PATH": os.environ["PATH"]}
    pa.run_steps("setup", [
        {"name": "first", "run": "echo one > {job_dir}/one"},
        {"name": "flaky", "run": "exit 1", "optional": True},
        {"name": "second", "run": "echo two > {job_dir}/two"},
    ], {"job_dir": str(tmp_path)}, str(tmp_path), env, said.append)
    assert (tmp_path / "one").exists() and (tmp_path / "two").exists()
    assert [line.split(" · ")[1][:9] for line in said] == ["first ✓ (", "flaky ✗ e", "second ✓ "]

    with pytest.raises(pa.StepFailed) as failed:
        pa.run_steps("setup", [
            {"name": "clone", "run": "echo 'fatal: no repo' >&2; exit 128"},
            {"name": "never", "run": "echo three > {job_dir}/three"},
        ], {"job_dir": str(tmp_path)}, str(tmp_path), env, said.append)
    assert "setup step 'clone' failed (exit 128)" in str(failed.value)
    assert "fatal: no repo" in str(failed.value)
    assert not (tmp_path / "three").exists()


# --- the tools as an MCP server ----------------------------------------------

def test_the_automation_server_lists_the_tools_and_runs_them(tmp_path):
    spec_file = pa.write_tools_spec(
        tmp_path,
        [{"name": "transcribe", "description": "Transcribe a clip.",
          "run": "echo \"transcribed $ARG_CLIP\"",
          "params": {"clip": {"description": "The clip.", "required": True},
                     "speakers": {"description": "How many."}}}],
        {"job_dir": str(tmp_path)}, str(tmp_path), pa.shell_env("j", tmp_path))
    tools = automation_mcp.Tools.load(spec_file)

    listed = automation_mcp.handle(tools, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    tool = listed["result"]["tools"][0]
    assert tool["name"] == "transcribe"
    assert tool["inputSchema"]["required"] == ["clip"]
    assert set(tool["inputSchema"]["properties"]) == {"clip", "speakers"}

    ran = automation_mcp.handle(tools, {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                                        "params": {"name": "transcribe", "arguments": {"clip": "a.mp4"}}})
    assert ran["result"]["isError"] is False
    assert "transcribed a.mp4" in ran["result"]["content"][0]["text"]

    missing = automation_mcp.handle(tools, {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                                            "params": {"name": "transcribe", "arguments": {}}})
    assert missing["result"]["isError"] is True and "clip" in missing["result"]["content"][0]["text"]

    init = automation_mcp.handle(tools, {"jsonrpc": "2.0", "id": 4, "method": "initialize", "params": {}})
    assert init["result"]["serverInfo"]["name"] == "automation"


# --- around the agent, in job_exec --------------------------------------------

def _cmd(job_dir: Path, automation: dict, argv=None) -> None:
    (job_dir / "cmd.json").write_text(json.dumps({
        "argv": argv or [sys.executable, "-c", "print('hello from the agent')"],
        "provider": "video-editor", "env": {}, "cwd": str(job_dir / "work"),
        "claude": True, "automation": automation,
    }))


def test_setup_runs_before_the_agent_and_teardown_after(tmp_path):
    env = pa.shell_env("job-1", tmp_path)
    _cmd(tmp_path, {
        "setup": [{"name": "prepare", "run": "mkdir -p {job_dir}/work && echo ready > {job_dir}/work/marker"}],
        "teardown": [{"name": "clean", "run": "echo done-in-$AGENT_CWD > {job_dir}/teardown"}],
        "values": {"job_dir": str(tmp_path)}, "env": env,
    })
    code = job_exec.run(tmp_path, out=open(os.devnull, "w"))

    assert code == 0
    outcome = json.loads((tmp_path / "result.json").read_text())
    assert outcome["result"] == "hello from the agent" and not outcome["error"]
    assert (tmp_path / "work" / "marker").read_text().strip() == "ready"
    assert (tmp_path / "teardown").read_text().strip() == f"done-in-{tmp_path / 'work'}"
    log = (tmp_path / "log.txt").read_text()
    assert "⚙ setup · prepare ✓" in log and "⚙ teardown · clean ✓" in log
    assert log.index("prepare ✓") < log.index("hello from the agent") < log.index("clean ✓")


def test_a_failing_setup_step_ends_the_run_without_the_agent(tmp_path):
    _cmd(tmp_path, {
        "setup": [{"name": "video-use", "run": "echo 'uv: command not found' >&2; exit 127"}],
        "teardown": [{"name": "clean", "run": "echo cleaned > {job_dir}/teardown"}],
        "values": {"job_dir": str(tmp_path)}, "env": pa.shell_env("job-1", tmp_path),
    }, argv=[sys.executable, "-c", f"open({str(tmp_path / 'agent-ran')!r}, 'w').close()"])
    code = job_exec.run(tmp_path, out=open(os.devnull, "w"))

    assert code != 0
    outcome = json.loads((tmp_path / "result.json").read_text())
    assert "setup step 'video-use' failed (exit 127)" in outcome["error"]
    assert "uv: command not found" in outcome["error"]
    assert not (tmp_path / "agent-ran").exists(), "the agent must not run in an unprepared job"
    assert (tmp_path / "teardown").exists(), "teardown runs however the run ended"
    assert (tmp_path / "exit_code").exists()


def test_setup_may_hand_over_where_the_agent_starts(tmp_path):
    handed = tmp_path / "elsewhere"
    _cmd(tmp_path, {
        "setup": [{"name": "prepare",
                   "run": f"mkdir -p {handed} && echo '{{\"cwd\": \"{handed}\"}}' > {{job_dir}}/automation.json"}],
        "teardown": [], "values": {"job_dir": str(tmp_path)}, "env": pa.shell_env("job-1", tmp_path),
    }, argv=[sys.executable, "-c", "import os; print(os.getcwd())"])
    assert job_exec.run(tmp_path, out=open(os.devnull, "w")) == 0
    assert json.loads((tmp_path / "result.json").read_text())["result"] == str(handed.resolve())


def test_a_job_without_automation_runs_as_before(tmp_path):
    (tmp_path / "cmd.json").write_text(json.dumps({
        "argv": [sys.executable, "-c", "print('plain')"], "provider": "claude", "env": {}, "cwd": None}))
    assert job_exec.run(tmp_path, out=open(os.devnull, "w")) == 0
    assert json.loads((tmp_path / "result.json").read_text())["result"] == "plain"


# --- preparing a job's automation in jobs.py -----------------------------------

VIDEO_PROFILE = {
    "systemPrompt": "You edit videos.",
    "allowedTools": ["mcp__notesgraph", "mcp__run__ask_user", "mcp__automation"],
    "mcpServers": ["notesgraph", "run"],
    "workdir": "job",
    "toolTimeoutMs": 86400000,
    "automation": {
        "setup": [{"name": "video-use", "run": "test -d {cache}/video-use || git clone x {cache}/video-use"}],
        "tools": [{"name": "transcribe", "description": "t", "run": "echo $ARG_CLIP",
                   "params": {"clip": {"description": "c", "required": True}}}],
        "teardown": [{"name": "tidy", "run": "rm -rf {job_dir}/tmp", "optional": True}],
        "cwd": "{job_dir}/project",
    },
    "skills": [],
}


def job(**kw):
    return Job.from_payload({"id": "job-1234abcd", "agentId": "a", "agentName": "editor",
                             "instructions": "Edit the footage", "context": "", **kw})


def client():
    return JobClient(url="https://app.notesgraph.com", workspace="ws-1", token="pat")


def test_a_model_the_runner_does_not_know_runs_on_its_server_profile():
    assert is_profiled_claude_job(job(model="video-editor", profile=VIDEO_PROFILE))
    assert not is_profiled_claude_job(job(model="claude-code", profile=VIDEO_PROFILE)), "has its own handling"
    assert not is_profiled_claude_job(job(model="video-editor")), "no profile: nothing to run it as"
    assert not is_profiled_claude_job(job(model="command", profile=VIDEO_PROFILE))


def test_a_job_profile_gets_setup_tools_and_teardown(tmp_path, monkeypatch):
    monkeypatch.setattr(pa, "CACHE_DIR", tmp_path / "cache")
    job_dir = tmp_path / "job"
    job_dir.mkdir()
    spec, tools_file, cwd = prepare_automation(job(model="video-editor", profile=VIDEO_PROFILE, title="Launch cut"),
                                               job_dir, tmp_path)

    assert cwd == job_dir / "project"
    assert [s["name"] for s in spec["setup"]] == ["video-use"]
    assert [s["name"] for s in spec["teardown"]] == ["tidy"]
    assert spec["values"]["cache"] == str(tmp_path / "cache") and (tmp_path / "cache").is_dir()
    assert spec["values"]["job_short"] == "job-1234" and spec["values"]["job_dir"] == str(job_dir)
    assert spec["env"]["AGENT_CWD"] == str(job_dir / "project") and spec["env"]["JOB_DIR"] == str(job_dir)
    assert "NG_TOKEN" not in spec["env"], "steps never see the runner's secrets"
    assert (job_dir / "prompt.md").read_text() == "Edit the footage"
    assert (job_dir / "title.txt").read_text() == "Launch cut"
    served = json.loads(tools_file.read_text())
    assert [t["name"] for t in served["tools"]] == ["transcribe"] and served["cwd"] == str(job_dir / "project")


def test_a_repo_profile_keeps_wfs_worktree_but_gets_the_tools(tmp_path, monkeypatch):
    monkeypatch.setattr(pa, "CACHE_DIR", tmp_path / "cache")
    profile = {**VIDEO_PROFILE, "workdir": "repo"}
    job_dir, worktree = tmp_path / "job", tmp_path / "job" / "repo"
    worktree.mkdir(parents=True)
    spec, tools_file, cwd = prepare_automation(job(model="claude-code", profile=profile), job_dir, tmp_path,
                                               worktree)
    assert cwd == worktree
    assert spec["setup"] == [] and spec["teardown"] == [], "wf prepared the worktree itself"
    assert json.loads(tools_file.read_text())["cwd"] == str(worktree)


def test_a_profile_without_automation_changes_nothing(tmp_path):
    spec, tools_file, cwd = prepare_automation(
        job(model="claude-code", profile={"systemPrompt": "x", "workdir": "repo"}), tmp_path, tmp_path)
    assert (spec, tools_file, cwd) == (None, None, None)
    assert not (tmp_path / "prompt.md").exists()


def test_claude_gets_the_automation_server_when_there_are_tools(tmp_path):
    tools_file = tmp_path / pa.TOOLS_FILE
    tools_file.write_text("{}")
    argv, _ = claude_code_argv(job(model="video-editor", profile=VIDEO_PROFILE), "go", tmp_path, client(),
                               tools_spec=tools_file)
    servers = json.loads((tmp_path / "mcp.json").read_text())["mcpServers"]
    assert servers["automation"]["args"] == ["-m", "workflow.deploy.automation_mcp"]
    assert servers["automation"]["env"] == {"NG_AUTOMATION_TOOLS": str(tools_file)}
    assert servers["automation"]["command"] == sys.executable
    allowed = next(a for a in argv if a.startswith("--allowedTools="))
    assert "mcp__automation" in allowed
    assert argv[argv.index("--append-system-prompt") + 1].startswith("You edit videos.")

    # A profile that does not pre-approve them still gets them, allowed:
    # they are the profile owner's own commands (as on the cloud runner).
    argv, _ = claude_code_argv(job(model="claude-code", profile={**VIDEO_PROFILE, "allowedTools": ["Read"]}),
                               "go", tmp_path, client(), tools_spec=tools_file)
    assert "mcp__automation" in next(a for a in argv if a.startswith("--allowedTools="))

    argv, _ = claude_code_argv(job(model="claude-code"), "go", tmp_path, client())
    assert "automation" not in json.loads((tmp_path / "mcp.json").read_text())["mcpServers"]
    assert "mcp__automation" not in next(a for a in argv if a.startswith("--allowedTools="))


# --- artifacts and attachments (the run tools) ----------------------------------

class FakeServer:
    """Just enough of the jobs API: artifacts and questions, answered at once."""

    def __init__(self, artifact_status=200):
        self.uploads, self.asked = [], []
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, status, payload):
                body = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                assert self.headers["Authorization"] == "Bearer pat"
                raw = self.rfile.read(int(self.headers["Content-Length"]))
                if "/artifacts" in self.path:
                    server.uploads.append({"path": self.path, "type": self.headers["Content-Type"], "body": raw})
                    if artifact_status != 200:
                        self._send(artifact_status, {"message": "nope"})
                        return
                    self._send(200, {"artifact": {
                        "id": "art-1", "key": "agent-artifacts/job-1/art-1-preview.mp4",
                        "name": "preview.mp4", "mime": self.headers["Content-Type"], "size": len(raw),
                        "url": "/api/workspaces/ws-1/blobs/agent-artifacts/job-1/art-1-preview.mp4",
                        "createdAt": 1.0}})
                    return
                if self.path.endswith("/steer/take"):
                    self._send(200, {"steers": []})
                    return
                body = json.loads(raw)
                server.asked.append(body)
                self._send(200, {"question": {"id": "q1", "answeredAt": 1.0, "answer": "Approve", **body}})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def questions(self):
        return agent_mcp.Questions(self.url, "ws-1", "job-1", "pat", poll_seconds=0.05, notify_after=0)

    def close(self):
        self.server.shutdown()


@pytest.fixture
def server():
    made = []

    def make(**kw):
        made.append(FakeServer(**kw))
        return made[-1]
    yield make
    for s in made:
        s.close()


def test_upload_artifact_posts_the_bytes_with_their_name_and_type(server, tmp_path):
    clip = tmp_path / "preview.mp4"
    clip.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"x" * 100)
    fake = server()
    result = agent_mcp.call_tool(fake.questions(), "upload_artifact", {"path": str(clip)})

    assert result["isError"] is False, result
    text = result["content"][0]["text"]
    assert "key: agent-artifacts/job-1/art-1-preview.mp4" in text
    assert "url: /api/workspaces/ws-1/blobs/agent-artifacts/job-1/art-1-preview.mp4" in text
    assert "attachments" in text
    [upload] = fake.uploads
    assert upload["path"] == "/api/registry/workspaces/ws-1/jobs/job-1/artifacts?name=preview.mp4"
    assert upload["type"] == "video/mp4" and upload["body"] == clip.read_bytes()


def test_upload_artifact_takes_a_shown_name_and_guesses_its_type(server, tmp_path):
    strip = tmp_path / "f_000.png"
    strip.write_bytes(b"\x89PNG rest")
    fake = server()
    agent_mcp.call_tool(fake.questions(), "upload_artifact", {"path": str(strip), "name": "cut 3 filmstrip.png"})
    [upload] = fake.uploads
    assert upload["path"].endswith("/artifacts?name=cut%203%20filmstrip.png") and upload["type"] == "image/png"


def test_upload_artifact_refuses_a_missing_or_oversized_file(server, tmp_path, monkeypatch):
    fake = server()
    missing = agent_mcp.call_tool(fake.questions(), "upload_artifact", {"path": str(tmp_path / "nope.mp4")})
    assert missing["isError"] and "no file at" in missing["content"][0]["text"]

    big = tmp_path / "master.mp4"
    big.write_bytes(b"x" * 2048)
    monkeypatch.setattr(agent_mcp, "MAX_ARTIFACT_BYTES", 1024)
    refused = agent_mcp.call_tool(fake.questions(), "upload_artifact", {"path": str(big)})
    assert refused["isError"] and "lower-resolution preview" in refused["content"][0]["text"]
    assert fake.uploads == [], "nothing was sent"


def test_an_older_server_without_artifacts_is_explained_not_fatal(server, tmp_path):
    clip = tmp_path / "p.mp4"
    clip.write_bytes(b"x")
    result = agent_mcp.call_tool(server(artifact_status=404).questions(), "upload_artifact", {"path": str(clip)})
    assert result["isError"] and "does not take artifacts yet" in result["content"][0]["text"]


def test_ask_user_sends_attachments_only_when_given(server):
    fake = server()
    result = agent_mcp.call_tool(fake.questions(), "ask_user", {
        "question": "Approve this cut?", "options": ["Approve", "Revise"],
        "attachments": ["agent-artifacts/job-1/art-1-preview.mp4", " ", 7]})
    assert result["content"][0]["text"] == "Approve"
    assert fake.asked[-1]["attachments"] == ["agent-artifacts/job-1/art-1-preview.mp4", "7"]
    assert fake.asked[-1]["options"] == ["Approve", "Revise"]

    agent_mcp.call_tool(fake.questions(), "ask_user", {"question": "Go on?"})
    assert "attachments" not in fake.asked[-1], "an older server may refuse a field it doesn't know"

    too_many = [f"k{i}" for i in range(10)]
    agent_mcp.call_tool(fake.questions(), "ask_user", {"question": "Which?", "attachments": too_many})
    assert fake.asked[-1]["attachments"] == too_many[:agent_mcp.MAX_ATTACHMENTS]


def test_the_run_tools_advertise_upload_and_attachments():
    names = {t["name"] for t in agent_mcp.TOOLS}
    assert {"ask_user", "set_title", "approve", "upload_artifact"} <= names
    ask = next(t for t in agent_mcp.TOOLS if t["name"] == "ask_user")
    assert "attachments" in ask["inputSchema"]["properties"]
    assert "upload_artifact" in ask["description"]


# --- from the review of #55 ----------------------------------------------------

import signal
import stat
import subprocess
import time


def _start_job(job_dir: Path) -> subprocess.Popen:
    return subprocess.Popen([sys.executable, "-m", "workflow.deploy.job_exec", str(job_dir)],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _wait_for(path: Path, timeout: float = 10) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists() and path.read_text().strip():
            return path.read_text().strip()
        time.sleep(0.05)
    raise AssertionError(f"{path} never appeared")


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


@pytest.mark.parametrize("sig", [signal.SIGHUP, signal.SIGTERM])
def test_a_stop_during_setup_kills_the_step_and_still_tears_down(tmp_path, sig):
    """tmux kill-session sends SIGHUP; a runner without tmux sends SIGTERM."""
    _cmd(tmp_path, {
        "setup": [{"name": "clone", "run": "echo $$ > {job_dir}/step.pid; exec sleep 30"}],
        "teardown": [{"name": "clean", "run": "echo cleaned > {job_dir}/teardown"}],
        "values": {"job_dir": str(tmp_path)}, "env": pa.shell_env("job-1", tmp_path),
    })
    job = _start_job(tmp_path)
    step = int(_wait_for(tmp_path / "step.pid"))
    job.send_signal(sig)
    assert job.wait(timeout=15) == 143
    assert not _alive(step), "the setup step must not outlive the job"
    assert (tmp_path / "teardown").read_text().strip() == "cleaned"
    # Stopped, not finished: no exit_code, so the attempt can still be resumed.
    assert _wait_for(tmp_path / "stopped") == "stopped during setup"
    assert not (tmp_path / "exit_code").exists()


def test_a_stop_ends_the_agent_before_teardown_runs(tmp_path):
    agent = (f"import os, time; open({str(tmp_path / 'agent.pid')!r}, 'w').write(str(os.getpid()));"
             " time.sleep(30)")
    _cmd(tmp_path, {
        "setup": [], "values": {"job_dir": str(tmp_path)}, "env": pa.shell_env("job-1", tmp_path),
        "teardown": [{"name": "clean",
                      "run": "kill -0 $(cat {job_dir}/agent.pid) 2>/dev/null && r=alive || r=gone; "
                             "echo $r > {job_dir}/teardown"}],
    }, argv=[sys.executable, "-c", agent])
    job = _start_job(tmp_path)
    pid = int(_wait_for(tmp_path / "agent.pid"))
    job.send_signal(signal.SIGTERM)
    assert job.wait(timeout=30) == 143
    assert (tmp_path / "teardown").read_text().strip() == "gone"
    assert not _alive(pid)
    assert _wait_for(tmp_path / "stopped") == "stopped"
    assert not (tmp_path / "exit_code").exists()


def test_timeouts_take_units_and_odd_steps_do_not_crash(tmp_path):
    assert [pa.seconds(v) for v in (90, "90", "90s", "10m", "1h")] == [90, 90, 90, 600, 3600]
    assert pa.seconds("soon") == pa.seconds(None) == pa.seconds(0) == pa.DEFAULT_TIMEOUT_SECONDS
    said = []
    pa.run_steps("setup", [{"run": ""}, {"run": "true", "timeout": "10m"}, "true"], {}, str(tmp_path),
                 {"PATH": os.environ["PATH"]}, said.append)
    assert "nothing to run, skipped" in said[0] and said[1].startswith("⚙ setup · true ✓")


def test_a_run_that_crashes_still_writes_its_exit_code(tmp_path):
    (tmp_path / "cmd.json").write_text(json.dumps({"provider": "x"}))  # no argv
    job = _start_job(tmp_path)
    job.wait(timeout=15)
    assert _wait_for(tmp_path / "exit_code") == "1"
    assert "KeyError" in json.loads((tmp_path / "result.json").read_text())["error"]


def test_tool_commands_are_killed_when_claude_goes(tmp_path):
    spec = pa.write_tools_spec(tmp_path, [{"name": "render", "run": f"echo $$ > {tmp_path}/tool.pid; exec sleep 30"}],
                               {}, str(tmp_path), {"PATH": os.environ["PATH"]})
    server = subprocess.Popen([sys.executable, "-m", "workflow.deploy.automation_mcp"],
                              stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
                              env={**os.environ, "NG_AUTOMATION_TOOLS": str(spec)})
    server.stdin.write(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                   "params": {"name": "render", "arguments": {}}}) + "\n")
    server.stdin.flush()
    pid = int(_wait_for(tmp_path / "tool.pid"))
    server.stdin.close()  # Claude exited
    server.wait(timeout=10)
    time.sleep(0.2)
    assert not _alive(pid)


def test_every_tool_call_gets_an_answer(tmp_path):
    spec = pa.write_tools_spec(tmp_path, [{"name": "ok", "run": "true"},
                                          {"name": "broken", "run": "true", "params": ["x"]}],
                               {}, str(tmp_path), {"PATH": os.environ["PATH"]})
    calls = [{"name": "ok", "arguments": ["not", "a", "dict"]}, {"name": "broken", "arguments": {}}]
    lines = "".join(json.dumps({"jsonrpc": "2.0", "id": i, "method": "tools/call", "params": c}) + "\n"
                    for i, c in enumerate(calls, 1))
    out = subprocess.run([sys.executable, "-m", "workflow.deploy.automation_mcp"], input=lines,
                         capture_output=True, text=True, timeout=20,
                         env={**os.environ, "NG_AUTOMATION_TOOLS": str(spec)}).stdout
    replies = {r["id"]: r for r in map(json.loads, out.splitlines())}
    assert replies[1]["result"]["isError"] and "must be an object" in replies[1]["result"]["content"][0]["text"]
    assert replies[2]["error"]["code"] == -32603


def test_handed_over_cwd_reaches_the_tools(tmp_path):
    handed = tmp_path / "elsewhere"
    pa.write_tools_spec(tmp_path, [{"name": "where", "run": "pwd"}], {}, str(tmp_path),
                        {"AGENT_CWD": str(tmp_path)})
    _cmd(tmp_path, {
        "setup": [{"name": "prepare",
                   "run": f"mkdir -p {handed} && echo '{{\"cwd\": \"{handed}\"}}' > {{job_dir}}/automation.json"}],
        "teardown": [], "values": {"job_dir": str(tmp_path)}, "env": pa.shell_env("job-1", tmp_path),
    }, argv=[sys.executable, "-c", "import json, os; print(json.load(open(os.environ.get('X', 'automation-tools.json'))) ['cwd'])"])
    # The agent (started in the handed-over cwd) reads the tools' spec from the job dir.
    spec = json.loads((tmp_path / "cmd.json").read_text())
    spec["env"] = {"X": str(tmp_path / pa.TOOLS_FILE)}
    (tmp_path / "cmd.json").write_text(json.dumps(spec))
    assert job_exec.run(tmp_path, out=open(os.devnull, "w")) == 0
    assert json.loads((tmp_path / "result.json").read_text())["result"] == str(handed)


def test_a_resumed_run_skips_setup(tmp_path):
    _cmd(tmp_path, {
        "setup": [{"name": "wipe", "run": "echo ran > {job_dir}/setup-ran"}],
        "teardown": [], "values": {"job_dir": str(tmp_path)}, "env": pa.shell_env("job-1", tmp_path),
    })
    spec = json.loads((tmp_path / "cmd.json").read_text())
    spec["resumed"] = True
    (tmp_path / "cmd.json").write_text(json.dumps(spec))
    assert job_exec.run(tmp_path, out=open(os.devnull, "w")) == 0
    assert not (tmp_path / "setup-ran").exists()
    assert "setup skipped: resuming" in (tmp_path / "log.txt").read_text()


def test_resume_is_decided_where_setup_moved_the_agent(tmp_path):
    from workflow.deploy.jobs import _resume_workdir

    (tmp_path / "automation.json").write_text(json.dumps({"cwd": str(tmp_path / "repo")}))
    setup = {"setup": [{"run": "x"}]}
    assert _resume_workdir(tmp_path, tmp_path, "sess", setup) == tmp_path / "repo"
    assert _resume_workdir(tmp_path, tmp_path, None, setup) == tmp_path  # not resuming
    assert _resume_workdir(tmp_path, tmp_path, "sess", {"setup": []}) == tmp_path


def test_files_with_tokens_are_private_and_scrubbed_after(tmp_path):
    env = {**pa.shell_env("job-1", tmp_path), "GH_TOKEN": "ghp_secret"}
    tools = pa.write_tools_spec(tmp_path, [{"name": "t", "run": "true"}], {}, str(tmp_path), env)
    assert stat.S_IMODE(tools.stat().st_mode) == 0o600
    pa.write_private(tmp_path / "cmd.json", {
        "argv": [sys.executable, "-c", "print(1)"], "provider": "x", "env": {}, "cwd": None,
        "automation": {"setup": [], "teardown": [], "values": {}, "env": env}})
    assert stat.S_IMODE((tmp_path / "cmd.json").stat().st_mode) == 0o600
    assert job_exec.run(tmp_path, out=open(os.devnull, "w")) == 0
    assert not tools.exists()
    assert "ghp_secret" not in (tmp_path / "cmd.json").read_text()
    assert stat.S_IMODE((tmp_path / "cmd.json").stat().st_mode) == 0o600
