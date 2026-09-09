from pathlib import Path
import yaml
import subprocess
import typer
from typing import Dict, List, Optional, Any, Union
import json
import os
import re

class HookManager:
    def __init__(self, config_path: Path = None):
        if config_path is None:
            config_path = Path.home() / ".wf" / "hooks.yaml"
        self.config_path = config_path
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        self._hooks = self._load_hooks()
    
    def _load_hooks(self) -> Dict:
        """Load hooks from config file"""
        if self.config_path.exists():
            with open(self.config_path, 'r') as f:
                return yaml.safe_load(f) or {}
        return {}
    
    def _save_hooks(self):
        """Save hooks to config file"""
        with open(self.config_path, 'w') as f:
            yaml.dump(self._hooks, f, default_flow_style=False)
    
    def add_hook(self, name: str, trigger: str, action: str, 
                 success_hook: str = None, fail_hook: str = None,
                 output_var: str = None, condition: str = None,
                 pass_output: bool = True, environment: Dict[str, str] = None,
                 shell_cmd: str = None, folder: str = None):
        """Add a new hook"""
        if name in self._hooks:
            typer.echo(f"❌ Hook '{name}' already exists. Use 'wf hook remove' first.")
            return False
        
        hook_config = {
            'trigger': trigger,
            'action': action,
            'pass_output': pass_output,
            'output_var': output_var or f"{name.upper()}_OUTPUT"
        }
        
        if success_hook:
            hook_config['success_hook'] = success_hook
        if fail_hook:
            hook_config['fail_hook'] = fail_hook
        if condition:
            hook_config['condition'] = condition
        if environment:
            hook_config['environment'] = environment
        if shell_cmd:
            hook_config['shell_cmd'] = shell_cmd
        if folder:
            hook_config['folder'] = folder
        
        self._hooks[name] = hook_config
        self._save_hooks()
        return True
    
    def get_hook(self, name: str) -> Optional[Dict]:
        """Get hook by name"""
        return self._hooks.get(name)
    
    def list_hooks(self) -> Dict:
        """List all hooks"""
        return self._hooks
    
    def remove_hook(self, name: str) -> bool:
        """Remove a hook"""
        if name in self._hooks:
            del self._hooks[name]
            self._save_hooks()
            return True
        return False
    
    def _execute_command(self, command: str, cwd: str = None, 
                        env: Dict[str, str] = None, input_data: str = "") -> Dict[str, Any]:
        """Execute a command and return result"""
        work_dir = cwd or str(Path.cwd())
        
        # Prepare environment
        process_env = os.environ.copy()
        if env:
            process_env.update(env)
        
        try:
            # Handle complex commands with cd, &&, etc.
            if 'cd ' in command or '&&' in command or ';' in command:
                bash_script = f'#!/bin/bash\nset -e\ncd "{work_dir}"\n{command}'
                result = subprocess.run(
                    bash_script,
                    shell=True,
                    executable="/bin/bash",
                    cwd=str(Path.home()),
                    env=process_env,
                    input=input_data or "",
                    text=True,
                    capture_output=True,
                    check=False
                )
            else:
                result = subprocess.run(
                    command,
                    shell=True,
                    cwd=work_dir,
                    env=process_env,
                    input=input_data or "",
                    text=True,
                    capture_output=True,
                    check=False
                )
            
            return {
                'success': result.returncode == 0,
                'return_code': result.returncode,
                'stdout': result.stdout.strip(),
                'stderr': result.stderr.strip(),
                'command': command
            }
            
        except Exception as e:
            return {
                'success': False,
                'return_code': -1,
                'stdout': '',
                'stderr': str(e),
                'command': command
            }
    
    def execute_applescript(self, script: str, cwd: str = None) -> Dict[str, Any]:
        """Execute an AppleScript in Terminal"""
        try:
            temp_file = "/tmp/wf_hook_applescript.applescript"
            with open(temp_file, "w") as f:
                f.write(script)
            os.system(f"osascript '{temp_file}' 2>/dev/null &")
            return {
                'success': True,
                'return_code': 0,
                'stdout': 'AppleScript executed',
                'stderr': '',
                'command': script
            }
        except Exception as e:
            return {
                'success': False,
                'return_code': -1,
                'stdout': '',
                'stderr': str(e),
                'command': script
            }
    
    def _evaluate_condition(self, condition: str, context: Dict[str, Any]) -> bool:
        """Evaluate a condition string with context"""
        try:
            # Simple condition evaluation - supports basic comparisons
            # More complex conditions could be added with a proper expression evaluator
            condition = condition.strip()
            
            # Replace variables in condition
            for key, value in context.items():
                if isinstance(value, str):
                    condition = condition.replace(f"${key}", value)
                    condition = condition.replace(f"${{{key}}}", value)
            
            # Simple evaluation for common patterns
            if "==" in condition:
                left, right = condition.split("==", 1)
                return left.strip() == right.strip()
            elif "!=" in condition:
                left, right = condition.split("!=", 1)
                return left.strip() != right.strip()
            elif "contains" in condition:
                parts = condition.split("contains", 1)
                if len(parts) == 2:
                    return parts[1].strip() in parts[0].strip()
            
            # For simple success checks
            return condition.lower() in ["true", "success", "1"]
            
        except Exception:
            return False
    
    def execute_hook(self, name: str, trigger_output: Dict[str, Any] = None) -> Dict[str, Any]:
        """Execute a hook and its chain"""
        hook = self.get_hook(name)
        if not hook:
            return {
                'success': False,
                'error': f"Hook '{name}' not found",
                'hook_chain': []
            }
        
        hook_chain = []
        context = trigger_output or {}
        
        # Prepare environment
        env = hook.get('environment', {})
        
        # Add trigger output to environment if configured
        if hook.get('pass_output') and trigger_output:
            output_var = hook['output_var']
            env[output_var] = trigger_output.get('stdout', '')
            
            # Also add to context for condition evaluation
            context[output_var] = trigger_output.get('stdout', '')
        
        # Check condition if present
        condition = hook.get('condition')
        if condition:
            if not self._evaluate_condition(condition, context):
                hook_chain.append({
                    'hook': name,
                    'command': f"CONDITION: {condition}",
                    'success': False,
                    'output': "Condition not met, skipping hook"
                })
                return {
                    'success': True,  # Not an error, just skipped
                    'hook_chain': hook_chain,
                    'message': "Hook skipped due to condition"
                }
        
        # Execute main action
        input_data = None
        if hook.get('pass_output') and trigger_output and trigger_output.get('stdout'):
            input_data = trigger_output.get('stdout')
        
        action_result = self._execute_command(
            hook['action'],
            env=env,
            input_data=input_data
        )
        
        action_result['hook'] = name
        action_result['command'] = hook['action']
        hook_chain.append(action_result)
        
        # Execute success or fail hook
        next_hook = None
        if action_result['success'] and hook.get('success_hook'):
            next_hook = hook['success_hook']
        elif not action_result['success'] and hook.get('fail_hook'):
            next_hook = hook['fail_hook']
        
        if next_hook:
            next_result = self.execute_hook(next_hook, action_result)
            hook_chain.extend(next_result.get('hook_chain', []))
            
            return {
                'success': action_result['success'],
                'hook_chain': hook_chain,
                'main_hook': name,
                'chained_to': next_hook
            }
        
        return {
            'success': action_result['success'],
            'hook_chain': hook_chain,
            'main_hook': name
        }
    
    def find_triggered_hooks(self, command: str, output: Dict[str, Any] = None) -> List[str]:
        """Find hooks that should be triggered by a command"""
        triggered = []
        
        for name, hook in self._hooks.items():
            trigger = hook['trigger']
            
            # Simple string matching for now - could be enhanced with regex
            if trigger in command or command in trigger:
                triggered.append(name)
        
        return triggered
    
    def get_shell_commands(self, command: str, context: Dict[str, Any] = None) -> List[str]:
        """Get shell commands from hooks that should run in the user's terminal"""
        shell_cmds = []
        
        for name, hook in self._hooks.items():
            trigger = hook.get('trigger', '')
            if trigger in command or command in trigger:
                # Check folder filter
                folder_filter = hook.get('folder')
                if folder_filter and context:
                    repo_name = context.get('repo_name', '')
                    if folder_filter not in repo_name:
                        continue
                
                shell_cmd = hook.get('shell_cmd')
                if shell_cmd:
                    # Substitute variables in shell_cmd
                    if context:
                        for key, value in context.items():
                            if isinstance(value, str):
                                shell_cmd = shell_cmd.replace(f"${{{key}}}", value)
                                shell_cmd = shell_cmd.replace(f"${key}", value)
                    shell_cmds.append(shell_cmd)
        
        return shell_cmds
    
    def execute_triggered_hooks(self, command: str, command_result: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Execute all hooks triggered by a command"""
        triggered_hooks = self.find_triggered_hooks(command, command_result)
        results = []
        
        for hook_name in triggered_hooks:
            result = self.execute_hook(hook_name, command_result)
            results.append(result)
        
        return results