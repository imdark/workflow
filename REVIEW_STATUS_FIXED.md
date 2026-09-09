# ✅ Fixed "In Progress" vs "In Review" Status Detection

## Problem
The `wf status` command was showing "In Progress" even when the task was actually in "In Review" state.

## Root Cause
The status detection logic was too simple - it only checked if the task was "done" or not, without considering the "review" state.

## Solution Implemented

### 🔧 **Added `is_in_review` Method to Jira Backend**

```python
def is_in_review(self, issue):
    try:
        # Get the actual issue to check if it's in review
        jira_issue = self.client.issue(issue.key)
        status_name = jira_issue.fields.status.name.lower()
        return status_name in ["code review", "in review", "review", "peer review"]
    except:
        return False
```

### 📊 **Enhanced Status Detection**

```python
# Check if task is done or in review
try:
    is_done = backend.is_done(issue)
    is_in_review = backend.is_in_review(issue) if hasattr(backend, 'is_in_review') else False
except (AttributeError, Exception):
    is_done = False
    is_in_review = False

if is_done:
    typer.echo("✅ State: Completed")
elif is_in_review:
    typer.echo("👀 State: In Review")
else:
    typer.echo("🔄 State: In Progress")
```

## 🎯 **Status States Now**

### ✅ **Completed**
- Jira status category is "done"
- Shows: `✅ State: Completed`

### 👀 **In Review**  
- Jira status name matches: "code review", "in review", "review", "peer review"
- Shows: `👀 State: In Review`

### 🔄 **In Progress**
- Task is neither done nor in review
- Shows: `🔄 State: In Progress`

## 📊 **Output Examples**

### Task In Progress:
```
🔄 State: In Progress
```

### Task In Review:
```
👀 State: In Review
```

### Task Completed:
```
✅ State: Completed
```

## 🔍 **How It Works**

1. **Fresh Data**: Gets current issue status from Jira API (not cached)
2. **Status Name**: Checks the actual status name (case-insensitive)
3. **Pattern Matching**: Recognizes multiple review-related status names
4. **Fallback Safe**: Handles API errors gracefully

## ✅ **Benefits**

- ✅ **Accurate Status**: Shows correct state based on actual Jira status
- ✅ **Multiple Review States**: Recognizes various "review" status names
- ✅ **Consistent Icons**: Uses appropriate emoji for each state
- ✅ **Real-time**: Gets fresh status from Jira API
- ✅ **Error Resilient**: Gracefully handles API failures

Now when your task is in review, `wf status` will correctly show:
```
👀 State: In Review
```

instead of incorrectly showing:
```
🔄 State: In Progress
```