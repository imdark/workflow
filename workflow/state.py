import yaml
import os
from pathlib import Path

STATE = Path.home() / ".wf" / "state.yaml"

def _load():
    return yaml.safe_load(STATE.read_text()) if STATE.exists() else {}

def _save(d):
    STATE.write_text(yaml.dump(d, default_flow_style=False))

def set_current_task(issue):
    d = _load()
    d["previous"] = d.get("current")
    d["current"] = issue.__dict__
    _save(d)

def get_current_task():
    from workflow.backends.base import Issue
    d = _load()
    if "current" not in d:
        return None
    return Issue(**d["current"])

def get_previous_task():
    from workflow.backends.base import Issue
    d = _load()
    if "previous" not in d:
        return None
    return Issue(**d["previous"])

def shelve_changes(repo_path, branch_name):
    """Record that changes are shelved for a specific repository and branch"""
    d = _load()
    if "shelved_changes" not in d:
        d["shelved_changes"] = {}
    
    repo_key = str(repo_path)
    d["shelved_changes"][repo_key] = {
        "branch": branch_name,
        "timestamp": str(Path(__file__).stat().st_mtime)
    }
    _save(d)

def has_shelved_changes(repo_path):
    """Check if there are shelved changes for a specific repository"""
    d = _load()
    repo_key = str(repo_path)
    return "shelved_changes" in d and repo_key in d["shelved_changes"]

def get_shelved_branch(repo_path):
    """Get the branch where changes were shelved for a specific repository"""
    d = _load()
    repo_key = str(repo_path)
    if has_shelved_changes(repo_path):
        return d["shelved_changes"][repo_key]["branch"]
    return None

def clear_shelved_changes(repo_path):
    """Clear shelved changes record for a specific repository"""
    d = _load()
    repo_key = str(repo_path)
    if "shelved_changes" in d and repo_key in d["shelved_changes"]:
        del d["shelved_changes"][repo_key]
        _save(d)


def get_task_dir(task_key: str) -> Path:
    """Get the task directory for storing AI session info"""
    task_dir = Path.home() / ".wf" / "tasks" / task_key.lower()
    task_dir.mkdir(parents=True, exist_ok=True)
    return task_dir


def get_ai_pid_file(task_key: str) -> Path:
    """Get the path to the AI session PID file"""
    return get_task_dir(task_key) / "ai.pid"


def set_ai_session(task_key: str, pid: int, started_at: str = None):
    """Store AI session info for a task"""
    if started_at is None:
        from datetime import datetime
        started_at = datetime.now().isoformat()
    
    pid_file = get_ai_pid_file(task_key)
    pid_file.write_text(f"{pid}\n{started_at}\n")
    
    d = _load()
    if "ai_sessions" not in d:
        d["ai_sessions"] = {}
    d["ai_sessions"][task_key] = {"pid": pid, "started_at": started_at}
    _save(d)


def get_ai_session(task_key: str) -> dict:
    """Get AI session info for a task"""
    d = _load()
    return d.get("ai_sessions", {}).get(task_key)


def has_ai_session(task_key: str) -> bool:
    """Check if a task has an active AI session"""
    session = get_ai_session(task_key)
    if not session:
        return False
    
    pid = session.get("pid")
    if not pid:
        return False
    
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        clear_ai_session(task_key)
        return False


def clear_ai_session(task_key: str):
    """Remove AI session info for a task"""
    pid_file = get_ai_pid_file(task_key)
    if pid_file.exists():
        pid_file.unlink()
    
    d = _load()
    if "ai_sessions" in d and task_key in d["ai_sessions"]:
        del d["ai_sessions"][task_key]
        _save(d)


def get_all_ai_sessions() -> dict:
    """Get all AI sessions (cleaning up stale ones)"""
    d = _load()
    sessions = d.get("ai_sessions", {})
    cleaned = {}
    
    for task_key, info in sessions.items():
        pid = info.get("pid")
        if pid:
            try:
                os.kill(pid, 0)
                cleaned[task_key] = info
            except OSError:
                clear_ai_session(task_key)
    
    return cleaned
