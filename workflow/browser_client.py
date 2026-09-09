"""Thin HTTP client for the workflow browser daemon (workflow/browser_daemon.py).

Wraps the daemon's REST endpoints so automations can drive a real,
CDP-attached Chrome tab (open a URL, read the DOM, type text, click,
scroll, hover, screenshot) without each caller re-implementing the
request/response plumbing.
"""

import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

import requests

DAEMON_URL = "http://127.0.0.1:18765"


class BrowserDaemonError(Exception):
    pass


class BrowserClient:
    def __init__(self, base_url: str = DAEMON_URL, timeout: int = 25):
        self.base_url = base_url
        self.timeout = timeout

    def _post(self, path: str, payload: Optional[dict] = None, timeout: Optional[int] = None) -> dict:
        resp = requests.post(f"{self.base_url}{path}", json=payload or {}, timeout=timeout or self.timeout)
        return resp.json()

    def _put(self, path: str, payload: Optional[dict] = None, timeout: Optional[int] = None) -> dict:
        resp = requests.put(f"{self.base_url}{path}", json=payload or {}, timeout=timeout or self.timeout)
        return resp.json()

    def health(self) -> bool:
        try:
            resp = requests.get(f"{self.base_url}/health", timeout=3)
            return resp.json().get("status") == "ok"
        except Exception:
            return False

    def open(self, url: str) -> dict:
        return self._put("/open", {"url": url}, timeout=30)

    def eval(self, script: str, timeout: Optional[int] = None) -> dict:
        """script must be a JS function, e.g. '() => document.title'."""
        return self._post("/eval", {"script": script}, timeout=timeout)

    def fill(self, selector: str, value: str) -> dict:
        return self._post("/fill", {"uid": selector, "value": value})

    def press(self, key: str) -> dict:
        return self._post("/press", {"key": key})

    def click(self, selector: str) -> dict:
        return self._post("/click", {"uid": selector})

    def screenshot(self, output_path: str) -> dict:
        return self._post("/screenshot", {"output": output_path}, timeout=20)

    def snapshot(self) -> str:
        data = self._post("/snapshot", timeout=20)
        return data.get("content", "")

    def cookies(self) -> list:
        data = self._post("/cookies", timeout=20)
        return data.get("cookies", [])

    def select_page(self, url_contains: str) -> dict:
        return self._post("/select_page", {"url_contains": url_contains})

    def scroll(self, x: int = 640, y: int = 400, delta_y: int = 1000) -> dict:
        return self._post("/scroll", {"x": x, "y": y, "deltaY": delta_y})

    def hover(self, x: int, y: int) -> dict:
        return self._post("/hover", {"x": x, "y": y})

    def real_click(self, x: int, y: int) -> dict:
        """A genuine CDP mouse click at the given viewport coordinates --
        more reliable than a synthetic `.click()` for elements whose React
        handlers care about real pointer events (e.g. Slack's search button)."""
        return self._post("/real_click", {"x": x, "y": y})

    def real_click_selector(self, selector: str) -> dict:
        """Scroll an element into view and real-click its center."""
        rect = self.eval(
            "() => { const el = document.querySelector(" + __import__("json").dumps(selector) + "); "
            "if (!el) return null; el.scrollIntoView({block: 'center'}); const r = el.getBoundingClientRect(); "
            "return JSON.stringify({x: Math.round(r.x + r.width/2), y: Math.round(r.y + r.height/2)}); }"
        )
        result = rect.get("result")
        if not result or result == "null":
            raise BrowserDaemonError(f"element not found for selector: {selector}")
        import json as _json
        coords = _json.loads(result)
        return self.real_click(coords["x"], coords["y"])

    def paste_text(self, selector: str, text: str) -> dict:
        """Insert text via a synthetic clipboard paste event rather than
        simulated keystrokes -- required for multi-line text, since typing
        a literal newline into Slack's composer sends the message early."""
        import json as _json
        text_js = _json.dumps(text)
        selector_js = _json.dumps(selector)
        script = (
            "() => { const el = document.querySelector(" + selector_js + "); "
            "if (!el) return 'NO_EL'; el.focus(); const dt = new DataTransfer(); "
            "dt.setData('text/plain', " + text_js + "); "
            "const evt = new ClipboardEvent('paste', {clipboardData: dt, bubbles: true, cancelable: true}); "
            "el.dispatchEvent(evt); return 'pasted:' + el.innerText.length; }"
        )
        return self.eval(script)


def is_daemon_running(base_url: str = DAEMON_URL) -> bool:
    return BrowserClient(base_url).health()


def start_daemon(chrome_debug_port: int = 9222, wait_seconds: float = 3.0) -> bool:
    """Start the daemon if it isn't already running. Requires a Chrome
    instance already listening on chrome_debug_port (see 'wf browser open'
    and the Confluence skill docs for how to launch one)."""
    if is_daemon_running():
        return True

    state_dir = Path.home() / ".wf" / "browser"
    state_dir.mkdir(parents=True, exist_ok=True)
    for f in ("state", "daemon.pid"):
        p = state_dir / f
        if p.exists():
            p.unlink()

    env = {"CHROME_DEBUG_PORT": str(chrome_debug_port)}
    import os
    subprocess.Popen(
        [sys.executable, "-m", "workflow.browser_daemon", "start"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env={**os.environ, **env},
    )
    for _ in range(int(wait_seconds * 5)):
        time.sleep(0.2)
        if is_daemon_running():
            return True
    return False
