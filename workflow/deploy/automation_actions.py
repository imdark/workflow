"""Run a NotesGraph automation written in wf's actions YAML.

An 'actions' automation's code is YAML, the same action language monitors
use (workflow/actions):

    actions:
      - id: merge
        type: custom_command
        command: gh pr merge {input.number} -R {input.repo} --merge
      - id: commit
        type: custom_command
        command: gh pr view {input.number} -R {input.repo} --json mergeCommit --jq .mergeCommit.oid
      - id: ship
        type: cloud_deploy

The actions run in order. `{input.x}` is filled from what started the run,
`{steps.<id>.<field>}` from an earlier action's result (`stdout` for a
command); in a `command`, filled-in values are shell-quoted. An action
that fails stops the run unless it has `optional: true`. The run's output
is each action's result by id.

Run by automations.py as its own process, speaking automation_runner.js's
protocol: the job as JSON on stdin; one JSON object a line on stdout,
{"log"} as it goes, then {"done"} or {"error"}. Anything else an action
prints on stdout or stderr is taken as a log line.
"""

import json
import os
import re
import shlex
import subprocess
import sys
from typing import Any, Callable

import yaml

# Most of a command's output kept in its result.
OUTPUT_CHARS = 20_000
PLACEHOLDER = re.compile(r"\{((?:input|steps)(?:\.[\w-]+)+)\}")


class ActionsFailed(RuntimeError):
    pass


def emit(message: dict) -> None:
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()


def log(line: str) -> None:
    emit({"log": line})


def parse(code: str) -> list:
    """The actions in an automation's YAML; raises ActionsFailed if malformed."""
    try:
        spec = yaml.safe_load(code)
    except yaml.YAMLError as e:
        raise ActionsFailed(f"the YAML doesn't parse: {e}")
    actions = spec.get("actions") if isinstance(spec, dict) else spec
    if not isinstance(actions, list) or not actions:
        raise ActionsFailed("the YAML needs a list of `actions`")
    for i, action in enumerate(actions):
        if not isinstance(action, dict) or not action.get("type"):
            raise ActionsFailed(f"action {i + 1} needs a `type`")
        action.setdefault("id", f"step{i + 1}")
    return actions


def lookup(path: str, scope: dict) -> Any:
    value: Any = scope
    for key in path.split("."):
        if not isinstance(value, dict) or key not in value:
            raise ActionsFailed(f"{{{path}}} names nothing")
        value = value[key]
    return value


def render(value: Any, scope: dict, quote: bool = False) -> Any:
    """Fill placeholders in every string of `value`."""
    if isinstance(value, str):
        def fill(match: "re.Match") -> str:
            found = lookup(match.group(1), scope)
            text = found if isinstance(found, str) else json.dumps(found)
            return shlex.quote(text) if quote else text
        return PLACEHOLDER.sub(fill, value)
    if isinstance(value, list):
        return [render(v, scope, quote) for v in value]
    if isinstance(value, dict):
        return {k: render(v, scope, quote) for k, v in value.items()}
    return value


def run_command(action: dict) -> dict:
    """A custom_command, its output streamed to the log as it comes."""
    env = {**os.environ, **{k: str(v) for k, v in (action.get("environment") or {}).items()}}
    process = subprocess.Popen(
        action["command"], shell=True, cwd=action.get("working_dir") or None,
        env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    output = ""
    for line in process.stdout:
        log(line.rstrip("\n"))
        output = (output + line)[-OUTPUT_CHARS:]
    code = process.wait()
    result = {"success": code == 0, "return_code": code, "stdout": output.strip()}
    if code != 0:
        tail = output.strip()[-500:]
        result["error"] = f"exited {code}" + (f": {tail}" if tail else "")
    return result


def run_registered(action: dict, scope: dict) -> dict:
    from workflow.actions import create_action_from_config

    created = create_action_from_config(action)
    if not created:
        raise ActionsFailed(f"unknown or misconfigured action type '{action['type']}'")
    return created.execute({**scope.get("input", {}), **action})


def run(job: dict, execute: Callable[[dict, dict], dict] = None) -> dict:
    actions = parse(job.get("code") or "")
    scope = {"input": job.get("input") or {}, "steps": {}}
    for action in actions:
        name = action["id"]
        if action["type"] == "custom_command" and "command" in action:
            action = {**render({k: v for k, v in action.items() if k != "command"}, scope),
                      "command": render(action["command"], scope, quote=True)}
        else:
            action = render(action, scope)
        log(f"▶ {name} ({action['type']})")
        if execute:
            result = execute(action, scope)
        elif action["type"] == "custom_command":
            result = run_command(action)
        else:
            result = run_registered(action, scope)
        result = result if isinstance(result, dict) else {"success": bool(result)}
        scope["steps"][name] = result
        if result.get("success") is False:
            why = result.get("error") or "failed"
            if action.get("optional"):
                log(f"✗ {name}: {why} (optional, carrying on)")
                continue
            raise ActionsFailed(f"{name}: {why}")
        log(f"✓ {name}")
    return scope["steps"]


def main() -> None:
    # What actions print to stderr reads as log lines too.
    os.dup2(sys.stdout.fileno(), sys.stderr.fileno())
    try:
        job = json.loads(sys.stdin.read())
        steps = run(job)
        emit({"done": json.dumps(steps, default=str)})
    except Exception as e:
        emit({"error": str(e), "stack": ""})
        sys.exit(1)


if __name__ == "__main__":
    main()
