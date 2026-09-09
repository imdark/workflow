# POD Field Value Testing

## 🎯 **Test These POD Values**

Try these common values for the POD field:

```bash
# Test each one until it works
wf start TEST-123 --POD=Backend
wf start TEST-123 --POD=Frontend  
wf start TEST-123 --POD=Core
wf start TEST-123 --POD=Infrastructure
wf start TEST-123 --POD=Platform
wf start TEST-123 --POD=backend    # Lowercase
wf start TEST-123 --POD=CORE        # Uppercase
```

## 🔄 **Value Translation Solution**

Instead of complex mapping, once you find a working value, you can set up a simple script or alias:

### **Option 1: Simple Script**
Create a script called `set-pod-default.sh`:
```bash
#!/bin/bash
# Set POD field to "Backend" Jira equivalent
wf config set-default POD "Backend"
```

### **Option 2: Use the Working Value**
Once you find the value that works, just use it:
```bash
# If "Backend" works:
wf config set-default POD "Backend"

# Use it in daily work:
wf start "New feature"  # Will use Backend as default
```

## 🚀 **Recommended Approach**

1. **Test Manually First:**
   ```bash
   wf start TEST-POD --POD=Backend
   ```

2. **If It Works, Set as Default:**
   ```bash
   wf config set-default POD "Backend"
   ```

3. **Use for All Future Tasks:**
   ```bash
   wf start "Any task description"  # Auto-applies Backend
   ```

## 💡 **Most Likely Working Values**

Based on common Jira setups, try these in order:
1. `Backend` (most common)
2. `Frontend`
3. `Core`
4. `Infrastructure`
5. `Platform`

## 🎯 **Current Status**

✅ **System Ready:** Custom field defaults system implemented  
✅ **Feature Working:** Field parsing and default application  
✅ **Need:** Correct Jira value for POD field  

Once you find the working value, the system will work perfectly with automatic defaults!