"""An agent profile's tools as an MCP server: mcp__automation__<name>.

    python -m workflow.deploy.automation_mcp      (stdio MCP server)

Started by Claude Code for a job whose server profile has automation tools
(see `jobs.claude_code_argv`). Each tool is a command the profile's owner
wrote (plugins/registry/agent-profiles.ts); its params reach the command as
$ARG_<NAME> environment variables, never spliced into it, so what the agent
passes can't change what runs. $AGENT_CWD is where the agent works.

Configured by NG_AUTOMATION_TOOLS, the path of the spec the runner wrote
(profile_automation.write_tools_spec). Speaks newline-delimited JSON-RPC on
stdio; stdout is the protocol, so nothing else may be printed there.
"""

import json
import os
import signal
import sys
import threading
from pathlib import Path
from typing import Optional

from workflow.deploy.profile_automation import kill_running, run_tool, tool_schema

PROTOCOL_VERSION = "2025-06-18"


class Tools:
    """The spec's tools, served and run."""

    def __init__(self, spec: dict):
        self.tools = [t for t in (spec.get("tools") or []) if isinstance(t, dict) and t.get("name")]
        self.values = spec.get("values") or {}
        self.cwd = spec.get("cwd") or None
        self.env = spec.get("env") or {}
        if self.cwd:
            self.env.setdefault("AGENT_CWD", self.cwd)

    @classmethod
    def load(cls, path: Path) -> "Tools":
        return cls(json.loads(path.read_text()))

    def listing(self) -> list:
        return [tool_schema(t) for t in self.tools]

    def call(self, name: str, args: dict) -> dict:
        spec = next((t for t in self.tools if t["name"] == name), None)
        if spec is None:
            return {"content": [{"type": "text", "text": f"unknown tool {name}"}], "isError": True}
        if not isinstance(args, dict):
            return {"content": [{"type": "text", "text": f"{name}: arguments must be an object"}],
                    "isError": True}
        text, ok = run_tool(spec, args, self.values, self.cwd, self.env)
        return {"content": [{"type": "text", "text": text}], "isError": not ok}


def handle(tools: Tools, message: dict) -> Optional[dict]:
    """Answer one JSON-RPC message; None for notifications."""
    method, msg_id = message.get("method"), message.get("id")
    if msg_id is None:
        return None
    if method == "initialize":
        result = {
            "protocolVersion": (message.get("params") or {}).get("protocolVersion", PROTOCOL_VERSION),
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "automation", "version": "1"},
        }
    elif method == "tools/list":
        result = {"tools": tools.listing()}
    elif method == "tools/call":
        params = message.get("params") or {}
        result = tools.call(str(params.get("name", "")), params.get("arguments") or {})
    elif method == "ping":
        result = {}
    else:
        return {"jsonrpc": "2.0", "id": msg_id,
                "error": {"code": -32601, "message": f"Method not found: {method}"}}
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def main() -> None:
    tools = Tools.load(Path(os.environ["NG_AUTOMATION_TOOLS"]))
    lock = threading.Lock()

    def reply(message: dict) -> None:
        try:
            response = handle(tools, message)
        except Exception as e:
            # Every request gets an answer: without one the agent waits out
            # the tool timeout, which a profile may set to a day.
            response = None if message.get("id") is None else {
                "jsonrpc": "2.0", "id": message.get("id"),
                "error": {"code": -32603, "message": f"{type(e).__name__}: {e}"}}
        if response is not None:
            with lock:
                sys.stdout.write(json.dumps(response) + "\n")
                sys.stdout.flush()

    def stop(*_):
        kill_running()
        os._exit(0)

    # Claude stopping its servers, or the job's session going away.
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGHUP, stop)
    try:
        for line in sys.stdin:
            if not line.strip():
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(message, dict):
                continue
            if message.get("method") == "tools/call":
                # A render or an install can take minutes; it must not hold up
                # pings or a second call behind it.
                threading.Thread(target=reply, args=(message,), daemon=True).start()
            else:
                reply(message)
    finally:
        # Stdin closed: Claude is gone. Its tools' commands run in sessions of
        # their own and would outlive it.
        kill_running()


if __name__ == "__main__":
    main()
