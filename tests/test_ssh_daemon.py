"""Tests for the shared SSH session daemon behind the `ssh` MCP server.

`ssh` is replaced by a local `sh -c`, which runs the same remote shell
command, so these cover the command framing, output splitting, state kept
between commands, timeouts and interrupts without a reachable host.
"""

import asyncio
import json

import pytest

from workflow import ssh_daemon


@pytest.fixture
def daemon(monkeypatch):
    monkeypatch.setattr(ssh_daemon, "ssh_argv",
                        lambda host, *a: ["sh", "-c", ssh_daemon.REMOTE_SHELL])
    return ssh_daemon.Daemon()


def run(coro):
    return asyncio.run(coro)


def test_ssh_argv_passes_connection_options():
    argv = ssh_daemon.ssh_argv("box", "me", 2222, "~/.ssh/k", ["ProxyJump=bastion"])
    assert argv[:2] == ["ssh", "-T"]
    assert "BatchMode=yes" in argv
    assert argv[argv.index("-l") + 1] == "me"
    assert argv[argv.index("-p") + 1] == "2222"
    assert not argv[argv.index("-i") + 1].startswith("~")
    assert "ProxyJump=bastion" in argv
    assert argv[-2:] == ["box", ssh_daemon.REMOTE_SHELL]


def test_state_carries_between_commands(daemon):
    async def go():
        s = await daemon.op_open("box", cwd="/tmp")
        sid = s["session_id"]
        await daemon.op_exec(sid, "export FOO=bar; cd /")
        r = await daemon.op_exec(sid, "echo $FOO; pwd")
        await daemon.op_close(sid)
        return r

    r = run(go())
    assert r["stdout"] == "bar\n/\n"
    assert r["exit_code"] == 0
    assert r["cwd"] == "/"
    assert r["running"] is False


def test_stdout_stderr_and_exit_code(daemon):
    async def go():
        sid = (await daemon.op_open("box"))["session_id"]
        r = await daemon.op_exec(sid, "printf 'no newline'; echo oops >&2; false")
        quoted = await daemon.op_exec(sid, "echo \"it's\" 'a \"test\"'")
        await daemon.op_close(sid)
        return r, quoted

    r, quoted = run(go())
    assert r["stdout"] == "no newline"
    assert r["stderr"] == "oops\n"
    assert r["exit_code"] == 1
    assert quoted["stdout"] == "it's a \"test\"\n"


def test_long_command_is_followed_with_read(daemon):
    async def go():
        sid = (await daemon.op_open("box"))["session_id"]
        first = await daemon.op_exec(sid, "echo start; sleep 1; echo end", timeout=0.3)
        with pytest.raises(ssh_daemon.SessionError, match="still running"):
            await daemon.op_exec(sid, "true")
        rest = await daemon.op_read(sid, timeout=5)
        await daemon.op_close(sid)
        return first, rest

    first, rest = run(go())
    assert first["running"] is True
    # A trailing newline could begin the end marker, so it waits for the next read.
    assert first["stdout"] == "start"
    assert rest["running"] is False
    assert first["stdout"] + rest["stdout"] == "start\nend\n"
    assert rest["exit_code"] == 0


def test_interrupt_stops_command_and_keeps_session(daemon):
    async def go():
        sid = (await daemon.op_open("box"))["session_id"]
        await daemon.op_exec(sid, "sleep 30", timeout=0.3)
        info = await daemon.op_interrupt(sid)
        done = await daemon.op_read(sid, timeout=5)
        after = await daemon.op_exec(sid, "echo alive")
        await daemon.op_close(sid)
        return info, done, after

    info, done, after = run(go())
    assert info["interrupted"] is True
    assert done["running"] is False
    assert done["exit_code"] != 0
    assert after["stdout"] == "alive\n"


def test_parallel_sessions_run_concurrently(daemon):
    async def go():
        a = (await daemon.op_open("box"))["session_id"]
        b = (await daemon.op_open("box"))["session_id"]
        loop = asyncio.get_event_loop()
        t0 = loop.time()
        ra, rb = await asyncio.gather(daemon.op_exec(a, "sleep 1; echo a"),
                                      daemon.op_exec(b, "sleep 1; echo b"))
        elapsed = loop.time() - t0
        listed = await daemon.op_list()
        await daemon.op_close(a)
        await daemon.op_close(b)
        return ra, rb, elapsed, listed

    ra, rb, elapsed, listed = run(go())
    assert (ra["stdout"], rb["stdout"]) == ("a\n", "b\n")
    assert elapsed < 1.8
    assert len(listed["sessions"]) == 2


def test_exit_closes_session(daemon):
    async def go():
        sid = (await daemon.op_open("box"))["session_id"]
        r = await daemon.op_exec(sid, "exit 3", timeout=5)
        with pytest.raises(ssh_daemon.SessionError, match="closed"):
            await daemon.op_exec(sid, "true")
        return r

    r = run(go())
    assert r["exit_code"] is None
    assert "session closed" in r["error"]


def test_output_is_cut_to_its_end(daemon):
    async def go():
        sid = (await daemon.op_open("box"))["session_id"]
        r = await daemon.op_exec(sid, "seq 1 20000", max_output=100)
        await daemon.op_close(sid)
        return r

    r = run(go())
    assert r["stdout"].startswith("[... ")
    assert r["stdout"].endswith("19999\n20000\n")


def test_unknown_session_and_op(daemon):
    async def go():
        with pytest.raises(ssh_daemon.SessionError, match="no session"):
            await daemon.op_exec("s-nope", "true")

        class Pipe:
            def __init__(self):
                self.data = b""

            async def readline(self):
                return b'{"op": "bogus"}\n'

            def write(self, data):
                self.data += data

            async def drain(self):
                pass

            def close(self):
                pass

        pipe = Pipe()
        await daemon.handle(pipe, pipe)
        return json.loads(pipe.data)

    assert run(go()) == {"ok": False, "error": "unknown op"}


def test_marker_split_across_chunks():
    async def go():
        job = ssh_daemon.Job("x", "__WF_SSH_t__")
        payload = b"out\n\n__WF_SSH_t__ 0 /very/long/working/dir\n"
        for i in range(len(payload)):
            job.feed("stdout", payload[i:i + 1])
            assert b"__WF" not in job.buf["stdout"][:job.visible("stdout")]
        job.feed("stderr", b"\n__WF_SSH_t__ 0\n")
        return job

    job = run(go())
    assert job.done.is_set()
    r = job.take(1000)
    assert r["stdout"] == "out\n"
    assert r["cwd"] == "/very/long/working/dir"


def test_install_targets_every_claude_config_dir(tmp_path, monkeypatch):
    from workflow import cli_ssh

    (tmp_path / ".claude-work").mkdir()
    (tmp_path / ".claude-work" / ".claude.json").write_text("{}")
    (tmp_path / ".claude-empty").mkdir()
    monkeypatch.setattr(cli_ssh.Path, "home", lambda: tmp_path)
    monkeypatch.setattr("workflow.config.load_config", lambda: {"projects": {
        "personal": {"claude_config_dir": str(tmp_path / ".claude-personal")},
        "other": {}}})

    assert cli_ssh.claude_config_dirs() == [
        None, str(tmp_path / ".claude-personal"), str(tmp_path / ".claude-work")]
