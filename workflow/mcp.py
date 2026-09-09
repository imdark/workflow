from fastmcp import FastMCP
from typing import Annotated
import json
import subprocess
from workflow.actions import get_action_registry, ActionType
from workflow.hooks import HookManager
from workflow.state import get_current_task, set_current_task
from workflow.config import load_config, save_config, get_custom_fields
from workflow.projects import get_current_project, list_projects
from workflow.git_utils import get_available_branches
from workflow.backends import get_backend

mcp = FastMCP("wf-tools")


@mcp.tool()
def get_current_task_info() -> str:
    """Get the current active task"""
    task = get_current_task()
    if task and hasattr(task, 'key'):
        return json.dumps({
            "key": task.key,
            "title": task.title,
            "description": getattr(task, 'description', '')
        }, indent=2)
    return "No current task set"


@mcp.tool()
def list_configured_projects() -> str:
    """List all configured projects"""
    projects = list_projects()
    if not projects:
        return "No projects configured"
    return json.dumps(projects, indent=2)


@mcp.tool()
def get_current_project() -> str:
    """Get the current active project"""
    project = get_current_project()
    if project:
        return project
    return "No current project set"


@mcp.tool()
def get_custom_fields_list() -> str:
    """List all configured custom fields"""
    fields = get_custom_fields()
    if not fields:
        return "No custom fields configured"
    return json.dumps(fields, indent=2)


@mcp.tool()
def list_git_branches(repo_path: Annotated[str, "Repository path (optional, defaults to current directory)"] = None) -> str:
    """List available git branches"""
    try:
        branches = get_available_branches(repo_path)
        return json.dumps(branches, indent=2)
    except Exception as e:
        return f"Error: {str(e)}"


@mcp.tool()
def get_current_git_branch(repo_path: Annotated[str, "Repository path (optional)"] = None) -> str:
    """Get the current git branch"""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True,
            text=True,
            cwd=repo_path
        )
        if result.returncode == 0:
            return result.stdout.strip()
        return "Not in a git repository"
    except Exception as e:
        return f"Error: {str(e)}"


@mcp.tool()
def list_assigned_tasks() -> str:
    """List tasks assigned to the current user from Jira"""
    try:
        cfg = load_config()
        backend = get_backend(cfg)
        tasks = backend.get_assigned_tasks()
        task_list = []
        for t in tasks[:20]:
            task_list.append({
                "key": t.key,
                "title": t.title,
                "status": getattr(t, 'status', 'Unknown')
            })
        return json.dumps(task_list, indent=2)
    except Exception as e:
        return f"Error: {str(e)}"


@mcp.tool()
def list_hooks() -> str:
    """List all configured hooks"""
    try:
        hm = HookManager()
        hooks = hm.list_hooks()
        if not hooks:
            return "No hooks configured"
        return json.dumps(hooks, indent=2)
    except Exception as e:
        return f"Error: {str(e)}"


@mcp.tool()
def add_hook(
    name: Annotated[str, "Name of the hook"],
    trigger: Annotated[str, "Trigger command (e.g., 'wf start')"],
    action: Annotated[str, "Action to execute"],
    success_hook: Annotated[str, "Hook to run on success (optional)"] = None,
    fail_hook: Annotated[str, "Hook to run on failure (optional)"] = None,
    condition: Annotated[str, "Condition to evaluate (optional)"] = None
) -> str:
    """Add a new hook"""
    try:
        hm = HookManager()
        result = hm.add_hook(name, trigger, action, success_hook, fail_hook, condition=condition)
        if result:
            return f"✅ Hook '{name}' added successfully"
        return f"❌ Hook '{name}' already exists"
    except Exception as e:
        return f"Error: {str(e)}"


@mcp.tool()
def remove_hook(name: Annotated[str, "Name of the hook to remove"]) -> str:
    """Remove a hook"""
    try:
        hm = HookManager()
        result = hm.remove_hook(name)
        if result:
            return f"✅ Hook '{name}' removed successfully"
        return f"❌ Hook '{name}' not found"
    except Exception as e:
        return f"Error: {str(e)}"


@mcp.tool()
def execute_hook(name: Annotated[str, "Name of the hook to execute"]) -> str:
    """Execute a specific hook"""
    try:
        hm = HookManager()
        result = hm.execute_hook(name)
        return json.dumps(result, indent=2)
    except Exception as e:
        return f"Error: {str(e)}"


@mcp.tool()
def list_actions() -> str:
    """List all registered actions"""
    try:
        registry = get_action_registry()
        actions = registry.list_actions()
        if not actions:
            return "No actions registered"
        return json.dumps(actions, indent=2)
    except Exception as e:
        return f"Error: {str(e)}"


@mcp.tool()
def get_action_types() -> str:
    """Get available action types"""
    action_types = {
        "notification": "Send desktop notifications",
        "start_task": "Start working on a task",
        "finish_task": "Complete current task",
        "open_ai_console": "Launch AI console",
        "open_terminal": "Open terminal tabs",
        "custom_command": "Execute custom shell commands",
        "alias": "Create alias that triggers multiple actions"
    }
    return json.dumps(action_types, indent=2)


@mcp.tool()
def test_notification(
    title: Annotated[str, "Notification title"] = "Test",
    message: Annotated[str, "Notification message"] = "This is a test"
) -> str:
    """Test sending a notification"""
    try:
        from workflow.actions import get_action_registry, ActionType
        registry = get_action_registry()
        action = registry.create_action("test_notification", ActionType.NOTIFICATION)
        result = action.execute({
            "title": title,
            "message": message,
            "notification_id": "mcp_test"
        })
        if result.get('success'):
            return "✅ Notification sent"
        return f"❌ Failed: {result.get('error')}"
    except Exception as e:
        return f"Error: {str(e)}"


@mcp.tool()
def get_wf_commands() -> str:
    """Get list of available wf commands"""
    commands = {
        "wf start <task>": "Start working on a task",
        "wf status": "Show current task status",
        "wf switch <task>": "Switch to another task",
        "wf ai": "Start AI assistant session",
        "wf config": "Manage configuration",
        "wf project": "Manage projects",
        "wf action": "Manage actions",
        "wf hook": "Manage hooks (via HookManager)",
        "wf variables": "Manage variables",
        "wf repo": "Repository management"
    }
    return json.dumps(commands, indent=2)


@mcp.tool()
def get_config_value(key: Annotated[str, "Config key to retrieve"]) -> str:
    """Get a configuration value"""
    try:
        cfg = load_config()
        value = cfg.get(key)
        if value is None:
            return f"Key '{key}' not found"
        return json.dumps(value, indent=2)
    except Exception as e:
        return f"Error: {str(e)}"


@mcp.tool()
def set_config_value(
    key: Annotated[str, "Config key to set"],
    value: Annotated[str, "Value to set (will be parsed as JSON if possible)"]
) -> str:
    """Set a configuration value"""
    try:
        cfg = load_config()
        # Try to parse as JSON
        try:
            parsed_value = json.loads(value)
        except json.JSONDecodeError:
            parsed_value = value
        
        cfg[key] = parsed_value
        save_config(cfg)
        return f"✅ Set {key} = {value}"
    except Exception as e:
        return f"Error: {str(e)}"


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "install":
        print("Installing wf-tools MCP server...")
        print("Add this to your MCP config:")
        print(json.dumps({
            "mcpServers": {
                "wf-tools": {
                    "command": "python",
                    "args": [__file__]
                }
            }
        }, indent=2))
    else:
        mcp.run()
