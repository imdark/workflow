import subprocess

from workflow.debt_ledger import find_markers, render_ledger, run_debt


def repo(tmp_path, files):
    for name, text in files.items():
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_text(text)
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True)
    return tmp_path


FILES = {
    "app.py": "x = 1  # shortcut: in-memory cache, move to redis when there are 2 workers\n"
              "# TODO: handle unicode\n"
              "label = 'TODO list'\n",
    "web/a.ts": "// FIXME(mike): flaky once the socket drops\n",
    "README.md": "- TODO: write docs\n",
}


def test_markers_and_triggers(tmp_path):
    markers = {m.path: m for m in find_markers(repo(tmp_path, FILES)) if m.line == 1}
    found = sorted((m.path, m.line, m.kind) for m in find_markers(tmp_path))
    assert found == [("app.py", 1, "shortcut"), ("app.py", 2, "todo"), ("web/a.ts", 1, "fixme")]
    assert markers["app.py"].has_trigger and markers["web/a.ts"].has_trigger
    unicode = next(m for m in find_markers(tmp_path) if m.line == 2)
    assert unicode.text == "handle unicode" and not unicode.has_trigger


def test_ledger_keeps_a_status_while_the_marker_stays(tmp_path):
    markers = find_markers(repo(tmp_path, FILES))
    first = render_ledger(markers, "#work #project:x")
    assert "- [ ] `app.py:2` handle unicode #work #project:x #no-trigger" in first
    edited = first.replace("- [ ] `app.py:2`", "- [-] `app.py:2`")
    # The marker moved down a line: still the same task.
    moved = [m if m.line != 2 else type(m)(m.path, 5, m.kind, m.text) for m in markers]
    assert "- [-] `app.py:5` handle unicode" in render_ledger(moved, "#work #project:x", edited)


class FakeNotes:
    def __init__(self):
        self.docs, self.calls = {}, []

    def find(self, title):
        return {"docId": title} if title in self.docs or title == "Parent" else None

    def read(self, doc_id):
        return self.docs[doc_id]

    def call(self, tool, args):
        self.calls.append(tool)
        if tool == "create_document":
            self.docs[args["title"]] = args["content"]
        elif tool == "update_document":
            self.docs[args["docId"]] = args["content"]


def test_run_creates_then_updates_the_note(tmp_path):
    root = repo(tmp_path, FILES)
    notes = FakeNotes()
    assert run_debt(root, "work", "Parent", False, reader=notes, echo=lambda *_: None) == 0
    title = f"{root.name} shortcut ledger"
    assert notes.calls == ["create_document", "link_document"] and "handle unicode" in notes.docs[title]
    assert run_debt(root, "work", None, False, reader=notes, echo=lambda *_: None) == 0
    assert notes.calls[-1] == "update_document"
