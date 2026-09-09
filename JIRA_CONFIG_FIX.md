# Quick Fix for Jira Configuration

The issue is that `wf init` starts interactive setup but may have issues with terminal prompts. Here's a quick manual fix:

## 🔧 **Manual Configuration Setup**

Instead of using `wf init`, create the Jira config manually:

```bash
# Create the directory
mkdir -p ~/.wf

# Create the config file
cat > ~/.wf/config.yaml << 'EOF'
backend: jira
jira:
  url: "https://your-domain.atlassian.net"
  email: "you@example.com"
  token: "YOUR_JIRA_TOKEN_HERE"
  project: "PROJ"
git_enabled: true
github_enabled: true
custom_fields:
  POD:
    field_id: customfield_10373
    default_value: Backend
git:
  base_branch: development
EOF

echo "✅ Manual configuration created at ~/.wf/config.yaml"
echo "💡 Edit the file and add your actual Jira token"
```

## 🚀 **After Setup**

1. **Replace the token** in `~/.wf/config.yaml` with your actual Jira API token
2. **Test the configuration:**
   ```bash
   wf config list-fields     # Should show POD field
   wf status              # Should work without errors
   ```

## 🎯 **Then Use Field Values**

Once Jira is configured, you can:

```bash
# Get all valid POD values
wf config field-values POD

# Test values safely
wf config test-field POD "Backend"

# Set working default
wf config set-default POD "Working-Value"

# Use in daily work
wf start "New task"     # Auto-applies default
```

## 📋 **Alternative: Fix Interactive Issues**

If you prefer to use `wf init` but it's hanging, you can provide answers inline:

```bash
# Non-interactive setup (works around hanging prompts)
echo "y" | wf init --no-interactive
```

The field defaults system will work perfectly once Jira is properly configured!