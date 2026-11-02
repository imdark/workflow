"""`wf terminal serve`: harnesses in a pty, relayed to NotesGraph's gateway."""

import asyncio
import json
import os
import stat

import pytest

from workflow.deploy import supervisor
from workflow.deploy.terminal_serve import (
    Device, HarnessRefused, NotesGraphMcp, harness_argv, serve)


# --- which command a harness runs ----------------------------------------------

def test_harness_argv():
    assert harness_argv("claude") == ["claude"]
    assert harness_argv("claude", "abc") == ["claude", "--resume", "abc"]
    assert harness_argv("codex", "abc") == ["codex", "resume", "abc"]
    assert harness_argv("claude", None, {"claude": "wf ai --x"}) == ["wf", "ai", "--x"]
    with pytest.raises(HarnessRefused):
        harness_argv("vim")


def test_a_shell_is_refused_unless_the_device_allows_it(monkeypatch):
    monkeypatch.setenv("SHELL", "/bin/zsh")
    with pytest.raises(HarnessRefused, match="allow_shell"):
        harness_argv("shell")
    assert harness_argv("shell", allow_shell=True) == ["/bin/zsh", "-l"]


# --- running a session ---------------------------------------------------------

def run_session(device_kwargs, session, stdin=None):
    events = []

    async def emit(ev, body):
        events.append((ev, body))

    async def go():
        d = Device("w", "dev", emit, **device_kwargs)
        await d.spawn({"id": "s1", "cols": 100, "rows": 30, **session})
        if stdin:
            await asyncio.sleep(0.3)
            d.stdin("s1", stdin)
        for _ in range(100):
            if any(e == "terminal:device:exit" for e, _ in events):
                break
            await asyncio.sleep(0.1)

    asyncio.run(go())
    return events


def output(events):
    return "".join(b["data"] for e, b in events if e == "terminal:device:output")


def test_output_exit_and_size():
    ev = run_session({"overrides": {"claude": "sh -c 'stty size; echo hi; exit 3'"}},
                     {"harness": "claude"})
    assert "30 100" in output(ev) and "hi" in output(ev)
    assert ev[0][0] == "terminal:device:started"
    assert ev[-1] == ("terminal:device:exit", {"workspaceId": "w", "sessionId": "s1", "code": 3})


def test_stdin_reaches_the_harness():
    ev = run_session({"overrides": {"claude": "sh -c 'read x; echo got:$x'"}},
                     {"harness": "claude"}, stdin="ping\n")
    assert "got:ping" in output(ev)


def test_a_refused_harness_ends_the_session_with_why():
    ev = run_session({}, {"harness": "shell"})
    assert "allow_shell" in output(ev)
    assert ev[-1][1]["code"] == 127
    ev = run_session({}, {"harness": "vim"})
    assert ev[-1][1]["code"] == 127


def test_claude_gets_notesgraphs_tools_in_a_private_file_gone_after(tmp_path, monkeypatch):
    """wf's own Claude: NotesGraph's MCP as the runner's user. The config
    carries the token, so it is owner-only and removed with the session."""
    seen = {}
    monkeypatch.setattr("workflow.session.apply_claude_env",
                        lambda env, project=None: env.update({"CLAUDE_CONFIG_DIR": "/c"}))
    fake_claude = tmp_path / "claude"
    # Prints its arguments, the config's mode and the account env, then exits.
    fake_claude.write_text(
        "#!/bin/sh\n"
        "echo \"args:$*\"\n"
        "stat -f '%Lp' \"$2\" 2>/dev/null || stat -c '%a' \"$2\"\n"
        "echo \"dir:$CLAUDE_CONFIG_DIR\"\n"
        "cat \"$2\"\n")
    fake_claude.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")
    mcp = NotesGraphMcp("https://ng.test", "ws1", "tok")
    ev = run_session({"mcp": mcp, "sessions_dir": tmp_path / "sessions"},
                     {"harness": "claude", "resume": "abc"})
    out = output(ev)
    config_path = tmp_path / "sessions" / "s1" / "mcp.json"
    assert f"args:--mcp-config {config_path} --resume abc" in out
    assert "\n600" in out.replace("\r", "")
    assert "dir:/c" in out
    assert '"https://ng.test/api/workspaces/ws1/mcp"' in out
    assert "Bearer tok" in out
    assert not config_path.parent.exists()


def test_an_overridden_claude_runs_as_written(tmp_path):
    ev = run_session({"overrides": {"claude": "sh -c 'echo \"$@\"' x"},
                      "mcp": NotesGraphMcp("u", "w", "t"),
                      "sessions_dir": tmp_path},
                     {"harness": "claude"})
    assert "mcp-config" not in output(ev)
    assert not any(tmp_path.iterdir())


# --- the connection --------------------------------------------------------------

def fake_gateway(on_attach, handlers):
    """A socket.io server standing in for NotesGraph's; returns (app, sio)."""
    from aiohttp import web
    import socketio

    sio = socketio.AsyncServer(async_mode="aiohttp")
    app = web.Application()
    sio.attach(app)
    sio.on("terminal:device:attach")(on_attach(sio))
    for event, handler in handlers.items():
        sio.on(event)(handler(sio))
    return app


async def start(app):
    from aiohttp import web

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    return runner, f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"


def test_against_a_fake_gateway():
    out, seen = [], {}

    async def go():
        done = asyncio.Event()

        def attach(sio):
            async def handler(sid, body):
                seen["device"] = body["deviceKey"]
                await sio.emit("terminal:spawn",
                               {"id": "s", "harness": "shell", "cols": 80, "rows": 24}, to=sid)
                return {"data": {"pending": [], "live": []}}
            return handler

        def started(sio):
            async def handler(sid, body):
                await sio.emit("terminal:stdin",
                               {"sessionId": "s", "data": "echo wire-ok; exit\n"}, to=sid)
            return handler

        def got_output(sio):
            async def handler(sid, body):
                out.append(body["data"])
            return handler

        def exited(sio):
            async def handler(sid, body):
                seen["code"] = body["code"]
                done.set()
            return handler

        runner, url = await start(fake_gateway(attach, {
            "terminal:device:started": started,
            "terminal:device:output": got_output,
            "terminal:device:exit": exited,
        }))
        task = asyncio.create_task(serve(url, "tok", "w", "dev", allow_shell=True))
        await asyncio.wait_for(done.wait(), 15)
        task.cancel()
        await runner.cleanup()

    asyncio.run(go())
    assert seen["device"] == "dev"
    assert "wire-ok" in "".join(out) and seen["code"] == 0


def test_an_update_exits_once_no_session_is_running(monkeypatch):
    """Under a supervisor it restarts on new code, but never cuts a session off."""
    monkeypatch.setattr("workflow.deploy.terminal_serve.UPDATE_POLL_SECONDS", 0.05)

    async def go():
        def attach(sio):
            async def handler(sid, body):
                return {"data": {"pending": [], "live": []}}
            return handler

        runner, url = await start(fake_gateway(attach, {}))
        events = []
        await asyncio.wait_for(
            serve(url, "tok", "w", "dev", stop_when=lambda: True,
                  on_event=lambda kind, detail=None: events.append(kind)),
            10)
        await runner.cleanup()
        return events

    assert "stopping" in asyncio.run(go())


def test_an_update_waits_for_a_running_session(monkeypatch):
    monkeypatch.setattr("workflow.deploy.terminal_serve.UPDATE_POLL_SECONDS", 0.05)
    exits = []

    async def go():
        def attach(sio):
            async def handler(sid, body):
                # A session waiting for this device when it connects.
                return {"data": {"pending": [{"id": "s", "harness": "claude"}], "live": []}}
            return handler

        def exited(sio):
            async def handler(sid, body):
                exits.append((asyncio.get_running_loop().time(), body["code"]))
            return handler

        runner, url = await start(fake_gateway(attach, {"terminal:device:exit": exited}))
        await asyncio.wait_for(
            serve(url, "tok", "w", "dev", overrides={"claude": "sleep 0.6"},
                  stop_when=lambda: True),
            10)
        returned = asyncio.get_running_loop().time()
        await runner.cleanup()
        return returned

    returned = asyncio.run(go())
    # It ran to its own end (0), not killed by the restart, before serve returned.
    assert exits and exits[0][1] == 0 and exits[0][0] <= returned


# --- the service -----------------------------------------------------------------

def test_the_terminal_service_runs_beside_the_agent_runner(tmp_path, monkeypatch):
    calls = []

    def run(argv, **kw):
        calls.append(argv)

        class Done:
            stdout, stderr, returncode = "", "", 0
        return Done()

    paths = supervisor.Paths(tmp_path)
    monkeypatch.setattr(supervisor, "ensure_supervisor", lambda run=None: None)
    monkeypatch.setattr(supervisor, "start_supervisord", lambda p, env, run=None: "test")
    monkeypatch.setattr(supervisor, "_wait_for_socket", lambda p, timeout=20.0: True)

    supervisor.install("laptop", tmp_path, 2, paths=paths, run=run)
    supervisor.install_program(supervisor.TERMINAL_PROGRAM,
                               supervisor.terminal_command("laptop"), tmp_path,
                               paths=paths, run=run)
    ini = paths.program_file(supervisor.TERMINAL_PROGRAM).read_text()
    assert "[program:wf-terminal]" in ini
    assert "-m workflow.cli terminal serve --exit-on-update --device laptop" in ini
    assert paths.program_file().exists()

    # Removing the terminal leaves the runner, and supervisord, in place.
    paths.socket.parent.mkdir(parents=True, exist_ok=True)
    paths.socket.touch()
    calls.clear()
    supervisor.uninstall(paths, run, name=supervisor.TERMINAL_PROGRAM)
    assert not paths.program_file(supervisor.TERMINAL_PROGRAM).exists()
    assert paths.program_file().exists()
    sub = [argv[3:] for argv in calls if str(argv[0]).endswith("supervisorctl")]
    assert sub == [["stop", "wf-terminal"], ["reread"], ["update"]]


# --- what the review of PR #48 found here too ------------------------------------

@pytest.mark.parametrize("sid", ["..", ".", "...", "/tmp/x", "a/../..", "", None])
def test_a_session_id_that_is_a_path_is_ignored(tmp_path, sid):
    """The id names the session's folder, which is deleted when it ends."""
    sessions = tmp_path / "terminal"
    sessions.mkdir()
    (tmp_path / "keep").write_text("config")
    events = []

    async def emit(ev, body):
        events.append(ev)

    async def go():
        d = Device("w", "dev", emit, sessions_dir=sessions,
                   overrides={"claude": "true"})
        await d.spawn({"id": sid, "harness": "claude"})
        await asyncio.sleep(0.2)

    asyncio.run(go())
    assert events == []
    assert (tmp_path / "keep").exists()


def test_a_dropped_connection_keeps_the_harness_and_its_output(tmp_path):
    """Output while the socket is down waits for it, instead of killing the
    reader -- and with it the harness."""
    sent, down = [], {"on": False}

    async def emit(ev, body):
        if down["on"]:
            raise ConnectionError("not a connected namespace")
        sent.append((ev, body))

    async def go():
        d = Device("w", "dev", emit,
                   overrides={"claude": "sh -c 'sleep 0.4; echo during; sleep 0.4; exit 5'"})
        await d.spawn({"id": "s1", "harness": "claude"})
        down["on"] = True
        await asyncio.sleep(1.5)       # it prints and exits while disconnected
        assert d.backlog["s1"]
        down["on"] = False
        return await d.flush()

    ended = asyncio.run(go())
    assert ended == {"s1"}
    assert "during" in output(sent)
    assert sent[-1] == ("terminal:device:exit", {"workspaceId": "w", "sessionId": "s1", "code": 5})


def test_a_failed_start_leaves_no_token_behind(tmp_path, monkeypatch):
    events = []

    async def emit(ev, body):
        events.append((ev, body))

    def broken_fork():
        raise OSError("out of ptys")

    monkeypatch.setattr("workflow.deploy.terminal_serve.pty.fork", broken_fork)
    monkeypatch.setattr("workflow.session.apply_claude_env", lambda env, *a: env)
    mcp = NotesGraphMcp("https://ng", "w", "secret")
    asyncio.run(Device("w", "dev", emit, mcp=mcp, sessions_dir=tmp_path)
                .spawn({"id": "s1", "harness": "claude"}))
    assert not (tmp_path / "s1").exists()
    assert events[-1][1]["code"] == 127


def test_installing_a_program_leaves_a_running_supervisord_alone(tmp_path, monkeypatch):
    """Restarting it would restart the runner's jobs and every open terminal."""
    calls = []

    def run(argv, **kw):
        calls.append(argv)

        class Done:
            stdout, stderr, returncode = ("4242\n" if argv[-1] == "pid" else ""), "", 0
        return Done()

    paths = supervisor.Paths(tmp_path)
    paths.socket.parent.mkdir(parents=True, exist_ok=True)
    paths.socket.touch()
    monkeypatch.setattr(supervisor, "ensure_supervisor", lambda run=None: None)
    restarted = []
    monkeypatch.setattr(supervisor, "start_supervisord",
                        lambda p, env, run=None: restarted.append(1) or "restarted")

    how = supervisor.install_program(supervisor.TERMINAL_PROGRAM,
                                     supervisor.terminal_command("laptop"), tmp_path,
                                     paths=paths, run=run)
    assert restarted == [] and "already running" in how
    assert [argv[3:] for argv in calls] == [["pid"], ["reread"], ["update"]]
