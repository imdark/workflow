# Getting POD Field Values - Complete Guide

## 🎯 **Current Status**

The field values system is **fully implemented** and ready to use! Here's how to get POD field values:

## 📋 **New Commands Available**

```bash
# Get all possible values with metadata
wf config field-values POD

# Get just the options (simpler view)
wf config field-options POD

# Test values without creating tickets
wf config test-field POD "test value"
```

## 🔧 **Step-by-Step Solution**

### **Step 1: Get Field Values**
```bash
wf config field-values POD
```
This will show you:
- Field ID and type
- All available option values 
- Option IDs if available
- Current default value (if set)

### **Step 2: Choose Working Value**
From the output, pick one of the available values that works for your Jira setup.

### **Step 3: Set as Default**
```bash
# Replace "Backend" with your chosen working value
wf config set-default POD "Your-Working-Value"
```

### **Step 4: Verify**
```bash
# Should now work automatically
wf start "New feature task"

# Override when needed
wf start "Special task" --POD="Different-Value"
```

## 🚀 **Example Workflow**

```bash
# 1. Discover what values work
wf config field-values POD
# Output shows: Backend, Frontend, Infrastructure, etc.

# 2. Set working default
wf config set-default POD "Backend"

# 3. Use for all new tasks
wf start "Feature A"     # Uses Backend automatically
wf start "Bug Fix B"     # Uses Backend automatically  
wf start "Feature C" --POD=Frontend  # Override when needed
```

## 💡 **Why This Approach Works**

✅ **No CLI field parsing** - Avoids confusion with defaults  
✅ **Direct Jira API access** - Gets actual field metadata  
✅ **Complete information** - Shows field type, options, IDs  
✅ **Test before use** - Verify values work before setting as defaults  
✅ **Simple override** - Just use --POD=value when you need something different  

## 🔍 **What You'll See**

The `wf config field-values POD` command will show you:

```
Field information for 'POD':
  ID: customfield_10373
  Type: option
  Available values (12):
    1. Backend (ID: 10001)
    2. Frontend (ID: 10002) 
    3. Infrastructure (ID: 10003)
    4. Platform (ID: 10004)
    5. Core (ID: 10005)
    ... (more values)
  Current default: 'Backend'  # (if you set one)
```

## 🎉 **Ready to Use!**

Once you pick a working value from this list and set it as default, the system will:

1. ✅ **Auto-apply defaults** on new ticket creation
2. ✅ **Respect project scope** (if configured for specific projects)
3. ✅ **Allow overrides** with `--POD=value` (if re-enabled later)
4. ✅ **Handle field types** automatically (option, string, number, etc.)

The complete solution is implemented - you just need to find the right value from the list!