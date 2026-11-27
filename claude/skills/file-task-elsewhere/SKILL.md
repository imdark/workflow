---
name: file-task-elsewhere
description: File a task in the project or repo it belongs to, after asking the user. Use when the task you were started on belongs to another project or repo, or when the work needs a subtask or follow-up that another project or repo has to do.
---

# File a task elsewhere

A task belongs **elsewhere** when its work lives in another repo (a `wf` CLI change filed under notes graph) or another project's tracker, or when part of it does: a subtask, a follow-up, a fix the current change depends on. Work that fits this checkout is done here, not filed.

## Steps

1. **Pick the home.** Run `wf project list` and name the repo that owns the work (its folder name, e.g. `workflow`, `notes-graph`) and the project that owns that repo. If two could own it, offer both in step 2.
2. **Ask.** Use `mcp__run__ask_user` when you have it, else `AskUserQuestion`, else ask in chat and stop until answered. One question naming the repo, project, title and one-line description, with options such as `["Yes, file it in <repo>", "File it somewhere else", "Don't file it"]`. File nothing without a yes. Each yes covers this one task.
3. **File it.**
   ```bash
   wf task create "<title>" --repo <repo> --journal -d "<description>"
   ```
   `--repo` picks the owning project, puts the task under that repo's heading in today's journal note, and tags it `#repo:<repo>` so starting it runs in that repo. The description is for someone who hasn't seen this run: what to do and why, the source task's key, and what you already found. Done when the command prints `Created task: <KEY>` and `Journal: under '<repo heading>'`.
4. **Close the loop on the source task** with `update_task`, naming the new key and repo.
   - Whole task elsewhere: set the source task to BLOCKED pointing at the new key, and stop. This branch has nothing to ship, so skip `wf done`.
   - Subtask only: carry on with the rest of the work here.

## When it doesn't go through

- **The user says no:** file nothing. Note on the source task what you would have filed, then do as the user says.
- **`wf task create` fails, or prints no `Journal:` line:** report its output on the source task. A task without a printed key wasn't filed; write no key or journal line by hand.
