# Background Workflow Service

A configurable background service for monitoring various sources with customizable hooks.

## Features

### 🎯 **Monitor Types**
- **GitHub PR Monitor**: Track commits in specific pull requests
- **File System Monitor**: Watch directories for file changes
- **API Monitor**: Poll APIs for data changes
- **Custom Monitor**: Extensible for new monitor types

### 🔧 **Configuration**
All monitors are configured via JSON files:
```bash
# Add a monitor from config file
./background_workflow.py add-monitor --file path/to/config.json

# Built-in monitor examples in examples/ folder:
- vscode_pr.json    - GitHub PR monitoring with hooks
- downloads_monitor.json - File system monitoring with webhook
- weather_api.json - API monitoring with command hooks
```

### 🔗 **Hook System**
Configurable hooks for various events:
- **Script Hook**: Execute shell scripts with environment data
- **Webhook Hook**: Send JSON data to HTTP endpoints  
- **Command Hook**: Run system commands

### 📱 **Notifications**
Integrates with existing workflow notifications:
- Desktop-notifier with inline reply fields
- Smart response handling (approve, comment, view, etc.)
- Configurable enable/disable per monitor

### 🚀 **Usage**

```bash
# Start the service (background daemon)
./pyobjc_env/bin/python background_workflow.py start --daemon

# Check status
./pyobjc_env/bin/python background_workflow.py status

# Add new monitor
./pyobjc_env/bin/python background_workflow.py add-monitor --file examples/vscode_pr.json

# Remove monitor
./pyobjc_env/bin/python background_workflow.py remove-monitor --name monitor_name

# Stop service
./pyobjc_env/bin/python background_workflow.py stop
```

### 📁 **Storage**
- Configuration: `~/.background_workflow_config.json`
- Logs: `/tmp/background_workflow.log`
- PID: `/tmp/background_workflow.pid`

The service runs continuously in the background and integrates with your existing notification system while supporting extensible monitoring capabilities.