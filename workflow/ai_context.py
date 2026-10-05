from workflow.memory import load_memory
from workflow.git_utils import get_repo
from workflow.rag import get_rag
from workflow.config import load_config


def get_ai_instructions(issue, repo_path=None):
    """Get AI instructions based on scope (global, project, repo, task)"""
    from workflow.projects import get_current_project
    
    cfg = load_config()
    ai_instructions = cfg.get("ai_instructions", {})
    
    instructions = []
    
    # Global/cross-project instructions
    if "global" in ai_instructions:
        instructions.append(ai_instructions["global"])
    
    # Project-specific instructions (current project)
    current_project = get_current_project()
    if current_project and "projects" in ai_instructions:
        if current_project in ai_instructions["projects"]:
            instructions.append(ai_instructions["projects"][current_project])
    
    # Repo-level instructions
    if repo_path and "repos" in ai_instructions:
        if repo_path in ai_instructions["repos"]:
            instructions.append(ai_instructions["repos"][repo_path])
    
    # Task-level instructions
    if issue and hasattr(issue, 'key') and "tasks" in ai_instructions:
        if issue.key in ai_instructions["tasks"]:
            instructions.append(ai_instructions["tasks"][issue.key])
    
    return instructions


def get_ai_skills(issue, repo_path=None):
    """Get AI skills based on scope (global, project, repo, task)"""
    from workflow.projects import get_current_project
    
    cfg = load_config()
    ai_skills = cfg.get("ai_skills", {})
    
    skills = []
    
    # Global/cross-project skills
    if "global" in ai_skills:
        for name, content in ai_skills["global"].items():
            skills.append({"name": name, "content": content, "scope": "global"})
    
    # Project-specific skills (current project)
    current_project = get_current_project()
    if current_project and "projects" in ai_skills:
        if current_project in ai_skills["projects"]:
            for name, content in ai_skills["projects"][current_project].items():
                skills.append({"name": name, "content": content, "scope": "project"})
    
    # Repo-level skills
    if repo_path and "repos" in ai_skills:
        if repo_path in ai_skills["repos"]:
            for name, content in ai_skills["repos"][repo_path].items():
                skills.append({"name": name, "content": content, "scope": "repo"})
    
    # Task-level skills
    if issue and hasattr(issue, 'key') and "tasks" in ai_skills:
        if issue.key in ai_skills["tasks"]:
            for name, content in ai_skills["tasks"][issue.key].items():
                skills.append({"name": name, "content": content, "scope": "task"})
    
    return skills


def build_context(issue, query: str = None, repo_path: str = None):
    """Build context using RAG for project information"""
    rag = get_rag()
    
    # Get AI instructions
    ai_instructions = get_ai_instructions(issue, repo_path)
    
    # Note: Skills are now passed via Claude's skills system (see claude.py provider)
    # Skills are dynamically created and linked to ~/.claude/skills/ during AI sessions
    
    # Start with task information
    context_parts = []
    
    # Add AI instructions at the top
    if ai_instructions:
        context_parts.append("""
CUSTOM INSTRUCTIONS:
""")
        for i, instruction in enumerate(ai_instructions, 1):
            context_parts.append(f"{i}. {instruction}")
        context_parts.append("")
    
    # Note: Skills are now passed via Claude's skills system (see claude.py provider)
    # Skills are dynamically created and linked to ~/.claude/skills/ during AI sessions
    
    context_parts.append(f"""
TASK: {issue.key}
TITLE: {issue.title}

DESCRIPTION:
{issue.description}
""")
    
    # Get relevant project context using RAG
    if query and query.strip():
        # Use the query to retrieve relevant context
        relevant_chunks = rag.retrieve_context(query, max_chunks=5)
        if relevant_chunks:
            context_parts.append(f"""
RELEVANT PROJECT CONTEXT:
{format_retrieved_context(relevant_chunks)}
""")
    else:
        # Get a minimal initial context
        context_parts.append("""
PROJECT CONTEXT:
You have access to a RAG system with project information. Ask for specific details about:
- project structure, files, dependencies, commands, testing, patterns, etc.
The system will retrieve relevant context based on your questions.
""")
    
    # Add memory summary (not full session - use summary only)
    mem = load_memory(issue)
    if mem['summary']:
        context_parts.append(f"""
MEMORY:
{mem['summary']}
""")
    
    # Agents used to stop once the code worked, leaving the branch unshipped.
    context_parts.append(f"""
WHEN YOU ARE DONE:
Once the implementation is complete and verified, ship it: commit all your
changes on this branch, then run `wf done --task {issue.key}`. That pushes the
branch, opens a PR and moves the task to Committed. Don't stop before this.
""")

    # Add MCP tools information
    context_parts.append("""
AVAILABLE MCP TOOLS:
You have access to wf-tools MCP server. If Claude Desktop is configured with the wf-tools MCP server, you can use these tools:
- get_current_task_info: Get the current active task
- list_configured_projects: List all configured projects
- get_current_project: Get the current active project
- list_git_branches: List available git branches
- get_current_git_branch: Get the current git branch
- list_assigned_tasks: List Jira tasks assigned to you
- list_hooks: List all configured hooks
- add_hook: Add a new hook (name, trigger, action, success_hook?, fail_hook?, condition?)
- remove_hook: Remove a hook
- execute_hook: Execute a specific hook
- list_actions: List all registered actions
- get_action_types: Get available action types
- test_notification: Send a test notification (title, message)
- get_wf_commands: Get list of available wf commands
- get_config_value: Get a configuration value
- set_config_value: Set a configuration value

Example usage: Use add_hook to create automation hooks for commands.
""")
    
    return "\n".join(context_parts)

def format_retrieved_context(chunks: list) -> str:
    """Format retrieved chunks for context"""
    if not chunks:
        return "No specific project context available."
    
    context_parts = []
    for chunk in chunks:
        context_parts.append(f"""
### {chunk['title']} (Category: {chunk['category']}, Relevance: {chunk.get('similarity_score', 0):.2f})
{chunk['content']}
""")
    
    return "\n".join(context_parts)
