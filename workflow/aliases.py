from pathlib import Path
import yaml
import subprocess
import typer
from typing import Dict, Optional, List
import json
import os
from git import Repo
from pathlib import Path

class AliasManager:
    def __init__(self, config_path: Optional[Path] = None):
        if config_path is None:
            config_path = Path.home() / ".wf" / "aliases.yaml"
        self.config_path = config_path
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        self._aliases = self._load_aliases()
        
        # Load variable resolver
        try:
            from workflow.config import load_config
            from workflow.variables import VariableResolver
            config = load_config()
            self._variable_resolver = VariableResolver(config)
        except Exception:
            self._variable_resolver = None
    
    def _load_aliases(self) -> Dict:
        """Load aliases from config file"""
        if self.config_path.exists():
            with open(self.config_path, 'r') as f:
                return yaml.safe_load(f) or {}
        return {}
    
    def _save_aliases(self):
        """Save aliases to config file"""
        with open(self.config_path, 'w') as f:
            yaml.dump(self._aliases, f, default_flow_style=False)
    
    def add_alias(self, name: str, command: str, context: Optional[Dict] = None):
        """Add a new alias with optional context"""
        if context is None:
            context = {}
        
        self._aliases[name] = {
            'command': command,
            'context': context
        }
        self._save_aliases()
        return True
    
    def get_alias(self, name: str) -> Optional[Dict]:
        """Get alias by name"""
        return self._aliases.get(name)
    
    def list_aliases(self) -> Dict:
        """List all aliases"""
        return self._aliases
    
    def remove_alias(self, name: str) -> bool:
        """Remove an alias"""
        if name in self._aliases:
            del self._aliases[name]
            self._save_aliases()
            return True
        return False
    
    def resolve_alias(self, name: str, cwd: Optional[str] = None) -> Optional[str]:
        """Resolve alias with context to actual command"""
        alias = self.get_alias(name)
        if not alias:
            return None
        
        command = alias['command']
        context = alias.get('context', {})
        
        # Apply variable substitutions
        command = self._substitute_variables(command, cwd)
        
        # Apply context-aware modifications
        if context:
            # Repository-specific commands
            if 'repositories' in context:
                repo_mapping = context['repositories']
                if cwd:
                    # Find matching repository
                    for repo_path, repo_config in repo_mapping.items():
                        if cwd.startswith(repo_path):
                            if 'command' in repo_config:
                                command = self._substitute_variables(repo_config['command'], cwd)
                            if 'env' in repo_config:
                                # Environment variables would be handled when executing
                                pass
                            break
            
            # File/directory-specific commands
            if 'files' in context:
                file_mapping = context['files']
                if cwd:
                    for file_pattern, file_config in file_mapping.items():
                        if Path(cwd).glob(file_pattern):
                            if 'command' in file_config:
                                command = self._substitute_variables(file_config['command'], cwd)
                            break
        
        return command
    
    def _substitute_variables(self, command: str, cwd: Optional[str] = None) -> str:
        """Substitute variables in command using the variable resolver"""
        if cwd is None:
            cwd = str(Path.cwd())
        
        # Find repository root
        repo_root = self._find_repo_root(cwd)
        
        # Create context for variable resolution
        context = {
            'REPO_ROOT': repo_root,
            'PWD': cwd,
            'HOME': str(Path.home()),
        }
        
        # Use variable resolver if available
        if self._variable_resolver:
            return self._variable_resolver.resolve_variables(command, context)
        
        # Fallback to basic variable substitution
        for var_name, var_value in context.items():
            if var_value is not None:
                command = command.replace(f'%{var_name}%', str(var_value))
        
        return command
    
    def _find_repo_root(self, cwd: str) -> Optional[str]:
        """Find git repository root directory"""
        try:
            repo = Repo(cwd, search_parent_directories=True)
            git_dir = str(repo.git_dir) if repo.git_dir else None
            return git_dir.replace('/.git', '') if git_dir and git_dir.endswith('/.git') else git_dir
        except Exception:
            return None
    
    def execute_alias(self, name: str, args: Optional[List[str]] = None, cwd: Optional[str] = None) -> int:
        """Execute an alias with arguments"""
        if args is None:
            args = []
        
        resolved_command = self.resolve_alias(name, cwd)
        if not resolved_command:
            typer.echo(f"❌ Alias '{name}' not found")
            return 1
        
        # Determine working directory - start from current directory
        work_dir = cwd if cwd is not None else str(Path.cwd())
        
        # For complex commands with cd, we execute from user's home directory
        # to allow cd commands to work properly
        execution_dir = str(Path.home())
        
        try:
            # Check if command contains cd and should be executed as a complex bash script
            if 'cd ' in resolved_command or '&&' in resolved_command or ';' in resolved_command:
                # This is a complex command, create a bash script
                # Append arguments to the command
                if args:
                    resolved_command = f"{resolved_command} {' '.join(args)}"
                
                # Create a bash script that starts in current directory
                bash_script = f'#!/bin/bash\nset -e\ncd "{work_dir}"\n{resolved_command}'
                
                typer.echo(f"🚀 Running complex command: {resolved_command}")
                typer.echo(f"📁 Starting in: {work_dir}")
                
                # Execute via bash to support complex commands
                result = subprocess.run(
                    bash_script,
                    shell=True,
                    executable="/bin/bash",
                    cwd=execution_dir,
                    check=False
                )
            else:
                # Simple command, append arguments
                if args:
                    resolved_command = f"{resolved_command} {' '.join(args)}"
                
                typer.echo(f"🚀 Running: {resolved_command}")
                if cwd:
                    typer.echo(f"📁 In: {cwd}")
                
                result = subprocess.run(
                    resolved_command,
                    shell=True,
                    cwd=work_dir,
                    check=False
                )
            
            if result.returncode == 0:
                typer.echo("✅ Command completed successfully")
            else:
                typer.echo(f"❌ Command failed with exit code {result.returncode}")
            
            return result.returncode
            
        except Exception as e:
            typer.echo(f"❌ Error executing command: {e}")
            return 1