"""Stop a run from fetching more pages from a site that keeps failing.

    python -m workflow.deploy.fetch_guard   (Claude Code PreToolUse hook, WebFetch)

A run looking for a price fetched one hp.com product page after another,
and each timed out: a site that is slow or refusing automated fetches fails
the same way on every page. Before each WebFetch this hook reads the run's
transcript, counts the earlier WebFetch failures per site, and once a site
has failed `MAX_FAILURES` times it refuses further fetches to it, telling the
model to use another source.

Reads the hook's JSON input on stdin and prints its JSON output on stdout;
prints nothing to let the fetch go ahead. Never fails: a guard that errors
should not stop a fetch.
"""

import json
import sys
from collections import Counter
from pathlib import Path
from typing import Iterable, Optional
from urllib.parse import urlsplit

MAX_FAILURES = 2


def site(url: str) -> str:
    """The host a URL points at, without `www.`; "" if there is none."""
    try:
        host = (urlsplit(str(url)).hostname or "").lower()
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host


def _blocks(entry: dict) -> list:
    message = entry.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    return [b for b in content if isinstance(b, dict)] if isinstance(content, list) else []


def failures_by_site(lines: Iterable[str]) -> Counter:
    """WebFetch calls that came back as errors, counted per site, from transcript lines."""
    fetched = {}
    failed = Counter()
    for line in lines:
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if not isinstance(entry, dict):
            continue
        for block in _blocks(entry):
            if block.get("type") == "tool_use" and block.get("name") == "WebFetch":
                url = (block.get("input") or {}).get("url", "")
                fetched[block.get("id")] = site(url)
            elif (block.get("type") == "tool_result" and block.get("is_error")
                  and fetched.get(block.get("tool_use_id"))):
                failed[fetched[block["tool_use_id"]]] += 1
    return failed


def decision(hook_input: dict) -> Optional[dict]:
    """The hook output refusing this fetch, or None to let it go ahead."""
    if hook_input.get("tool_name") != "WebFetch":
        return None
    host = site((hook_input.get("tool_input") or {}).get("url", ""))
    transcript = hook_input.get("transcript_path")
    if not host or not transcript:
        return None
    try:
        with open(Path(transcript), encoding="utf-8", errors="replace") as f:
            failures = failures_by_site(f)[host]
    except OSError:
        return None
    if failures < MAX_FAILURES:
        return None
    reason = (f"WebFetch to {host} already failed {failures} times in this run, so other "
              f"pages on {host} will most likely fail too. Don't fetch from {host} again. "
              "Use what WebSearch results already say, or a different site.")
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                   "permissionDecision": "deny",
                                   "permissionDecisionReason": reason}}


def main(stdin=None, stdout=None) -> int:
    stdin, stdout = stdin or sys.stdin, stdout or sys.stdout
    try:
        hook_input = json.loads(stdin.read() or "{}")
        output = decision(hook_input) if isinstance(hook_input, dict) else None
    except Exception:  # noqa: BLE001 - see the module docstring
        output = None
    if output:
        stdout.write(json.dumps(output))
        stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
