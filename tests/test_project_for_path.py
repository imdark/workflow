"""projects.project_for_path: which project's repositories hold a folder."""
from unittest import mock

from workflow import projects


def _projects(tmp_path):
    return {
        "acme": {"repositories": {str(tmp_path / "acme-app"): {}}, "jira": {"project": "ACME"}},
        "side": {"repositories": {str(tmp_path / "side"): {}}, "linear": {"team": "SIDE"}},
        "both": {"repositories": {str(tmp_path / "acme-app"): {}}},
        "notes": {"repositories": {}, "task_backend": "markdown"},
    }


def test_finds_project_by_folder_or_subfolder(tmp_path):
    (tmp_path / "acme-app" / "src").mkdir(parents=True)
    (tmp_path / "side").mkdir()
    with mock.patch.object(projects, "list_projects", return_value=_projects(tmp_path)), \
         mock.patch.object(projects, "get_current_project", return_value=None):
        assert projects.project_for_path(tmp_path / "acme-app" / "src") == "acme"
        assert projects.project_for_path(tmp_path / "side") == "side"
        assert projects.project_for_path(tmp_path) is None
        assert projects.project_for_path(tmp_path / "acme-application") is None


def test_current_project_wins_a_shared_repo(tmp_path):
    (tmp_path / "acme-app").mkdir()
    with mock.patch.object(projects, "list_projects", return_value=_projects(tmp_path)), \
         mock.patch.object(projects, "get_current_project", return_value="both"):
        assert projects.project_for_path(tmp_path / "acme-app") == "both"


def test_project_key_from_jira_or_linear(tmp_path):
    with mock.patch.object(projects, "get_project", side_effect=lambda n: _projects(tmp_path).get(n)):
        assert projects.get_project_key("acme") == "ACME"
        assert projects.get_project_key("side") == "SIDE"
        assert projects.get_project_key("both") is None
        assert projects.get_project_key("notes") == "NOTES"
        assert projects.get_project_key(None) is None
