from typing import Dict, Any, Optional
from .base import Action, ActionType


class BrowserAction(Action):
    """Action that performs browser automation via Chrome DevTools Protocol"""
    
    def __init__(self, action_id: str, action_subtype: str = "connect"):
        super().__init__(action_id, ActionType.BROWSER_OPEN)
        self.action_subtype = action_subtype
    
    def execute(self, context: Dict[str, Any]) -> Dict[str, Any]:
        """Execute browser action"""
        try:
            from workflow.browser_automation import (
                is_port_listening, get_websocket_url, 
                get_browser_daemon, DEFAULT_DEBUG_PORT
            )
            
            port = context.get('debug_port', DEFAULT_DEBUG_PORT)
            
            if self.action_subtype == "connect":
                if not is_port_listening(port):
                    return {
                        'success': False,
                        'error': f'Chrome debug port {port} is not listening. Please enable remote debugging in Chrome.',
                        'instructions': 'Open chrome://inspect/#remote-debugging and enable remote debugging, then retry.'
                    }
                
                daemon = get_browser_daemon()
                if daemon.start_daemon():
                    return {
                        'success': True,
                        'action': 'browser_connect',
                        'port': port,
                        'ws_url': daemon.ws_url
                    }
                else:
                    return {
                        'success': False,
                        'error': 'Failed to connect to Chrome debug port'
                    }
            
            elif self.action_subtype == "check":
                listening = is_port_listening(port)
                ws_url = get_websocket_url(port) if listening else None
                return {
                    'success': True,
                    'action': 'browser_check',
                    'port': port,
                    'listening': listening,
                    'ws_url': ws_url
                }
            
            else:
                return {'success': False, 'error': f'Unknown browser action subtype: {self.action_subtype}'}
                
        except Exception as e:
            self.logger.error(f"Failed to execute browser action: {e}")
            return {'success': False, 'error': str(e)}
    
    def validate_config(self, config: Dict[str, Any]) -> bool:
        """Validate browser action configuration"""
        return True
