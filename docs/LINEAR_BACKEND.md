# Linear backend

Linear is a drop-in alternative to Jira for `wf`'s task backend. Once it's
selected, the commands you already use — `wf start`, `wf done`, `wf status`,
`wf task list`, `wf task comment` — work against Linear issues instead of Jira
issues, with no change to the workflow.

## Setup

```bash
# 1. Store an API key (created at https://linear.app/<team-name>/settings/account/security/api-keys)
wf config linear-token

# 2. Point it at your team (the prefix on your issue IDs, e.g. ENG from ENG-123)
wf config set linear.team ENG

# 3. Switch the backend over
wf task backend linear

# 4. Check what it resolved
wf config linear-status
```

`wf init` also offers Linear when setting up a fresh config.

The API key is kept in the macOS Keychain (service `workflow.linear`), never in
`~/.wf/config.yaml`. `LINEAR_API_KEY` in the environment overrides the Keychain,
which is the easiest route for CI. If you already have a key sitting in the
config file, `wf config linear-migrate-token` moves it into the Keychain.

## Configuration

```yaml
task_backend: linear
linear:
  team: ENG            # default team key
  workspace: acme      # URL slug from https://linear.app/acme/ (optional)
  states:              # optional overrides, see "Transitions" below
    in_progress: In Progress
    review: Code Review
    done: Done
```

`workspace` is only used to build issue links without an extra API call. Leave
it out and `wf` reads the URL off the issue instead.

Any of these can be set from the CLI:

```bash
wf config set linear.workspace acme
wf config set linear.states.review "Code Review"
```

## Transitions

Jira has named transitions that the server hands out; Linear just has workflow
states per team, each carrying a coarse `type` (`triage`, `backlog`,
`unstarted`, `started`, `completed`, `canceled`). `wf start` moves an issue to
in-progress, and `wf done` moves it to review (if it opened a PR) or to done.
Each of those resolves to a concrete state in this order:

1. an explicit `linear.states.<target>` from the config,
2. a state whose name matches (`In Progress`, `Code Review`, `Done`, …),
3. any state with the right `type` — `started` for in-progress and review,
   `completed` for done.

Because Linear has no review *type* (a review state is just another `started`
state), a workspace with no review-ish state name leaves the issue on the
in-progress state rather than moving it backwards. If your states are named
something the heuristics don't recognise, set them explicitly:

```bash
wf config linear-teams                                  # list states per team
wf config set linear.states.review "Waiting on review"
wf config linear-status                                 # confirm what resolved
```

## Differences from Jira

| Jira | Linear | Notes |
|---|---|---|
| Issue types (Task/Bug/Story/Epic) | Labels | `wf create "…" --issue-type Bug` attaches a `Bug` label if the team has one. Unknown types are skipped, not created. |
| Sprints | Cycles | `wf`'s `active_sprint` variables resolve to the team's active cycle. |
| Project key | Team key | Both are the prefix in `ENG-123`. Linear *projects* are a separate concept and are matched by name. |
| Custom fields | Built-in attributes | Linear has no custom fields; see below. |
| Chosen issue keys | Server-assigned | `wf start ENG-999` on a missing issue creates a new one in team `ENG`, but Linear picks the number. |
| Numeric comment IDs | UUIDs | `wf task comment` numbers comments 1, 2, 3… so `wf task comment edit ENG-1 2 "…"` works as it does elsewhere. |
| Boards | — | `wf config list-boards` is Jira-only. |

### Fields

Linear has no custom fields, so the `custom_fields` entries in your config are
mapped onto the attributes it does have. Defaults are applied when an issue is
created, the same as on the Jira backend:

```bash
wf config add-field priority --default High
wf config add-field labels   --default teleop
```

Recognised names and what they accept:

| Field | Accepts |
|---|---|
| `priority` | `Urgent`, `High`, `Medium`, `Low`, `None` |
| `labels` | comma-separated label names (must already exist) |
| `project` | a Linear project name |
| `estimate` | a number |
| `assignee` | a name, display name, email, or `me` |
| `due_date` | `YYYY-MM-DD` |

Anything else is reported and skipped rather than failing the write. As with
Jira, a field's `project_key` scopes it to one team, `default_value` may contain
`{variables}`, and `value_mapping` translates the value you type into the one
Linear wants. `wf config field-options priority` lists a field's valid values.

## Per-project tracking

A project can be tracked in Linear while others stay on Jira:

```bash
wf project add teleop --linear-team PAN --default-repo ~/code/teleop
```

That writes `task_backend: linear` and `linear.team` into the project's config,
which then wins over the global setting for commands run inside it.

## Not the Slack `@Linear` flow

`wf slack teleop-linear-sync` and `wf slack teleop-convert` are a separate,
older path: they drive Slack to @-mention the Linear app so *it* files issues,
and need no Linear API key. The backend described here talks to Linear's GraphQL
API directly. The two don't interact.
