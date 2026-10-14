"""`wf agent install`: the runner as a supervisord service that comes back on
new wf code, and the serve loop's graceful stop that makes that safe."""

import threading
import time

from workflow.deploy import supervisor
from workflow.deploy.jobs import serve
from workflow.deploy.updates import UpdateWatch

from tests.test_agent_jobs import FakeClient, payload


# --- noticing an update ------------------------------------------------------

def test_an_update_is_noticed_once_the_code_changes():
    code = {"fingerprint": "abc:1"}
    now = {"t": 0.0}
    watch = UpdateWatch(interval=30, fingerprint=lambda: code["fingerprint"],
                        clock=lambda: now["t"])
    assert watch() is False
    code["fingerprint"] = "def:2"
    now["t"] = 10
    assert watch() is False  # not checked again before the interval
    now["t"] = 31
    assert watch() is True
    code["fingerprint"] = "abc:1"
    assert watch() is True  # stays noticed


# --- stopping gracefully -----------------------------------------------------

def test_once_told_to_stop_it_claims_nothing_more_and_waits_for_its_jobs(monkeypatch):
    finished = threading.Event()

    def slow_job(job, cfg=None, **kw):
        time.sleep(0.3)
        finished.set()
        return {"result": "ok", "steps": 1, "session": None}

    monkeypatch.setattr("workflow.deploy.jobs.run_job", slow_job)
    client = FakeClient([payload(), payload()])
    # Stop as soon as the first job has been claimed.
    ran = serve(client, "laptop", poll_seconds=0, stop_when=lambda: client.claims >= 1)

    assert finished.is_set(), "serve returned before its running job finished"
    assert client.claims == 1
    assert ran == 1
    assert len(client.queue) == 1  # the second job stays queued for later


# --- the service files ---------------------------------------------------------

def test_the_program_runs_serve_with_exit_on_update_and_always_restarts(tmp_path):
    paths = supervisor.Paths(tmp_path)
    ini = supervisor.render_program(
        paths, supervisor.serve_command("laptop", 3), tmp_path / "repo",
        {"PATH": "/usr/bin:/opt/homebrew/bin", "HOME": str(tmp_path)})
    assert "[program:wf-agent]" in ini
    assert "-m workflow.cli agent serve --exit-on-update --max-jobs 3 --device laptop" in ini
    assert "autorestart=true" in ini
    assert "stopsignal=INT" in ini
    assert f"directory={tmp_path / 'repo'}" in ini
    assert 'PATH="/usr/bin:/opt/homebrew/bin"' in ini


def test_supervisord_keeps_everything_under_the_users_wf_folder(tmp_path):
    paths = supervisor.Paths(tmp_path)
    conf = supervisor.render_supervisord_conf(paths)
    assert f"file={paths.socket}" in conf
    assert f"serverurl=unix://{paths.socket}" in conf
    assert f"files={tmp_path / '.wf/supervisor/programs'}/*.ini" in conf
    assert "nodaemon=true" in conf


def test_the_launchd_agent_starts_supervisord_at_login(tmp_path):
    plist = supervisor.render_launchd_plist(supervisor.Paths(tmp_path), {"PATH": "/usr/bin"})
    assert "<string>dev.wf.supervisord</string>" in plist
    assert "<key>RunAtLoad</key><true/>" in plist
    assert f"<string>{tmp_path / '.wf/supervisor/supervisord.conf'}</string>" in plist


def test_install_writes_the_files_and_loads_the_program(tmp_path, monkeypatch):
    calls = []

    def run(argv, **kw):
        calls.append(argv)

        class Done:
            stdout, stderr, returncode = "wf-agent  RUNNING", "", 0
        return Done()

    paths = supervisor.Paths(tmp_path)
    monkeypatch.setattr(supervisor, "ensure_supervisor", lambda run=None: None)
    monkeypatch.setattr(supervisor, "start_supervisord", lambda p, env, run=None: "test")
    monkeypatch.setattr(supervisor, "_wait_for_socket", lambda p, timeout=20.0: True)

    how = supervisor.install("laptop", tmp_path / "repo", 2, paths=paths, run=run)

    assert how == "test"
    assert paths.conf.exists() and paths.program_file().exists()
    assert "--device laptop" in paths.program_file().read_text()
    sub = [argv[3:] for argv in calls if argv[0].endswith("supervisorctl")]
    assert sub == [["reread"], ["update"]]


def test_the_socket_stays_short_enough_for_a_unix_socket():
    from pathlib import Path

    assert supervisor.Paths(Path("/Users/me")).socket == \
        Path("/Users/me/.wf/supervisor/supervisor.sock")
    deep = supervisor.Paths(Path("/x/" + "very-long-folder-name/" * 6))
    assert len(str(deep.socket)) <= supervisor.MAX_SOCKET_PATH
    assert deep.socket.name.startswith("wf-supervisor-")


def test_jobs_run_claude_as_the_account_wf_was_installed_from(monkeypatch):
    """A login item gets no shell env; without CLAUDE_CONFIG_DIR claude reads
    ~/.claude, whose OAuth login may be expired ("Failed to authenticate")."""
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/Users/me/.claude-personal")
    assert supervisor.service_env()["CLAUDE_CONFIG_DIR"] == "/Users/me/.claude-personal"

    monkeypatch.delenv("CLAUDE_CONFIG_DIR")
    assert "CLAUDE_CONFIG_DIR" not in supervisor.service_env()
