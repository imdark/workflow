"""Start, stop and inspect the capture proxy as a background daemon.

Uses the same ~/.wf/state convention as `workflow.pid_manager`, with one
PID file for the single proxy instance.
"""

import json
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional

STATE_DIR = Path.home() / ".wf" / "state"
PID_FILE = STATE_DIR / "proxy.pid"
LOG_FILE = Path.home() / ".wf" / "logs" / "proxy.log"


def _read_pid() -> Optional[dict]:
    if not PID_FILE.exists():
        return None
    try:
        return json.loads(PID_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def _alive(pid: int) -> bool:
    if pid is None or pid < 1:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # It exists but belongs to another user: running, just not ours.
        return True
    except OSError:
        return False
    return True


def status() -> dict:
    """Current daemon state: running, its endpoint, and health if reachable."""
    data = _read_pid()
    if not data or not _alive(data.get("pid", -1)):
        return {"running": False}

    info = {"running": True, **data}
    url = f"http://{data.get('host')}:{data.get('port')}/_wf/health"
    try:
        with urllib.request.urlopen(url, timeout=2) as r:
            info["health"] = json.loads(r.read().decode())
    except (urllib.error.URLError, OSError, json.JSONDecodeError):
        info["health"] = None  # process is up but not answering yet
    return info


def base_url(config=None) -> str:
    """Endpoint agents should point at, whether or not the daemon is up."""
    if config is None:
        from workflow.config import load_effective_config
        config = load_effective_config()
    proxy_cfg = config.get("proxy") or {}
    host = proxy_cfg.get("host", "127.0.0.1")
    port = int(proxy_cfg.get("port", 8099))
    return f"http://{host}:{port}"


def start(config=None, foreground: bool = False) -> dict:
    """Launch the proxy. Returns its status dict."""
    existing = status()
    if existing.get("running"):
        return existing

    if config is None:
        from workflow.config import load_effective_config
        config = load_effective_config()
    proxy_cfg = config.get("proxy") or {}
    host = proxy_cfg.get("host", "127.0.0.1")
    port = int(proxy_cfg.get("port", 8099))

    if foreground:
        from workflow.proxy.server import serve
        _write_pid(os.getpid(), host, port)
        try:
            serve(config)
        finally:
            PID_FILE.unlink(missing_ok=True)
        return {"running": False}

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    log = LOG_FILE.open("a")
    process = subprocess.Popen(
        [sys.executable, "-m", "workflow.proxy.daemon", "--serve"],
        stdout=log, stderr=log, stdin=subprocess.DEVNULL,
        start_new_session=True,  # survive the launching shell
    )
    _write_pid(process.pid, host, port)

    # Give it a moment to bind so the caller gets a truthful status.
    for _ in range(20):
        time.sleep(0.1)
        current = status()
        if current.get("health"):
            return current
        if process.poll() is not None:
            PID_FILE.unlink(missing_ok=True)
            return {"running": False, "error": f"exited {process.returncode}; see {LOG_FILE}"}
    return status()


def _write_pid(pid: int, host: str, port: int) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    PID_FILE.write_text(json.dumps({
        "pid": pid, "host": host, "port": port,
        "started_at": time.time(),
    }, indent=2))


def stop(timeout: float = 10.0) -> bool:
    """Stop the daemon, giving it time to flush and summarize open sessions."""
    data = _read_pid()
    if not data:
        return False
    pid = data.get("pid", -1)
    if not _alive(pid):
        PID_FILE.unlink(missing_ok=True)
        return False

    os.kill(pid, signal.SIGTERM)
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not _alive(pid):
            PID_FILE.unlink(missing_ok=True)
            return True
        time.sleep(0.2)

    os.kill(pid, signal.SIGKILL)
    PID_FILE.unlink(missing_ok=True)
    return True


def env_for_agents(config=None) -> dict:
    """Environment that routes supported agents through the proxy."""
    url = base_url(config)
    return {
        "ANTHROPIC_BASE_URL": url,
        "OPENAI_BASE_URL": f"{url}/v1",
    }


if __name__ == "__main__":
    # Entry point for the detached child process.
    if "--serve" in sys.argv:
        from workflow.config import load_effective_config
        from workflow.proxy.server import serve
        serve(load_effective_config())
