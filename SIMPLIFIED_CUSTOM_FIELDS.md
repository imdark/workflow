# Smart Custom Fields with `wf start` (Simplified)

The `wf start` command now includes **smart custom field discovery** with the **simplest possible interface** - just use the field name and we'll auto-resolve everything!

## 🚀 **Ultra-Simple Usage**

### **Single Command Field Addition**
```bash
# Most common case - field name matches command name
wf config add-field PRIORITY
wf config add-field POD

# With project restriction
wf config add-field TEAM --project-key=DATA
```

**No more manual field ID lookup!** The system automatically:
1. Uses the field name to search Jira
2. Resolves to the internal field ID
3. Configures everything for you

## Command Options

| Command | When to Use | Example |
|---------|--------------|---------|
| `wf config add-field FIELD_NAME` | Field name matches your desired command name | `wf config add-field PRIORITY` |
| `wf config add-field CMD_NAME "Actual Field"` | You want a different command name than the field | `wf config add-field POINTS "Story Points"` |
| `wf config add-field FIELD customfield_123` | You already know the field ID | `wf config add-field LEGACY customfield_10045` |

## Real-World Examples

### **Basic Usage (95% of cases)**
```bash
# Add standard fields
wf config add-field PRIORITY
wf config add-field POD

# Add to specific project only
wf config add-field TEAM --project-key=DEV
```

### **Custom Command Names**
```bash
# Use shorter/more convenient command name
wf config add-field POINTS "Story Points"
wf config add-field EPIC "Epic Link"

# Now you can use: --POINTS=5 --EPIC=PROJ-123
```

### **Project-Specific Fields**
```bash
# Field only applies to DATA project tickets
wf config add-field POD --project-key=DATA

# Field only applies to DEV project tickets  
wf config add-field SPRINT "Sprint" --project-key=DEV
```

## Workflow Integration

Once configured, using custom fields is effortless:

```bash
# Use your configured field names
wf start DATA-123 --POD=backend --POINTS=8
wf start DEV-456 --TEAM=infrastructure --SPRINT="Sprint 42"

# Mix with other options
wf start TASK-789 --base=develop --PRIORITY=High --POINTS=5
```

## Field Discovery Tools

### **Search for Available Fields**
```bash
# Find all story-related fields
wf config search-fields "story"

# Find all priority fields
wf config search-fields "priority"

# Show all available fields
wf config search-fields
```

### **Quick Field Examples**
```bash
wf config add-field SPRINT        # If "Sprint" field exists
wf config add-field EPIC_NAME     # If "Epic Name" field exists  
wf config add-field ASSIGNEES     # If "Assignees" field exists
```

## Auto-Detection Behavior

The system tries these in order:
1. **Exact name match**: `wf config add-field PRIORITY` finds field named "Priority"
2. **Case-insensitive match**: `wf config add-field priority` also works
3. **Partial match**: If multiple matches found, shows options
4. **Error with suggestions**: If no matches, suggests similar fields

### **Example Auto-Detection**
```bash
wf config add-field STORY_POINTS
# 🔍 Auto-detecting field for 'STORY_POINTS'...
# ✅ Resolved 'STORY_POINTS' → Story Points (customfield_10031)
# ✅ Added custom field 'STORY_POINTS' with ID 'customfield_10031'
```

## Field Management

### **See Your Configured Fields**
```bash
wf config list-fields
```

**Output:**
```
Configured custom fields:
  PRIORITY: priority
  POD: customfield_10373 (project: DATA)
  STORY_POINTS: customfield_10031
  TEAM: customfield_10101
```

### **Remove Fields**
```bash
wf config remove-field PRIORITY
wf config remove-field POD
```

### **Refresh Field Discovery**
```bash
# If recently added Jira fields aren't found
wf config discover-fields
```

## Complete Workflow Example

### **Setup (one-time)**
```bash
# Configure your commonly used fields
wf config add-field PRIORITY
wf config add-field POD --project-key=DATA
wf config add-field POINTS "Story Points"
```

### **Daily Usage**
```bash
# Work on tasks with custom fields
wf start DATA-123 --POD=backend --POINTS=8
wf start TASK-456 --PRIORITY=High --POINTS=3
```

## Benefits

✅ **One-command setup** - No field ID hunting  
✅ **Smart auto-detection** - Finds the right field automatically  
✅ **Flexible naming** - Use any command name you prefer  
✅ **Project-specific** - Fields can be scoped to specific projects  
✅ **Type-aware** - Handles different field types automatically  
✅ **Cached performance** - Fast after initial discovery  

The simplified interface makes custom field configuration as easy as typing the field name - no technical knowledge required!