from typing import Dict, Any, Optional, List, Callable
import logging


class ActionRegistry:
    """Registry for managing available actions"""
    
    def __init__(self):
        self.actions: Dict[str, Any] = {}
        self._action_creators: Dict[str, Callable] = {}
        self._register_builtin_actions()
    
    def _register_builtin_actions(self):
        """Register built-in action creators"""
        from .notification import NotificationAction
        from .task import TaskAction
        from .ai import AIConsoleAction
        from .terminal import TerminalAction
        from .command import CustomCommandAction, AliasAction
        from .browser import BrowserAction
        from .github import GitHubCommentLookupAction
        from .base import ActionType
        
        self._action_creators = {
            ActionType.NOTIFICATION: lambda action_id: NotificationAction(action_id),
            ActionType.START_TASK: lambda action_id: TaskAction(action_id, 'start'),
            ActionType.FINISH_TASK: lambda action_id: TaskAction(action_id, 'finish'),
            ActionType.OPEN_AI_CONSOLE: lambda action_id: AIConsoleAction(action_id),
            ActionType.OPEN_TERMINAL: lambda action_id: TerminalAction(action_id),
            ActionType.CUSTOM_COMMAND: lambda action_id: CustomCommandAction(action_id),
            ActionType.ALIAS: lambda action_id: AliasAction(action_id),
            ActionType.BROWSER_OPEN: lambda action_id: BrowserAction(action_id, 'open'),
            ActionType.BROWSER_CLICK: lambda action_id: BrowserAction(action_id, 'click'),
            ActionType.BROWSER_TYPE: lambda action_id: BrowserAction(action_id, 'type'),
            ActionType.GITHUB_COMMENT_LOOKUP: lambda action_id: GitHubCommentLookupAction(action_id),
        }
    
    def register_action(self, action: Any):
        """Register a new action"""
        self.actions[action.action_id] = action
        self.logger.info(f"Registered action: {action.action_id} ({action.action_type.value})")
    
    def get_action(self, action_id: str) -> Optional[Any]:
        """Get action by ID"""
        return self.actions.get(action_id)
    
    def create_action(self, action_id: str, action_type: Any) -> Optional[Any]:
        """Create and register a new action of the given type"""
        if action_type in self._action_creators:
            action = self._action_creators[action_type](action_id)
            self.register_action(action)
            return action
        return None
    
    def list_actions(self) -> List[Dict[str, str]]:
        """List all registered actions"""
        return [
            {
                'action_id': action_id,
                'type': action.action_type.value
            }
            for action_id, action in self.actions.items()
        ]
    
    def execute_action(self, action_id: str, context: Dict[str, Any]) -> Dict[str, Any]:
        """Execute an action by ID"""
        action = self.get_action(action_id)
        if not action:
            return {
                'success': False,
                'error': f"Action '{action_id}' not found"
            }
        
        return action.execute(context)
    
    @property
    def logger(self):
        """Get logger instance"""
        if not hasattr(self, '_logger'):
            self._logger = logging.getLogger('action_registry')
        return self._logger


_action_registry: Optional[ActionRegistry] = None


def get_action_registry() -> ActionRegistry:
    """Get or create the global action registry instance"""
    global _action_registry
    if _action_registry is None:
        _action_registry = ActionRegistry()
    return _action_registry


def create_action_from_config(config: Dict[str, Any]) -> Optional[Any]:
    """Create an action from configuration dictionary"""
    from .base import ActionType
    
    action_id = config.get('id')
    action_type_str = config.get('type')
    
    if not action_id or not action_type_str:
        return None
    
    try:
        action_type = ActionType(action_type_str)
        registry = get_action_registry()
        action = registry.create_action(action_id, action_type)
        
        if action and not action.validate_config(config):
            registry.logger.error(f"Invalid configuration for action {action_id}")
            return None
        
        return action
    except ValueError:
        registry = get_action_registry()
        registry.logger.error(f"Unknown action type: {action_type_str}")
        return None
