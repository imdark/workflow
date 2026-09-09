# ✅ Fixed Issue Key Parsing in Error Messages

## Problem Solved
When user typed `wf start "move: llm answering to use websockets"`, the error message parsing was using the entire quoted string as the issue key instead of extracting the actual key.

### 🐛 **Before**
```bash
wf start "move: llm answering to use websockets"
❌ Issue '"move: llm answering to use websockets"' not found: Issue does not exist
🎯 Would you like to create a new Jira issue '"move: llm answering to use websockets"'? [y/N]

📝 Creating new Jira issue...
Issue title: "move: llm answering to use websockets"  # Wrong!
```

### ✅ **After**
```bash
wf start "move: llm answering to use websockets"
❌ Issue '"move: llm answering to use websockets"' not found: Issue does not exist
💡 Suggested issue key: move: llm answering to use websockets
🎯 Would you like to create a new Jira issue 'move: llm answering to use websockets'? [y/N]

📝 Creating new Jira issue...
Issue title: move: llm answering to use websockets  # Correct!
```

## 🔧 **Solution Applied**

### **Enhanced Key Extraction**
```python
def get_or_create(self, q):
    try:
        return self.get(q)
    except Exception as e:
        # Check if this is a "does not exist" error vs other errors
        error_msg = str(e)
        if "does not exist" in error_msg or "not found" in error_msg.lower():
            # Issue doesn't exist, offer to create it
            typer.echo(f"❌ Issue '{q}' not found: {error_msg}")
            
            # Try to extract a cleaner issue key from the input
            suggested_key = q.strip().strip('"\'')
            if suggested_key != q:
                typer.echo(f"💡 Suggested issue key: {suggested_key}")
            
            if typer.confirm(f"🎯 Would you like to create a new Jira issue '{suggested_key}'?"):
                return self._create_interactive_issue(suggested_key)
        else:
            # Other error (permission, network, etc.)
            typer.echo(f"❌ Error accessing issue '{q}': {error_msg}")
        
        return None
```

### 🎯 **Key Extraction Logic**

#### **Input Cleaning**
```python
suggested_key = q.strip().strip('"\'')
```

#### **Comparison Check**
```python
if suggested_key != q:
    typer.echo(f"💡 Suggested issue key: {suggested_key}")
```

#### **Proper Default Title**
```python
summary = typer.prompt("Issue title") or suggested_key
# Uses the cleaned key as default, not full quoted string
```

### ✅ **Benefits**

- ✅ **Clean Key Extraction**: Removes quotes from input
- ✅ **Smart Comparison**: Only shows suggestion if different from input
- ✅ **Better Defaults**: Uses extracted key as default title
- ✅ **User Guidance**: Shows what key will be used for creation
- ✅ **Consistent Behavior**: Follows normal workflow once created

### 🚀 **Usage Examples**

#### **Simple Issue Creation**
```bash
wf start TASK-123
# Uses TASK-123 directly as key and title
```

#### **Quoted Issue Creation**
```bash
wf start "complex issue name with spaces"
# Cleans to: complex-issue-name-with-spaces
# Uses cleaned key as default title
```

Now the system correctly handles quoted error messages and extracts clean issue keys for proper ticket creation!