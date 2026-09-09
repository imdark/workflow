"""PID file manager for tracking AI sessions and agent state."""
import os
import json
from pathlib import Path
from datetime import datetime
from typing import Optional, Dict, Any

# State directory for PID files
STATE_DIR = Path.home() / ".wf" / "state"

def ensure_state_dir():
    """Ensure the state directory exists."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)

def get_pid_file_path(repo_path: str) -> Path:
    """Get the PID file path for a specific repository."""
    # Use a hash of the repo path to create a unique filename
    import hashlib
    repo_hash = hashlib.md5(str(repo_path).encode()).hexdigest()[:8]
    return STATE_DIR / f"ai_session_{repo_hash}.pid"

def create_ai_pid_file(repo_path: str, task_key: str, branch_name: str) -> Path:
    """Create a PID file when AI session starts.
    
    Args:
        repo_path: Path to the repository
        task_key: The task/issue key being worked on
        branch_name: The git branch name
        
    Returns:
        Path to the created PID file
    """
    ensure_state_dir()
    pid_file = get_pid_file_path(repo_path)
    
    pid_data = {
        "pid": os.getpid(),
        "repo_path": str(repo_path),
        "task_key": task_key,
        "branch_name": branch_name,
        "started_at": datetime.now().isoformat(),
    }
    
    pid_file.write_text(json.dumps(pid_data, indent=2))
    return pid_file

def remove_ai_pid_file(repo_path: str) -> bool:
    """Remove the PID file when AI session ends.
    
    Args:
        repo_path: Path to the repository
        
    Returns:
        True if file was removed, False if it didn't exist
    """
    pid_file = get_pid_file_path(repo_path)
    if pid_file.exists():
        pid_file.unlink()
        return True
    return False

def get_ai_session_info(repo_path: str) -> Optional[Dict[str, Any]]:
    """Get information about an active AI session for a repository.
    
    Args:
        repo_path: Path to the repository
        
    Returns:
        Session info dict if session exists and is active, None otherwise
    """
    pid_file = get_pid_file_path(repo_path)
    if not pid_file.exists():
        return None
    
    try:
        pid_data = json.loads(pid_file.read_text())
        
        # Check if the process is still running
        pid = pid_data.get("pid")
        if pid and _is_process_running(pid):
            return pid_data
        else:
            # Process is dead, clean up the stale PID file
            pid_file.unlink()
            return None
    except (json.JSONDecodeError, FileNotFoundError):
        # Invalid or missing PID file
        if pid_file.exists():
            pid_file.unlink()
        return None

def is_ai_active(repo_path: str) -> bool:
    """Check if an AI session is currently active for a repository.
    
    Args:
        repo_path: Path to the repository
        
    Returns:
        True if an AI session is active, False otherwise
    """
    return get_ai_session_info(repo_path) is not None

def get_active_ai_sessions() -> list:
    """Get all currently active AI sessions across all repositories.
    
    Returns:
        List of active session info dicts
    """
    ensure_state_dir()
    active_sessions = []
    
    for pid_file in STATE_DIR.glob("ai_session_*.pid"):
        try:
            pid_data = json.loads(pid_file.read_text())
            pid = pid_data.get("pid")
            repo_path = pid_data.get("repo_path")
            
            if pid and _is_process_running(pid):
                active_sessions.append(pid_data)
            else:
                # Clean up stale PID file
                pid_file.unlink()
        except (json.JSONDecodeError, FileNotFoundError):
            # Invalid PID file, remove it
            if pid_file.exists():
                pid_file.unlink()
    
    return active_sessions

def _is_process_running(pid: int) -> bool:
    """Check if a process with the given PID is still running.
    
    Args:
        pid: Process ID to check
        
    Returns:
        True if process is running, False otherwise
    """
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False
