"""Hand a running agent what the person sent it from NotesGraph.

    python -m workflow.deploy.steer_hook <job_dir>/mcp.json   (Claude Code hook)

`claude --print` reads no input once started, so a run is steered through
its hooks instead (see `jobs.claude_code_argv`): after every tool call
(PostToolUse) the instructions sent since the last one go to the model as
context alongside the tool's result, and one sent as the agent wraps up
(Stop) sends it back to work. The server hands each instruction out once and
writes it into the run's transcript as it does.

Connection details come from the run's own MCP config, the `run` server's
environment, so the token is in one owner-only file rather than on a
command line. Reads the hook's JSON input on stdin and prints its JSON
output on stdout; prints nothing when there is nothing to say. Never fails:
a hook that errors would put noise in front of the model for no gain.
"""

import json
import sys
from pathlib import Path
from typing import Optional

from workflow.deploy.agent_mcp import Questions, steer_note


def questions_for(mcp_config: Path) -> Optional[Questions]:
    """The run's connection to NotesGraph, from its MCP config; None if unreadable."""
    try:
        env = json.loads(mcp_config.read_text())["mcpServers"]["run"]["env"]
        return Questions(env["NG_URL"], env["NG_WORKSPACE"], env["NG_JOB"], env["NG_TOKEN"])
    except (OSError, ValueError, KeyError, TypeError):
        return None


def hook_output(event: str, note: str) -> Optional[dict]:
    """What to print for `event` to put `note` to the model; None for nothing."""
    if not note:
        return None
    if event == "Stop":
        return {"decision": "block", "reason": note}
    return {"hookSpecificOutput": {"hookEventName": event, "additionalContext": note}}


def main(argv: Optional[list] = None, stdin=None, stdout=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    stdin, stdout = stdin or sys.stdin, stdout or sys.stdout
    try:
        event = str(json.loads(stdin.read() or "{}").get("hook_event_name") or "PostToolUse")
    except (ValueError, AttributeError):
        event = "PostToolUse"
    questions = questions_for(Path(argv[0])) if argv else None
    if questions is None:
        return 0
    output = hook_output(event, steer_note(questions.take_steers()))
    if output:
        stdout.write(json.dumps(output))
        stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
