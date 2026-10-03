import subprocess
import os
import uuid
import sys
import threading
import time
import shutil
from datetime import datetime
from pathlib import Path
from workflow.ai_providers.base import AIProvider, apply_proxy_env


def get_skills_for_session(issue=None, repo_path=None):
    """Get skills from config and return as a dict"""
    from workflow.ai_context import get_ai_skills
    return get_ai_skills(issue, repo_path)


class ClaudeProvider(AIProvider):
    name = "claude"

    def run(self, context: str, issue=None):
        # Determine task directory - prefer issue key, fallback to current task or prompt-based name
        current_issue = None
        repo_path = None
        
        if issue and hasattr(issue, 'key'):
            task_dir = Path.home() / ".wf" / "tasks" / issue.key.lower()
        else:
            # Try to get current task from state
            from workflow.state import get_current_task
            current_issue = get_current_task()
            if current_issue and hasattr(current_issue, 'key'):
                task_dir = Path.home() / ".wf" / "tasks" / current_issue.key.lower()
            else:
                # Fallback: use a prompt-based hash for non-task prompts
                import hashlib
                prompt_hash = hashlib.md5(context.encode()).hexdigest()[:8]
                task_dir = Path.home() / ".wf" / "tasks" / f"prompt_{prompt_hash}"
        
        # Get task key for plugin naming
        task_key = None
        if issue and hasattr(issue, 'key'):
            task_key = issue.key.lower()
        elif current_issue and hasattr(current_issue, 'key'):
            task_key = current_issue.key.lower()
        
        # Try to get repo path from current working directory
        try:
            result = subprocess.run(
                ["git", "rev-parse", "--show-toplevel"],
                capture_output=True,
                text=True,
                cwd=os.getcwd()
            )
            if result.returncode == 0:
                repo_path = result.stdout.strip()
        except:
            pass
        
        # Get skills and create plugin directory in task folder
        skills = get_skills_for_session(issue or current_issue, repo_path)
        plugin_dir = None
        plugin_arg = ""
        
        if skills and task_key:
            try:
                # Create or reuse plugin directory
                plugin_dir = task_dir / "claude-plugin"
                plugin_dir.mkdir(parents=True, exist_ok=True)
                
                # Create or update skills subdirectory
                skills_dir = plugin_dir / "skills"
                skills_dir.mkdir(parents=True, exist_ok=True)
                
                # Write plugin.json if not exists or update
                plugin_json = {
                    "name": "workflow-skills",
                    "version": "1.0.0",
                    "skills": ["./skills/"]
                }
                import json
                plugin_json_file = plugin_dir / "plugin.json"
                plugin_json_file.write_text(json.dumps(plugin_json, indent=2))
                
                # Track existing skill files to detect deletions
                existing_skills = set(f.name for f in skills_dir.glob("*.md"))
                current_skills = set()
                
                # Create/update skill files
                for skill in skills:
                    skill_filename = skill['name'].lower().replace(' ', '-').replace('/', '-') + ".md"
                    current_skills.add(skill_filename)
                    skill_file = skills_dir / skill_filename
                    new_content = f"""---
name: {skill['name']}
description: Workflow skill - {skill['scope']} scope
---

{skill['content']}
"""
                    # Only write if content changed
                    if not skill_file.exists() or skill_file.read_text() != new_content:
                        skill_file.write_text(new_content)
                
                # Remove skills that are no longer in config
                for old_skill in existing_skills - current_skills:
                    (skills_dir / old_skill).unlink()
                
                plugin_arg = f"--plugin-dir {plugin_dir}"
                print(f"DEBUG: Using plugin at {plugin_dir}", file=sys.stderr)
            except Exception as e:
                print(f"DEBUG: Failed to setup plugin: {e}", file=sys.stderr)
        
        task_dir.mkdir(parents=True, exist_ok=True)
        
        # Clean up any existing prompt file from previous runs
        prompt_file = task_dir / "current.prompt"
        if prompt_file.exists():
            prompt_file.unlink()
        
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        transcript_file = task_dir / f"transcript_{timestamp}.txt"
        
        try:
            prompt_file.write_text(context)

            # Route this session's model traffic through the capture proxy so
            # the real conversation is recorded, rather than the ANSI terminal
            # transcript `script` produces below.
            captured = apply_proxy_env()
            if captured:
                print(f"DEBUG: capturing via {captured.get('ANTHROPIC_BASE_URL')}", file=sys.stderr)

            # Log this session in as the current project's Claude account.
            from workflow import claude_accounts
            account_dir = claude_accounts.apply()
            if account_dir:
                print(f"DEBUG: claude account dir {account_dir}", file=sys.stderr)

            # Build command with optional plugin dir
            if plugin_arg:
                script_cmd = f'script -q {transcript_file} bash -c \'cat {prompt_file} | claude {plugin_arg}\''
            else:
                script_cmd = f'script -q {transcript_file} bash -c \'cat {prompt_file} | claude\''
            print(f"DEBUG: Running: {script_cmd}", file=sys.stderr)
            os.system(script_cmd)
            
            # Read the transcript from file
            transcript = ""
            if transcript_file.exists():
                transcript = transcript_file.read_text()
            
            # Generate summary in background
            if transcript and (issue or current_issue):
                target_issue = issue or current_issue
                try:
                    from workflow.memory import save_summary
                    
                    def generate_summary():
                        try:
                            prompt = f"Summarize durable decisions from this session:\n\n{transcript[:50000]}"
                            result = subprocess.run(
                                ["claude", "--print", prompt],
                                capture_output=True,
                                text=True,
                                timeout=120
                            )
                            if result.returncode == 0 and result.stdout:
                                summary_file = task_dir / "summary.txt"
                                summary_file.write_text(result.stdout)
                                print(f"DEBUG: Background summary generated", file=sys.stderr)
                        except Exception as e:
                            print(f"DEBUG: Background summary failed: {e}", file=sys.stderr)
                    
                    threading.Thread(target=generate_summary, daemon=True).start()
                except Exception as e:
                    print(f"DEBUG: Summary setup failed: {e}", file=sys.stderr)
            
            try:
                prompt_file.unlink()
            except:
                pass
            return transcript
        except Exception as e:
            print(f"DEBUG: Claude provider error: {e}", file=sys.stderr)
            return ""
