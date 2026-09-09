from workflow.backends.jira import JiraBackend
from workflow.backends.markdown import MarkdownBackend


def get_backend(cfg, force_type=None):
    """
    Get the appropriate task backend.

    Args:
        cfg: Configuration dictionary
        force_type: Force a specific backend type ('jira' or 'markdown')

    Returns:
        TaskBackend instance or None if configuration is incomplete
    """
    # Only use 'task_backend' config key
    backend_type = force_type or cfg.get('task_backend', 'jira')

    if backend_type == 'markdown':
        return MarkdownBackend(cfg)
    elif backend_type == 'jira':
        try:
            return JiraBackend(cfg)
        except KeyError as e:
            # Handle missing Jira configuration gracefully
            from typer import echo
            echo(f"⚠️  Jira configuration incomplete: {e}")
            echo("💡 Run 'wf init' to set up Jira integration, or use the Markdown backend with:")
            echo("   wf config set task_backend markdown")
            return None
    else:
        from typer import echo
        echo(f"⚠️  Unknown backend type: {backend_type}")
        echo("💡 Supported backends: jira, markdown")
        return None


def get_markdown_backend(cfg):
    """Convenience function to get the Markdown backend"""
    return MarkdownBackend(cfg)
