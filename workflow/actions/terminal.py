from typing import Dict, Any

from .base import Action, ActionType


class TerminalAction(Action):
    """Action for opening terminal tabs"""
    
    def __init__(self, action_id: str):
        super().__init__(action_id, ActionType.OPEN_TERMINAL)
    
    def execute(self, context: Dict[str, Any]) -> Dict[str, Any]:
        """Open terminal tabs"""
        try:
            from workflow.config import get_config
            from workflow.state import get_current_task
            from workflow.terminal import open_terminal_tabs
            
            config = get_config()
            current_task = get_current_task()
            
            repos = config.get('repositories', {})
            task_key = current_task.key if current_task else 'no-task'
            
            open_terminal_tabs(repos, task_key)
            
            self.logger.info(f"Opened terminal tabs for {len(repos)} repositories")
            
            return {
                'success': True,
                'repositories': list(repos.keys()),
                'task_key': task_key,
                'action': 'terminal_opened'
            }
        except Exception as e:
            self.logger.error(f"Failed to open terminal: {e}")
            return {'success': False, 'error': str(e)}
    
    def validate_config(self, config: Dict[str, Any]) -> bool:
        """Terminal action doesn't require additional configuration"""
        return True
