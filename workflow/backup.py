"""`wf backup`: back up the production data of the repository it is run in.

Before a risky release someone takes a dump by hand: ssh in, find the
database's user in a .env, pipe pg_dump through a compressor, and hope the
file is whole. That is written down once per repository, next to its
release:

    repositories:
      /Users/me/code/notes-graph:
        backup:
          ssh: prod                       # a `wf deploy ssh` name; omit for this machine
          dir: /opt/notesgraph/backups    # where backups are kept there
          command: docker exec db pg_dump -U app -d app   # writes the dump to stdout
          extension: sql
          complete_marker: PostgreSQL database dump complete   # must be in its last lines
          keep: 10                        # newest N of wf's own backups
          min_free_gb: 5                  # don't start with less free disk

A backup is `<command> | zstd` (gzip when the machine has no zstd) into
`dir`, named `wf-<label>-<time>.<extension>.zst`. It counts only once the
command exited 0, the archive passes its integrity test, the marker is
there, and it isn't empty. A failed one is removed, not left looking like a
backup. Retention touches wf's own files only: other backups in the folder
(a deploy's) are listed but never deleted.
"""
from __future__ import annotations

import re
import shlex
import subprocess
import time
from pathlib import Path
from typing import Callable, List, Optional

from workflow.release import Machine, ReleaseError, Shell, find_release, save_release, shell_for

KEY = "backup"
PREFIX = "wf-"
DEFAULT_KEEP = 10
DEFAULT_MIN_FREE_GB = 5
# A dump of a few GB compresses in well under this; a hung one must not hold the terminal.
DUMP_TIMEOUT = 3600

_LABEL = re.compile(r"[^A-Za-z0-9._]+")


class BackupError(ReleaseError):
    pass


def find_backup(path: Optional[str] = None, cfg: Optional[dict] = None) -> dict:
    return find_release(path, cfg, key=KEY)


def save_backup(info: dict, section: dict) -> None:
    save_release(info, section, key=KEY)


def configured(section: dict) -> bool:
    return bool(section.get("command") and section.get("dir"))


def shell(section: dict) -> Shell:
    return shell_for(Machine(name="backup", path=section["dir"], ssh=section.get("ssh") or None))


def backup_name(section: dict, label: Optional[str] = None, now: Optional[float] = None) -> str:
    """`wf-<label>-<time>.<extension>`, without the compressor's suffix."""
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime(now))
    tag = _LABEL.sub("-", label).strip("-") if label else ""
    return f"{PREFIX}{tag + '-' if tag else ''}{stamp}.{section.get('extension') or 'dump'}"


def _script(section: dict, name: str) -> str:
    """The shell that takes one backup and says how it went, a line per fact."""
    directory = shlex.quote(section["dir"])
    base = shlex.quote(name)
    min_kb = int(float(section.get("min_free_gb", DEFAULT_MIN_FREE_GB)) * 1024 * 1024)
    marker = section.get("complete_marker")
    check_marker = (f'"${{cat[@]}}" "$f" | tail -c 4096 | grep -qF {shlex.quote(marker)}'
                    f' || {{ echo "::fail the dump does not end with its completion marker"; rm -f "$f"; exit 1; }}'
                    if marker else ":")
    return f"""set -o pipefail
mkdir -p {directory} && cd {directory} || {{ echo "::fail cannot use {section['dir']}"; exit 1; }}
free=$(df -Pk . | awk 'NR==2 {{print $4}}')
[ "$free" -ge {min_kb} ] || {{ echo "::fail only $((free / 1024 / 1024)) GB free"; exit 1; }}
if command -v zstd >/dev/null 2>&1; then pack=(zstd -3 -T0 -q); test=(zstd -tq); cat=(zstd -dc); f={base}.zst
else pack=(gzip); test=(gzip -t); cat=(gzip -dc); f={base}.gz; fi
( {section['command']} ) | "${{pack[@]}}" > "$f"
code=$?
[ "$code" = 0 ] || {{ echo "::fail the backup command exited $code"; rm -f "$f"; exit 1; }}
"${{test[@]}}" "$f" || {{ echo "::fail the archive is damaged"; rm -f "$f"; exit 1; }}
{check_marker}
raw=$("${{cat[@]}}" "$f" | wc -c | tr -d ' ')
[ "$raw" -gt 0 ] || {{ echo "::fail the backup is empty"; rm -f "$f"; exit 1; }}
echo "::file $f"
echo "::bytes $(wc -c < "$f" | tr -d ' ')"
echo "::raw $raw"
echo "::free $(df -Pk . | awk 'NR==2 {{print $4}}')"
"""


def _facts(out: str) -> dict:
    facts = {}
    for line in out.splitlines():
        if line.startswith("::") and " " in line:
            key, value = line[2:].split(" ", 1)
            facts[key] = value.strip()
    return facts


def take(section: dict, label: Optional[str] = None, run: Optional[Shell] = None,
         now: Optional[float] = None) -> dict:
    """Take one backup. Raises BackupError saying why when there is none."""
    run = run or shell(section)
    # bash, whatever the machine's login shell: the script uses pipefail and arrays.
    code, out = run("bash -c " + shlex.quote(_script(section, backup_name(section, label, now))), DUMP_TIMEOUT)
    facts = _facts(out)
    if code != 0 or "file" not in facts:
        other = [line for line in out.splitlines() if line.strip() and not line.startswith("::")]
        raise BackupError(facts.get("fail") or (other[-1] if other else f"exit {code}"))
    return {"file": f"{section['dir'].rstrip('/')}/{facts['file']}", "name": facts["file"],
            "bytes": int(facts["bytes"]), "raw_bytes": int(facts["raw"]),
            "free_gb": round(int(facts["free"]) / 1024 / 1024, 1),
            "where": section.get("ssh") or "this machine"}


def list_backups(section: dict, run: Optional[Shell] = None) -> List[dict]:
    """Every file in the backup folder, newest first; `ours` marks wf's own."""
    run = run or shell(section)
    code, out = run(f"cd {shlex.quote(section['dir'])} 2>/dev/null || exit 0; "
                    f"for f in *; do [ -f \"$f\" ] && echo \"$(stat -c '%Y %s' \"$f\" 2>/dev/null"
                    f" || stat -f '%m %z' \"$f\") $f\"; done", 60)
    if code != 0:
        raise BackupError(out.strip().splitlines()[-1] if out.strip() else f"exit {code}")
    rows = []
    for line in out.splitlines():
        parts = line.split(" ", 2)
        if len(parts) == 3 and parts[0].isdigit() and parts[1].isdigit():
            rows.append({"name": parts[2], "bytes": int(parts[1]), "at": int(parts[0]),
                         "ours": parts[2].startswith(PREFIX)})
    return sorted(rows, key=lambda r: -r["at"])


def prune(section: dict, run: Optional[Shell] = None) -> List[str]:
    """Remove wf's own backups beyond the newest `keep`; the names removed."""
    keep = int(section.get("keep", DEFAULT_KEEP))
    run = run or shell(section)
    old = [row["name"] for row in list_backups(section, run) if row["ours"]][keep:]
    if old:
        code, out = run(f"cd {shlex.quote(section['dir'])} && rm -f -- "
                        + " ".join(shlex.quote(name) for name in old), 60)
        if code != 0:
            raise BackupError(f"could not remove old backups: {out.strip()}")
    return old


def fetch(section: dict, name: Optional[str], to: str, cfg: Optional[dict] = None) -> dict:
    """Copy a backup (default: wf's newest) from its machine into the folder `to`."""
    if not name:
        ours = [row for row in list_backups(section) if row["ours"]]
        if not ours:
            raise BackupError("no wf backup to fetch; take one with 'wf backup'")
        name = ours[0]["name"]
    if "/" in name or name.startswith("."):
        raise BackupError(f"'{name}' is not a backup's name")
    target = Path(to).expanduser()
    target.mkdir(parents=True, exist_ok=True)
    local = target / name
    remote = f"{section['dir'].rstrip('/')}/{name}"
    if not section.get("ssh"):
        import shutil
        shutil.copyfile(Path(remote).expanduser(), local)
    else:
        from workflow.deploy import ssh_config
        from workflow.release import _ssh_target
        argv = ssh_config.ssh_argv(_ssh_target(section["ssh"], cfg), ["-o", "BatchMode=yes"],
                                   [f"cat {shlex.quote(remote)}"])
        with open(local, "wb") as out:
            done = subprocess.run(argv, stdout=out, stderr=subprocess.PIPE, timeout=DUMP_TIMEOUT)
        if done.returncode != 0:
            local.unlink(missing_ok=True)
            raise BackupError(done.stderr.decode(errors="replace").strip() or f"ssh exited {done.returncode}")
    # Owner-only: a database dump holds everyone's data.
    local.chmod(0o600)
    return {"file": str(local), "bytes": local.stat().st_size}


# --- how the CLI prints them ---------------------------------------------

def size(count: int) -> str:
    for unit, scale in (("GB", 1024 ** 3), ("MB", 1024 ** 2), ("kB", 1024)):
        if count >= scale:
            return f"{count / scale:.1f} {unit}" if count < 10 * scale else f"{count / scale:.0f} {unit}"
    return f"{count} B"


def format_taken(result: dict, removed: Optional[List[str]] = None) -> str:
    lines = [f"✓ {result['file']} on {result['where']}",
             f"  {size(result['bytes'])} ({size(result['raw_bytes'])} before compression), verified",
             f"  {result['free_gb']} GB free there now"]
    if removed:
        lines.append(f"  removed {len(removed)} older: {', '.join(removed)}")
    return "\n".join(lines)


def format_list(rows: List[dict], clock: Callable[[int], str] = lambda at: time.strftime(
        "%Y-%m-%d %H:%M", time.localtime(at))) -> str:
    if not rows:
        return "No backups."
    return "\n".join(f"{'✓' if row['ours'] else '·'} {clock(row['at'])}  {size(row['bytes']):>8}  {row['name']}"
                     for row in rows)
