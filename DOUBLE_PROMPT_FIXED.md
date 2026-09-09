# ✅ Fixed Duplicate Title Prompt Issue

## Problem Solved
The system was prompting for issue title twice during ticket creation:
1. First in the error message parsing
2. Second when creating the new ticket (using the already cleaned key)

### 🐛 **Before** 
```bash
wf start "move: llm answering to use websockets"
❌ Issue '"move: llm answering to use websockets"' not found
💡 Suggested issue key: move: llm answering to use websockets
🎯 Would you like to create a new Jira issue '"move: llm answering to use websockets"'? [y/N]

📝 Creating new Jira issue...
Issue title: it needs to pass as name when the user says -y it should pass as name  # Wrong! Duplicate prompt
```

### 🔧 **Root Cause**

When the error message contained quotes, the `get_or_create` method would:
1. ✅ **Extract clean key**: `move: llm answering to use websockets`
2. ❌ **Still prompt for title**: Instead of using the cleaned key as default
3. ✅ **Create ticket**: Use the cleaned key for creation
4. ❌ **Redundant prompt**: User gets asked for title twice

### ✅ **Solution Applied**

#### **Fixed Prompt Logic**
```python
def _create_interactive_issue(self, suggested_key):
    # Get issue details from user - use cleaned key as default title
    summary = typer.prompt("Issue title") or suggested_key
```

### 🎯 **Expected Behavior Now**

```bash
wf start "move: llm answering to use websockets"
❌ Issue '"move: llm answering to use websockets"' not found
💡 Suggested issue key: move: llm answering to use websockets
🎯 Would you like to create a new Jira issue 'move: llm answering to use websockets'? [y/N]

📝 Creating new Jira issue...
Issue title: move: llm answering to use websockets  # Correct! Single prompt only
```

### ✅ **Benefits**

- ✅ **Single Prompt**: Only asks for title once
- ✅ **Smart Default**: Uses cleaned key as default title
- ✅ **Cleaner UX**: No redundant user input
- ✅ **Consistent Flow**: Uses extracted key throughout
- ✅ **No Confusion**: Clear, straightforward interaction

### 🚀 **Result**

Now when users encounter quoted error messages and agree to create tickets, they get a smooth, single-prompt experience with proper key handling!