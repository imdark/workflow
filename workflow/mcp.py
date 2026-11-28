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


def _task_key_or_current(task_key):
    if task_key:
        return task_key
    task = get_current_task()
    return getattr(task, "key", None)


@mcp.tool()
def search_task_facts(
    query: Annotated[str, "Words the fact must contain; empty lists every fact"] = "",
    task_key: Annotated[str, "Task key (optional, defaults to the current task)"] = None,
    limit: Annotated[int, "Maximum facts to return"] = 20,
) -> str:
    """Search durable facts (decisions, constraints, preferences) recorded for a task"""
    from workflow.recall import search_facts
    try:
        hits = search_facts(query, _task_key_or_current(task_key), limit=limit)
    except Exception as e:
        return f"Error: {str(e)}"
    if not hits:
        return "No matching facts"
    return json.dumps(hits, indent=2, ensure_ascii=False)


@mcp.tool()
def search_past_conversations(
    query: Annotated[str, "Text to search for"],
    task_key: Annotated[str, "Restrict to one task (optional; omit to search every task)"] = None,
    limit: Annotated[int, "Maximum hits to return"] = 10,
) -> str:
    """Full-text search over earlier captured agent sessions"""
    from workflow.recall import search_conversations
    try:
        hits = search_conversations(query, task_key, limit=limit)
    except Exception as e:
        return f"Error: {str(e)}"
    if not hits:
        return f"No matches for '{query}'"
    return json.dumps(hits, indent=2, ensure_ascii=False)


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
def create_task(
    title: Annotated[str, "Task title"],
    description: Annotated[str, "What and why"] = "",
    project: Annotated[str, "wf project to file it in (optional, defaults to the session's)"] = None,
) -> str:
    """Create a task in the project's own backend, as `wf task create` does, and list it under the project in
    today's NotesGraph journal when called from a wf ai session"""
    import shutil
    import sys

    wf = shutil.which("wf")
    command = [wf] if wf else [sys.executable, "-m", "workflow.cli"]
    command += ["task", "create", title, "--description", description]
    if project:
        command += ["--project", project]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.SubprocessError) as e:
        return f"Error: {e}"
    output = (result.stdout + result.stderr).strip()
    return output if result.returncode == 0 else f"Error: {output}"


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


@mcp.tool()
def lint(
    repo_path: Annotated[str, "Path inside the git repository to lint"],
    all_files: Annotated[bool, "Lint the whole repository instead of the files the branch changed"] = False,
    fix: Annotated[bool, "Run each step's fix command where it has one"] = False,
    base: Annotated[str, "Branch to diff against (default: the remote's default branch)"] = None,
) -> str:
    """Run the repository's lint on the files the branch changed, before committing.

    The steps come from wf.yaml `lint:` at the repository root (else a `lint`
    script in package.json). Returns JSON: `ok` overall, and per step its
    name, ok, skipped, exit_code, files and the tail of its output.
    """
    from workflow.lint import LintError, lint_repo
    try:
        results = lint_repo(repo_path, base=base, all_files=all_files, fix=fix)
    except LintError as e:
        return f"Error: {e}"
    return json.dumps({"ok": all(r["ok"] for r in results), "steps": results}, indent=2)


def _pr_tool(fn, **kwargs) -> str:
    from workflow.pr import PrError
    try:
        return json.dumps(fn(**kwargs), indent=2)
    except PrError as e:
        return f"Error: {e}"


_PR = "A number, URL, branch or task key (PERSONAL-12); omit for the current branch's pull request"
_REPO_PATH = "Path inside the git repository"
_REPO = "owner/name, when not the repository at repo_path"


@mcp.tool()
def pr_status(
    repo_path: Annotated[str, _REPO_PATH],
    pr: Annotated[str, _PR] = None,
    repo: Annotated[str, _REPO] = None,
) -> str:
    """Where a pull request stands, in one call instead of several `gh pr view`s.

    Returns JSON: state, draft, base and head, mergeable, review decision,
    merge commit, size, `checks` (counts, with the failing and pending ones
    named), `task` (the task it is for, from the branch or title: key,
    title, status, and its NotesGraph note block) and a one-line `verdict`
    (e.g. "ready to merge", "not ready: 2 checks failing").
    """
    from workflow.pr import pr_summary
    return _pr_tool(pr_summary, repo_path=repo_path, pr=pr, repo=repo)


@mcp.tool()
def pr_list(
    repo_path: Annotated[str, _REPO_PATH],
    state: Annotated[str, "open, closed, merged or all"] = "open",
    mine: Annotated[bool, "Only pull requests the signed-in user opened"] = False,
    search: Annotated[str, "GitHub search terms"] = None,
    limit: Annotated[int, "How many to list"] = 30,
    repo: Annotated[str, _REPO] = None,
) -> str:
    """The repository's pull requests, each with its checks counted and the task it is for."""
    from workflow.pr import pr_list as _pr_list
    return _pr_tool(_pr_list, repo_path=repo_path, state=state, mine=mine, search=search,
                    limit=limit, repo=repo)


@mcp.tool()
def pr_checks(
    repo_path: Annotated[str, _REPO_PATH],
    pr: Annotated[str, _PR] = None,
    logs: Annotated[bool, "Fetch the end of each failing job's log"] = True,
    repo: Annotated[str, _REPO] = None,
) -> str:
    """Why a pull request's checks fail: each failing job, the step that failed, and the end of its log."""
    from workflow.pr import pr_checks as _pr_checks
    return _pr_tool(_pr_checks, repo_path=repo_path, pr=pr, repo=repo, logs=logs)


@mcp.tool()
def ci_runs(
    repo_path: Annotated[str, _REPO_PATH],
    branch: Annotated[str, "Only runs of this branch"] = None,
    workflow: Annotated[str, "Only this workflow (its name or file)"] = None,
    commit: Annotated[str, "Only runs of this commit"] = None,
    limit: Annotated[int, "How many to list"] = 10,
    repo: Annotated[str, _REPO] = None,
) -> str:
    """Recent GitHub Actions runs (builds, deploys), newest first, with status and conclusion."""
    from workflow.pr import ci_runs as _ci_runs
    return _pr_tool(_ci_runs, repo_path=repo_path, branch=branch, workflow=workflow, commit=commit,
                    limit=limit, repo=repo)


@mcp.tool()
def ci_workflows(
    repo_path: Annotated[str, _REPO_PATH],
    repo: Annotated[str, _REPO] = None,
) -> str:
    """The repository's CI workflows (what runs are made from): file, enabled or not, what starts each, and its last run."""
    from workflow.pr import ci_workflows as _ci_workflows
    return _pr_tool(_ci_workflows, repo_path=repo_path, repo=repo)


def _runs_tool(fn, **kwargs) -> str:
    from workflow.agent_runs import RunsError
    from workflow.deploy.registry.notesgraph import NotesGraphError
    try:
        return json.dumps(fn(**kwargs), indent=2)
    except (RunsError, NotesGraphError) as e:
        return f"Error: {e}"


@mcp.tool()
def agent_runs(
    status: Annotated[str, "queued, running, done, error or cancelled"] = None,
    device: Annotated[str, "Only runs on this device (cloud, prod, a laptop's key)"] = None,
    kind: Annotated[str, "agent or automation"] = None,
    agent: Annotated[str, "Only runs whose agent name or title contains this text"] = None,
    since: Annotated[str, "Ended within 30m, 24h, 7d, 2w, or since an ISO date"] = None,
    limit: Annotated[int, "How many to list"] = 20,
) -> str:
    """NotesGraph agent and automation runs on every device, newest first: status, device, agent, time, cost, error. No prompts or results."""
    from workflow.agent_runs import agent_runs as _agent_runs
    return _runs_tool(_agent_runs, status=status, device=device, kind=kind, agent=agent, since=since, limit=limit)


@mcp.tool()
def agent_templates() -> str:
    """What NotesGraph runs are started from: ready-made automation templates, the saved automations with their schedule, and the agents recent runs used."""
    from workflow.agent_runs import agent_templates as _agent_templates
    return _runs_tool(_agent_templates)


@mcp.tool()
def ci_run(
    repo_path: Annotated[str, _REPO_PATH],
    run_id: Annotated[int, "The run's id, as ci_runs lists it"],
    logs: Annotated[bool, "Fetch the end of each failed job's log"] = True,
    repo: Annotated[str, _REPO] = None,
) -> str:
    """One Actions run: its jobs, and for each failed job the step that failed and the end of its log."""
    from workflow.pr import ci_run as _ci_run
    return _pr_tool(_ci_run, run_id=run_id, repo_path=repo_path, repo=repo, logs=logs)


@mcp.tool()
def pr_review_context(
    repo_path: Annotated[str, _REPO_PATH],
    pr: Annotated[str, _PR] = None,
    repo: Annotated[str, _REPO] = None,
) -> str:
    """Everything a review of a pull request reads, gathered once.

    The pr_status fields, plus the task's full description, the pull
    request's description, reviews and comments in order, the changed files
    and the diff (cut at 60,000 characters; `diff_truncated` says so).
    """
    from workflow.pr import pr_review
    return _pr_tool(pr_review, repo_path=repo_path, pr=pr, repo=repo)


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
