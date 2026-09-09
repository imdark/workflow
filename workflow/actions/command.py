from typing import Dict, Any, Optional, List
from .base import Action, ActionType


class CustomCommandAction(Action):
    """Action for executing custom commands"""
    
    def __init__(self, action_id: str):
        super().__init__(action_id, ActionType.CUSTOM_COMMAND)
    
    def execute(self, context: Dict[str, Any]) -> Dict[str, Any]:
        """Execute custom command"""
        try:
            import subprocess
            import os
            
            command = context.get('command')
            if not command:
                return {'success': False, 'error': 'command is required'}
            
            working_dir = context.get('working_dir')
            env_vars = context.get('environment', {})
            
            process_env = os.environ.copy()
            process_env.update(env_vars)
            
            result = subprocess.run(
                command,
                shell=True,
                cwd=working_dir,
                env=process_env,
                capture_output=True,
                text=True,
                check=False
            )
            
            self.logger.info(f"Executed custom command: {command[:50]}...")
            
            return {
                'success': result.returncode == 0,
                'return_code': result.returncode,
                'stdout': result.stdout,
                'stderr': result.stderr,
                'command': command
            }
        except Exception as e:
            self.logger.error(f"Failed to execute custom command: {e}")
            return {'success': False, 'error': str(e)}
    
    def validate_config(self, config: Dict[str, Any]) -> bool:
        """Validate custom command configuration"""
        return 'command' in config


class AliasAction(Action):
    """Action that aliases other actions or command sequences"""
    
    def __init__(self, action_id: str):
        super().__init__(action_id, ActionType.ALIAS)
        self._action_registry = None
    
    def _get_action_registry(self):
        if self._action_registry is None:
            from .registry import get_action_registry
            self._action_registry = get_action_registry()
        return self._action_registry
    
    def execute(self, context: Dict[str, Any]) -> Dict[str, Any]:
        """Execute the aliased actions in sequence"""
        try:
            from .registry import create_action_from_config
            
            alias_config = context.get('alias_config', {})
            if not alias_config:
                return {'success': False, 'error': 'alias_config is required'}
            
            actions = alias_config.get('actions', [])
            if not actions:
                return {'success': False, 'error': 'alias must have actions'}
            
            results = []
            overall_success = True
            registry = self._get_action_registry()
            
            for i, action_ref in enumerate(actions):
                if isinstance(action_ref, str):
                    action_id = action_ref
                    action_context = context.copy()
                elif isinstance(action_ref, dict):
                    action_id = action_ref.get('id')
                    if not action_id:
                        results.append({
                            'step': i + 1,
                            'success': False,
                            'error': 'Action ID is required'
                        })
                        overall_success = False
                        continue
                    
                    action_context = {**context, **action_ref}
                else:
                    results.append({
                        'step': i + 1,
                        'success': False,
                        'error': f'Invalid action reference: {action_ref}'
                    })
                    overall_success = False
                    continue
                
                action = registry.get_action(action_id)
                if not action:
                    if isinstance(action_ref, dict):
                        action = create_action_from_config(action_ref)
                        if action:
                            registry.register_action(action)
                
                if not action:
                    results.append({
                        'step': i + 1,
                        'action_id': action_id,
                        'success': False,
                        'error': f"Action '{action_id}' not found"
                    })
                    overall_success = False
                    continue
                
                result = action.execute(action_context)
                result['step'] = i + 1
                result['action_id'] = action_id
                results.append(result)
                
                if not result.get('success', True) and alias_config.get('stop_on_failure', True):
                    self.logger.warning(f"Alias '{self.action_id}' stopped at step {i + 1} due to failure")
                    break
            
            return {
                'success': overall_success,
                'alias': self.action_id,
                'steps_executed': len(results),
                'results': results
            }
            
        except Exception as e:
            self.logger.error(f"Failed to execute alias action: {e}")
            return {'success': False, 'error': str(e)}
    
    def validate_config(self, config: Dict[str, Any]) -> bool:
        """Validate alias configuration"""
        alias_config = config.get('alias_config', {})
        actions = alias_config.get('actions', [])
        
        if not actions:
            return False
        
        for action_ref in actions:
            if isinstance(action_ref, str):
                continue
            elif isinstance(action_ref, dict):
                if not any(key in action_ref for key in ['id', 'type']):
                    return False
            else:
                return False
        
        return True
