"""The cloud_deploy action: which project's deploy it runs, and how it reports."""

import pytest

from workflow import projects
from workflow.actions import CloudDeployAction
from workflow.deploy import cloud

DEPLOY = {"cloud_deploy": {"repo": "/r", "command": "./deploy.sh",
                           "verify_url": "https://x/"}}


@pytest.fixture
def config(monkeypatch):
    def set_projects(all_projects, current=None):
        monkeypatch.setattr(projects, "list_projects", lambda: all_projects)
        monkeypatch.setattr(projects, "get_current_project", lambda: current)
    return set_projects


def test_a_named_project_is_used(config):
    config({"a": DEPLOY, "b": DEPLOY}, current="b")
    assert cloud.find_target("a")[0] == "a"


def test_else_the_current_project_then_the_only_one_with_a_deploy(config):
    config({"a": DEPLOY, "b": DEPLOY}, current="b")
    assert cloud.find_target()[0] == "b"
    config({"a": DEPLOY, "work": {}}, current="work")
    assert cloud.find_target()[0] == "a"


def test_several_or_none_says_why(config):
    config({"a": DEPLOY, "b": DEPLOY}, current="work")
    with pytest.raises(ValueError, match="several projects.*a, b"):
        cloud.find_target()
    config({"work": {}}, current="work")
    with pytest.raises(ValueError, match="no project has a cloud deploy"):
        cloud.find_target()


def test_a_deploy_runs_after_syncing_and_reports_the_commit(config, monkeypatch):
    config({"a": DEPLOY}, current="a")
    monkeypatch.setattr(cloud, "sync_checkout", lambda target: None)
    monkeypatch.setattr(cloud, "_git", lambda repo, *args: type(
        "R", (), {"stdout": "abc123\n" if args[0] == "rev-parse" else "Ship it\n"}))
    monkeypatch.setattr(cloud, "run_deploy", lambda target: 0)
    monkeypatch.setattr(cloud, "verify", lambda url: (True, "HTTP 200"))
    result = CloudDeployAction("d").execute({})
    assert result == {"success": True, "project": "a", "commit": "abc123",
                      "subject": "Ship it", "return_code": 0, "verified": True,
                      "verify": "https://x/ answered HTTP 200"}


def test_a_checkout_that_cant_be_synced_is_not_deployed(config, monkeypatch):
    config({"a": DEPLOY}, current="a")
    monkeypatch.setattr(cloud, "sync_checkout", lambda target: "it is dirty")
    monkeypatch.setattr(cloud, "run_deploy", lambda target: pytest.fail("deployed"))
    assert CloudDeployAction("d").execute({}) == {
        "success": False, "error": "it is dirty", "project": "a"}
