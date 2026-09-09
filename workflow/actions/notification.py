from typing import Dict, Any

from .base import Action, ActionType


class NotificationAction(Action):
    """Action for sending desktop notifications"""
    
    def __init__(self, action_id: str):
        super().__init__(action_id, ActionType.NOTIFICATION)
        self.notification_service = None
    
    def _get_notification_service(self):
        if self.notification_service is None:
            from workflow.notifications import get_notification_service
            self.notification_service = get_notification_service()
        return self.notification_service
    
    def execute(self, context: Dict[str, Any]) -> Dict[str, Any]:
        """Send desktop notification"""
        try:
            title = self._substitute_variables(context.get('title', 'Workflow Notification'), context)
            message = self._substitute_variables(context.get('message', ''), context)
            response_options = context.get('response_options')
            notification_id = context.get('notification_id')
            url = context.get('url')
            
            response_handler = None
            if url:
                def open_url(response):
                    import subprocess
                    import webbrowser
                    try:
                        webbrowser.open(url)
                        self.logger.info(f"Opened URL: {url}")
                    except Exception as e:
                        self.logger.error(f"Failed to open URL {url}: {e}")
                        try:
                            subprocess.run(['open', url], check=True)
                        except:
                            pass
                
                response_handler = open_url
                if not response_options:
                    response_options = ["Open"]
            
            result_id = self._get_notification_service().send_notification(
                title=title,
                message=message,
                response_options=response_options,
                response_handler=response_handler,
                notification_id=notification_id
            )
            
            self.logger.info(f"Notification sent: {title}")
            return {
                'success': True,
                'notification_id': result_id,
                'title': title,
                'message': message,
                'url': url
            }
        except Exception as e:
            self.logger.error(f"Failed to send notification: {e}")
            return {
                'success': False,
                'error': str(e)
            }
    
    def _substitute_variables(self, text: str, context: Dict[str, Any]) -> str:
        """Substitute ${variable} placeholders with values from context"""
        if not text or not isinstance(text, str):
            return text
        
        import re
        
        def replace_var(match):
            var_name = match.group(1)
            value = context.get(var_name, '')
            if isinstance(value, str) and len(value) > 100:
                value = value[:97] + '...'
            return str(value)
        
        return re.sub(r'\$\{([^}]+)\}', replace_var, text)
    
    def validate_config(self, config: Dict[str, Any]) -> bool:
        """Validate notification configuration"""
        required_fields = ['title']
        return all(field in config for field in required_fields)
