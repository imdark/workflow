import yaml
import time
import subprocess
import json
import re
from pathlib import Path
from typing import Dict, Any, Optional, List
import typer

# State file paths
STATE_DIR = Path.home() / ".wf"
STATE_FILE = STATE_DIR / "state.yaml"

class VariableResolver:
    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.state = self._load_state()
    
    def _load_state(self) -> Dict[str, Any]:
        """Load the state file with variable values and cache info"""
        if STATE_FILE.exists():
            try:
                return yaml.safe_load(STATE_FILE.read_text()) or {}
            except Exception as e:
                typer.echo(f"⚠️  Error loading state file: {e}")
                return {}
        return {}
    
    def _save_state(self):
        """Save the state file"""
        try:
            STATE_DIR.mkdir(parents=True, exist_ok=True)
            # Use atomic write to avoid file exists errors
            import tempfile
            import shutil
            with tempfile.NamedTemporaryFile(mode='w', dir=STATE_DIR, delete=False, suffix='.tmp') as temp_file:
                yaml.dump(self.state, temp_file, default_flow_style=False)
                temp_path = temp_file.name
            
            # Move temp file to final location
            shutil.move(temp_path, STATE_FILE)
        except Exception as e:
            typer.echo(f"⚠️  Error saving state file: {e}")
    
    def resolve_variables(self, text: str, context: Dict[str, Any] = None) -> str:
        """Resolve all variables in a text string"""
        if not text or not isinstance(text, str):
            return text
        
        if context is None:
            context = {}
        
        # Find all variables in the format {variable_name}
        variables = re.findall(r'\{([^}]+)\}', text)
        resolved_text = text
        
        for var_name in variables:
            value = self.get_variable_value(var_name, context)
            if value is not None:
                resolved_text = resolved_text.replace(f'{{{var_name}}}', str(value))
        
        return resolved_text
    
    def get_variable_value(self, var_name: str, context: Dict[str, Any] = None) -> Any:
        """Get the value of a specific variable"""
        if context is None:
            context = {}
        
        # Check context first (highest priority)
        if var_name in context:
            return context[var_name]
        
        # Check state cache
        cached_value = self._get_cached_value(var_name)
        if cached_value is not None:
            return cached_value
        
        # Resolve variable and cache it
        value = self._resolve_variable(var_name)
        if value is not None:
            self._cache_variable(var_name, value)
        
        return value
    
    def _get_cached_value(self, var_name: str) -> Any:
        """Get cached value if still valid"""
        variables_config = self.config.get("variables", {})
        
        if var_name not in variables_config:
            return None
        
        var_config = variables_config[var_name]
        
        # Check if cache is disabled
        if var_config.get("cache", {}).get("enabled", True) == False:
            return None
        
        # Get cache TTL (default 5 minutes)
        cache_ttl = var_config.get("cache", {}).get("ttl", 300)
        
        # Check cached value in state
        cached_vars = self.state.get("cached_variables", {})
        if var_name in cached_vars:
            cached_info = cached_vars[var_name]
            cache_time = cached_info.get("timestamp", 0)
            
            if time.time() - cache_time < cache_ttl:
                return cached_info.get("value")
        
        return None
    
    def _cache_variable(self, var_name: str, value: Any):
        """Cache a variable value"""
        variables_config = self.config.get("variables", {})
        
        if var_name not in variables_config:
            return
        
        var_config = variables_config[var_name]
        
        # Check if cache is disabled
        if var_config.get("cache", {}).get("enabled", True) == False:
            return
        
        # Initialize cached_variables if it doesn't exist
        if "cached_variables" not in self.state:
            self.state["cached_variables"] = {}
        
        # Cache the variable
        self.state["cached_variables"][var_name] = {
            "value": value,
            "timestamp": time.time()
        }
        
        self._save_state()
    
    def _resolve_variable(self, var_name: str) -> Any:
        """Resolve a variable by running its command or using its static value"""
        variables_config = self.config.get("variables", {})
        
        if var_name not in variables_config:
            # Try built-in variables
            return self._resolve_builtin_variable(var_name)
        
        var_config = variables_config[var_name]
        
        # If it has a command, run it
        if "command" in var_config:
            return self._run_command(var_config["command"])
        
        # If it has a static value, use it
        if "value" in var_config:
            return var_config["value"]
        
        # If it has a type, use the resolver
        if "type" in var_config:
            return self._resolve_by_type(var_config)
        
        return None
    
    def _resolve_builtin_variable(self, var_name: str) -> Any:
        """Resolve built-in variables"""
        if var_name == "current_time":
            return int(time.time())
        elif var_name == "current_date":
            return time.strftime("%Y-%m-%d")
        elif var_name == "current_datetime":
            return time.strftime("%Y-%m-%d %H:%M:%S")
        elif var_name == "username":
            import os
            return os.environ.get("USER", os.environ.get("USERNAME", "unknown"))
        
        return None
    
    def _run_command(self, command: str) -> str:
        """Run a command and return its output"""
        try:
            result = subprocess.run(
                command, 
                shell=True, 
                capture_output=True, 
                text=True, 
                timeout=30
            )
            
            if result.returncode == 0:
                return result.stdout.strip()
            else:
                typer.echo(f"⚠️  Command failed: {command}")
                typer.echo(f"Error: {result.stderr.strip()}")
                return None
        except subprocess.TimeoutExpired:
            typer.echo(f"⚠️  Command timed out: {command}")
            return None
        except Exception as e:
            typer.echo(f"⚠️  Error running command '{command}': {e}")
            return None
    
    def _resolve_by_type(self, var_config: Dict[str, Any]) -> Any:
        """Resolve variable by type (for complex resolvers)"""
        var_type = var_config.get("type")
        
        if var_type == "jira_sprint":
            return self._resolve_jira_sprint(var_config)
        elif var_type == "jira_field":
            return self._resolve_jira_field(var_config)
        
        return None
    
    def _resolve_jira_sprint(self, var_config: Dict[str, Any]) -> Any:
        """Resolve Jira sprint variable"""
        try:
            from workflow.backends import get_backend
            backend = get_backend(self.config)
            
            project_key = var_config.get("project_key", self.config.get("jira", {}).get("project", "DEV"))
            active_sprint = backend.get_active_sprint(project_key)
            
            if not active_sprint:
                return None
            
            field = var_config.get("field", "name")  # name, id, state
            
            if field == "id":
                return active_sprint.get("id")
            elif field == "state":
                return active_sprint.get("state")
            else:  # default to name
                return active_sprint.get("name")
                
        except Exception as e:
            typer.echo(f"⚠️  Error resolving Jira sprint: {e}")
            return None
    
    def _resolve_jira_field(self, var_config: Dict[str, Any]) -> Any:
        """Resolve Jira field variable"""
        try:
            from workflow.backends import get_backend
            backend = get_backend(self.config)
            
            field_name = var_config.get("field_name")
            if not field_name:
                return None
            
            project_key = var_config.get("project_key", self.config.get("jira", {}).get("project", "DEV"))
            active_sprint = backend.get_active_sprint(project_key)
            
            if not active_sprint:
                return None
            
            # Return sprint info mapped to field
            field_mapping = {
                "sprint_name": active_sprint.get("name"),
                "sprint_id": str(active_sprint.get("id")),
                "sprint_state": active_sprint.get("state")
            }
            
            return field_mapping.get(field_name)
            
        except Exception as e:
            typer.echo(f"⚠️  Error resolving Jira field: {e}")
            return None
    
    def clear_cache(self, var_name: str = None):
        """Clear cache for a specific variable or all variables"""
        if "cached_variables" not in self.state:
            return
        
        if var_name:
            if var_name in self.state["cached_variables"]:
                del self.state["cached_variables"][var_name]
                typer.echo(f"✅ Cleared cache for variable: {var_name}")
        else:
            self.state["cached_variables"] = {}
            typer.echo("✅ Cleared all variable caches")
        
        self._save_state()
    
    def list_variables(self) -> Dict[str, Any]:
        """List all configured variables with their current values"""
        variables_config = self.config.get("variables", {})
        result = {}
        
        for var_name, var_config in variables_config.items():
            value = self.get_variable_value(var_name)
            result[var_name] = {
                "config": var_config,
                "current_value": value
            }
        
        return result
    
    def add_variable(self, name: str, config: Dict[str, Any]):
        """Add a new variable configuration"""
        if "variables" not in self.config:
            self.config["variables"] = {}
        
        self.config["variables"][name] = config
        
        # Save to main config
        from workflow.config import save_config
        save_config(self.config)
    
    def remove_variable(self, name: str):
        """Remove a variable configuration"""
        if "variables" in self.config and name in self.config["variables"]:
            del self.config["variables"][name]
            
            # Clear cache
            self.clear_cache(name)
            
            # Save to main config
            from workflow.config import save_config
            save_config(self.config)
            typer.echo(f"✅ Removed variable: {name}")
        else:
            typer.echo(f"❌ Variable '{name}' not found")