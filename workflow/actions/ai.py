from typing import Dict, Any

from .base import Action, ActionType


class AIConsoleAction(Action):
    """Action for opening AI console"""
    
    def __init__(self, action_id: str):
        super().__init__(action_id, ActionType.OPEN_AI_CONSOLE)
    
    def execute(self, context: Dict[str, Any]) -> Dict[str, Any]:
        """Open AI console"""
        try:
            from workflow.state import get_current_task
            from workflow.ai import launch_ai_session
            
            current_task = get_current_task()
            launch_ai_session(current_task)
            
            task_key = current_task.key if current_task else 'no-task'
            self.logger.info(f"Opened AI console for task: {task_key}")
            
            return {
                'success': True,
                'task_key': task_key,
                'action': 'ai_console_opened'
            }
        except Exception as e:
            self.logger.error(f"Failed to open AI console: {e}")
            return {'success': False, 'error': str(e)}
    
    def validate_config(self, config: Dict[str, Any]) -> bool:
        """AI console doesn't require additional configuration"""
        return True


from typing import Dict, Any
