"""Terminal tab and window management utilities."""

import os
from pathlib import Path
import typer


def open_terminal_tabs(repos, task_key):
    """Open terminal tabs for all repositories in current terminal"""
    if not repos:
        return
    
    # Create a temporary script to open terminal tabs in current terminal
    script_content = []
    repo_paths = list(repos.keys())
    
    if len(repo_paths) == 1:
        # Single repo - just change directory in current tab
        repo_path = str(Path(repo_paths[0]).expanduser())
        applescript = f"""
        tell application "Terminal"
            set currentTab to (selected tab of window 1)
            do script "cd '{repo_path}' && echo 'Working on {task_key}'" in currentTab
        end tell
        """
    else:
        # Multiple repos - create tabs in current terminal
        applescript = f"""
        tell application "Terminal"
            activate
            set currentWindow to window 1
            
            -- Create tabs for each repository
        """
        
        for i, repo_path in enumerate(repo_paths):
            repo_path = str(Path(repo_path).expanduser())
            if i == 0:
                # First repo - use current tab
                applescript += f"""
            set currentTab to (selected tab of currentWindow)
            do script "cd '{repo_path}' && echo 'Working on {task_key}'" in currentTab
                """
            else:
                # Additional repos - create new tabs
                applescript += f"""
            tell currentWindow
                set newTab to (make new tab at after currentTab)
                do script "cd '{repo_path}' && echo 'Working on {task_key}'" in newTab
            end tell
                """
        
        applescript += """
        end tell
        """
    
    # Write to temp file and execute
    temp_file = "/tmp/wf_terminal.applescript"
    with open(temp_file, "w") as f:
        f.write(applescript)
    
    result = os.system(f"osascript '{temp_file}' 2>/dev/null &")
    typer.echo(f"📁 Opened {len(repo_paths)} terminal tab(s) for {task_key}")