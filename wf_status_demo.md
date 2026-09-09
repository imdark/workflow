# wf status command demonstration

The new `wf status` command provides comprehensive task and PR information:

## Example Output

```
📋 Current Task Status
==================================================
🔑 Task Key: TASK-123
📝 Title: Fix authentication bug in login flow
📊 Status: In Progress
🔄 State: In Progress

📁 Repository Status
------------------------------

📂 frontend (./repos/frontend)
  ❌ No PR found
  🌿 Branch: fix/TASK-123-auth-bug
  📝 Uncommitted changes: 3 files modified

📂 backend (./repos/backend)
  🔗 PR: https://github.com/company/backend/pull/456
  📝 PR Title: Fix authentication bug in login flow
  📊 PR State: OPEN
  🌿 Branch: fix/TASK-123-auth-bug
  ✅ Working directory clean

📂 api-docs (./repos/api-docs)
  ❌ No PR found
  🌿 Branch: fix/TASK-123-auth-bug
  ✅ Working directory clean

💡 No PRs found for 2 repositories. Use 'wf done' to create PRs.

🚀 Quick Actions:
  wf done               # Complete task and create PRs
  wf ai                 # Start AI session
  wf slack notify       # Post status to Slack
```

## Features

### Task Status
- ✅ Current task key and title
- ✅ Backend status (In Progress/Completed)
- ✅ Task state from Jira/GitHub

### Repository Status
- ✅ Multi-repository support
- ✅ PR existence and links
- ✅ PR titles and states
- ✅ Current branch information
- ✅ Uncommitted changes detection
- ✅ Working directory status

### Quick Actions
- ✅ Context-sensitive command suggestions
- ✅ Direct workflow integration

## Usage

```bash
# Show current task status
wf status

# Works with no task (shows helpful message)
wf status
❌ No current task set.
💡 Use 'wf start <task-key>' to begin working on a task.
```

The status command is fully integrated with existing workflow features and provides real-time visibility into development progress.