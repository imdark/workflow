"""Tests for project config updates (workflow.projects.update_project)."""
import pytest
import yaml


@pytest.fixture
def config_file(tmp_path, monkeypatch):
    """Point load_config/save_config at a throwaway config.yaml."""
    cfg_path = tmp_path / "config.yaml"
    monkeypatch.setattr("workflow.config.CFG", cfg_path)
    cfg_path.write_text(yaml.dump({
        "task_backend": "linear",
        "linear": {"team": "GLOBAL"},
        "projects": {
            "work": {
                "name": "PEN",
                "task_backend": "linear",
                "linear": {"team": "PEN", "workspace": "acme"},
                "repositories": {"/code/robots": {"base_branch": "main"}},
                "git_enabled": True,
            },
            "personal": {
                "name": "PERSONAL",
                "task_backend": "markdown",
                "repositories": {},
            },
        },
    }))
    return cfg_path


def read_project(config_file, name):
    return yaml.safe_load(config_file.read_text())["projects"][name]


class TestUpdateProject:
    def test_merges_nested_dicts_a_level_deep(self, config_file):
        from workflow.projects import update_project

        update_project("work", {"linear": {"team": "PAN"}})

        linear = read_project(config_file, "work")["linear"]
        assert linear["team"] == "PAN"
        # The sibling override survives rather than being replaced wholesale.
        assert linear["workspace"] == "acme"

    def test_replaces_scalars_and_keeps_untouched_keys(self, config_file):
        from workflow.projects import update_project

        update_project("work", {"name": "PAN"})

        project = read_project(config_file, "work")
        assert project["name"] == "PAN"
        assert project["repositories"] == {"/code/robots": {"base_branch": "main"}}
        assert project["git_enabled"] is True

    def test_adds_a_section_the_project_lacks(self, config_file):
        from workflow.projects import update_project

        update_project("personal", {"task_backend": "linear", "linear": {"team": "EXE"}})

        project = read_project(config_file, "personal")
        assert project["task_backend"] == "linear"
        assert project["linear"] == {"team": "EXE"}

    def test_returns_the_updated_config(self, config_file):
        from workflow.projects import update_project

        updated = update_project("work", {"linear": {"team": "PAN"}})

        assert updated["linear"]["team"] == "PAN"
        assert updated["repositories"] == {"/code/robots": {"base_branch": "main"}}

    def test_unknown_project_raises_and_writes_nothing(self, config_file):
        from workflow.projects import update_project

        before = config_file.read_text()
        with pytest.raises(ValueError, match="ghost"):
            update_project("ghost", {"linear": {"team": "PAN"}})
        assert config_file.read_text() == before

    def test_leaves_other_projects_and_globals_alone(self, config_file):
        from workflow.projects import update_project

        update_project("work", {"linear": {"team": "PAN"}})

        cfg = yaml.safe_load(config_file.read_text())
        assert cfg["projects"]["personal"]["task_backend"] == "markdown"
        assert cfg["linear"]["team"] == "GLOBAL"


class TestRepoForNotesgraphProject:
    @pytest.fixture
    def repos(self, tmp_path, monkeypatch):
        """Two projects: personal with wf and notes-graph, work with robots."""
        code = tmp_path / "code"
        for name in ("workflow", "notes-graph", "robots", "app"):
            (code / name).mkdir(parents=True)
        cfg_path = tmp_path / "config.yaml"
        monkeypatch.setattr("workflow.config.CFG", cfg_path)
        monkeypatch.setattr("workflow.projects.get_current_project", lambda: "personal")
        cfg_path.write_text(yaml.dump({"projects": {
            "personal": {"repositories": {str(code / "workflow"): {},
                                          str(code / "notes-graph"): {}}},
            "work": {"default_repo": str(code / "robots"),
                     "repositories": {str(code / "robots"): {},
                                      str(code / "app"): {"notesgraph_project": "proj-9"}}},
        }}))
        return code

    def test_a_repo_folder_named_like_the_project(self, repos):
        from workflow.projects import repo_for_notesgraph_project

        assert repo_for_notesgraph_project("notes graph", "e8b7") == \
            ("personal", str(repos / "notes-graph"))

    def test_a_wf_project_named_like_it_gives_its_default_repo(self, repos):
        from workflow.projects import repo_for_notesgraph_project

        assert repo_for_notesgraph_project("Work", "x") == ("work", str(repos / "robots"))

    def test_a_repo_that_names_the_project_wins(self, repos):
        from workflow.projects import repo_for_notesgraph_project

        assert repo_for_notesgraph_project("Robots", "proj-9") == ("work", str(repos / "app"))

    def test_nothing_matching(self, repos):
        from workflow.projects import repo_for_notesgraph_project

        assert repo_for_notesgraph_project("garden", "p-1") is None
