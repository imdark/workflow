# Custom Field Default Troubleshooting

## 🚨 **Option Field Error**

The error `Specify a valid 'id' or 'name' for POD` indicates that the default value we set isn't a valid option for that field.

## 🔧 **Solution: Use Exact Jira Field Values**

For option-type fields, you must use the **exact value** that Jira recognizes, not the display name.

### **Find Valid Values:**

1. **Check Jira UI:**
   - Go to any existing issue with the POD field
   - Click on the field to see available options
   - Use the exact text shown in the dropdown

2. **Use API (if available):**
   ```bash
   # Test with suspected values
   wf config set-default POD "Backend Team"
   wf start TEST-123  # See if it works
   ```

3. **Try Common Patterns:**
   ```bash
   # Try variations until you find the right one
   wf config set-default POD backend
   wf config set-default POD Backend
   wf config set-default POD "Backend Team"
   wf config set-default POD "backend"
   ```

## 🎯 **Quick Fix Example**

```bash
# Clear the current incorrect default
wf config clear-default POD

# Try the correct value (example - replace with your actual option)
wf config set-default POD backend

# Test it
wf start TEST-TICKET-123
```

## 📋 **Common Field Value Patterns**

| Field Type | Common Value Format | Example |
|-------------|-------------------|----------|
| **Team POD** | lowercase team name | `backend`, `frontend`, `infrastructure` |
| **Priority** | Capitalized priority name | `High`, `Medium`, `Low` |
| **Status** | Status name | `To Do`, `In Progress`, `Done` |

## 🔍 **Debugging Steps**

1. **Test manually:**
   ```bash
   # Create a task and specify the value directly
   wf start TEST-123 --POD=backend
   ```

2. **If it works, set as default:**
   ```bash
   wf config set-default POD backend
   ```

3. **Verify configuration:**
   ```bash
   wf config list-fields
   # Should show: POD: customfield_10373 (default: backend)
   ```

## 💡 **Best Practice**

**Test field values manually first** before setting as defaults:

```bash
# Step 1: Test manually
wf start TEST-MANUAL --POD=backend

# Step 2: If successful, set as default  
wf config set-default POD backend

# Step 3: Verify
wf start TEST-AUTO  # Should work automatically
```

This approach ensures you only set defaults that work with your specific Jira field configuration!