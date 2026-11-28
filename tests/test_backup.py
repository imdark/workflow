"""wf backup: a repository's production data, dumped, verified and kept.

The backup script really runs, in bash on this machine against a temp folder: a `printf` stands in for the
database dump. Nothing connects anywhere.
"""
import gzip
import json
import subprocess

import pytest
import yaml
from typer.testing import CliRunner

from workflow import backup as bk
from workflow import cli_backup

runner = CliRunner()
DUMP = "printf 'row 1\\nrow 2\\n-- dump complete\\n'"


def unpack(path):
    if str(path).endswith(".zst"):
        return subprocess.run(["zstd", "-dc", str(path)], capture_output=True, check=True).stdout.decode()
    return gzip.decompress(path.read_bytes()).decode()


@pytest.fixture
def world(tmp_path, monkeypatch):
    repo, store = tmp_path / "app", tmp_path / "backups"
    repo.mkdir()
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
    cfg_path = tmp_path / "config.yaml"
    section = {"dir": str(store), "command": DUMP, "extension": "sql", "complete_marker": "dump complete",
               "keep": 2, "min_free_gb": 0}
    cfg_path.write_text(yaml.dump({"task_backend": "markdown", "projects": {"personal": {"repositories": {
        str(repo): {"base_branch": "stage", "backup": section}}}}}))
    monkeypatch.setattr("workflow.config.CFG", cfg_path)
    return type("W", (), {"repo": repo, "store": store, "cfg": cfg_path, "section": section, "root": tmp_path})()


def _wf(world, *args):
    return runner.invoke(cli_backup.backup_app, [*args, "--repo-path", str(world.repo)])


def test_a_backup_is_the_commands_output_compressed_named_and_verified(world):
    result = bk.take(world.section, label="pre 478!", now=0)
    assert result["name"].startswith("wf-pre-478-19700101-000000.sql.") and result["where"] == "this machine"
    taken = world.store / result["name"]
    assert unpack(taken) == "row 1\nrow 2\n-- dump complete\n"
    assert result["raw_bytes"] == 29 and result["bytes"] == taken.stat().st_size
    text = bk.format_taken(result, ["wf-old.sql.zst"])
    assert f"✓ {taken} on this machine" in text and "verified" in text and "removed 1 older: wf-old.sql.zst" in text


@pytest.mark.parametrize("change, why", [
    ({"command": "printf 'half a dump'; exit 3"}, "the backup command exited 3"),
    ({"command": "printf 'row 1\\n'"}, "does not end with its completion marker"),
    ({"command": "true", "complete_marker": None}, "the backup is empty"),
    ({"min_free_gb": 10 ** 9}, "GB free"),
])
def test_a_backup_that_fails_a_check_is_removed_and_says_why(world, change, why):
    with pytest.raises(bk.BackupError, match=why):
        bk.take({**world.section, **change})
    # Nothing is left that could be mistaken for a backup.
    assert not world.store.exists() or list(world.store.iterdir()) == []


def test_only_wfs_own_backups_are_pruned(world):
    world.store.mkdir()
    (world.store / "db-20261127-011555.sql.zst").write_text("the deploy's own")
    names = [bk.take(world.section, now=t)["name"] for t in (1000, 2000, 3000)]
    import os
    for at, name in zip((1000, 2000, 3000), names):
        os.utime(world.store / name, (at, at))
    removed = bk.prune(world.section)
    assert removed == [names[0]]
    left = bk.list_backups(world.section)
    assert [row["name"] for row in left if row["ours"]] == [names[2], names[1]]
    assert [row["name"] for row in left if not row["ours"]] == ["db-20261127-011555.sql.zst"]
    assert "· " in bk.format_list(left) and "✓ " in bk.format_list(left)
    assert bk.format_list([]) == "No backups."


def test_fetch_copies_the_newest_readable_by_the_owner_only(world):
    name = bk.take(world.section, now=5)["name"]
    out = bk.fetch(world.section, None, str(world.root / "copies"))
    copy = world.root / "copies" / name
    assert out["file"] == str(copy) and unpack(copy).endswith("dump complete\n")
    assert copy.stat().st_mode & 0o777 == 0o600
    with pytest.raises(bk.BackupError, match="not a backup's name"):
        bk.fetch(world.section, "../../etc/passwd", str(world.root / "copies"))


def test_the_command_takes_lists_and_exits_1_when_there_is_no_backup(world):
    r = _wf(world, "--label", "pre-release", "--json")
    assert r.exit_code == 0, r.output
    taken = json.loads(r.output)
    assert "wf-pre-release-" in taken["name"] and taken["removed"] == []
    assert taken["name"] in _wf(world, "list").output
    assert _wf(world, "config", "--command", "exit 9").exit_code == 0
    r = _wf(world)
    assert r.exit_code == 1 and "the backup command exited 9" in r.output
    # The repository's other settings are kept.
    repos = yaml.safe_load(world.cfg.read_text())["projects"]["personal"]["repositories"]
    assert repos[str(world.repo)]["base_branch"] == "stage" and repos[str(world.repo)]["backup"]["command"] == "exit 9"


def test_a_repository_with_no_backup_set_says_how_to_set_one(world):
    cfg = yaml.safe_load(world.cfg.read_text())
    cfg["projects"]["personal"]["repositories"][str(world.repo)].pop("backup")
    world.cfg.write_text(yaml.dump(cfg))
    r = _wf(world)
    assert r.exit_code == 2 and "has no backup set" in r.output
    assert "no backup set" in _wf(world, "config").output


def test_a_remote_backup_runs_the_same_script_over_ssh(world):
    sent = []
    result = bk.take({**world.section, "ssh": "prod"}, now=0, run=lambda command, timeout: sent.append(command) or (
        0, "::file wf-19700101-000000.sql.zst\n::bytes 100\n::raw 900\n::free 27262976\n"))
    assert sent[0].startswith("bash -c ") and "set -o pipefail" in sent[0]
    assert result == {"file": f"{world.store}/wf-19700101-000000.sql.zst", "name": "wf-19700101-000000.sql.zst",
                      "bytes": 100, "raw_bytes": 900, "free_gb": 26.0, "where": "prod"}
    with pytest.raises(bk.BackupError, match="Permission denied"):
        bk.take(world.section, run=lambda command, timeout: (255, "ubuntu@host: Permission denied (publickey)."))
