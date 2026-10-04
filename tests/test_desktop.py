"""Tests for the per-project and per-repo desktop setups `wf cd` applies.

These cover resolving the configuration, the geometry, and the commands and
AppleScript generated from it. Nothing here launches an app or moves a
window: `subprocess` is replaced wherever apply() would reach the desktop.
"""

import subprocess

import pytest
import yaml

from workflow import desktop

MAIN = (0, 25, 1440, 875)
SECOND = (1440, 0, 1920, 1080)


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    """A workflow config with a project-level and a repo-level setup.

    Patches the config *path*, not load_config/save_config, for the same
    reason tests/test_claude_accounts.py does.
    """
    config = {"projects": {
        "work": {
            "desktop": {
                "apps": [
                    "Slack",
                    {"app": "Visual Studio Code", "open": "{repo_path}", "position": "left"},
                ],
                "focus": "Terminal",
            },
            "repositories": {
                "/code/api": {
                    "base_branch": "main",
                    "desktop": {"apps": [
                        {"app": "Visual Studio Code", "open": "{repo_path}", "position": "right"},
                        {"app": "Postman", "bounds": [0, 25, 1200, 800]},
                    ]},
                },
                "/code/web": {"base_branch": "main"},
            },
        },
        "personal": {"repositories": {"/code/blog": None}},
    }}
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(yaml.dump(config))
    monkeypatch.setattr("workflow.config.CFG", cfg_path)
    return config


def _saved(tmp_path):
    return yaml.safe_load((tmp_path / "config.yaml").read_text())


# --- resolving -----------------------------------------------------------------

def test_project_setup_applies_to_a_repo_without_its_own(cfg):
    setup = desktop.resolve("work", "/code/web", cfg)
    assert [e["app"] for e in setup["apps"]] == ["Slack", "Visual Studio Code"]
    assert setup["focus"] == "Terminal"


def test_repo_setup_overrides_same_app_and_adds_its_own(cfg):
    setup = desktop.resolve("work", "/code/api", cfg)
    apps = {e["app"]: e for e in setup["apps"]}
    assert list(apps) == ["Slack", "Visual Studio Code", "Postman"]
    assert apps["Visual Studio Code"]["position"] == "right"
    assert setup["focus"] == "Terminal"


def test_app_names_merge_case_insensitively(cfg):
    cfg["projects"]["work"]["repositories"]["/code/web"]["desktop"] = {"apps": ["slack"]}
    setup = desktop.resolve("work", "/code/web", cfg)
    assert [e["app"] for e in setup["apps"]] == ["slack", "Visual Studio Code"]


def test_inherit_false_drops_the_project_setup(cfg):
    cfg["projects"]["work"]["repositories"]["/code/web"]["desktop"] = {
        "inherit": False, "apps": ["Simulator"],
    }
    setup = desktop.resolve("work", "/code/web", cfg)
    assert [e["app"] for e in setup["apps"]] == ["Simulator"]
    assert "focus" not in setup


def test_unconfigured_project_resolves_empty(cfg):
    assert desktop.resolve("personal", "/code/blog", cfg) == {"apps": []}
    assert desktop.resolve("nope", None, cfg) == {"apps": []}


def test_entry_without_app_is_rejected():
    with pytest.raises(ValueError):
        desktop.normalize_entry({"position": "left"})


def test_find_repo_exact_then_partial(cfg):
    assert desktop.find_repo("api", cfg) == ("work", "/code/api")
    assert desktop.find_repo("blo", cfg) == ("personal", "/code/blog")
    assert desktop.find_repo("missing", cfg) is None


# --- geometry ------------------------------------------------------------------

def test_named_position_is_a_fraction_of_the_main_screen():
    assert desktop.frame_for({"app": "A", "position": "left"}, [MAIN]) == (0, 25, 720, 875)
    assert desktop.frame_for({"app": "A", "position": "right-third"}, [MAIN]) == (960, 25, 480, 875)


def test_screen_picks_which_display():
    entry = {"app": "A", "position": "bottom-right", "screen": 2}
    assert desktop.frame_for(entry, [MAIN, SECOND]) == (2400, 540, 960, 540)


def test_bounds_win_over_position():
    entry = {"app": "A", "position": "left", "bounds": [10, 20, 300, 400]}
    assert desktop.frame_for(entry, []) == (10, 20, 300, 400)


def test_launch_only_entry_has_no_frame():
    assert desktop.frame_for({"app": "Slack"}, []) is None


@pytest.mark.parametrize("entry", [
    {"app": "A", "position": "sideways"},
    {"app": "A", "position": "left", "screen": 3},
    {"app": "A", "bounds": [1, 2, 3]},
])
def test_bad_geometry_is_rejected(entry):
    with pytest.raises(ValueError):
        desktop.frame_for(entry, [MAIN])


def test_screens_only_needed_for_named_positions():
    assert not desktop.needs_screens([{"app": "A"}, {"app": "B", "bounds": [0, 0, 1, 1]}])
    assert desktop.needs_screens([{"app": "A", "position": "left"}])


# --- commands and script ---------------------------------------------------------

def test_launch_command_fills_in_the_repo():
    entry = {"app": "Visual Studio Code", "open": "{repo_path}"}
    assert desktop.launch_command(entry, {"repo_path": "/code/api"}) == [
        "open", "-a", "Visual Studio Code", "/code/api",
    ]


def test_launch_command_keeps_url_braces_and_backgrounds():
    entry = {"app": "Google Chrome", "open": ["http://x/{id}", "~/notes.md"], "background": True}
    cmd = desktop.launch_command(entry, {"repo_path": "/code/api"})
    assert cmd[:4] == ["open", "-g", "-a", "Google Chrome"]
    assert cmd[4] == "http://x/{id}"
    assert not cmd[5].startswith("~")


def test_layout_script_places_each_app_by_bundle_id():
    script = desktop.layout_script([("Visual Studio Code", (0, 25, 720, 875))], focus="Terminal")
    assert 'id of application "Visual Studio Code"' in script
    assert "bundle identifier is appId" in script
    assert "set position to {0, 25}" in script
    assert "set size to {720, 875}" in script
    assert 'tell application "Terminal" to activate' in script


def test_layout_script_quotes_app_names():
    script = desktop.layout_script([('Odd "App"', (0, 0, 1, 1))])
    assert 'application "Odd \\"App\\""' in script


@pytest.mark.skipif(subprocess.run(["which", "osacompile"], capture_output=True).returncode != 0,
                    reason="needs macOS osacompile")
def test_layout_script_compiles(tmp_path):
    script = desktop.layout_script([("Finder", (0, 25, 800, 600)), ("TextEdit", (800, 25, 640, 600))],
                                   focus="Terminal")
    src = tmp_path / "layout.applescript"
    src.write_text(script)
    compiled = subprocess.run(["osacompile", "-o", str(tmp_path / "layout.scpt"), str(src)],
                              capture_output=True, text=True)
    assert compiled.returncode == 0, compiled.stderr


# --- apply -----------------------------------------------------------------------

def test_apply_launches_and_places(cfg, monkeypatch):
    calls, scripts = [], []
    monkeypatch.setattr(desktop.sys, "platform", "darwin")
    monkeypatch.setattr(desktop, "screens", lambda: [MAIN])
    monkeypatch.setattr(desktop.subprocess, "run",
                        lambda cmd, **kw: calls.append(cmd) or subprocess.CompletedProcess(cmd, 0, "", ""))
    monkeypatch.setattr(desktop, "_run_detached", scripts.append)

    result = desktop.apply("work", "/code/api", cfg=cfg)

    assert result.launched == ["Slack", "Visual Studio Code", "Postman"]
    assert result.placing == ["Visual Studio Code", "Postman"]
    assert ["open", "-a", "Visual Studio Code", "/code/api"] in calls
    assert len(scripts) == 1 and "set position to {720, 25}" in scripts[0]


def test_apply_reports_an_app_that_wont_open(cfg, monkeypatch):
    monkeypatch.setattr(desktop.sys, "platform", "darwin")
    monkeypatch.setattr(desktop, "screens", lambda: [MAIN])
    monkeypatch.setattr(desktop.subprocess, "run", lambda cmd, **kw: subprocess.CompletedProcess(
        cmd, 1, "", "Unable to find application named 'Slack'") if "Slack" in cmd
        else subprocess.CompletedProcess(cmd, 0, "", ""))
    monkeypatch.setattr(desktop, "_run_detached", lambda script: None)

    result = desktop.apply("work", "/code/web", cfg=cfg)

    assert result.failed == {"Slack": "Unable to find application named 'Slack'"}
    assert result.launched == ["Visual Studio Code"]


def test_apply_does_nothing_without_a_setup(cfg, monkeypatch):
    monkeypatch.setattr(desktop.subprocess, "run", lambda *a, **kw: pytest.fail("ran a command"))
    assert desktop.apply("personal", "/code/blog", cfg=cfg) == desktop.Result()


def test_apply_skips_off_macos(cfg, monkeypatch):
    monkeypatch.setattr(desktop.sys, "platform", "linux")
    assert desktop.apply("work", "/code/api", cfg=cfg).skipped


# --- editing the config -----------------------------------------------------------

def test_set_entry_adds_then_replaces(cfg, tmp_path):
    desktop.set_entry("work", "/code/web", {"app": "Figma", "position": "left"})
    desktop.set_entry("work", "/code/web", {"app": "figma", "position": "right"})
    apps = _saved(tmp_path)["projects"]["work"]["repositories"]["/code/web"]["desktop"]["apps"]
    assert apps == [{"app": "figma", "position": "right"}]


def test_set_entry_on_a_repo_with_no_settings(cfg, tmp_path):
    desktop.set_entry("personal", "/code/blog", {"app": "Obsidian"})
    repo = _saved(tmp_path)["projects"]["personal"]["repositories"]["/code/blog"]
    assert repo == {"desktop": {"apps": [{"app": "Obsidian"}]}}


def test_remove_entry_normalizes_string_entries(cfg, tmp_path):
    assert desktop.remove_entry("work", None, "slack")
    apps = _saved(tmp_path)["projects"]["work"]["desktop"]["apps"]
    assert [a["app"] for a in apps] == ["Visual Studio Code"]
    assert not desktop.remove_entry("work", None, "Slack")


def test_set_focus_and_clear(cfg, tmp_path):
    desktop.set_focus("work", "/code/api", "iTerm")
    assert _saved(tmp_path)["projects"]["work"]["repositories"]["/code/api"]["desktop"]["focus"] == "iTerm"
    desktop.set_focus("work", "/code/api", None)
    assert "focus" not in _saved(tmp_path)["projects"]["work"]["repositories"]["/code/api"]["desktop"]


def test_editing_an_unknown_target_raises(cfg):
    with pytest.raises(ValueError):
        desktop.set_entry("nope", None, {"app": "A"})
    with pytest.raises(ValueError):
        desktop.set_entry("work", "/code/missing", {"app": "A"})
