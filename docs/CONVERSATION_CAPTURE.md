# Conversation capture

`wf proxy` is a local reverse proxy for agent model traffic. Point an agent's
base URL at it and every conversation is recorded — the real structured
messages, not a terminal transcript — then summarized into durable memory
scoped to the workflow task that was active.

## Why a proxy

The previous capture path wrapped `claude` in `script -q`, which records ANSI
escape sequences and TUI redraws rather than the conversation. A proxy sits on
the API boundary instead, so it sees exactly what the model saw: system prompt,
every user turn, every tool call and result, and the assistant's reply.

It also captures agents launched outside `wf` — anything you point at the
endpoint — and works the same for all three wire protocols.

## Quick start

```bash
wf proxy start                       # starts the daemon on 127.0.0.1:8099
eval "$(wf proxy env)"               # export the base URLs into this shell
claude                               # ...or just run `wf ai`, which does both

wf mem sessions                      # what was captured
wf mem show <session-id>             # read a conversation back
wf mem search "retry logic"          # full-text across everything
```

`wf ai` calls `apply_proxy_env()`, which starts the daemon if needed and sets
the base URLs for the child process. An endpoint you have already exported is
never overridden.

## What it captures

| Route | Protocol | Client |
|---|---|---|
| `POST /v1/messages` | `anthropic` | Claude Code |
| `POST /v1/chat/completions` | `openai-chat` | opencode, flow, most SDKs |
| `POST /v1/responses` | `openai-responses` | Codex CLI |

An agent-name prefix is accepted and stripped, so `/claude-code/v1/messages`
works and tags the session with that agent. Every other path — `count_tokens`,
`embeddings`, `models` — is proxied through untouched.

All three protocols normalize to one turn shape: `system`, `user`,
`assistant`, `tool_call`, `tool_result`. Content is stored verbatim. Nothing
is dropped on the way in; harness scaffolding is stripped only from the copy
fed to the summarizer.

Credentials are forwarded and never stored. `Authorization`, `x-api-key` and
cookies are excluded from every record and log line.

### De-duplication

Agents resend the entire history on every turn. The proxy fingerprints each
turn and appends only the tail. Two details make that work:

- Tool payloads are canonicalized before hashing. The same call arrives once
  assembled from streamed `input_json_delta` fragments (`{"file":"a.py"}`) and
  once echoed back after a JSON round-trip (`{"file": "a.py"}`).
- Request parsing emits assistant text *before* tool calls, matching the order
  the stream assembler produces.

Get either wrong and the whole tail re-appends on every turn. Both are covered
by tests in `tests/test_proxy_capture.py`.

When the prefix genuinely diverges — the client compacted its own history —
everything after the divergence point is treated as new. The raw log stays
append-only rather than being rewritten.

## Storage

Configurable, so conversations can land wherever you want them.

```yaml
proxy:
  capture:
    store: sqlite            # primary: reads and writes
    mirrors: [markdown]      # additional write-only destinations
    sqlite:
      path: ~/.wf/conversations.db
    markdown:
      dir: ~/code/notes-graph/vault/sessions
    jsonl:
      dir: ~/.wf/memory/conversations
    http:
      url: http://127.0.0.1:3000/api/ingest
      headers:
        Authorization: Bearer xxx
```

| Backend | Shape | Use for |
|---|---|---|
| `sqlite` | one file, FTS5 index | the default; fast `wf mem search` |
| `jsonl` | one file per day, one message per line | greppable, diffable raw log |
| `markdown` | one document per session, YAML frontmatter | note vaults that ingest a directory |
| `http` | `POST` per session/turn/summary | any service with an ingest endpoint |

Reads always go to the primary, so it should be a local backend. Mirrors are
best-effort: a failing mirror logs and never costs you the primary write.

Adding a backend means implementing `ConversationStore`
(`workflow/conversation_store/base.py`) and one branch in `build_store()`.

## The summarization ladder

Three levels, each built from the one below. Run when a session goes idle
(default 300s), or on demand with `wf mem summarize`.

| Level | What it is | Command |
|---|---|---|
| `session` | prose summary of one conversation | `wf mem summarize` |
| `facts` | structured durable statements, deduplicated against the task's existing facts | `wf mem facts` |
| `rollup` | per-task briefing rebuilt from recent summaries + facts | `wf mem rollup --rebuild` |

```yaml
proxy:
  ladder:
    enabled: true
    facts: true
    rollup: true
    rollup_every: 5      # rebuild the rollup every N sessions
    min_turns: 4         # skip sessions shorter than this
```

Facts are typed `decision` / `preference` / `constraint` / `fact`, carry a
confidence, and are checked against what the task already knows before being
written — the extraction prompt is shown the existing set and told not to
repeat it.

Session summaries are also appended to `~/.wf/memory/<TASK>/summary.md`, so
everything already reading that directory keeps working.

Summaries run through the configured `ai.provider` in non-interactive mode. If
the provider is missing the ladder degrades to a no-op: the raw transcript is
the durable artifact and summaries are a convenience on top.

## Configuration reference

```yaml
proxy:
  enabled: true            # false disables capture entirely
  auto_start: true         # let `wf ai` start the daemon on demand
  host: 127.0.0.1
  port: 8099
  idle_seconds: 300        # quiet period before a session is summarized
  verbose: false           # log each capture to ~/.wf/logs/proxy.log
  upstream:
    anthropic: https://api.anthropic.com
    openai: https://api.openai.com
```

## Operational notes

- The daemon detaches and survives the launching shell. PID file:
  `~/.wf/state/proxy.pid`. Log: `~/.wf/logs/proxy.log`.
- `wf proxy stop` sends `SIGTERM` and waits, so in-flight sessions are
  summarized before exit.
- There is no end-of-session signal in any of these protocols. Idleness is the
  proxy's stand-in for "the user walked away".
- Streaming is passed through byte-for-byte; the assembler only sees a copy.
- Capture failures never fail a request. A broken store degrades to plain
  proxying.

---

# Per-project Claude accounts

`wf auth` gives each project its own Claude login, so `work` authenticates as
one account and `personal` as another, with no `/logout` in between.

## Why config directories

Claude Pro/Max logins are OAuth credentials created by `/login`, not API keys,
so `ANTHROPIC_API_KEY` and `apiKeyHelper` don't switch them. What does is the
config directory. From the Claude Code docs:

> If you've set the `CLAUDE_CONFIG_DIR` environment variable, Claude Code keeps
> the `.credentials.json` file under that directory instead, including the file
> the macOS fallback writes, **and keys the macOS Keychain entry to that
> directory too, so a session with a different `CLAUDE_CONFIG_DIR` reads a
> different entry.**

Both accounts stay logged in simultaneously. Switching is free.

## Setup

```bash
wf auth login work        # opens Claude Code against ~/.claude-work; run /login
wf auth login personal    # same for ~/.claude-personal
wf auth status            # which account each project uses
```

`wf auth login` drops `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN` and
`CLAUDE_CODE_OAUTH_TOKEN` for that launch. All three outrank a `/login`
credential in Claude Code's authentication precedence, so leaving one set would
authenticate the session as the key's account and the login would look like it
hadn't taken.

To use a directory you already have:

```bash
wf auth set work ~/.claude-pantheon
wf auth set personal --clear      # back to the default ~/.claude
```

## When it applies

| Path | How |
|---|---|
| `wf ai` | `claude_accounts.apply()` sets `CLAUDE_CONFIG_DIR` before launching |
| `wf cd <repo>` | the export is added to the shell it opens, so a bare `claude` there uses the right account |
| Any shell | `eval "$(wf auth env)"` |

A `CLAUDE_CONFIG_DIR` you exported yourself is never overridden.

Config:

```yaml
projects:
  work:
    claude_config_dir: ~/.claude-work
  personal:
    claude_config_dir: ~/.claude-personal
```

A project with no mapping uses Claude Code's default `~/.claude`.

## What stops being shared

A config directory holds the login, `settings.json`, session history, and
plugins. Two accounts therefore mean two sets of user-level settings and two
histories.

A repo's own `.claude/settings.json` is read from the repo, not the config
directory, so project-scoped settings, hooks, and permissions still apply to
both. Put anything you want shared there, or symlink individual files between
the two directories.

## Verifying

Run `/status` inside Claude Code — it shows the organization and email of the
active login. That is the only reliable confirmation; `wf auth status` can only
report whether a directory has been set up, because on macOS the credential
itself lives in the Keychain rather than in a file it can read.

---

# Deployment registry and toolkit

`wf deploy` registers machines and folders as deployment and agent-execution
targets, then acts on them: run a recipe, probe health, push an update, open a
route home.

## The registry is a concept

`DeploymentRegistry` (`workflow/deploy/registry/base.py`) is where the fleet is
written down. Same shape as `ConversationStore` — one interface, several
implementations, a config-driven factory.

```yaml
deploy:
  registry: local          # authoritative: reads come from here
  mirrors: [notesgraph]    # published to as well
  notesgraph:
    url: https://app.notesgraph.com
    workspace: <workspace id>
```

| Implementation | Role |
|---|---|
| `local` | `~/.wf/config.yaml`. The source of truth, always available. |
| `notesgraph` | Publishes into a NotesGraph device inventory on your account. |

The factory **refuses** to let `notesgraph` be primary: it is where the fleet is
published, not where it is decided. A mirror that fails logs a warning and never
costs you the primary write, so an inventory outage cannot block a deploy.

Two record kinds, because a target is not always a whole box:

- `machine` — a host reachable over SSH
- `folder` — a checkout registered in its own right, namespaced
  `gem5:robots_realtime`, so an agent can be pointed at one directory without
  being handed the host

## Registering

```bash
wf deploy add pantheon@gem5.local --name gem5 --recipe pantheon \
    --repo ~/code/robots_realtime --agent -l site=hq
wf deploy setup gem5                 # install this machine's SSH key
wf deploy add-folder gem5 /home/pantheon/code/robots_realtime
wf deploy list --agents
```

## Recipes are per target

A recipe is four ordered lists: `steps` (the deploy), `health` (probes that
change nothing), `version` (one step whose output is the deployed revision), and
`restart`.

`restart` is separate and empty in both presets. **A deploy never bounces a
target as a side effect** — bouncing a rig mid-session is an explicit act, and
`wf deploy restart` confirms before it does anything.

```yaml
deploy:
  recipes:
    pantheon:                     # merged over the preset, list by list
      restart:
        - name: relaunch
          run: cd {repo} && ./clean_relaunch.sh
    webapp:                       # or define one from scratch
      steps:
        - git pull
        - {name: build, run: npm ci && npm run build, timeout: 900}
```

Placeholders: `{repo} {path} {ref} {branch} {pin} {channel} {host} {user} {port} {id}`.
Unknown braces are left alone, so shell variables survive. `{ref}` is the pin
when one is set, else the branch.

Built-in `pantheon` follows `robots_realtime`'s own documented install: fetch,
checkout, `submodule update`, `install.sh`, with `rr-debug status` as an
**optional** health probe — a sim box with no hardware attached is healthy
without a rig.

```bash
wf deploy recipes                 # show them all
wf deploy run gem5 --plan         # print the steps, run nothing
wf deploy run gem5                # confirms, then runs
```

A required step that fails halts the run; the remaining steps come back marked
skipped rather than silently not appearing. `install.sh` never executes against
a half-updated checkout.

## Health

```bash
wf deploy status --all
  ● gem5      online     abc1234
  ● build01   degraded   1f2e3d4   — checkout: not a git repository
  ● rig02     offline
```

| State | Meaning |
|---|---|
| `online` | reachable, every required probe passed |
| `degraded` | reachable, something on it failed |
| `offline` | could not be reached — no probes ran |
| `unknown` | never checked |

`offline` and `degraded` are deliberately distinct: a device that answered and
failed a probe is not the same as one that never answered. Optional probes are
reported but never degrade a device.

Each run records a `DeviceStatus` into the registry, which fans out to mirrors —
so a NotesGraph inventory shows live state, not just registrations.

## OTA

Push, today:

```bash
wf deploy update --all --channel stable
wf deploy update gem5 --pin v2.1.0      # exact revision
wf deploy update gem5 --unpin           # follow the branch again
```

Every target carries a `channel` and an optional `pin`. A pull-based updater — an
agent on the rig polling its channel — reads the same two fields, so adding it
later changes no configuration.

## Reverse tunnel

```bash
wf proxy start
wf deploy tunnel gem5
   On the target, run an agent with:
   ANTHROPIC_BASE_URL=http://127.0.0.1:8099 OPENAI_BASE_URL=http://127.0.0.1:8099/v1
```

This is where the toolkit meets the capture proxy. A field robot gets exactly one
route home, and an agent running there authenticates as your account, has its
conversation captured into your store, and is tagged with the task you have open
— **without any credential being copied onto the machine.**

`ExitOnForwardFailure` is set, so a remote port that is already bound is a loud
error rather than a tunnel that silently forwards nothing.

## The NotesGraph inventory plugin

The other half lives in the notes-graph repo, built to its plugin conventions:

| Path | |
|---|---|
| `src/plugins/inventory/` | module, config, service, controller, tests |
| `src/models/inventory-device.ts` | model layer, registered in `models/index.ts` |
| `schema.prisma` + `migrations/20260915000000_inventory_devices/` | `InventoryDevice`, unique on `(workspace_id, key)` |

PAT-authenticated REST at `/api/inventory`, off by default behind
`inventory.enabled`. Reads need `Workspace.Read`; writes need
`Workspace.Settings.Update`, so a viewer cannot rewrite the fleet. Status is its
own endpoint because health runs are frequent and should not resend the whole
record.

```bash
wf deploy notes-login <personal access token>
wf config set deploy.mirrors '[notesgraph]'
wf config set deploy.notesgraph.workspace <workspace id>
wf deploy publish            # backfill everything already registered
```

---

# Remote agent execution

An agent defined in NotesGraph can run on a registered device instead of in the
browser tab. The device polls for work; nothing connects inward, so a machine
behind NAT is no harder than one on the desk.

```
Agent editor → harness "Remote device" + a device
     ↓
POST /api/inventory/workspaces/:ws/devices/:key/jobs        queued
     ↓  the device polls
POST .../devices/:key/jobs/claim                            running (leased)
     ↓  wf agent serve → the configured AI provider
POST .../jobs/:id/report                                    done | error
     ↓  the GUI polls and streams into its run panel
```

## On the device

```bash
wf agent serve              # claim and run jobs for this machine
wf agent serve --once       # take at most one job, then exit
wf agent jobs               # recent jobs and their outcomes
```

`serve` defaults to whichever registered agent target *is* this machine, so it
usually needs no arguments. Ctrl-C stops it; a job already running is left to
finish, and its lease lapses if the process dies.

## Rules worth knowing

**Registering a device is not consent to run code on it.** Enqueue checks
`agentTarget` server-side, so a direct API call cannot skip the opt-in that
`wf deploy add --agent` represents.

**Claiming is a conditional update, not read-then-write.** The claim re-asserts
the claimable condition in its `updateMany`; a runner that loses the race
matches zero rows and retries. An expired lease is claimable again, which is how
work is recovered from a runner that died mid-job. A heartbeat renews the lease
every 60s while a job runs.

**Enqueue needs `Workspace.Settings.Update`; reading a job needs
`Workspace.Read`.** Making someone else's machine execute something is as
privileged as changing the fleet. Watching a run you started is not.

**Closing the tab cancels the run.** Leaving a device working on a run nobody is
watching is the one failure that costs hardware you don't own.

---

# Publishing conversations to NotesGraph

When a session goes quiet and the ladder summarizes it, the finished
conversation is published as a document:

```
<project> / ai / conversations              ← index, links every conversation
<project> / ai / conversations / <chat id>  ← one per session
```

Each document leads with **Ending** (the session summary), then **Durable
facts**, then the transcript — the outcome first, because that is what someone
opens it for months later. The header carries task, project, repo, agent, model
and session id.

```yaml
proxy:
  capture:
    notesgraph:
      enabled: true
      url: https://app.notesgraph.com
      workspace: <workspace id>
      max_doc_bytes: 90000        # optional
```

```bash
wf mem publish              # the most recent session
wf mem publish --all        # backfill everything captured
wf mem publish --task PAN-484
```

Publishing is a mirror, not the record: the primary store stays the source of
truth, and a failure to publish never fails a session.

## Two limits this has to respect

**Document size.** The notes API runs NestJS's default body parser — Express's
100KB. A long session renders well past that, so the transcript is elided from
the middle outward: the opening frames the task and the close holds the outcome,
so the turns between are the ones worth losing. The header, ending and facts are
never dropped.

**Tag-shaped text.** NotesGraph's markdown importer treats `<word>` as an HTML
tag. Agent transcripts are full of them — `<system-reminder>`, `<user_query>`,
`Vec<T>` — and older servers answer 500 on one they cannot parse. Angle brackets
outside a code fence are entity-escaped on the way out; inside a fence they are
safe and left alone, so tool payloads stay readable.

The server-side half of that is fixed in the notes-graph repo: the markdown
validator now refuses only real HTML elements and keeps tag-shaped prose as
text, and an unsupported construct surfaces as a 400 naming it rather than a
500. The client-side escaping stays regardless — it is still correct, and it
keeps publishing working against a server that has not taken the fix.
