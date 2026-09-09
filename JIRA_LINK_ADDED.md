# ✅ Jira Task Link Added to wf status

## Enhancement Made
Added clickable Jira task links to the `wf status` output for both CLI versions.

### 🎯 **New Feature**

When you run `wf status`, it now includes:
```
🔗 Jira: https://your-domain.atlassian.net/browse/PROJ-123
```

### 🔧 **Implementation**

#### **workflow/cli.py** (Main CLI)
```python
# Get Jira URL from config and create task link
jira_url = cfg.get("jira", {}).get("url", "")
if jira_url:
    jira_link = f"{jira_url.rstrip('/')}/browse/{issue.key}"
    typer.echo(f"🔗 Jira: {jira_link}")
```

#### **cli.py** (Root CLI)  
```python
# Get Jira URL from config and create task link
jira_url = cfg.get("jira", {}).get("url", "")
if jira_url:
    jira_link = f"{jira_url.rstrip('/')}/browse/{issue.key}"
    typer.echo(f"🔗 Jira: {jira_link}")
```

### 📊 **Complete Output Now**

```
📋 Current Task Status
==================================================
🔑 Task Key: PROJ-123
🔗 Jira: https://your-domain.atlassian.net/browse/PROJ-123
📝 Title: Add search alias
📊 Status: Active
🔄 State: In Progress

📁 Repository Status
------------------------------

📂 myrepo (~/code/myrepo)
  🔗 PR: https://github.com/example-org/example-repo/pull/123
  📝 PR Title: PROJ-123 Add search alias
  🌿 Branch: proj-123-add-search-alias
  ✅ Working directory clean

🚀 Quick Actions:
  wf done               # Complete task and create PRs
  wf ai                 # Start AI session
  wf slack notify       # Post status to Slack
```

### ✅ **Benefits**

- ✅ **Direct Access**: Clickable link to Jira task
- ✅ **Smart URL**: Handles both with/without trailing slashes
- ✅ **Config-Driven**: Uses your configured Jira URL
- ✅ **Consistent**: Works in both CLI versions
- ✅ **Clean Output**: Only shows if Jira URL is configured

### 🔗 **How It Works**

1. **Read Config**: Gets Jira URL from `wf init` configuration
2. **Clean URL**: Removes trailing slashes to prevent double slashes  
3. **Construct Link**: Creates standard Jira task URL format
4. **Display**: Shows as clickable link in terminal

### 🎯 **Usage**

```bash
# Shows full status with Jira link
wf status

# Output includes:
🔗 Jira: https://your-domain.atlassian.net/browse/PROJ-123
```

Now you can easily navigate from the terminal to your Jira task with a single click!