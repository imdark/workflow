"""
Actions module - provides a unified way to trigger various actions.

This module exports all action classes and the action registry.
For backward compatibility, imports from workflow.actions still work.
"""

from .base import Action, ActionType
from .registry import ActionRegistry, get_action_registry, create_action_from_config
from .notification import NotificationAction
from .task import TaskAction
from .ai import AIConsoleAction
from .terminal import TerminalAction
from .command import CustomCommandAction, AliasAction
from .browser import BrowserAction
from .github import GitHubCommentLookupAction

__all__ = [
    'Action', 'ActionType', 'ActionRegistry',
    'get_action_registry', 'create_action_from_config',
    'NotificationAction', 'TaskAction', 'AIConsoleAction',
    'TerminalAction', 'CustomCommandAction', 'AliasAction',
    'BrowserAction', 'GitHubCommentLookupAction'
]
