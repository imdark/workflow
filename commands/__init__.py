from commands.start import app as start_app
from commands.switch import app as switch_app
from commands.ai import app as ai_app
from commands.rag import app as rag_app
from commands.project import app as project_app
from commands.repo import app as repo_app
from commands.config import app as config_app
from commands.status import app as status_app

__all__ = [
    "start_app",
    "switch_app",
    "ai_app",
    "rag_app",
    "project_app",
    "repo_app",
    "config_app",
    "status_app",
]
