# Custom Field Defaults with `wf start`

The `wf start` command now supports **default values** for custom fields, automatically applying commonly used values when creating new tasks!

## 🚀 **Default Value Features**

### **Automatic Defaults on Task Creation**
When you create a new task with `wf start NEW-TASK`, the system automatically:
1. Detects if it needs to create a new Jira ticket
2. Applies default values for configured custom fields
3. Creates the ticket with all defaults pre-populated

### **Simple Default Management**
```bash
# Add field with default value
wf config add-field POD --project-key=DATA --default=backend
wf config add-field STORY_POINTS "Story Points" --default=8

# Manage defaults
wf config set-default POD infrastructure
wf config clear-default STORY_POINTS
wf config list-fields
```

## 📋 **Configuration Examples**

### **Setup Fields with Defaults**
```bash
# Project-specific field with default
wf config add-field POD --project-key=DATA --default=backend

# Global field with default  
wf config add-field TEAM --default=infrastructure

# Story points with numeric default
wf config add-field STORY_POINTS "Story Points" --default=5

# Priority field with default
wf config add-field PRIORITY --default=Medium
```

### **View Configuration**
```bash
wf config list-fields
```
**Output:**
```
Configured custom fields:
  POD: customfield_10373 (project: DATA) (default: backend)
  STORY_POINTS: customfield_10031 (default: 8)
  TEAM: customfield_10001 (default: infrastructure)
  PRIORITY: priority (default: Medium)
```

## 🎯 **Real-World Usage**

### **Daily Workflow**
```bash
# Create new task - defaults automatically applied!
wf start "Fix login bug"
# 🎯 Applying default field values: POD=backend, STORY_POINTS=8
# ✅ Created ticket PROJ-123
# ✅ Now working on task PROJ-123

# Override defaults when needed
wf start "API integration" --POD=frontend --STORY_POINTS=13
# 🎯 Applying default field values: TEAM=infrastructure  
# ✅ Set POD = frontend for PROJ-124 (overrides default)
```

### **Project-Specific Defaults**
```bash
# Setup
wf config add-field POD --project-key=DATA --default=backend
wf config add-field POD --project-key=FRONTEND --default=frontend

# Usage - automatic project detection
wf start "Data pipeline issue"     # Uses DATA project defaults
wf start "UI component update"      # Uses FRONTEND project defaults
```

## 🛠️ **Default Value Management**

### **Set Defaults During Creation**
```bash
# Add field with default immediately
wf config add-field SPRINT --default="Sprint 42"
```

### **Modify Defaults Later**
```bash
# Change existing default
wf config set-default POD infrastructure

# Remove default
wf config clear-default POD
```

### **Check Current Defaults**
```bash
wf config list-fields
# Shows (default: value) for each field
```

## 🎨 **Field Types & Default Values**

The system handles different field types automatically:

| Field Type | Default Example | Processing |
|------------|------------------|-------------|
| **String** | `--default=backend` | Direct string |
| **Number** | `--default=8` | Converted to number |
| **Option** | `--default=Backend` | Single-select option |
| **User** | `--default=user@email.com` | User object |
| **Array** | `--default="tag1,tag2"` | Array of strings |

## 🔄 **How It Works**

1. **Task Creation**: When `wf start` needs to create a new ticket
2. **Default Detection**: System finds all applicable defaults for the project
3. **Type Processing**: Values are processed based on field type
4. **Auto-Application**: Defaults applied automatically during ticket creation
5. **Override Support**: Command-line values override defaults

## 📊 **Configuration Storage**

Defaults are stored in your `~/.wf/config.yaml`:

```yaml
custom_fields:
  POD:
    field_id: customfield_10373
    project_key: DATA
    default_value: backend          # ← New default support
  STORY_POINTS:
    field_id: customfield_10031
    default_value: 8               # ← Numeric default
```

## 🎯 **Best Practices**

### **Team Defaults**
Set defaults for your common team assignments:
```bash
wf config add-field TEAM --default=backend-team
```

### **Sprint Defaults**
Automatically assign to current sprint:
```bash
wf config add-field SPRINT --default="Sprint 45"
```

### **Priority Defaults**
Set consistent priority levels:
```bash
wf config add-field PRIORITY --default=Medium
```

### **Story Point Defaults**
Standardize your estimation approach:
```bash
wf config add-field STORY_POINTS "Story Points" --default=3
```

## 💡 **Use Cases**

### **Onboarding New Team Members**
```bash
# One-time setup
wf config add-field TEAM --default=onboarding
wf config add-field STORY_POINTS --default=1

# New member creates tasks with appropriate defaults
wf start "Learn codebase"  # Gets TEAM=onboarding, STORY_POINTS=1
```

### **Project Type Separation**
```bash
# Backend projects
wf config add-field POD --project-key=BACKEND --default=backend

# Frontend projects  
wf config add-field POD --project-key=FRONTEND --default=frontend

# Automatic context switching based on project
wf start "API endpoint"         # Backend context
wf start "Component library"      # Frontend context
```

### **Consistent Estimation**
```bash
wf config add-field COMPLEXITY --default=Medium
wf config add-field STORY_POINTS --default=5

# All new tasks get consistent baseline estimates
wf start "Bug fix"              # Gets default complexity and points
```

## 🎉 **Benefits**

✅ **Zero Configuration Required** - New tasks get defaults automatically  
✅ **Consistent Team Standards** - Enforce consistent field values  
✅ **Fast Task Creation** - No manual field entry for common values  
✅ **Project Context Awareness** - Different defaults per project  
✅ **Override Flexibility** - Change defaults when needed  
✅ **Type Safe** - Automatic type processing for all field types  

With default values, your team can create tasks faster and more consistently while maintaining the flexibility to override when needed!