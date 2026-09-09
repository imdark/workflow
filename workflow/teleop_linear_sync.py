"""Browser-driven automation for the "Tele-op Issue -> Linear" workflow.

Slack's official Web API rejects the browser's session cookie for
conversations.history/chat.postMessage (it needs a real xoxc/xoxb/xoxp
token, which this workspace doesn't have configured) -- see
workflow/teleop_issues.py for the API-based version that works once a
token exists. Until then, this module drives a real, already-logged-in
Chrome tab over CDP (via workflow/browser_client.py) to do the same job:

1. Search "#channel NEW ISSUE" for every report the Tele-op Issue Slack
   workflow has posted, and read off which ones already carry the team's
   "converted to Linear" checkmark reaction.
2. For each one missing the checkmark, open its thread and post a reply
   that @-mentions the Linear Slack app with the team/project/labels/
   priority/status -- the Linear app converts that mention into an issue
   on its own, so no Linear API access is needed either.

Two hard-won mechanics this module depends on:
- Slack reuses the same texty_input component for the search box and the
  message composer, so every selector here is scoped to a specific
  ancestor (the search modal, or `.p-threads_footer__input`) -- a bare
  `[data-qa=texty_input]` matches more than one element and can end up
  typing into the wrong box.
- `@Linear` must be typed as real keystrokes so Slack's mention
  autocomplete resolves it to a real mention entity; the rest of the
  message (which contains newlines) is inserted via a synthetic clipboard
  paste, since typing a literal newline into the composer sends the
  message early.
"""

import re
import time
from dataclasses import dataclass
from typing import Dict, List, Optional

from .browser_client import BrowserClient

WORKFLOW_NAME = "Tele-op Issue"
NEW_ISSUE_PREFIX = "NEW ISSUE"
CHECKMARK_MARKER = "white_check_mark"

SEARCH_INPUT_SELECTOR = "[data-qa=texty_input][data-feat=qs]"
THREAD_INPUT_SELECTOR = ".p-threads_footer__input [data-qa=texty_input]"


@dataclass
class ScannedIssue:
    ts: str
    when: str
    title: str
    already_converted: bool
    replies: str


def _extract_ts_positions(html: str):
    return list(re.finditer(r'<a aria-label="([^"]+)"[^>]*data-ts="(\d+\.\d+)" class="c-link c-timestamp', html))


def parse_search_results_page(html: str) -> List[ScannedIssue]:
    """Pure parser for one page of Slack search-results HTML (as returned
    by BrowserClient.snapshot()). Only returns results actually sent by
    the Tele-op Issue workflow -- Slack search also surfaces unrelated
    replies that happen to share vocabulary with a NEW ISSUE thread."""
    results = []
    for m in _extract_ts_positions(html):
        when, ts = m.group(1), m.group(2)
        start = m.start()

        before = html[max(0, start - 2000):start]
        sender_m = re.search(r'data-stringify-text="([^"]+)">\s*<span class="p-member_profile_hover_card"', before)
        sender = sender_m.group(1) if sender_m else None
        if sender != WORKFLOW_NAME:
            continue

        seg = html[start:start + 6000]
        already_converted = CHECKMARK_MARKER in seg

        title_m = re.search(r'<i data-stringify-type="italic">Issue</i></b>:\s*([^<]{0,90})', seg)
        title = title_m.group(1).strip().replace('&nbsp;&', '').strip() if title_m else ""

        replies_m = re.search(r'data-qa="reply_bar_count"[^>]*>(\d+ repl\w+)<', seg)
        replies = replies_m.group(1) if replies_m else "0 replies"

        results.append(ScannedIssue(ts=ts, when=when, title=title, already_converted=already_converted, replies=replies))
    return results


def open_channel(client: BrowserClient, slack_team_id: str, channel_id: str) -> None:
    client.open(f"https://app.slack.com/client/{slack_team_id}/{channel_id}")
    time.sleep(2.5)
    # Slack restores whatever thread panel was last open for this user, which
    # steals layout/selector scope from the search flow below -- close it.
    client.eval(
        "() => { const btn = Array.from(document.querySelectorAll('button[aria-label=Close]'))"
        ".find(b => b.closest('.p-threads_flexpane, [data-qa=threads_flexpane]')); "
        "if (btn) btn.click(); return !!btn; }"
    )
    time.sleep(0.3)


def scan_new_issue_status(
    client: BrowserClient,
    slack_team_id: str,
    channel_id: str,
    channel_name: str,
    max_pages: int = 20,
) -> List[ScannedIssue]:
    """Search the channel for Tele-op Issue "NEW ISSUE" posts and report,
    for each, whether it already carries the converted-to-Linear checkmark.
    Paginates through all result pages (Slack shows 20 per page)."""
    open_channel(client, slack_team_id, channel_id)

    exists = client.eval('() => !!document.querySelector("[data-qa=top_nav_search]")')
    if exists.get("result") != "True":
        raise RuntimeError(f"could not find Slack's search button: {exists}")
    client.real_click_selector("[data-qa=top_nav_search]")
    time.sleep(0.8)

    count = client.eval(f'() => document.querySelectorAll("{SEARCH_INPUT_SELECTOR}").length')
    if count.get("result") != "1":
        raise RuntimeError(f"search input not uniquely found (got {count}); refusing to type to avoid a misfire")

    # The search box can come up pre-filled with a recent search; clear it
    # first so a re-run doesn't end up typing "query query".
    client.eval(
        f'() => {{ const el = document.querySelector("{SEARCH_INPUT_SELECTOR}"); el.focus(); '
        "document.execCommand('selectAll', false, null); document.execCommand('delete', false, null); }"
    )

    query = f'in:#{channel_name} "{NEW_ISSUE_PREFIX}"'
    fill_result = client.fill(SEARCH_INPUT_SELECTOR, query)
    if fill_result.get("status") != "ok":
        raise RuntimeError(f"failed to type search query: {fill_result}")
    time.sleep(1.0)
    client.press("Enter")
    time.sleep(1.5)

    on_full_results = client.eval("() => !!document.querySelector('.p_search_filter__block_container, [data-qa=search_filters]')")
    if on_full_results.get("result") != "True":
        expand = client.eval(
            "() => { const btn = Array.from(document.querySelectorAll('button'))"
            ".find(b => b.textContent.trim() === 'View full search'); "
            "if (!btn) return 'no_expand_button'; btn.click(); return 'expanded'; }"
        )
        if expand.get("result") != "expanded":
            raise RuntimeError(f"could not reach full search results: {expand}")
        time.sleep(1.2)

    all_results: Dict[str, ScannedIssue] = {}
    for page_num in range(1, max_pages + 1):
        html = client.snapshot()
        for item in parse_search_results_page(html):
            all_results[item.ts] = item

        clicked_next = client.eval(
            "() => { const buttons = Array.from(document.querySelectorAll('button')); "
            f"const btn = buttons.find(b => b.textContent.trim() === '{page_num + 1}'); "
            "if (!btn) return 'no_more_pages'; btn.click(); return 'clicked'; }"
        )
        if clicked_next.get("result") != "clicked":
            break
        time.sleep(1.0)

    return sorted(all_results.values(), key=lambda r: float(r.ts))


class AlreadyConverted(Exception):
    """Raised when a thread already has a Linear reply -- the checkmark
    reaction is only a best-effort signal (a team convention, not something
    the Linear app sets automatically), so this is checked for real at
    posting time to make duplicate-issue creation impossible regardless of
    whether the checkmark was applied."""


def thread_has_linear_reply(client: BrowserClient) -> bool:
    """Check the currently-open thread panel's replies for one already
    from the Linear app, or containing a linear.app issue link."""
    result = client.eval(
        "() => { const panel = document.querySelector('.p-threads_flexpane, [data-qa=threads_flexpane]'); "
        "if (!panel) return 'no_panel'; "
        "const senders = Array.from(panel.querySelectorAll('[data-qa=message_sender_name]')).map(e => e.textContent); "
        "const text = panel.textContent || ''; "
        "const hasLinearSender = senders.some(s => /linear/i.test(s)); "
        "const hasLinearLink = /linear\\.app\\//i.test(text); "
        "return (hasLinearSender || hasLinearLink) ? 'yes' : 'no'; }"
    )
    return result.get("result") == "yes"


def build_linear_mention_body(*, team: str, project: str, title: str, description_lines: List[str],
                               labels: List[str], priority: str, status: str) -> str:
    """The part of the message after the @Linear mention itself."""
    lines = [
        f" create an issue in the {team} team and add it to the {project} project.",
        "",
        f"Title: {title}",
        "Description:",
    ]
    lines.extend(f"- {line}" for line in description_lines)
    lines.append(f"Labels: {', '.join(labels)}")
    lines.append(f"Priority: {priority}")
    lines.append(f"Status: {status}")
    return "\n".join(lines)


def convert_new_issue(
    client: BrowserClient,
    slack_team_id: str,
    channel_id: str,
    ts: str,
    title: str,
    *,
    team: str,
    project: str,
    labels: List[str],
    priority: str,
    status: str,
    description_lines: Optional[List[str]] = None,
) -> None:
    """Open the message's thread and post the @Linear conversion reply.
    Raises RuntimeError on any step that doesn't look right, rather than
    guessing -- callers should treat that as "did not send" and stop."""
    if description_lines is None:
        description_lines = [
            "Reported via the Tele-op Issue workflow in #teleop",
            "Full details in the thread above (situation, station, blocking status, operator)",
        ]

    url = f"https://app.slack.com/client/{slack_team_id}/{channel_id}/thread/{channel_id}-{ts}"
    r = client.open(url)
    if r.get("status") != "ok":
        raise RuntimeError(f"failed to open thread: {r}")
    time.sleep(2.2)

    # The /thread/{channel}-{ts} deep link doesn't always actually open the
    # panel (observed flaky in practice) -- fall back to clicking the
    # message's own reply-count/reply-in-thread control if it didn't.
    def _panel_open() -> bool:
        return client.eval("() => !!document.querySelector('.p-threads_flexpane, [data-qa=threads_flexpane]')").get("result") == "True"

    if not _panel_open():
        ts_selector = f'[data-item-key="{ts}"]'
        clicked = client.eval(
            f'() => {{ const item = document.querySelector({ts_selector!r}); if (!item) return "item_not_found"; '
            "const replyBtn = item.querySelector('[data-qa=reply_bar_count]'); "
            "if (replyBtn) { replyBtn.click(); return 'clicked_reply_count'; } "
            "const threadBtn = item.querySelector('[data-qa=start_thread], [aria-label=\"Reply in thread\"]'); "
            "if (threadBtn) { threadBtn.click(); return 'clicked_start_thread'; } "
            "return 'no_thread_control'; }"
        )
        for _ in range(8):
            time.sleep(0.5)
            if _panel_open():
                break
        else:
            raise RuntimeError(f"could not open the thread panel for {ts} (fallback: {clicked})")

    # Ground-truth safety check: the checkmark reaction is only a team
    # convention (not set automatically by the Linear app), so re-verify
    # directly against this thread's actual replies before posting --
    # this is what actually prevents duplicate Linear issues.
    if thread_has_linear_reply(client):
        raise AlreadyConverted(f"thread {ts} already has a Linear reply")

    count = client.eval(f'() => document.querySelectorAll("{THREAD_INPUT_SELECTOR}").length')
    if count.get("result") != "1":
        raise RuntimeError(f"thread reply input not uniquely found: {count}")

    r = client.fill(THREAD_INPUT_SELECTOR, "@Linear")
    if r.get("status") != "ok":
        raise RuntimeError(f"failed to type @Linear: {r}")
    time.sleep(0.9)

    r = client.press("Enter")
    if r.get("status") != "ok":
        raise RuntimeError(f"failed to select mention autocomplete: {r}")
    time.sleep(0.6)

    mention_check = client.eval(
        f'() => {{ const el = document.querySelector("{THREAD_INPUT_SELECTOR}"); '
        'return el ? (el.querySelector("a,span[data-stringify-type=mention]") ? "resolved" : "unresolved:" + el.innerText) : "no_el"; }'
    )
    if "resolved" not in (mention_check.get("result") or ""):
        raise RuntimeError(f"@Linear did not resolve to a real mention: {mention_check}")

    body = build_linear_mention_body(
        team=team, project=project, title=title, description_lines=description_lines,
        labels=labels, priority=priority, status=status,
    )
    r = client.paste_text(THREAD_INPUT_SELECTOR, body)
    if r.get("status") != "ok" or not (r.get("result") or "").startswith("pasted"):
        raise RuntimeError(f"failed to paste message body: {r}")
    time.sleep(0.5)

    send = client.eval(
        '() => { const panel = document.querySelector(".p-threads_flexpane, [data-qa=threads_flexpane]"); '
        "if (!panel) return 'no_panel'; const btn = panel.querySelector('[data-qa=texty_send_button]'); "
        "if (!btn || btn.disabled) return 'no_button_or_disabled'; btn.click(); return 'clicked'; }"
    )
    if send.get("result") != "clicked":
        raise RuntimeError(f"failed to click send: {send}")
    time.sleep(1.3)

    verify = client.eval(f'() => {{ const el = document.querySelector("{THREAD_INPUT_SELECTOR}"); return el ? el.innerText.trim().length : -1; }}')
    if str(verify.get("result")) not in ("0",):
        raise RuntimeError(f"reply box did not clear after send -- send may have failed: {verify}")
