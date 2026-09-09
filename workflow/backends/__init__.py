from workflow.backends.jira import JiraBackend
from workflow.backends.linear import LinearBackend
from workflow.backends.markdown import MarkdownBackend

# Backends that can be selected via the `task_backend` config key.
BACKEND_TYPES = ("jira", "linear", "markdown")

# Setup hints shown when a backend's configuration is incomplete.
_SETUP_HINTS = {
    "jira": [
        "💡 Run 'wf init' to set up Jira integration, or switch backends with:",
        "   wf task backend linear",
        "   wf task backend markdown",
    ],
    "linear": [
        "💡 Run 'wf config linear-token' to store a Linear API key",
        "   (create one at https://linear.app/<team-name>/settings/account/security/api-keys), then set your team:",
        "   wf config set linear.team <TEAM-KEY>",
    ],
}


def get_backend(cfg, force_type=None):
    """
    Get the appropriate task backend.

    Args:
        cfg: Configuration dictionary
        force_type: Force a specific backend type ('jira', 'linear' or 'markdown')

    Returns:
        TaskBackend instance or None if configuration is incomplete
    """
    # Only use 'task_backend' config key
    backend_type = force_type or cfg.get('task_backend', 'jira')

    if backend_type == 'markdown':
        return MarkdownBackend(cfg)

    classes = {'jira': JiraBackend, 'linear': LinearBackend}
    if backend_type in classes:
        try:
            return classes[backend_type](cfg)
        except KeyError as e:
            # Handle missing backend configuration gracefully
            from typer import echo
            echo(f"⚠️  {backend_type.capitalize()} configuration incomplete: {e}")
            for line in _SETUP_HINTS[backend_type]:
                echo(line)
            return None

    from typer import echo
    echo(f"⚠️  Unknown backend type: {backend_type}")
    echo(f"💡 Supported backends: {', '.join(BACKEND_TYPES)}")
    return None


def get_markdown_backend(cfg):
    """Convenience function to get the Markdown backend"""
    return MarkdownBackend(cfg)


def get_linear_backend(cfg):
    """Convenience function to get the Linear backend"""
    return LinearBackend(cfg)
