# SSH MCP server

Persistent SSH sessions for Claude. Instead of running `ssh host cmd` in Bash
for every remote command, a session opens one connection and keeps it open.
Every command after that goes over the same connection, in the same shell.

```bash
wf ssh install     # register the `ssh` server in every Claude config dir (user scope)
wf ssh status      # the daemon and the sessions it holds
wf ssh close <id>  # close one session
wf ssh stop        # close all sessions and stop the daemon
wf ssh uninstall
```

`install` registers the server at user scope in `~/.claude.json` and in every
`~/.claude-*` directory that holds a login or is set as a project's
`claude_config_dir`. Sessions that are already running need a restart to
pick it up.

## Tools

| Tool | What it does |
| --- | --- |
| `ssh_open(host, user?, port?, identity_file?, options?, cwd?, label?)` | Connects and returns a `session_id`. |
| `ssh_exec(session_id, command, timeout=120, max_output=30000)` | Runs a command and returns `stdout`, `stderr`, `exit_code` and `cwd`. |
| `ssh_read(session_id, timeout=30)` | Returns new output from a command that outlived its `timeout`, and its exit code once it finishes. |
| `ssh_interrupt(session_id)` | Sends SIGINT to the running command; the session stays open. |
| `ssh_list()` | Lists open sessions from every Claude session on the machine. |
| `ssh_close(session_id)` | Closes the session. |

## How it works

- One daemon per user (`workflow/ssh_daemon.py`, socket `~/.wf/ssh/daemon.sock`)
  holds every session. Each Claude session runs its own small stdio MCP
  server (`workflow/ssh_mcp.py`) that forwards tool calls to the daemon and
  starts it on first use. That is why one session id works from any Claude
  session.
- A session is one `ssh -T` process running a remote `bash -l` (`sh -l` when
  the host has no bash). Commands run with `eval`, so `cd` and `export` carry
  over to the next command. Stdin is `/dev/null`, so commands that prompt for
  input fail rather than hang.
- A session runs one command at a time. To run commands in parallel, open
  more sessions. Sessions to the same host share one TCP connection through
  an OpenSSH ControlMaster socket (`~/.wf/ssh/cm-*`).
- Logins are non-interactive (`BatchMode=yes`): keys, ssh-agent, and
  `~/.ssh/config` hosts work; password prompts do not.
- Sessions idle for 8 hours are closed. Sessions only live in the daemon's
  memory, so `wf ssh stop` or a reboot closes them all.
