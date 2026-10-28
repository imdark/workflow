---
name: side
description: Fork the current conversation into a new terminal tab and switch to it, to chase a side question without derailing this session.
disable-model-invocation: true
allowed-tools: Bash(wf side:*)
---

# Side

Run this, and nothing else:

```bash
wf side
```

It opens a new tab next to this one (tmux window, iTerm2 tab or Terminal.app
tab), runs `claude --resume $CLAUDE_CODE_SESSION_ID --fork-session` there and
selects it. The fork starts from this conversation as it stands; this session
is untouched.

Reply with one line saying the fork is open. If the command fails, show its
error. On Terminal.app an error about keystrokes or assistive access means the
terminal needs Accessibility permission (System Settings → Privacy & Security
→ Accessibility).
