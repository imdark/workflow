"""
Backward compatibility shim for actions module.

All imports have been moved to workflow/actions/ package.
This file maintains backward compatibility for existing imports.
"""

from workflow.actions import (
    Action,
    ActionType,
    ActionRegistry,
    get_action_registry,
    create_action_from_config,
    NotificationAction,
    TaskAction,
    AIConsoleAction,
    TerminalAction,
    CustomCommandAction,
    AliasAction,
    BrowserAction,
    GitHubCommentLookupAction,
)

__all__ = [
    'Action',
    'ActionType',
    'ActionRegistry',
    'get_action_registry',
    'create_action_from_config',
    'NotificationAction',
    'TaskAction',
    'AIConsoleAction',
    'TerminalAction',
    'CustomCommandAction',
    'AliasAction',
    'BrowserAction',
    'GitHubCommentLookupAction',
]
