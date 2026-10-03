"""`wf agent logs`: a job's transcript, from NotesGraph or this machine."""

from types import SimpleNamespace

from typer.testing import CliRunner

from workflow.deploy.jobs import JobClient, follow_log
from workflow.deploy.registry.notesgraph import NotesGraphError


class FakeClient(JobClient):
    """Serves a job as a script of polls; records the offsets asked for."""

    def __init__(self, polls, jobs=None, error=None):
        self.polls, self.jobs, self.error = list(polls), jobs or [], error
        self.asked_from = []

    def get_job(self, job_id, log_from=0):
        if self.error:
            raise NotesGraphError(self.error)
        self.asked_from.append(log_from)
        return self.polls.pop(0) if len(self.polls) > 1 else self.polls[0]

    def list_jobs(self, device_key=None):
        if self.error:
            raise NotesGraphError(self.error)
        return self.jobs


QUESTION = {"id": "q1", "kind": "question", "text": "Who is Cosmo?", "answeredAt": None}


def test_following_asks_only_for_new_text_and_stops_when_done():
    client = FakeClient([
        {"status": "running", "log": "▶ started\n", "logEnd": 10},
        {"status": "running", "log": "? asking you: Who is Cosmo?\n", "logEnd": 40,
         "questions": [QUESTION]},
        {"status": "running", "log": "", "logEnd": 40, "questions": [QUESTION]},
        {"status": "done", "log": "✓ finished\n", "logEnd": 51,
         "questions": [{**QUESTION, "answeredAt": 5}]},
    ])
    events = list(follow_log(client, "job-1", follow=True, sleep=lambda s: None))

    assert [k for k, _ in events] == ["log", "log", "question", "log", "end"]
    assert client.asked_from == [0, 10, 40, 40]
    # An open question is announced once, not on every poll.
    assert sum(1 for k, _ in events if k == "question") == 1
    assert events[-1][1]["status"] == "done"


def test_without_follow_it_prints_what_there_is_and_ends():
    client = FakeClient([{"status": "running", "log": "partial\n", "logEnd": 8}])
    events = list(follow_log(client, "job-1", follow=False))
    assert events == [("log", "partial\n"), ("end", {"status": "running", "log": "partial\n", "logEnd": 8})]


# --- the command ----------------------------------------------------------

def invoke(monkeypatch, client, args, job_root=None):
    from workflow import cli_agent

    monkeypatch.setattr(cli_agent, "_client_and_device",
                        lambda d: (client, SimpleNamespace(id="laptop")))
    if job_root is not None:
        monkeypatch.setattr("workflow.deploy.jobs.JOB_ROOT", job_root)
    return CliRunner().invoke(cli_agent.agent_app, ["logs", *args])


def test_logs_shows_the_newest_job_by_default(monkeypatch):
    client = FakeClient(
        [{"status": "done", "agentName": "shows", "log": "→ keyword_search\n✓ finished\n", "logEnd": 30}],
        jobs=[{"id": "newest-1234"}, {"id": "older-5678"}],
    )
    result = invoke(monkeypatch, client, [])
    assert result.exit_code == 0
    assert "→ keyword_search" in result.output
    assert "shows newest-1 — done" in result.output


def test_logs_finds_a_job_by_its_prefix(monkeypatch):
    client = FakeClient([{"status": "error", "agentName": "shows", "log": "", "logEnd": 0,
                          "error": "claude exited 1"}],
                        jobs=[{"id": "aaaa1111"}, {"id": "bbbb2222"}])
    result = invoke(monkeypatch, client, ["bbbb"])
    assert result.exit_code == 0
    assert "bbbb2222"[:8] in result.output
    assert "claude exited 1" in result.output


def test_an_unknown_prefix_is_a_clear_error(monkeypatch):
    client = FakeClient([{}], jobs=[{"id": "aaaa1111"}])
    result = invoke(monkeypatch, client, ["zzzz"])
    assert result.exit_code == 1
    assert "No job starting with 'zzzz'" in result.output


def test_a_running_job_points_at_follow_and_tmux(monkeypatch):
    client = FakeClient([{"status": "running", "agentName": "shows", "log": "▶ started\n",
                          "logEnd": 10, "tmuxSession": "wf-job-abcd1234", "deviceKey": "laptop"}],
                        jobs=[{"id": "abcd1234ef"}])
    result = invoke(monkeypatch, client, [])
    assert "wf agent logs abcd1234 -f" in result.output
    assert "tmux attach -t wf-job-abcd1234" in result.output


def test_offline_it_falls_back_to_this_machines_copy(monkeypatch, tmp_path):
    (tmp_path / "abcd1234ef").mkdir()
    (tmp_path / "abcd1234ef" / "log.txt").write_text("▶ started\n✓ finished\n")
    client = FakeClient([{}], error="inventory unreachable")

    result = invoke(monkeypatch, client, ["abcd"], job_root=tmp_path)
    assert result.exit_code == 0
    assert "inventory unreachable" in result.output
    assert "✓ finished" in result.output
