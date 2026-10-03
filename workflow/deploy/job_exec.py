"""Run one agent job and narrate it, so a person can watch.

    python -m workflow.deploy.job_exec <job-dir>

This is what runs inside a job's tmux session. It reads the prompt and
command the runner wrote into <job-dir>, runs the provider, and prints a
human-readable transcript -- to the terminal for whoever attached, and to
log.txt, which the runner tails up to the server so the same text shows in
NotesGraph.

Files in <job-dir>:
    cmd.json     written by the runner: {"argv": [...], "provider": "...",
                 "env": {...extra environment}, "cwd": "where to run" | null}
    log.txt      the transcript, appended as the run goes
    result.json  {"result": str, "steps": int, "error": str|null}
    exit_code    written last; its presence means the run is over
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional, TextIO

# How long a finished session stays open for someone to read. The runner
# does not wait for this; it reads exit_code.
LINGER_SECONDS = 600
TOOL_RESULT_CHARS = 400


def _short(value, limit: int = 160) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _tool_result_text(content) -> str:
    if isinstance(content, list):
        content = "\n".join(
            part.get("text", "") for part in content if isinstance(part, dict)
        )
    return str(content or "")


class Narrator:
    """Turns provider output into transcript lines.

    Claude's stream-json is rendered event by event; any other provider's
    output, and any line that isn't JSON, passes through as it is.

    A subagent's events (they carry `parent_tool_use_id`) are indented under
    a ↳ and do not count as the run's own turns. Claude can emit more than
    one `result` -- the main turn ends, background agents report back, and
    it carries on -- so the outcome is the last one, said once by `finish`.
    """

    def __init__(self, out: TextIO, log: TextIO):
        self.out, self.log = out, log
        self.result: Optional[str] = None
        self.error: Optional[str] = None
        self.steps = 0
        self.cost: Optional[float] = None
        self.started = False
        self.plain: list = []

    def say(self, text: str, sub: bool = False) -> None:
        if sub:
            text = "\n".join(f"    ↳ {line}" for line in text.split("\n"))
        for stream in (self.out, self.log):
            stream.write(text if text.endswith("\n") else text + "\n")
            stream.flush()

    def feed(self, line: str) -> None:
        stripped = line.strip()
        if not stripped:
            return
        try:
            event = json.loads(stripped)
        except json.JSONDecodeError:
            event = None
        if not isinstance(event, dict) or "type" not in event:
            self.plain.append(line.rstrip("\n"))
            self.say(line.rstrip("\n"))
            return
        self._event(event)

    def _event(self, event: dict) -> None:
        kind = event.get("type")
        sub = bool(event.get("parent_tool_use_id"))
        if kind == "system" and event.get("subtype") == "init":
            if sub:
                return
            if self.started:
                # The same run picking up again after background work.
                self.say("▶ continuing")
                return
            self.started = True
            model = event.get("model") or "?"
            self.say(f"▶ started · model {model} · {event.get('cwd', '')}")
        elif kind == "assistant":
            if not sub:
                self.steps += 1
            for part in (event.get("message") or {}).get("content") or []:
                if part.get("type") == "text" and part.get("text", "").strip():
                    self.say(part["text"].rstrip(), sub)
                elif part.get("type") == "tool_use":
                    name = part.get("name", "tool")
                    if name.endswith("__ask_user"):
                        # The question is the event worth seeing, not the call.
                        question = (part.get("input") or {}).get("question", "")
                        self.say(f"? asking you: {question}  (answer in NotesGraph)", sub)
                    else:
                        self.say(f"→ {name}  {_short(part.get('input', {}))}", sub)
        elif kind == "user":
            for part in (event.get("message") or {}).get("content") or []:
                if isinstance(part, dict) and part.get("type") == "tool_result":
                    text = _tool_result_text(part.get("content"))
                    mark = "✗" if part.get("is_error") else "←"
                    self.say(f"  {mark} {_short(text, TOOL_RESULT_CHARS)}", sub)
        elif kind == "result" and not sub:
            self.steps = int(event.get("num_turns") or self.steps)
            cost = event.get("total_cost_usd")
            self.cost = cost if isinstance(cost, (int, float)) else self.cost
            if event.get("is_error") or event.get("subtype") != "success":
                self.result = None
                self.error = str(event.get("result") or event.get("subtype") or "failed")
            else:
                self.result, self.error = str(event.get("result") or ""), None

    def finish(self) -> None:
        """Say how the run ended, once, from its last result."""
        tail = f" · ${self.cost:.4f}" if self.cost is not None else ""
        if self.error is not None:
            self.say(f"✗ failed after {self.steps} turns{tail}: {_short(self.error, 300)}")
        elif self.result is not None:
            self.say(f"✓ finished in {self.steps} turns{tail}")


def run(job_dir: Path, out: TextIO = sys.stdout) -> int:
    spec = json.loads((job_dir / "cmd.json").read_text())
    argv = spec["argv"]

    with open(job_dir / "log.txt", "a", encoding="utf-8") as log:
        narrator = Narrator(out, log)
        narrator.say(f"$ {argv[0]} … ({spec.get('provider', argv[0])}, job {job_dir.name})")
        if spec.get("provider") == "claude-code":
            narrator.say("  Claude can read and write your notes, and will ask you in "
                         "NotesGraph when it needs something.")
        code = 1
        try:
            proc = subprocess.Popen(
                argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1, env={**os.environ, **(spec.get("env") or {})},
                cwd=spec.get("cwd") or None,
            )
            for line in proc.stdout:
                narrator.feed(line)
            code = proc.wait()
            narrator.finish()
        except FileNotFoundError:
            narrator.error = f"'{argv[0]}' is not on PATH on this device"
            narrator.say(f"✗ {narrator.error}")
            code = 127

        result = narrator.result
        if result is None and code == 0 and not narrator.error:
            # A provider without structured output: its output is the answer.
            result = "\n".join(narrator.plain).strip()
        error = narrator.error
        if code != 0 and not error:
            error = ("\n".join(narrator.plain[-20:]).strip()[:500]
                     or f"{argv[0]} exited {code}")

    (job_dir / "result.json").write_text(json.dumps(
        {"result": result, "steps": max(1, narrator.steps), "error": error}))
    # Written last, and by rename, so the runner never sees a partial file.
    tmp = job_dir / "exit_code.tmp"
    tmp.write_text(str(code))
    os.replace(tmp, job_dir / "exit_code")
    return code


def main() -> None:
    job_dir = Path(sys.argv[1])
    code = run(job_dir)
    if os.environ.get("TMUX"):
        print(f"\n── job finished (exit {code}). This session closes in "
              f"{LINGER_SECONDS // 60} min; Ctrl-C to close it now.")
        try:
            time.sleep(LINGER_SECONDS)
        except KeyboardInterrupt:
            pass
    sys.exit(code)


if __name__ == "__main__":
    main()
