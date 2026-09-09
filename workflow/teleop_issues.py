"""Find "NEW ISSUE" reports posted by the Tele-op Issue Slack workflow and
hand them to Linear by @-mentioning the Linear Slack app in a thread reply
(no Linear API access required — the Linear Slack app converts the mention
into an issue itself)."""

from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .slackdump import SlackDumper

WORKFLOW_NAME = "Tele-op Issue"
NEW_ISSUE_PREFIX = "NEW ISSUE"
DEFAULT_LINEAR_MENTION = "@Linear"


@dataclass
class TeleopIssueMessage:
    channel_id: str
    ts: str
    text: str
    permalink: Optional[str] = None
    raw: Dict = field(default_factory=dict)
    already_converted: Optional[bool] = None


def is_teleop_new_issue(msg: dict) -> bool:
    """True if a raw Slack message was posted by the Tele-op Issue workflow
    and its text starts with NEW ISSUE."""
    username = (msg.get("username") or "").strip()
    bot_profile_name = ((msg.get("bot_profile") or {}).get("name") or "").strip()
    if WORKFLOW_NAME not in (username, bot_profile_name):
        return False

    text = (msg.get("text") or "").strip()
    return text.upper().startswith(NEW_ISSUE_PREFIX)


def thread_has_linear_conversion(dumper: SlackDumper, channel_id: str, ts: str) -> bool:
    """Best-effort check of a message's thread replies for evidence the Linear
    Slack app already converted it (a reply from the Linear app, or a reply
    containing a linear.app issue link)."""
    try:
        replies = dumper.get_thread_replies(channel_id, ts)
        for reply in replies:
            if reply.get("ts") == ts:
                continue
            username = (reply.get("username") or "").strip().lower()
            bot_profile_name = ((reply.get("bot_profile") or {}).get("name") or "").strip().lower()
            text = (reply.get("text") or "").lower()
            if "linear" in username or "linear" in bot_profile_name:
                return True
            if "linear.app/" in text:
                return True
    except Exception:
        return False
    return False


def find_new_issue_messages(
    dumper: SlackDumper,
    channel_id: str,
    oldest: Optional[str] = None,
    latest: Optional[str] = None,
    with_permalinks: bool = True,
    check_converted: bool = True,
) -> List[TeleopIssueMessage]:
    """Scan a channel's history for Tele-op Issue workflow "NEW ISSUE" posts.

    Uses get_channel_messages directly (not dump_channel) because dump_channel
    skips bot_message-subtype messages, which is exactly what workflow posts are.
    """
    messages = dumper.get_channel_messages(channel_id, oldest=oldest, latest=latest)

    found = []
    for msg in messages:
        if not is_teleop_new_issue(msg):
            continue
        ts = msg.get("ts")
        permalink = dumper.get_permalink(channel_id, ts) if with_permalinks else None
        already_converted = thread_has_linear_conversion(dumper, channel_id, ts) if check_converted else None
        found.append(TeleopIssueMessage(
            channel_id=channel_id, ts=ts, text=msg.get("text", ""),
            permalink=permalink, raw=msg, already_converted=already_converted,
        ))
    return found


def parse_new_issue(text: str) -> Dict:
    """Best-effort split of a NEW ISSUE workflow message into a title (first
    non-header line) and the remaining lines as description bullets."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if lines and lines[0].upper().startswith(NEW_ISSUE_PREFIX):
        lines = lines[1:]
    title = lines[0] if lines else text.strip()
    description_lines = lines[1:] if len(lines) > 1 else []
    return {"title": title, "description_lines": description_lines}


def build_linear_mention_comment(
    *,
    team: str,
    project: str,
    title: str,
    description_lines: List[str],
    labels: List[str],
    priority: str,
    status: str,
    linear_mention: str = DEFAULT_LINEAR_MENTION,
) -> str:
    """Build the Slack reply text that @-mentions the Linear app so it
    converts this thread into a Linear issue with the given fields."""
    lines = [
        f"{linear_mention} create an issue in the {team} team and add it to the {project} project.",
        "",
        f"Title: {title}",
        "Description:",
    ]
    lines.extend(f"- {line}" for line in description_lines)
    lines.append(f"Labels: {', '.join(labels)}")
    lines.append(f"Priority: {priority}")
    lines.append(f"Status: {status}")
    return "\n".join(lines)


def resolve_channel_id(dumper: SlackDumper, channel: str) -> str:
    """Resolve a channel name (with or without '#') to its Slack channel id."""
    channel = channel.lstrip("#")
    if channel.startswith(("C", "G")) and " " not in channel:
        return channel
    channels = dumper.get_channels()
    matching = [c for c in channels if c.get("name") == channel]
    if not matching:
        raise ValueError(f"Channel '{channel}' not found")
    return matching[0]["id"]
