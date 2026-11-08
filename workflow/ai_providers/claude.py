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


def get_skills_for_session(issue=None, repo_path=None, project=None):
    """Get skills from config and return as a dict"""
    from workflow.ai_context import get_ai_skills
    return get_ai_skills(issue, repo_path, project)


def skills_plugin_dir(issue, task_key, task_dir: Path, repo_path=None, project=None):
    """Write the session's wf skills as a Claude plugin; returns its directory.

    None when there are no skills or no task to file them under. Shared with
    agent jobs (see deploy.jobs), which pass it to `claude --plugin-dir`.
    """
    skills = get_skills_for_session(issue, repo_path, project)
    if not (skills and task_key):
        return None
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

        print(f"DEBUG: Using plugin at {plugin_dir}", file=sys.stderr)
        return plugin_dir
    except Exception as e:
        print(f"DEBUG: Failed to setup plugin: {e}", file=sys.stderr)
        return None


# Claude flags that pick up an existing session. With one of these the task
# context is not piped in again: the session already has it, and a piped
# stdin would also keep `claude -r` from showing its interactive picker.
RESUME_FLAGS = {"-r", "--resume", "-c", "--continue", "--from-pr", "--desktop"}


def resumes_session(args) -> bool:
    """True when `args` (claude CLI arguments) resume or continue a session."""
    for arg in args or ():
        if arg == "--":
            continue
        if arg in RESUME_FLAGS or arg.split("=", 1)[0] in RESUME_FLAGS:
            return True
    return False


def prints_once(args) -> bool:
    """True when `args` run claude in print mode: one answer, no session to track."""
    return any(a in ("-p", "--print") or a.startswith("--print=") for a in args or ())


def claude_command(transcript_file: Path, prompt_file=None, args=None) -> str:
    """The shell line that runs claude under `script` for the transcript.

    `args` go to the claude CLI verbatim (after wf's own --plugin-dir).
    With a `prompt_file` its content is piped in as the first message.
    """
    import shlex
    claude = shlex.join(["claude", *(args or [])])
    if prompt_file is not None:
        inner = f"cat {shlex.quote(str(prompt_file))} | {claude}"
    else:
        inner = claude
    return f"script -q {shlex.quote(str(transcript_file))} bash -c {shlex.quote(inner)}"


class ClaudeProvider(AIProvider):
    name = "claude"

    def run(self, context, issue=None, args=None, project=None):
        """Run an interactive Claude session; returns the terminal transcript.

        `context` is piped in as the first message; None starts Claude with
        nothing piped (its stdin stays the terminal). `args` are passed to
        the claude CLI as given, e.g. ["-r"] or ["--model", "opus"].
        `project` picks the Claude account and skills (default: the current
        project's).
        """
        args = list(args or [])
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
                prompt_hash = hashlib.md5((context or "").encode()).hexdigest()[:8]
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
        
        plugin_dir = skills_plugin_dir(issue or current_issue, task_key, task_dir, repo_path, project)
        if plugin_dir:
            args = ["--plugin-dir", str(plugin_dir), *args]

        task_dir.mkdir(parents=True, exist_ok=True)
        
        # Clean up any existing prompt file from previous runs
        prompt_file = task_dir / "current.prompt"
        if prompt_file.exists():
            prompt_file.unlink()
        
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        transcript_file = task_dir / f"transcript_{timestamp}.txt"
        
        try:
            if context is not None:
                prompt_file.write_text(context)

            # Route this session's model traffic through the capture proxy so
            # the real conversation is recorded, rather than the ANSI terminal
            # transcript `script` produces below; log it in as the current
            # project's Claude account.
            from workflow.session import apply_claude_env
            for name, value in apply_claude_env(project=project).items():
                print(f"DEBUG: {name}={value}", file=sys.stderr)

            script_cmd = claude_command(
                transcript_file,
                prompt_file if context is not None else None,
                args,
            )
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
