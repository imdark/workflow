from typing import Dict, Any

from .base import Action, ActionType


class TaskAction(Action):
    """Action for task management (start/finish tasks)"""
    
    def __init__(self, action_id: str, action_subtype: str):
        super().__init__(action_id, 
                        ActionType.START_TASK if action_subtype == 'start' else ActionType.FINISH_TASK)
        self.action_subtype = action_subtype
    
    def execute(self, context: Dict[str, Any]) -> Dict[str, Any]:
        """Execute task action"""
        try:
            from workflow.state import get_current_task, save_current_task
            from workflow.backends import get_backend
            
            if self.action_subtype == 'start':
                task_key = context.get('task_key')
                task_title = context.get('task_title', '')
                task_desc = context.get('task_description', '')
                
                if not task_key:
                    return {'success': False, 'error': 'task_key is required'}
                
                backend = get_backend()
                if not backend:
                    return {'success': False, 'error': 'No backend configured'}
                
                issue = backend.get_issue(task_key)
                if not issue:
                    return {'success': False, 'error': f'Task {task_key} not found'}
                
                save_current_task(issue)
                
                self.logger.info(f"Started task: {task_key}")
                return {
                    'success': True,
                    'task_key': task_key,
                    'task_title': task_title or issue.title,
                    'action': 'started'
                }
                
            elif self.action_subtype == 'finish':
                current = get_current_task()
                if not current:
                    return {'success': False, 'error': 'No current task to finish'}
                
                save_current_task(None)
                
                self.logger.info(f"Finished task: {current.key}")
                return {
                    'success': True,
                    'task_key': current.key,
                    'task_title': current.title,
                    'action': 'finished'
                }
            
        except Exception as e:
            self.logger.error(f"Failed to {self.action_subtype} task: {e}")
            return {'success': False, 'error': str(e)}
    
    def validate_config(self, config: Dict[str, Any]) -> bool:
        """Validate task action configuration"""
        if self.action_subtype == 'start':
            return 'task_key' in config
        return True


from typing import Dict, Any
