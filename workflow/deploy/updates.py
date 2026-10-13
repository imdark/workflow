"""Notice when wf's own code changes under a long-running process.

`wf agent serve` loads wf once and runs for days; a `git pull` in the wf
checkout does nothing to it until it restarts. Run under a supervisor with
`--exit-on-update`, it watches this and, once its jobs are done, exits so the
supervisor starts it again on the new code (see `wf agent install`).
"""

import subprocess
import time
from pathlib import Path
from typing import Callable, Optional

# workflow/ (this is workflow/deploy/updates.py); `workflow` is a namespace
# package, so it has no __file__ of its own to go by.
PACKAGE_DIR = Path(__file__).resolve().parents[1]


def code_fingerprint(package_dir: Path = PACKAGE_DIR) -> str:
    """What identifies the code a process would load now.

    The checkout's commit, when wf runs from one, plus the newest source
    file's mtime -- a pull moves the first, an edit or an install the second.
    """
    commit = ""
    try:
        commit = subprocess.run(
            ["git", "-C", str(package_dir), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=10,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    newest = 0.0
    for path in package_dir.rglob("*.py"):
        try:
            newest = max(newest, path.stat().st_mtime)
        except OSError:
            continue
    return f"{commit}:{newest:.0f}"


class UpdateWatch:
    """Callable: True once the code differs from when this was made.

    Checked at most every `interval` seconds, so a tight poll loop can ask
    each turn. Stays True once it has seen a change.
    """

    def __init__(self, interval: float = 30.0,
                 fingerprint: Callable[[], str] = code_fingerprint,
                 clock: Callable[[], float] = time.monotonic):
        self._fingerprint = fingerprint
        self._clock = clock
        self._interval = interval
        self._start = fingerprint()
        self._checked = clock()
        self._changed = False

    def __call__(self) -> bool:
        if self._changed:
            return True
        now = self._clock()
        if now - self._checked < self._interval:
            return False
        self._checked = now
        self._changed = self._fingerprint() != self._start
        return self._changed

    @property
    def started_on(self) -> Optional[str]:
        return self._start
