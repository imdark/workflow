import subprocess

from workflow.code_map import build_map, map_context


def repo(tmp_path, files):
    for name, text in files.items():
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_text(text)
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True)
    return tmp_path


def test_map_lists_names_shared_folders_first_and_skips_tests(tmp_path):
    root = repo(tmp_path, {
        "app/main.py": "def run():\n    pass\nclass App:\n    def _inner(self): ...\n",
        "lib/strings.ts": "export function slug(s) {}\nexport const MAX = 3\nfunction hidden() {}\n",
        "tests/test_main.py": "def test_run(): ...\n",
        "web/a.test.ts": "export function nope() {}\n",
        "notes.md": "def not_code(): ...\n",
    })
    assert build_map(root).splitlines() == ["lib/strings.ts: slug, MAX", "app/main.py: run, App"]


def test_map_keeps_to_its_budget(tmp_path):
    root = repo(tmp_path, {f"m{i}.py": f"def function_number_{i}(): ...\n" for i in range(50)})
    assert 0 < len(build_map(root, budget=200)) <= 200


def test_no_repo_no_map(tmp_path):
    assert build_map(tmp_path) == "" and map_context(tmp_path) == ""
