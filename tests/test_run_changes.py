"""What a run changed: the record the runner sends NotesGraph with its report."""

import io
import json
import subprocess

from workflow.deploy import run_changes
from workflow.deploy.job_exec import Narrator, run
from workflow.deploy.run_changes import ChangeRecorder, git_base, git_changes


def call(rec, tid, name, args, result="ok", is_error=False):
    rec.tool_use({"type": "tool_use", "id": tid, "name": name, "input": args})
    rec.tool_result({"type": "tool_result", "tool_use_id": tid, "is_error": is_error,
                     "content": [{"type": "text", "text": result}]})


def test_notes_created_edited_and_linked():
    rec = ChangeRecorder("/w")
    call(rec, "1", "mcp__notesgraph__create_document", {"title": "  Weekly \n plan  "},
         json.dumps({"success": True, "docId": "d1"}))
    call(rec, "2", "mcp__notesgraph__update_document", {"docId": "d2"})
    call(rec, "3", "mcp__notesgraph__update_document_meta", {"docId": "d2", "title": "New"})
    call(rec, "4", "mcp__notesgraph__update_block", {"blockId": "b"},
         json.dumps({"docId": "d3"}))
    call(rec, "5", "mcp__notesgraph__update_task", {"docId": "d1"})   # already created
    call(rec, "6", "mcp__notesgraph__link_document", {"parentDocId": "d2", "childDocId": "d1"},
         "linked")

    notes = rec.changes()["notes"]
    assert notes["created"] == [{"docId": "d1", "title": "Weekly plan"}]
    assert notes["edited"] == [{"docId": "d2", "title": "New"}, {"docId": "d3"}]
    assert notes["linked"] == [{"parentDocId": "d2", "childDocId": "d1"}]


def test_file_tools(tmp_path):
    rec = ChangeRecorder(str(tmp_path))
    call(rec, "1", "Edit", {"file_path": str(tmp_path / "a.py"),
                            "old_string": "x = 1", "new_string": "x = 2"})
    call(rec, "2", "Write", {"file_path": str(tmp_path / "sub" / "b.md"), "content": "hi"})
    call(rec, "3", "MultiEdit", {"file_path": "/elsewhere/c.py", "edits": [
        {"old_string": "a", "new_string": "b"}, {"old_string": "c", "new_string": "d"}]})
    call(rec, "4", "NotebookEdit", {"notebook_path": str(tmp_path / "n.ipynb"),
                                    "new_source": "print(1)"})
    assert rec.changes()["files"] == [
        {"path": "a.py", "before": "x = 1", "after": "x = 2"},
        {"path": "sub/b.md", "before": None, "after": "hi"},
        {"path": "/elsewhere/c.py", "before": "a", "after": "b"},
        {"path": "/elsewhere/c.py", "before": "c", "after": "d"},
        {"path": "n.ipynb", "before": None, "after": "print(1)"},
    ]


def test_errored_and_uncorrelated_results_are_ignored():
    rec = ChangeRecorder("/w")
    call(rec, "1", "Edit", {"file_path": "/w/a", "old_string": "a", "new_string": "b"},
         "no match", is_error=True)
    rec.tool_result({"type": "tool_result", "tool_use_id": "nope", "content": "x"})
    call(rec, "2", "Read", {"file_path": "/w/a"})
    assert rec.changes() is None


def test_caps():
    rec = ChangeRecorder("/w")
    call(rec, "big", "Write", {"file_path": "/w/big", "content": "y" * 9000})
    for i in range(60):
        call(rec, f"f{i}", "Write", {"file_path": f"/w/{i}", "content": "y"})
    for i in range(120):
        call(rec, f"n{i}", "mcp__notesgraph__update_document", {"docId": f"d{i}"})
    changes = rec.changes()
    assert len(changes["files"]) == 50
    assert changes["files"][0]["after"] == "y" * 8000 + "\n… (cut)"
    assert len(changes["notes"]["edited"]) == 100

    # Fifty files at the cap are over the record's size: the files go.
    rec = ChangeRecorder("/w")
    for i in range(50):
        call(rec, f"f{i}", "Write", {"file_path": f"/w/{i}", "content": "z" * 9000})
    changes = rec.changes()
    assert changes["files"] == []
    assert len(json.dumps(changes)) < 200_000


def test_oversized_record_drops_patch_then_files():
    record = {"notes": {"created": [], "edited": [], "linked": []},
              "files": [{"path": "a", "before": None, "after": "x" * 150_000}],
              "git": {"base": "b", "head": "h", "commits": [], "stat": [],
                      "patch": "p" * 100_000, "truncated": False}}
    fitted = run_changes.fit(record)
    assert fitted["git"]["patch"] == "" and fitted["git"]["truncated"] is True
    assert fitted["files"] == [{"path": "a", "before": None, "after": "x" * 150_000}]
    record["files"][0]["after"] = "x" * 250_000
    assert run_changes.fit(record)["files"] == []


def test_a_long_patch_is_truncated():
    rec = ChangeRecorder("/w")
    changes = rec.changes({"base": "b", "head": "h", "commits": [], "stat": [],
                           "patch": "p" * 70_000, "truncated": False})
    assert changes["git"]["truncated"] is True
    assert len(changes["git"]["patch"]) < 61_000


def git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def make_repo(path):
    path.mkdir()
    git(path, "init", "-q", "-b", "main")
    git(path, "config", "user.email", "t@example.com")
    git(path, "config", "user.name", "T")
    (path / "a.txt").write_text("one\n")
    git(path, "add", ".")
    git(path, "commit", "-qm", "first")
    return path


def test_git_changes_cover_commits_uncommitted_and_untracked(tmp_path):
    repo = make_repo(tmp_path / "repo")
    start = git_base(str(repo))
    assert start["branch"] == "main" and len(start["base"]) == 40
    assert git_changes(str(repo), start) is None   # nothing changed yet

    (repo / "b.txt").write_text("bee\n")
    git(repo, "add", "b.txt")
    git(repo, "commit", "-qm", "add b")
    (repo / "a.txt").write_text("one\ntwo\n")        # uncommitted
    (repo / "new.txt").write_text("fresh\nlines\n")  # untracked

    changes = git_changes(str(repo), start)
    assert changes["base"] == start["base"] and changes["head"] != start["base"]
    assert changes["branch"] == "main"
    assert [c["subject"] for c in changes["commits"]] == ["add b"]
    assert len(changes["commits"][0]["sha"]) == 12
    stat = {s["path"]: (s["added"], s["removed"]) for s in changes["stat"]}
    assert stat == {"a.txt": (1, 0), "b.txt": (1, 0), "new.txt": (2, 0)}
    assert "+two" in changes["patch"] and "+bee" in changes["patch"]
    assert "+fresh" in changes["patch"]
    assert changes["truncated"] is False


def test_no_git_outside_a_repo(tmp_path):
    assert git_base(str(tmp_path)) is None
    assert git_base(None) is None
    assert git_changes(str(tmp_path), None) is None


def test_the_narrator_records_subagent_calls_too():
    n = Narrator(io.StringIO(), io.StringIO(), "/w")
    n.feed(json.dumps({"type": "assistant", "parent_tool_use_id": "t0", "message": {
        "content": [{"type": "tool_use", "id": "t1", "name": "Write",
                     "input": {"file_path": "/w/x", "content": "c"}}]}}))
    n.feed(json.dumps({"type": "user", "parent_tool_use_id": "t0", "message": {
        "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "done"}]}}))
    assert n.recorder.changes()["files"] == [{"path": "x", "before": None, "after": "c"}]


FAKE = r"""
import json, pathlib
def emit(e): print(json.dumps(e), flush=True)
pathlib.Path("made.txt").write_text("hello\n")
emit({"type": "assistant", "message": {"content": [
    {"type": "tool_use", "id": "w1", "name": "Write",
     "input": {"file_path": str(pathlib.Path("made.txt").resolve()), "content": "hello\n"}}]}})
emit({"type": "user", "message": {"content": [
    {"type": "tool_result", "tool_use_id": "w1", "content": "written"}]}})
emit({"type": "result", "subtype": "success", "result": "made it", "num_turns": 1})
"""


def test_run_writes_changes_into_result_json(tmp_path):
    import sys

    repo = make_repo(tmp_path / "repo")
    script = tmp_path / "fake.py"
    script.write_text(FAKE)
    job_dir = tmp_path / "job"
    job_dir.mkdir()
    (job_dir / "cmd.json").write_text(json.dumps(
        {"argv": [sys.executable, str(script)], "provider": "claude", "cwd": str(repo)}))

    assert run(job_dir, out=io.StringIO()) == 0
    outcome = json.loads((job_dir / "result.json").read_text())
    changes = outcome["changes"]
    assert changes["files"] == [{"path": "made.txt", "before": None, "after": "hello\n"}]
    assert changes["git"]["stat"] == [{"path": "made.txt", "added": 1, "removed": 0}]
    assert "+hello" in changes["git"]["patch"]


def test_run_without_changes_writes_none(tmp_path):
    import sys

    job_dir = tmp_path / "job"
    job_dir.mkdir()
    (job_dir / "cmd.json").write_text(json.dumps(
        {"argv": [sys.executable, "-c", "print('hi')"], "provider": "x",
         "cwd": str(tmp_path)}))
    assert run(job_dir, out=io.StringIO()) == 0
    assert "changes" not in json.loads((job_dir / "result.json").read_text())
