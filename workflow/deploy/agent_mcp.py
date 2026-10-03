"""The two tools that let a Claude agent run pause for a person.

    python -m workflow.deploy.agent_mcp      (stdio MCP server)

Started by Claude Code for a "claude-code" job (see `jobs.claude_code_argv`).
Both tools post to the job's questions on the NotesGraph server and block
until the person who started the run answers in NotesGraph:

    ask_user   a free-text question -- "Who is Cosmo, and how old?"
    approve    Claude Code's --permission-prompt-tool: allow or deny a tool

While a tool is blocked it keeps a `waiting-<question id>` file in the job
directory, so the runner can stop counting that time against the job's
time limit.

Configured by environment, set by the runner: NG_URL, NG_WORKSPACE, NG_JOB,
NG_TOKEN and NG_JOB_DIR. Speaks newline-delimited JSON-RPC on stdio; stdout
is the protocol, so nothing else may be printed there.
"""

import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional

PROTOCOL_VERSION = "2025-06-18"
POLL_SECONDS = 2.0
# Matches the server's cap on a question's detail (inventory jobs.ts MAX_DETAIL).
MAX_DETAIL = 200_000

TOOLS = [
    {
        "name": "ask_user",
        "description": (
            "Ask the person who started this run a question and wait for their "
            "answer. Use it whenever you need a fact you could not find in "
            "NotesGraph or a decision only they can make. Ask one short, "
            "specific question; group closely related unknowns into it."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "question": {"type": "string", "description": "The question, as you would ask it in person."},
            },
            "required": ["question"],
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


class Questions:
    """Ask a job's questions over the inventory API and wait for answers."""

    def __init__(self, url: str, workspace: str, job: str, token: str,
                 job_dir: Optional[Path] = None, poll_seconds: float = POLL_SECONDS):
        self.base = f"{url.rstrip('/')}/api/inventory/workspaces/{workspace}/jobs/{job}/questions"
        self.token, self.job_dir, self.poll_seconds = token, job_dir, poll_seconds

    def _request(self, method: str, url: str, payload: Optional[dict] = None) -> dict:
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = urllib.request.Request(
            url, data=body, method=method,
            headers={"Authorization": f"Bearer {self.token}",
                     "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8") or "{}")

    def ask(self, kind: str, text: str, detail: Optional[str] = None) -> dict:
        """Post a question and block until it is answered; returns it."""
        question = self._request("POST", self.base,
                                 {"kind": kind, "text": text, "detail": detail})["question"]
        marker = self.job_dir / f"waiting-{question['id']}" if self.job_dir else None
        if marker:
            marker.write_text(text)
        try:
            while True:
                try:
                    data = self._request("GET", f"{self.base}/{question['id']}")
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


def call_tool(questions: Questions, name: str, args: dict) -> dict:
    """Run one tool; returns an MCP tools/call result."""
    def text(value: str, error: bool = False) -> dict:
        return {"content": [{"type": "text", "text": value}], "isError": error}

    try:
        if name == "ask_user":
            asked = str(args.get("question") or "").strip()
            if not asked:
                return text("question is required", error=True)
            answer = questions.ask("question", asked)
            return text(answer.get("answer") or "")

        if name == "approve":
            tool = str(args.get("tool_name") or "a tool")
            tool_input = args.get("input") or {}
            answer = questions.ask(
                "permission", f"Allow {tool}?",
                # Compact and uncut up to the server's cap: NotesGraph parses it
                # to show an edit as a diff, and clipped JSON does not parse.
                json.dumps(tool_input, ensure_ascii=False)[:MAX_DETAIL],
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


def handle(questions: Questions, message: dict) -> Optional[dict]:
    """Answer one JSON-RPC message; None for notifications."""
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
        result = call_tool(questions, params.get("name", ""), params.get("arguments") or {})
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

    def reply(message: dict) -> None:
        response = handle(questions, message)
        if response is not None:
            with lock:
                sys.stdout.write(json.dumps(response) + "\n")
                sys.stdout.flush()

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
