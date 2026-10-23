# Mobile agents: moving a run to another machine, model or agent

Status: design (PERSONAL-144, 2026-10-21). Phase 1 is built: checkpoints
in the job directory and same-device resume (`workflow/deploy/checkpoint.py`).

A run today lives and dies on the device that claimed it. If that device
drops off the network, the server notes "Lost contact" once the lease runs
out, and the job waits for a runner on the *same* device to start it again
from scratch (`registry/jobs.ts`, `leaseLapsedNote`). If the run errors, the
only way on is to start a new one, which knows nothing of the first. The
phone's "reassign" (`agent-block-runs.tsx`, `useReassign`) stops a run and
starts another agent on the same target, also from nothing.

A mobile agent is a run that can be picked up somewhere else and carry on:
on another machine (a cloud box is just another registered machine), with
another model, or under another agent recipe (a NotesGraph agent: its
instructions, model, access level and skills). The work, the conversation
and what the person already answered come with it.

## When a run moves

| Trigger | Who decides | What the source can still do |
| --- | --- | --- |
| The person presses **Move** on a running run | the person | everything: stop cleanly, take a last checkpoint, write a handoff |
| The run ends in an error (API outage, cost cap, a tool that only exists elsewhere) | the person, offered **Continue elsewhere** on the failed run; or the agent's policy | take a last checkpoint before reporting the error |
| The device loses contact (lease lapses) | the agent's policy (auto-move to a fallback device after a grace period) or the person | nothing. Only what it already sent up survives |

The third case shapes the whole design: since a device that has lost the
network cannot pack anything, **the run has to be packed continuously while
it is healthy**. Moving is then "start a continuation from the latest
checkpoint", the same in all three cases. A clean move only adds a fresher
checkpoint and a handoff note.

## What a run carries: the checkpoint

A checkpoint is a small set of files the runner uploads with its reports. It
holds no secrets: `mcp.json` (the NotesGraph token) is never in it, and text
goes through the same redaction the share links use.

```
checkpoint/
  manifest.json     job id, seq, taken at, source device, why
                    agent id + recipe, model, access level, skills
                    task: docId, blockId, taskIds, title, original prompt
                    repo: remote URL, base sha, branch (if any), project key
                    needs: providers / MCP servers / tools the run used
                    spent: costUsd, tokens, steps so far
  work.bundle       git bundle base..checkpoint commit (see below)
  outside.json      files changed outside the repo, from run_changes "files"
  session.jsonl     the provider's own session (Claude Code's transcript),
                    when the provider has one
  handoff.md        only on a clean move: the agent's own note on where it is
```

Already on the server and not copied: the transcript (`log`), questions and
answers and steers (`RegistryJobQuestion`), and note changes (they are in
NotesGraph already; `changes.notes` lists them).

### The work: a commit that never touches the agent's tree

`job_exec` already records `git_base` before the run. A checkpoint snapshots
the worktree without disturbing it, the way `git stash create` does:

```
GIT_INDEX_FILE=<job>/ckpt.index git add -A          # tracked + untracked
tree=$(GIT_INDEX_FILE=<job>/ckpt.index git write-tree)
commit=$(git commit-tree $tree -p HEAD -m "wf checkpoint <job> #<seq>")
git update-ref refs/wf/checkpoints/<job> $commit
git bundle create work.bundle <base>..refs/wf/checkpoints/<job>
```

The agent's index, branch and stash are untouched, and nothing is pushed to
the repo's remote. The bundle holds only the commits since `base` plus the
snapshot, so it stays small, and it goes to NotesGraph with the rest of the
checkpoint. (Pushing a hidden ref to `origin` would work too, but it writes
to a shared remote without asking; the bundle needs no permission.)
`.gitignore`d files are left out on purpose: `node_modules`, build output and
`.env` belong to the destination, not the run.

### The conversation

Two layers, because they serve different destinations:

- **`session.jsonl`**, the provider's own session. For Claude Code that is
  `~/.claude/projects/<cwd slug>/<session id>.jsonl`; the session id is in
  the `system/init` event `Narrator` already reads but does not keep. A
  destination running the same provider restores it and runs
  `claude --resume <id>`, so the agent continues with its full context,
  including tool results. A different model on the same provider can resume
  it too (`--model` with `--resume`).
- **The transcript and handoff**, for everything else (another provider,
  another recipe). The new run is told what happened in plain text: the
  original task, the handoff note if there is one, what changed, and the
  path to the old transcript as a file it can read. No separate
  summarising model call: the agent that continues reads what it needs.

### When to checkpoint

- After each top-level turn that changed something: the `Stop`/`PostToolUse`
  hooks already run per tool call (`steer_hook.py`); a checkpoint runs at
  most every 2 minutes and only when the tree, the session file or
  `changes` moved since the last one.
- Before reporting an error, and on a clean move.
- Uploads go with the next report. If the network is down the checkpoint
  waits on disk and the newest replaces older unsent ones.

What is lost on a lapse is the work since the last uploaded checkpoint,
a couple of minutes at most.

## The move itself

### Server (notes-graph, `plugins/registry`)

- `RegistryJob` gains `continuesJobId`, `checkpointSeq`, `checkpointKey`
  (blob key of the newest checkpoint) and `claimEpoch`.
- New terminal status `moved`. A moved job is never handed out again; its
  page links to the continuation, and the continuation links back.
- `POST /jobs/:id/move { deviceKey?, agentId?, model?, reason }`:
  - running and its runner is reporting: status becomes `moving`. The
    runner sees it on its next tick (like `cancelled` today), stops the
    agent, takes a last checkpoint and asks the agent for `handoff.md` (one
    short `--resume` turn: "you are being moved to X; write where you are").
    It reports `moved` with the checkpoint, and the server queues the
    continuation.
  - errored, or its lease has lapsed: the server queues the continuation
    straight from the newest checkpoint it holds.
- The continuation is a new job with the same `taskIds`, `docId` and
  `blockId`, so task claims pass from one to the other without going back
  to to-do in between (no `RELEASE_SETTLE_MS` dance).
- Access is checked again: moving to a recipe with a higher access level
  (Edit files, Everything) asks the person, as starting such a run would.
- Budget: the continuation's `maxCostUsd` is what is left of the original
  cap, so moving is not a way round it.

### Fencing: the device that comes back

A device that lost contact may come back while its continuation runs
elsewhere. Every claim gets a `claimEpoch`; reports carry it, and a report
on a moved job, or with an old epoch, is refused with `status: moved`. The
runner then stops the agent like a cancel, and keeps its local worktree and
checkpoint ref instead of dropping them (`_drop_worktree_if_empty` already
keeps uncommitted work). It uploads that last checkpoint as an *orphan*,
shown on the continuation's page as "the old run on laptop got further
than your last checkpoint: N changes", for the person or the new agent to
pull in. Nothing is merged automatically.

### Destination runner (`wf agent serve`)

A claimed job with `continuesJobId` is restored before it is launched:

1. Download the checkpoint into the new job directory.
2. Find the repo by remote URL among the configured repos
   (`session.configured_repos`); if the device lacks it, fail the claim
   with a clear reason so the move can be pointed elsewhere.
3. Make the job's worktree at `base`, `git fetch work.bundle`, check out
   the checkpoint commit, then `git reset HEAD~1` (its parent, the last
   real commit), so the snapshot's changes are uncommitted again and
   the agent's own history is as it left it. If the run had a branch,
   recreate it there.
4. Same provider and a session file: place it under the new worktree's
   cwd slug and launch with `--resume`, prepending one message: where it
   moved from and why, the new working directory (old absolute paths in its
   context are stale), and any answers it was waiting on.
5. Otherwise launch fresh with a prompt that leads with the original task,
   then "You are continuing a run that was moved", the handoff, the
   changes record, and the old transcript's path.
6. `outside.json` files are not written back automatically; the prompt
   lists them, since a path outside the repo may not mean anything here.

### Devices say what they can run

To offer only destinations that can take the run, `wf agent serve` reports
its capabilities with each claim poll: providers on PATH (`claude`,
`opencode`, `flow`), MCP servers it can start (OmniSeek), configured repos
by remote URL, and whether `allow_commands` is on. `manifest.needs` is
matched against that, and the Move dialog greys out devices that would
fail, saying why ("no checkout of notes-graph on gem4").

## In NotesGraph

- A running run's menu gets **Move…**: pick a device (with its
  capability verdict), and optionally another model or agent. The phone's
  reassign becomes "Move to another agent" when a checkpoint exists, and
  stays "start over" when it does not.
- A failed run shows **Continue elsewhere** next to its error.
- An agent's settings gain **If its device goes offline**: wait (today's
  behaviour), ask me (push notification after the grace period), or move
  to a named fallback device.
- The run page shows the chain: "moved from laptop at 14:02 (lost
  contact), checkpoint #7".

## Phases

1. **Checkpoints, same device.** Keep the Claude session id; write
   checkpoints to the job directory. A re-claimed job on the same device
   resumes with `--resume` instead of restarting. Small, testable alone,
   and it already fixes "the runner restarted and the run began again".
2. **Upload and continuation jobs.** Checkpoint blobs on the server,
   `continuesJobId`, `moved`, the move endpoint, the restore steps, and the
   Move button for a person-initiated move to another device.
3. **Lost contact and errors.** Claim epochs and fencing, orphan
   checkpoints, Continue elsewhere, the offline policy with a fallback
   device.
4. **Another model or recipe.** The handoff turn, the fresh-start prompt,
   access and budget carried over, device capabilities in the dialog.
5. **Other providers.** opencode and flow have no session we restore;
   they continue from the transcript and handoff only.

## Questions left open

- How long checkpoints are kept after a run ends (proposal: the newest one,
  for 14 days; they can hold tool output a person would not want kept).
- Whether a cloud fallback machine should be spun up on demand or stay
  registered and idle. This design only needs it to be a registered device
  running `wf agent serve`.
- Big untracked files: a size cap per checkpoint (proposal 50 MB) with
  the skipped paths listed in the manifest.
