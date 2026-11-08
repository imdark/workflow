"""The tools that let a Claude agent run talk to the person who started it.

    python -m workflow.deploy.agent_mcp      (stdio MCP server)

Started by Claude Code for a "claude-code" job (see `jobs.claude_code_argv`).
The first two post to the job's questions on the NotesGraph server and block
until the person who started the run answers in NotesGraph:

    ask_user   a question -- "Who is Cosmo, and how old?" -- optionally with
               choices the person can pick with one click
    approve    Claude Code's --permission-prompt-tool: allow or deny a tool

and one returns at once:

    set_title  name the run in NotesGraph's run lists, which otherwise show
               the text of the block it started from

While a tool is blocked it keeps a `waiting-<question id>` file in the job
directory, so the runner can stop counting that time against the job's
time limit. On a Mac it also shows a desktop notification once a question
has waited a few seconds unanswered, so a run waiting on its person doesn't
sit unnoticed; NG_NOTIFY=0 turns that
off. Clicking it opens a dialog to answer right there, like the phone's
notification pane (ask_notifier.swift, built on first use); without swiftc
it falls back to a plain notification.

Configured by environment, set by the runner: NG_URL, NG_WORKSPACE, NG_JOB,
NG_TOKEN and NG_JOB_DIR. Speaks newline-delimited JSON-RPC on stdio; stdout
is the protocol, so nothing else may be printed there.
"""

import hashlib
import json
import mimetypes
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Callable, Optional

PROTOCOL_VERSION = "2025-06-18"
POLL_SECONDS = 2.0
# How long a question waits before the Mac notifies about it. Most quick
# answers -- in an open NotesGraph tab, or an "Allow all" sweeping up the
# run's other open permissions -- land well inside this, and a notification
# for one would only flash up and vanish.
NOTIFY_AFTER_SECONDS = 5.0
# Matches the server's cap on a question's detail (registry jobs.ts MAX_DETAIL).
MAX_DETAIL = 200_000
# An artifact is for looking at on a phone: a preview, not the master.
MAX_ARTIFACT_BYTES = 100 * 1024 * 1024
# Artifacts shown with one question.
MAX_ATTACHMENTS = 6
# Rate limited, or the server restarting: worth asking again, for up to
# RETRY_FOR_SECONDS, rather than failing the tool call.
RETRY_STATUSES = {429, 502, 503, 504}
RETRY_FOR_SECONDS = 120.0
RETRY_PAUSE_SECONDS = 5.0
# How often a waiting ask_user or approve sends MCP progress, well inside
# Claude Code's idle timeout for a silent tool call.
HEARTBEAT_SECONDS = 20.0


def retry_after(header: Optional[str]) -> float:
    """Seconds to wait before asking again: the server's Retry-After, kept
    between 1 and 30, or RETRY_PAUSE_SECONDS when it gave none."""
    try:
        return min(max(float(header), 1.0), 30.0)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return RETRY_PAUSE_SECONDS

TOOLS = [
    {
        "name": "ask_user",
        "description": (
            "Ask the person who started this run a question and wait for their "
            "answer. Use it whenever you need a fact you could not find in "
            "NotesGraph or a decision only they can make. Ask one short, "
            "specific question; group closely related unknowns into it. When "
            "the answer is one of a few choices (yes/no, which of these, go on "
            "or stop), pass them as options so they can pick with one click; "
            "they can still type something else. To show them something you "
            "made (a preview video, a filmstrip, a chart), upload it first with "
            "upload_artifact and pass its key in attachments: it is shown with "
            "the question, on their phone too."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "question": {"type": "string", "description": "The question, as you would ask it in person."},
                "options": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional short choices, e.g. [\"Yes, open the PR\", \"No, stop here\"]. At most 10.",
                },
                "attachments": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": ("Artifact keys from upload_artifact to show with the question "
                                    "(a video or image), e.g. a preview cut with options "
                                    "[\"Approve\", \"Revise\", \"Try another take\"]. At most 6."),
                },
            },
            "required": ["question"],
        },
    },
    {
        "name": "upload_artifact",
        "description": (
            "Upload a file this run made (a preview mp4, a filmstrip png, a "
            "rendered final) to NotesGraph, so the person can see it. Returns "
            "its key and URL. Pass the key in ask_user's attachments to show "
            "it with a question; put the URL in a note for the final result. "
            "Keep uploads under 100 MB: a 720p preview, not the 4K master."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Absolute path of the file to upload."},
                "name": {"type": "string", "description": "A file name to show; default the file's own."},
            },
            "required": ["path"],
        },
    },
    {
        "name": "set_title",
        "description": (
            "Name this run in NotesGraph's run list, which until then shows the "
            "text of the block it started from. Call it once you know what you "
            "are doing, with a few words a person would recognise; call it "
            "again if the work turns out to be something else."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "A short title, e.g. \"Pick a show to watch with Cosmo\"."},
            },
            "required": ["title"],
        },
    },
    {
        "name": "approve",
        "description": "Permission prompt: asks the person whether a tool call may run.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "tool_name": {"type": "string"},
                "input": {"type": "object"},
                "tool_use_id": {"type": "string"},
            },
            "required": ["tool_name", "input"],
        },
    },
]


class JobCancelled(Exception):
    pass


# Text goes in as arguments, not spliced into the script, so quotes and
# backslashes in a question can't break it.
_NOTIFY_SCRIPT = [
    "on run argv",
    'display notification (item 3 of argv) with title (item 1 of argv)'
    ' subtitle (item 2 of argv) sound name "Glass"',
    "end run",
]


def notify(title: str, subtitle: str, message: str) -> None:
    """Show a macOS notification and return at once; a no-op elsewhere."""
    if sys.platform != "darwin" or os.environ.get("NG_NOTIFY") == "0":
        return
    argv = ["osascript"]
    for line in _NOTIFY_SCRIPT:
        argv += ["-e", line]
    try:
        # Never wait on it, and keep it off stdout: that is the protocol.
        subprocess.Popen(argv + [title, subtitle, message[:300]],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
    except OSError:
        pass  # no notification is no reason to fail the question


NOTIFIER_SOURCE = Path(__file__).with_name("ask_notifier.swift")
NOTIFIER_HOME = Path.home() / "Library" / "Caches" / "wf" / "ask-notifier"
NOTIFIER_APP = "NotesGraph Agent.app"
# A bundle id of its own is what lets macOS hand the click back to us;
# LSUIElement keeps it out of the Dock.
_NOTIFIER_PLIST = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleIdentifier</key><string>com.notesgraph.agent-questions</string>
  <key>CFBundleName</key><string>NotesGraph Agent</string>
  <key>CFBundleExecutable</key><string>ask-notifier</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>1</string>
  <key>CFBundleVersion</key><string>1</string>
  <key>LSUIElement</key><true/>
</dict>
</plist>
"""
_build_lock = threading.Lock()


def notifier_app() -> Optional[Path]:
    """The answerable-notification app, built from its source on first use
    (and again whenever the source changes); None when it can't be."""
    try:
        source = NOTIFIER_SOURCE.read_bytes() + _NOTIFIER_PLIST.encode()
        digest = hashlib.sha256(source).hexdigest()[:12]
    except OSError:
        return None
    app = NOTIFIER_HOME / digest / NOTIFIER_APP
    binary = app / "Contents" / "MacOS" / "ask-notifier"
    with _build_lock:
        if binary.exists():
            return app
        swiftc = shutil.which("swiftc")
        if not swiftc:
            return None
        try:
            NOTIFIER_HOME.mkdir(parents=True, exist_ok=True)
            # Built aside and renamed in, as several runs may race to build it.
            with tempfile.TemporaryDirectory(dir=NOTIFIER_HOME) as tmp:
                built = Path(tmp) / NOTIFIER_APP
                (built / "Contents" / "MacOS").mkdir(parents=True)
                (built / "Contents" / "Info.plist").write_text(_NOTIFIER_PLIST)
                subprocess.run([swiftc, "-O", "-o", str(built / "Contents" / "MacOS" / "ask-notifier"),
                                str(NOTIFIER_SOURCE)],
                               check=True, capture_output=True, timeout=600)
                subprocess.run(["codesign", "--force", "--sign", "-", str(built)],
                               check=True, capture_output=True, timeout=60)
                app.parent.mkdir(parents=True, exist_ok=True)
                try:
                    built.rename(app)
                except OSError:
                    pass  # another run got there first
        except (OSError, subprocess.SubprocessError):
            return None
    return app if binary.exists() else None


class MacQuestion:
    """One question showing in the notifier app.

    Started through `open`, as macOS only lets an app LaunchServices started
    post notifications -- so it is no child of ours, and the answer and the
    take-it-away come and go through files."""

    def __init__(self, app: Path, ask: dict):
        self.dir = NOTIFIER_HOME / "questions"
        self.dir.mkdir(parents=True, exist_ok=True)
        name = "".join(c if c.isalnum() or c in "-_" else "_" for c in ask["id"])
        self.out, self.cancel = self.dir / f"{name}.out", self.dir / f"{name}.cancel"
        self.out.unlink(missing_ok=True)
        ask = {**ask, "id": name, "dir": str(self.dir), "parentPid": os.getpid()}
        # -n: a process of its own, -W: so wait() lasts as long as it does.
        self.proc = subprocess.Popen(
            ["open", "-n", "-W", "--stdout", str(self.out), "--stderr", "/dev/null",
             str(app), "--args", json.dumps(ask)],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def answer(self) -> Optional[dict]:
        """Wait for it to close; what it said, or None when taken away."""
        self.proc.wait()
        try:
            line = self.out.read_text().strip().splitlines()[0]
            return json.loads(line)
        except (OSError, IndexError, ValueError):
            return None
        finally:
            self.out.unlink(missing_ok=True)
            self.cancel.unlink(missing_ok=True)

    def take_away(self) -> None:
        """Answered elsewhere: close it, notification and dialog both."""
        if self.proc.poll() is None:
            try:
                self.cancel.touch()
            except OSError:
                pass


def show_question(question_id: str, kind: str, heading: str, title: str, text: str,
                  detail: Optional[str], options: list) -> Optional[MacQuestion]:
    """Show a question as a notification that can be answered from the Mac;
    None when it shows a plain notification instead."""
    if sys.platform != "darwin" or os.environ.get("NG_NOTIFY") == "0":
        return None
    app = notifier_app()
    if app:
        try:
            return MacQuestion(app, {"id": question_id, "kind": kind, "heading": heading,
                                     "title": title, "text": text, "detail": detail,
                                     "options": options})
        except OSError:
            pass
    notify(heading, title, text)
    return None


class Questions:
    """Ask a job's questions over the registry API and wait for answers."""

    def __init__(self, url: str, workspace: str, job: str, token: str,
                 job_dir: Optional[Path] = None, poll_seconds: float = POLL_SECONDS,
                 notify_after: float = NOTIFY_AFTER_SECONDS,
                 retry_for: float = RETRY_FOR_SECONDS):
        self.job_url = f"{url.rstrip('/')}/api/registry/workspaces/{workspace}/jobs/{job}"
        self.base = f"{self.job_url}/questions"
        self.token, self.job_dir, self.poll_seconds = token, job_dir, poll_seconds
        self.notify_after, self.retry_for = notify_after, retry_for
        self.title = ""  # the run's name once set_title has given it one

    def _request(self, method: str, url: str, payload: Optional[dict] = None,
                 retry: bool = True) -> dict:
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        waited = 0.0 if retry else self.retry_for
        while True:
            request = urllib.request.Request(
                url, data=body, method=method,
                headers={"Authorization": f"Bearer {self.token}",
                         "Content-Type": "application/json"},
            )
            try:
                with urllib.request.urlopen(request, timeout=30) as response:
                    return json.loads(response.read().decode("utf-8") or "{}")
            except urllib.error.HTTPError as e:
                # Every run on this machine shares the server's rate limit,
                # and a 429 on a permission would deny a tool the person
                # allowed (with "Allow all", one after another). Wait it out.
                if e.code not in RETRY_STATUSES or waited >= self.retry_for:
                    raise
                pause = min(retry_after(e.headers.get("Retry-After")),
                            self.retry_for - waited)
                time.sleep(pause)
                waited += max(pause, 0.1)

    def take_steers(self) -> list:
        """The instructions sent to the run since the last take, each once.

        Empty when the server can't be reached: they wait there for the
        next step, and a failed read is no reason to stop the run."""
        try:
            # No waiting out a rate limit: they keep for the next take.
            return self._request("POST", f"{self.job_url}/steer/take", {},
                                 retry=False).get("steers") or []
        except (urllib.error.URLError, OSError, ValueError):
            return []

    def set_title(self, title: str) -> str:
        """Rename the job; returns the title the server kept."""
        self.title = self._request("POST", f"{self.job_url}/title", {"title": title})["job"]["title"]
        return self.title

    def upload_artifact(self, path: str, name: Optional[str] = None) -> dict:
        """Upload a file the run made to the job's artifacts; returns the
        artifact the server recorded ({id, key, name, mime, size, url, ...}).

        The bytes go as the request body, with the file's media type, so the
        server stores and serves it as what it is. Capped at MAX_ARTIFACT_BYTES:
        a preview is for looking at on a phone, not the master."""
        file = Path(path).expanduser()
        if not file.is_file():
            raise FileNotFoundError(f"no file at {file}")
        size = file.stat().st_size
        if size > MAX_ARTIFACT_BYTES:
            raise ValueError(
                f"{file.name} is {size / (1024 * 1024):.0f} MB; the limit is "
                f"{MAX_ARTIFACT_BYTES // (1024 * 1024)} MB. Upload a shorter or lower-resolution "
                "preview (720p, or a clip of the part in question) instead.")
        shown = (name or file.name).strip() or file.name
        mime = mimetypes.guess_type(shown)[0] or mimetypes.guess_type(file.name)[0] or "application/octet-stream"
        request = urllib.request.Request(
            f"{self.job_url}/artifacts?name={urllib.parse.quote(shown)}",
            data=file.read_bytes(), method="POST",
            headers={"Authorization": f"Bearer {self.token}", "Content-Type": mime},
        )
        with urllib.request.urlopen(request, timeout=600) as response:
            return json.loads(response.read().decode("utf-8") or "{}")["artifact"]

    def ask(self, kind: str, text: str, detail: Optional[str] = None,
            options: Optional[list] = None, attachments: Optional[list] = None,
            heartbeat: Optional[Callable[[], None]] = None) -> dict:
        """Post a question and block until it is answered; returns it.
        `attachments` are artifact keys (see upload_artifact) shown with it.
        `heartbeat` is called on every poll while it waits: Claude Code
        aborts a tool call that sends nothing for a few minutes, and a
        person can take hours to answer."""
        question = self._request("POST", self.base,
                                 {"kind": kind, "text": text, "detail": detail,
                                  "options": options or [],
                                  # Only when given: a server from before
                                  # attachments may refuse a field it doesn't know.
                                  **({"attachments": attachments} if attachments else {})})["question"]
        if question.get("answeredAt"):
            # Allowed on the spot ("Allow all", or one of the run's own
            # tools): a notification would only flash up and vanish.
            return question
        marker = self.job_dir / f"waiting-{question['id']}" if self.job_dir else None
        if marker:
            marker.write_text(text)
        heading = "Agent needs permission" if kind == "permission" else "Agent has a question"
        notifier, shown = None, False
        asked_at = time.monotonic()
        try:
            while True:
                if heartbeat:
                    heartbeat()
                if not shown and time.monotonic() - asked_at >= self.notify_after:
                    shown = True
                    notifier = show_question(question["id"], kind, heading, self.title, text,
                                             detail, options or [])
                    if notifier:
                        threading.Thread(target=self._relay,
                                         args=(notifier, question["id"], heading, text),
                                         daemon=True).start()
                try:
                    data = self._request("GET", f"{self.base}/{question['id']}", retry=False)
                except (urllib.error.URLError, OSError):
                    # A server blip while someone is thinking is not an
                    # answer; keep waiting.
                    time.sleep(self.poll_seconds)
                    continue
                if data["question"].get("answeredAt"):
                    return data["question"]
                if data.get("jobStatus") != "running":
                    raise JobCancelled(f"the run is {data.get('jobStatus')}")
                time.sleep(self.poll_seconds)
        finally:
            if marker:
                marker.unlink(missing_ok=True)
            if notifier:
                notifier.take_away()

    def _relay(self, notifier: MacQuestion, question_id: str, heading: str, text: str) -> None:
        """Send the answer given on the Mac; `ask`'s polling then sees it."""
        reply = notifier.answer()
        if reply is None:
            return  # taken away, or it died
        if reply.get("shown") is False:
            notify(heading, self.title, text)  # notifications off for it; the plain kind may still show
            return
        try:
            self._request("POST", f"{self.base}/{question_id}/answer", reply)
        except urllib.error.HTTPError as e:
            if e.code != 400:  # 400: answered in NotesGraph meanwhile
                notify("Could not send your answer", self.title,
                       f"Answer it in NotesGraph instead ({e.code}).")
        except (urllib.error.URLError, OSError):
            notify("Could not send your answer", self.title, "Answer it in NotesGraph instead.")


def steer_note(steers: list) -> str:
    """How instructions sent mid-run are put to the model (the cloud runner's
    run-tools.ts steerNote words them alike)."""
    texts = [str(s.get("text") or "").strip() for s in steers]
    texts = [t for t in texts if t]
    if not texts:
        return ""
    if len(texts) == 1:
        return (f"The user sent a new instruction while you were working: {texts[0]}\n"
                "Take it into account from here on.")
    return ("The user sent new instructions while you were working:\n"
            + "\n".join(f"- {t}" for t in texts)
            + "\nTake them into account from here on.")


def call_tool(questions: Questions, name: str, args: dict,
              heartbeat: Optional[Callable[[], None]] = None) -> dict:
    """Run one tool; returns an MCP tools/call result. `heartbeat` keeps
    the call alive while ask_user or approve waits for the person."""
    def text(value: str, error: bool = False) -> dict:
        return {"content": [{"type": "text", "text": value}], "isError": error}

    def with_steers(value: str) -> dict:
        # The PostToolUse hook (steer_hook.py) delivers these too; whichever
        # takes them first does, as the server hands each out once.
        note = steer_note(questions.take_steers())
        return text(f"{value}\n\n{note}" if note else value)

    try:
        if name == "ask_user":
            asked = str(args.get("question") or "").strip()
            if not asked:
                return text("question is required", error=True)
            options = args.get("options")
            options = [str(o).strip() for o in options if str(o).strip()] if isinstance(options, list) else []
            attachments = args.get("attachments")
            attachments = ([str(a).strip() for a in attachments if str(a).strip()][:MAX_ATTACHMENTS]
                           if isinstance(attachments, list) else [])
            answer = questions.ask("question", asked, options=options, attachments=attachments,
                                   heartbeat=heartbeat)
            return with_steers(answer.get("answer") or "")

        if name == "upload_artifact":
            path = str(args.get("path") or "").strip()
            if not path:
                return text("path is required", error=True)
            try:
                artifact = questions.upload_artifact(path, args.get("name"))
            except (FileNotFoundError, ValueError) as e:
                return text(str(e), error=True)
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    return text("This NotesGraph server does not take artifacts yet; describe "
                                "the result in words instead.", error=True)
                raise
            size = artifact.get("size")
            shown = f"{size / (1024 * 1024):.1f} MB" if isinstance(size, (int, float)) else "uploaded"
            return with_steers(
                f"Uploaded {artifact.get('name') or Path(path).name} ({shown}).\n"
                f"key: {artifact.get('key')}\nurl: {artifact.get('url')}\n"
                "Pass the key in ask_user's attachments to show it with a question; "
                "use the url in a note.")

        if name == "set_title":
            title = " ".join(str(args.get("title") or "").split())
            if not title:
                return text("title is required", error=True)
            try:
                kept = questions.set_title(title)
            except urllib.error.HTTPError as e:
                # A server from before run titles: the run keeps its block's
                # text, which is no reason to stop the task.
                return text(f"Could not rename the run ({e.code}); carry on.", error=True)
            return with_steers(f"Run renamed to: {kept}")

        if name == "approve":
            tool = str(args.get("tool_name") or "a tool")
            tool_input = args.get("input") or {}
            answer = questions.ask(
                "permission", f"Allow {tool}?",
                # Compact and uncut up to the server's cap: NotesGraph parses it
                # to show an edit as a diff, and clipped JSON does not parse.
                json.dumps(tool_input, ensure_ascii=False)[:MAX_DETAIL],
                heartbeat=heartbeat,
            )
            # The shape Claude Code expects back from a permission prompt tool.
            if answer.get("allowed"):
                decision = {"behavior": "allow", "updatedInput": tool_input}
            else:
                note = answer.get("answer") or ""
                decision = {"behavior": "deny",
                            "message": "The user denied this." + (f" They said: {note}" if note else "")}
            return text(json.dumps(decision))

        return text(f"unknown tool {name}", error=True)
    except JobCancelled as e:
        return text(f"Stop: {e}. Do not continue the task.", error=True)
    except (urllib.error.URLError, OSError, KeyError, ValueError) as e:
        return text(f"Could not reach NotesGraph to ask: {e}", error=True)


def progress_heartbeat(params: dict, send: Optional[Callable[[dict], None]],
                       every: float = HEARTBEAT_SECONDS) -> Optional[Callable[[], None]]:
    """A heartbeat that sends MCP progress for this call at most every
    `every` seconds; None when the client gave no progressToken to send it on.

    Claude Code aborts a tool call that sends no response or progress for a
    few minutes (CLAUDE_CODE_MCP_TOOL_IDLE_TIMEOUT). Progress resets that
    clock, so a question can wait as long as the person takes."""
    token = ((params.get("_meta") or {}).get("progressToken"))
    if token is None or send is None:
        return None
    state = {"count": 0, "last": None}

    def beat() -> None:
        now = time.monotonic()
        if state["last"] is not None and now - state["last"] < every:
            return
        state["last"] = now
        state["count"] += 1  # progress must go up with each notification
        try:
            send({"jsonrpc": "2.0", "method": "notifications/progress",
                  "params": {"progressToken": token, "progress": state["count"],
                             "message": "Waiting for the user to answer"}})
        except (OSError, ValueError):
            pass  # stdout gone: the client has stopped, and the wait ends with it

    return beat


def handle(questions: Questions, message: dict,
           send: Optional[Callable[[dict], None]] = None) -> Optional[dict]:
    """Answer one JSON-RPC message; None for notifications. `send` writes
    a message of our own (progress while a tool waits) to the client."""
    method, msg_id = message.get("method"), message.get("id")
    if msg_id is None:
        return None  # notifications/initialized and friends

    if method == "initialize":
        result = {
            "protocolVersion": (message.get("params") or {}).get("protocolVersion", PROTOCOL_VERSION),
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "notesgraph-run", "version": "1"},
        }
    elif method == "tools/list":
        result = {"tools": TOOLS}
    elif method == "tools/call":
        params = message.get("params") or {}
        result = call_tool(questions, params.get("name", ""), params.get("arguments") or {},
                           progress_heartbeat(params, send))
    elif method == "ping":
        result = {}
    else:
        return {"jsonrpc": "2.0", "id": msg_id,
                "error": {"code": -32601, "message": f"Method not found: {method}"}}
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def main() -> None:
    env = os.environ
    questions = Questions(env["NG_URL"], env["NG_WORKSPACE"], env["NG_JOB"],
                          env["NG_TOKEN"], Path(env["NG_JOB_DIR"]) if env.get("NG_JOB_DIR") else None)
    lock = threading.Lock()

    def send(message: dict) -> None:
        with lock:
            sys.stdout.write(json.dumps(message) + "\n")
            sys.stdout.flush()

    def reply(message: dict) -> None:
        response = handle(questions, message, send)
        if response is not None:
            send(response)

    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue
        if message.get("method") == "tools/call":
            # A question can wait hours for a person; it must not hold up
            # pings or a second call behind it.
            threading.Thread(target=reply, args=(message,), daemon=True).start()
        else:
            reply(message)


if __name__ == "__main__":
    main()
