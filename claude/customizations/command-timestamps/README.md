# Command timestamps

This is an example Claude Code customization. It makes Claude Code show the time
each shell command starts and ends. That helps when following long builds and
deploys, and when you need to see how long a step took.

```
▶ 17:05:12
  … command output …
■ 17:05:31
```

## How it works

There are two hooks on the `Bash` tool:

- `PreToolUse` runs just before a command.
- `PostToolUse` runs just after it.

Each hook prints JSON with a `systemMessage`. Claude Code shows that message
inline in the conversation. It doesn't change the command or block it.

## Install

Merge the `hooks` block from [`settings.json`](settings.json) into one of
these files:

- **Every project:** your global settings, `~/.claude/settings.json`. If
  `CLAUDE_CONFIG_DIR` is set, use `$CLAUDE_CONFIG_DIR/settings.json` instead.
- **One repo:** that repo's `.claude/settings.json`.

Restart Claude Code if the stamps don't show up straight away.

## Variations

- To stamp every tool (file reads, edits, MCP calls) as well as shell
  commands, set `"matcher": ".*"`.
- To add the date, use `date '+%Y-%m-%d %H:%M:%S'`.
