#!/usr/bin/env python3
"""
Workflow Monitor Module - Integrates monitoring capabilities into workflow CLI
"""

import json
import time
import threading
import os
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Optional, Any
from rich.console import Console
from rich.table import Table
from rich.live import Live
import typer
import logging
from datetime import datetime

# Add workflow path
sys.path.insert(0, str(Path(__file__).parent))

from workflow.actions import get_action_registry, create_action_from_config, ActionType

console = Console(color_system=None)

# Setup logging for monitor
monitor_logger = logging.getLogger('workflow_monitor')
monitor_logger.setLevel(logging.INFO)

# Create logs directory if it doesn't exist
logs_dir = Path.home() / '.wf' / 'logs'
logs_dir.mkdir(parents=True, exist_ok=True)

# File handler for monitor logs
log_file = logs_dir / 'monitor.log'
file_handler = logging.FileHandler(log_file)
file_handler.setLevel(logging.INFO)

# Console handler
console_handler = logging.StreamHandler()
console_handler.setLevel(logging.WARNING)  # Only warnings/errors to console

# Formatter
formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
file_handler.setFormatter(formatter)
console_handler.setFormatter(formatter)

# Add handlers
monitor_logger.addHandler(file_handler)
monitor_logger.addHandler(console_handler)


class FileMonitor:
    """Monitor file system for changes"""
    
    def __init__(self, path: str, config: Dict):
        self.path = path
        self.config = config
        self.last_files = {}
        self.running = False
        
    def check(self) -> List[Dict]:
        """Check for file changes"""
        changes = []
        current_files = {}
        
        try:
            for root, dirs, files in os.walk(self.path):
                # Skip hidden directories and common ignore patterns
                dirs[:] = [d for d in dirs if not d.startswith('.') and d not in ['node_modules', '__pycache__', 'venv', 'env']]
                
                for file in files:
                    if file.startswith('.'):
                        continue
                    
                    file_path = os.path.join(root, file)
                    try:
                        stat = os.stat(file_path)
                        mtime = stat.st_mtime
                        size = stat.st_size
                        current_files[file_path] = {'mtime': mtime, 'size': size}
                        
                        # Check if file is new or modified
                        if file_path not in self.last_files:
                            changes.append({
                                'type': 'created',
                                'path': file_path,
                                'size': size,
                                'monitor': 'filesystem'
                            })
                        elif self.last_files[file_path]['mtime'] != mtime or self.last_files[file_path]['size'] != size:
                            changes.append({
                                'type': 'modified',
                                'path': file_path,
                                'size': size,
                                'old_size': self.last_files[file_path]['size'],
                                'monitor': 'filesystem'
                            })
                    except (OSError, PermissionError):
                        continue
            
            # Check for deleted files
            for file_path in self.last_files:
                if file_path not in current_files:
                    changes.append({
                        'type': 'deleted',
                        'path': file_path,
                        'monitor': 'filesystem'
                    })
            
            self.last_files = current_files
            
        except Exception as e:
            changes.append({
                'type': 'error',
                'message': str(e),
                'monitor': 'filesystem'
            })
        
        return changes
    
    def _scan_current_files(self) -> Dict:
        """Scan current directory and return file metadata"""
        current_files = {}
        try:
            for root, dirs, files in os.walk(self.path):
                # Skip hidden directories and common ignore patterns
                dirs[:] = [d for d in dirs if not d.startswith('.') and d not in ['node_modules', '__pycache__', 'venv', 'env']]
                
                for file in files:
                    if file.startswith('.'):
                        continue
                    
                    file_path = os.path.join(root, file)
                    try:
                        stat = os.stat(file_path)
                        mtime = stat.st_mtime
                        size = stat.st_size
                        current_files[file_path] = {'mtime': mtime, 'size': size}
                    except (OSError, PermissionError):
                        continue
        except Exception:
            pass  # Silently handle scan errors
        return current_files
    
    def _compare_and_report_changes(self, current_files: Dict) -> List[Dict]:
        """Compare current files with last_files and return only deltas"""
        changes = []
        
        # Check for new files
        for file_path, current_info in current_files.items():
            if file_path not in self.last_files:
                changes.append({
                    'type': 'created',
                    'path': file_path,
                    'size': current_info['size'],
                    'monitor': 'filesystem'
                })
        
        # Check for modified files
        for file_path, current_info in current_files.items():
            if file_path in self.last_files:
                last_info = self.last_files[file_path]
                if last_info['mtime'] != current_info['mtime'] or last_info['size'] != current_info['size']:
                    changes.append({
                        'type': 'modified',
                        'path': file_path,
                        'size': current_info['size'],
                        'old_size': last_info['size'],
                        'monitor': 'filesystem'
                    })
        
        # Check for deleted files
        for file_path in self.last_files:
            if file_path not in current_files:
                changes.append({
                    'type': 'deleted',
                    'path': file_path,
                    'monitor': 'filesystem'
                })
        
        return changes


class GitHubPRMonitor:
    """Monitor GitHub PR for new commits"""
    
    def __init__(self, owner: str, repo: str, pr_number: int, config: Dict):
        self.owner = owner
        self.repo = repo
        self.pr_number = pr_number
        self.config = config
        self.last_commit = None
        self.running = False
        self.api_url = f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}/commits"
        
    def check(self) -> List[Dict]:
        """Check for new commits"""
        changes = []
        
        try:
            import requests
            
            headers = {}
            if self.config.get('github_token'):
                headers['Authorization'] = f"token {self.config['github_token']}"
            
            params = {'per_page': 5, 'sort': 'created', 'direction': 'desc'}
            response = requests.get(self.api_url, headers=headers, params=params, timeout=10)
            
            if response.status_code == 200:
                commits = response.json()
                if commits:
                    latest_commit = commits[0]
                    current_sha = latest_commit.get('sha')
                    
                    if current_sha != self.last_commit:
                        self.last_commit = current_sha
                        changes.append({
                            'type': 'new_commit',
                            'commit': latest_commit,
                            'sha': current_sha[:7],
                            'author': latest_commit.get('commit', {}).get('author', {}).get('name', 'Unknown'),
                            'message': latest_commit.get('commit', {}).get('message', 'No message'),
                            'monitor': 'github_pr'
                        })
            else:
                changes.append({
                    'type': 'error',
                    'message': f"GitHub API error: {response.status_code}",
                    'monitor': 'github_pr'
                })
        
        except ImportError:
            changes.append({
                'type': 'error',
                'message': "requests library not available - install with: pip install requests",
                'monitor': 'github_pr'
            })
        except Exception as e:
            changes.append({
                'type': 'error',
                'message': str(e),
                'monitor': 'github_pr'
            })
        
        return changes


class GitHubPRCommentMonitor:
    """Monitor GitHub PR for new comments"""
    
    def __init__(self, owner: str, repo: str, pr_number: int, config: Dict):
        self.owner = owner
        self.repo = repo
        self.pr_number = pr_number
        self.config = config
        self.last_comment_id = None
        self.running = False
        self.api_url = f"https://api.github.com/repos/{owner}/{repo}/issues/{pr_number}/comments"
        
    def check(self) -> List[Dict]:
        """Check for new comments"""
        changes = []
        
        try:
            import requests
            
            headers = {}
            if self.config.get('github_token'):
                headers['Authorization'] = f"token {self.config['github_token']}"
            
            params = {'per_page': 10, 'sort': 'created', 'direction': 'desc'}
            response = requests.get(self.api_url, headers=headers, params=params, timeout=10)
            
            if response.status_code == 200:
                comments = response.json()
                if comments:
                    latest_comment = comments[0]
                    current_comment_id = latest_comment.get('id')
                    
                    if current_comment_id != self.last_comment_id:
                        self.last_comment_id = current_comment_id
                        changes.append({
                            'type': 'new_comment',
                            'comment': latest_comment,
                            'comment_id': current_comment_id,
                            'author': latest_comment.get('user', {}).get('login', 'Unknown'),
                            'body': latest_comment.get('body', 'No comment'),
                            'url': latest_comment.get('html_url', ''),
                            'monitor': 'github_pr_comment'
                        })
            else:
                changes.append({
                    'type': 'error',
                    'message': f"GitHub API error: {response.status_code}",
                    'monitor': 'github_pr_comment'
                })
        
        except ImportError:
            changes.append({
                'type': 'error',
                'message': "requests library not available - install with: pip install requests",
                'monitor': 'github_pr_comment'
            })
        except Exception as e:
            changes.append({
                'type': 'error',
                'message': str(e),
                'monitor': 'github_pr_comment'
            })
        
        return changes


class ActionBasedMonitor:
    """Generic monitor that uses actions to perform monitoring checks"""
    
    def __init__(self, name: str, action_type: str, action_config: Dict, monitor_config: Dict):
        self.name = name
        self.action_type = action_type
        self.action_config = action_config
        self.monitor_config = monitor_config
        self.running = False
        self.last_result = None
        
        # Get the action instance
        from workflow.actions import get_action_registry, create_action_from_config
        self.action_registry = get_action_registry()
        
        # Create action based on type
        if action_type == 'github_comment_lookup':
            from workflow.github_comment_action import GitHubCommentLookupAction
            self.action = GitHubCommentLookupAction(f"{name}_action")
        else:
            # Try to create action from config
            self.action = create_action_from_config(action_config)
            if not self.action:
                raise ValueError(f"Could not create action of type: {action_type}")
    
    def check(self) -> List[Dict]:
        """Perform monitoring check using the action"""
        changes = []
        
        try:
            # Execute the action with monitoring context
            context = {**self.action_config, **self.monitor_config}
            result = self.action.execute(context)
            
            # Compare with last result to detect changes
            if result.get('success'):
                current_data = result.get('comments', result.get('data', []))
                
                # For GitHub comments, detect new comments
                if self.action_type == 'github_comment_lookup':
                    if current_data:
                        latest_id = current_data[0].get('id') if current_data else None
                        last_id = self.last_result.get('comments', [{}])[0].get('id') if self.last_result else None
                        
                        if latest_id != last_id:
                            changes.append({
                                'type': 'new_comments',
                                'comments': current_data,
                                'count': len(current_data),
                                'result': result,
                                'monitor': self.name
                            })
                else:
                    # For other actions, detect any data change
                    if self.last_result != result:
                        changes.append({
                            'type': 'action_result',
                            'result': result,
                            'monitor': self.name
                        })
                
                self.last_result = result
            else:
                changes.append({
                    'type': 'error',
                    'message': result.get('error', 'Unknown action error'),
                    'monitor': self.name
                })
                
        except Exception as e:
            changes.append({
                'type': 'error',
                'message': f"Action monitor error: {str(e)}",
                'monitor': self.name
            })
        
        return changes


class APIMonitor:
    """Monitor API endpoints for changes"""
    
    def __init__(self, name: str, url: str, config: Dict):
        self.name = name
        self.url = url
        self.config = config
        self.last_data = None
        self.running = False
        
    def check(self) -> List[Dict]:
        """Check API for changes"""
        changes = []
        
        try:
            import requests
            
            headers = self.config.get('headers', {})
            params = self.config.get('params', {})
            
            response = requests.get(self.url, headers=headers, params=params, timeout=10)
            
            if response.status_code == 200:
                data = response.json()
                
                # Compare with last data
                if self.last_data != data:
                    self.last_data = data
                    changes.append({
                        'type': 'data_change',
                        'data': data,
                        'monitor': 'api',
                        'name': self.name
                    })
            else:
                changes.append({
                    'type': 'error',
                    'message': f"API error: {response.status_code}",
                    'monitor': 'api'
                })
        
        except ImportError:
            changes.append({
                'type': 'error',
                'message': "requests library not available - install with: pip install requests",
                'monitor': 'api'
            })
        except Exception as e:
            changes.append({
                'type': 'error',
                'message': str(e),
                'monitor': 'api'
            })
        
        return changes


class SlackChannelMonitor:
    """Monitor Slack channel for new messages"""
    
    def __init__(self, name: str, channel_id: str, config: Dict):
        self.name = name
        self.channel_id = channel_id
        self.config = config
        self.last_message_ts = None
        self.running = False
        self.slack_token = config.get('slack_token')
        
    def check(self) -> List[Dict]:
        """Check for new messages in Slack channel"""
        changes = []
        
        try:
            import requests
            
            if not self.slack_token:
                changes.append({
                    'type': 'error',
                    'message': 'Slack token not configured',
                    'monitor': 'slack_channel'
                })
                return changes
            
            # Use Slack API to fetch channel history
            url = "https://slack.com/api/conversations.history"
            params = {
                'channel': self.channel_id,
                'limit': 10,
                'inclusive': 0
            }
            headers = {'Authorization': f'Bearer {self.slack_token}'}
            
            response = requests.get(url, headers=headers, params=params, timeout=10)
            
            if response.status_code == 200:
                data = response.json()
                if data.get('ok'):
                    messages = data.get('messages', [])
                    if messages:
                        latest_message = messages[0]
                        current_ts = latest_message.get('ts')
                        
                        if current_ts != self.last_message_ts:
                            self.last_message_ts = current_ts
                            
                            # Get all new messages since last check
                            new_messages = []
                            for msg in messages:
                                if msg.get('ts') == self.last_message_ts:
                                    break
                                new_messages.append(msg)
                            
                            if new_messages:
                                changes.append({
                                    'type': 'new_messages',
                                    'messages': new_messages,
                                    'count': len(new_messages),
                                    'latest_message': latest_message,
                                    'user': latest_message.get('user', latest_message.get('username', 'Unknown')),
                                    'text': latest_message.get('text', ''),
                                    'ts': current_ts,
                                    'monitor': 'slack_channel',
                                    'channel_id': self.channel_id
                                })
                else:
                    changes.append({
                        'type': 'error',
                        'message': f"Slack API error: {data.get('error', 'Unknown')}",
                        'monitor': 'slack_channel'
                    })
            else:
                changes.append({
                    'type': 'error',
                    'message': f"HTTP error: {response.status_code}",
                    'monitor': 'slack_channel'
                })
        
        except ImportError:
            changes.append({
                'type': 'error',
                'message': "requests library not available - install with: pip install requests",
                'monitor': 'slack_channel'
            })
        except Exception as e:
            changes.append({
                'type': 'error',
                'message': f"Slack monitor error: {str(e)}",
                'monitor': 'slack_channel'
            })
        
        return changes


class CLIActionMonitor:
    """Monitor that executes CLI commands/actions and detects changes in output"""
    
    def __init__(self, name: str, action_command: str, config: Dict):
        self.name = name
        self.action_command = action_command
        self.config = config
        self.last_output = None
        self.last_result = None
        self.running = False
        
    def check(self) -> List[Dict]:
        """Execute CLI action and check for changes"""
        changes = []
        
        try:
            # Execute the CLI command
            cmd = ['wf'] + self.action_command.split()
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=30
            )
            
            if result.returncode == 0:
                current_output = result.stdout.strip()
                current_result = {
                    'success': True,
                    'output': current_output,
                    'command': self.action_command,
                    'timestamp': datetime.now().isoformat()
                }
                
                # Compare with last output to detect changes
                if self.last_output != current_output:
                    # Special handling for github-comments command
                    if 'github-comments' in self.action_command:
                        # Parse the output to extract comment data
                        comment_data = self._parse_github_comments_output(current_output)
                        if comment_data:
                            # Extract GitHub URL from the command
                            github_url = self._extract_url_from_command(self.action_command)
                            changes.append({
                                'type': 'new_comments',
                                'comments': comment_data,
                                'count': len(comment_data),
                                'result': current_result,
                                'monitor': self.name,
                                'url': github_url
                            })
                        else:
                            # Fallback to generic change
                            changes.append({
                                'type': 'cli_action_change',
                                'command': self.action_command,
                                'old_output': self.last_output,
                                'new_output': current_output,
                                'result': current_result,
                                'monitor': self.name
                            })
                    else:
                        # Generic CLI action change
                        changes.append({
                            'type': 'cli_action_change',
                            'command': self.action_command,
                            'old_output': self.last_output,
                            'new_output': current_output,
                            'result': current_result,
                            'monitor': self.name
                        })
                
                self.last_output = current_output
                self.last_result = current_result
            else:
                error_msg = result.stderr.strip() if result.stderr else f"Command failed with return code {result.returncode}"
                changes.append({
                    'type': 'error',
                    'message': f"CLI action error: {error_msg}",
                    'command': self.action_command,
                    'monitor': self.name
                })
                
        except subprocess.TimeoutExpired:
            changes.append({
                'type': 'error',
                'message': f"CLI action timeout: {self.action_command}",
                'command': self.action_command,
                'monitor': self.name
            })
        except Exception as e:
            changes.append({
                'type': 'error',
                'message': f"CLI action monitor error: {str(e)}",
                'command': self.action_command,
                'monitor': self.name
            })
        
        return changes
    
    def _parse_github_comments_output(self, output: str) -> List[Dict]:
        """Parse GitHub comments CLI output to extract comment data"""
        comments = []
        lines = output.split('\n')
        
        # Parse individual comment entries
        current_comment = {}
        for line in lines:
            # Don't strip - we need to preserve indentation for parsing
            original_line = line
            line = line.rstrip()
            
            # Extract comment number and author line (e.g., "1. @author on 2016-09-21T15:51:26Z")
            if line and line[0].isdigit() and '.' in line and '@' in line:
                # Finalize previous comment if exists
                if current_comment and current_comment.get('author'):
                    current_comment['body'] = current_comment['body'].strip()
                    current_comment['id'] = str(hash(current_comment['author'] + current_comment['created_at']) % 1000000)
                    comments.append(current_comment.copy())
                
                # Remove color codes like [cyan] and [/cyan]
                clean_line = line
                import re
                clean_line = re.sub(r'\[\/?\w+\]', '', clean_line)
                
                # Extract comment details
                parts = clean_line.split(' ', 3)  # "1.", "@author", "on", "date"
                if len(parts) >= 3 and parts[1] and parts[1].startswith('@'):
                    author = parts[1][1:]  # Remove @
                    # Date info is in parts[2] and possibly parts[3]
                    date_str = ' '.join(parts[2:]) if len(parts) > 2 else ''
                    current_comment = {
                        'author': author,
                        'created_at': date_str,
                        'body': '',
                        'url': self._extract_url_from_command(self.action_command)
                    }
            
            # Extract comment body (indented lines)
            elif line.startswith('   ') and current_comment:
                # Remove indentation and add to body
                body_line = line[3:].strip()
                if body_line and 'URL:' not in body_line and '🔗' not in body_line:
                    current_comment['body'] += body_line + ' '
                elif '🔗' in body_line:
                    # Extract URL if present
                    url_part = body_line.split('🔗')[1].strip()
                    if url_part:
                        current_comment['url'] = url_part
        
        # Finalize last comment if exists
        if current_comment and current_comment.get('author'):
            current_comment['body'] = current_comment['body'].strip()
            current_comment['id'] = str(hash(current_comment['author'] + current_comment['created_at']) % 1000000)
            comments.append(current_comment)
        
        return comments
    
    def _extract_url_from_command(self, command: str) -> str:
        """Extract GitHub URL from the command string"""
        try:
            # Extract owner/repo#pr from command like "github-comments example-org/example-repo#16623"
            import re
            match = re.search(r'github-comments\s+([^#\s]+)#(\d+)', command)
            if match:
                owner_repo = match.group(1)
                pr_number = match.group(2)
                return f"https://github.com/{owner_repo}/pull/{pr_number}"
        except:
            pass
        return ""


class WorkflowMonitor:
    """Main workflow monitor that manages multiple monitor types"""
    
    def __init__(self):
        self.monitors: Dict[str, Any] = {}
        self.running = False
        self.config_file = os.path.expanduser('~/.wf_monitor_config.json')
        self.config = self._load_config()
        self.wf_config = self._load_wf_config()
        
    def _load_config(self) -> Dict:
        """Load monitor configuration"""
        if os.path.exists(self.config_file):
            try:
                with open(self.config_file, 'r') as f:
                    return json.load(f)
            except:
                pass
        
        return {
            'monitors': {},
            'interval': 30
        }
    
    def _load_wf_config(self) -> Dict:
        """Load main workflow configuration for GitHub token"""
        try:
            from workflow.config import load_config
            return load_config()
        except:
            return {}
    
    def _save_config(self):
        """Save monitor configuration"""
        try:
            with open(self.config_file, 'w') as f:
                json.dump(self.config, f, indent=2)
        except Exception as e:
            console.print(f"❌ Failed to save config: {e}")
    
    def add_monitor_from_config(self, monitor_config: Dict[str, Any]) -> bool:
        """Add a monitor from a configuration dictionary"""
        try:
            name = monitor_config.get('name')
            monitor_type = monitor_config.get('type')
            
            if not name or not monitor_type:
                console.print("❌ Monitor config must have 'name' and 'type'")
                return False
            
            if monitor_type == 'file':
                path = monitor_config.get('path')
                if not path or not os.path.exists(path):
                    console.print(f"❌ Invalid path for file monitor: {path}")
                    return False
                
                config = {k: v for k, v in monitor_config.items() if k not in ['name', 'type']}
                monitor = FileMonitor(path, config)
                
            elif monitor_type == 'github_pr':
                owner = monitor_config.get('owner')
                repo = monitor_config.get('repo')
                pr_number = monitor_config.get('pr_number')
                
                if not all([owner, repo, pr_number]):
                    console.print("❌ GitHub PR monitor requires owner, repo, and pr_number")
                    return False
                
                config = {k: v for k, v in monitor_config.items() if k not in ['name', 'type']}
                
                # Use GitHub token from workflow config if available
                if not config.get('github_token'):
                    github_token = self.wf_config.get("github", {}).get("token")
                    if github_token:
                        config['github_token'] = github_token
                
                monitor = GitHubPRMonitor(owner, repo, pr_number, config)
                
            elif monitor_type == 'github_comment':
                owner = monitor_config.get('owner')
                repo = monitor_config.get('repo')
                pr_number = monitor_config.get('pr_number')
                
                if not all([owner, repo, pr_number]):
                    console.print("❌ GitHub comment monitor requires owner, repo, and pr_number")
                    return False
                
                config = {k: v for k, v in monitor_config.items() if k not in ['name', 'type']}
                
                # Use GitHub token from workflow config if available
                if not config.get('github_token'):
                    github_token = self.wf_config.get("github", {}).get("token")
                    if github_token:
                        config['github_token'] = github_token
                
                monitor = GitHubPRCommentMonitor(owner, repo, pr_number, config)
                
            elif monitor_type == 'github_comment_action':
                owner = monitor_config.get('owner')
                repo = monitor_config.get('repo')
                pr_number = monitor_config.get('pr_number')
                
                if not all([owner, repo, pr_number]):
                    console.print("❌ GitHub comment action monitor requires owner, repo, and pr_number")
                    return False
                
                # Create action-based monitor using GitHub comment lookup action
                action_config = {
                    'owner': owner,
                    'repo': repo,
                    'pr_number': pr_number,
                    'limit': monitor_config.get('limit', 10),
                    'since': monitor_config.get('since')
                }
                
                monitor_config_data = {k: v for k, v in monitor_config.items() if k not in ['name', 'type']}
                
                monitor = ActionBasedMonitor(
                    name=monitor_config.get('name', 'github_comments'),
                    action_type='github_comment_lookup',
                    action_config=action_config,
                    monitor_config=monitor_config_data
                )
                
            elif monitor_type == 'api':
                url = monitor_config.get('url')
                if not url:
                    console.print("❌ API monitor requires url")
                    return False
                
                config = {k: v for k, v in monitor_config.items() if k not in ['name', 'type']}
                monitor = APIMonitor(name, url, config)
                
            elif monitor_type == 'cli_action':
                action_command = monitor_config.get('action_command')
                if not action_command:
                    console.print("❌ CLI action monitor requires action_command")
                    return False
                
                config = {k: v for k, v in monitor_config.items() if k not in ['name', 'type']}
                monitor = CLIActionMonitor(name, action_command, config)
                
            else:
                console.print(f"❌ Unknown monitor type: {monitor_type}")
                return False
            
            # Add monitor
            self.monitors[name] = monitor
            self.config['monitors'][name] = monitor_config
            self._save_config()
            
            console.print(f"✅ Added {monitor_type} monitor '{name}'")
            return True
            
        except Exception as e:
            console.print(f"❌ Failed to add monitor from config: {e}")
            return False

    def add_file_monitor(self, name: str, path: str, action: str = None, **kwargs):
        """Add a file system monitor"""
        if not os.path.exists(path):
            console.print(f"❌ Path '{path}' does not exist")
            return False
        
        config = {'path': path, 'action': action, **kwargs}
        monitor = FileMonitor(path, config)
        
        self.monitors[name] = monitor
        self.config['monitors'][name] = {'type': 'file', 'config': config}
        self._save_config()
        
        action_msg = f" with action: {action}" if action else ""
        console.print(f"✅ Added file monitor '{name}' for '{path}'{action_msg}")
        return True
    
    def add_github_monitor(self, name: str, owner: str, repo: str, pr_number: int, action: str = None, **kwargs):
        """Add a GitHub PR monitor"""
        config = {'owner': owner, 'repo': repo, 'pr_number': pr_number, 'action': action, **kwargs}
        
        # Use GitHub token from workflow config if available
        if not config.get('github_token'):
            github_token = self.wf_config.get("github", {}).get("token")
            if github_token:
                config['github_token'] = github_token
                console.print(f"🔐 Using GitHub token from workflow config")
        
        monitor = GitHubPRMonitor(owner, repo, pr_number, config)
        
        self.monitors[name] = monitor
        self.config['monitors'][name] = {'type': 'github_pr', 'config': config}
        self._save_config()
        
        action_msg = f" with action: {action}" if action else ""
        console.print(f"✅ Added GitHub PR monitor '{name}' for {owner}/{repo}#{pr_number}{action_msg}")
        return True
    
    def add_github_comment_monitor(self, name: str, owner: str, repo: str, pr_number: int, action: str = None, **kwargs):
        """Add a GitHub PR comment monitor"""
        config = {'owner': owner, 'repo': repo, 'pr_number': pr_number, 'action': action, **kwargs}
        
        # Use GitHub token from workflow config if available
        if not config.get('github_token'):
            github_token = self.wf_config.get("github", {}).get("token")
            if github_token:
                config['github_token'] = github_token
                console.print(f"🔐 Using GitHub token from workflow config")
        
        monitor = GitHubPRCommentMonitor(owner, repo, pr_number, config)
        
        self.monitors[name] = monitor
        self.config['monitors'][name] = {'type': 'github_pr_comment', 'config': config}
        self._save_config()
        
        action_msg = f" with action: {action}" if action else ""
        console.print(f"✅ Added GitHub PR comment monitor '{name}' for {owner}/{repo}#{pr_number}{action_msg}")
        return True
    
    def add_api_monitor(self, name: str, url: str, action: str = None, **kwargs):
        """Add an API monitor"""
        config = {'url': url, 'action': action, **kwargs}
        monitor = APIMonitor(name, url, config)
        
        self.monitors[name] = monitor
        self.config['monitors'][name] = {'type': 'api', 'config': config}
        self._save_config()
        
        action_msg = f" with action: {action}" if action else ""
        console.print(f"✅ Added API monitor '{name}' for '{url}'{action_msg}")
        return True
    
    def add_cli_action_monitor(self, name: str, action_command: str, action: str = None, **kwargs):
        """Add a CLI action monitor"""
        config = {'action_command': action_command, 'action': action, **kwargs}
        monitor = CLIActionMonitor(name, action_command, config)
        
        self.monitors[name] = monitor
        self.config['monitors'][name] = {'type': 'cli_action', 'config': config}
        self._save_config()
        
        action_msg = f" with action: {action}" if action else ""
        console.print(f"✅ Added CLI action monitor '{name}' for command 'wf {action_command}'{action_msg}")
        return True
    
    def remove_monitor(self, name: str):
        """Remove a monitor"""
        if name in self.monitors:
            del self.monitors[name]
            if name in self.config['monitors']:
                del self.config['monitors'][name]
            self._save_config()
            console.print(f"✅ Removed monitor '{name}'")
            return True
        else:
            console.print(f"❌ Monitor '{name}' not found")
            return False
    
    def list_monitors(self):
        """List all configured monitors"""
        if not self.monitors:
            console.print("No monitors configured")
            return
        
        table = Table(title="Configured Monitors")
        table.add_column("Name", style="cyan")
        table.add_column("Type", style="green")
        table.add_column("Target", style="yellow")
        table.add_column("Status", style="magenta")
        
        for name, monitor in self.monitors.items():
            monitor_type = type(monitor).__name__.replace('Monitor', '')
            target = ""
            
            if isinstance(monitor, FileMonitor):
                target = monitor.path
            elif isinstance(monitor, GitHubPRMonitor):
                target = f"{monitor.owner}/{monitor.repo}#{monitor.pr_number}"
            elif isinstance(monitor, APIMonitor):
                target = monitor.url
            
            status = "🟢 Active" if getattr(monitor, 'running', False) else "⚪ Inactive"
            
            table.add_row(name, monitor_type, target, status)
        
        console.print(table)
    
    def start_monitoring(self, duration: int = None, interval: int = None):
        """Start monitoring all configured monitors"""
        if not self.monitors:
            console.print("❌ No monitors configured. Use 'wf monitor add-*' commands to add monitors.")
            return
        
        interval = interval or self.config.get('interval', 2)
        console.print(f"🔍 Starting monitoring (interval: {interval}s)")
        
        if duration:
            console.print(f"⏱️  Will run for {duration} seconds")
        
        # Initialize last state for all monitors
        for name, monitor in self.monitors.items():
            monitor.running = True
            if isinstance(monitor, FileMonitor):
                # Initialize file list quietly (establish baseline)
                monitor.check()
                monitor_logger.info(f"[{name}] File monitor initialized for {monitor.path}")
            elif isinstance(monitor, GitHubPRMonitor):
                monitor_logger.info(f"[{name}] GitHub PR monitor initialized for {monitor.owner}/{monitor.repo}#{monitor.pr_number}")
            elif isinstance(monitor, APIMonitor):
                monitor_logger.info(f"[{name}] API monitor initialized for {monitor.url}")
        
        start_time = time.time()
        
        try:
            if duration:
                # Fixed duration mode
                start_time = time.time()
                end_time = start_time + duration
                
                while time.time() < end_time:
                    current_time = time.time()
                    
                    # Check all monitors
                    all_changes = []
                    for name, monitor in self.monitors.items():
                        if monitor.running:
                            changes = monitor.check()
                            for change in changes:
                                change['monitor_name'] = name
                                all_changes.append(change)
                    
                    # Display changes
                    if all_changes:
                        self._display_changes(all_changes)
                    
                    time.sleep(interval)
                
                console.print(f"⏱️  Monitoring stopped after {duration} seconds")
            else:
                # Indefinite mode with Live display
                with Live(console=console, refresh_per_second=1) as live:
                    try:
                        while True:
                            # Check all monitors
                            all_changes = []
                            for name, monitor in self.monitors.items():
                                if monitor.running:
                                    changes = monitor.check()
                                    for change in changes:
                                        change['monitor_name'] = name
                                        all_changes.append(change)
                            
                            # Display changes in live
                            if all_changes:
                                self._display_changes(all_changes)
                            
                            time.sleep(interval)
                    except KeyboardInterrupt:
                        console.print("\n🛑 Monitoring stopped by user")
                
        except KeyboardInterrupt:
            console.print("\n🛑 Monitoring stopped by user")
        
        finally:
            # Stop all monitors
            for monitor in self.monitors.values():
                monitor.running = False
    
    def _display_changes(self, changes: List[Dict]):
        """Display, log, and action on monitoring changes"""
        for change in changes:
            monitor_name = change.get('monitor_name', 'Unknown')
            timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            
            # Log the change
            log_message = ""
            
            if change.get('type') == 'error':
                log_message = f"[{monitor_name}] Error: {change.get('message', 'Unknown error')}"
                console.print(f"❌ {log_message}")
                monitor_logger.error(log_message)
            
            elif change['type'] == 'created':
                path = change['path']
                size = self._format_size(change.get('size', 0))
                log_message = f"[{monitor_name}] Created: {path} ({size})"
                console.print(f"📝 {log_message}")
                monitor_logger.info(log_message)
            
            elif change['type'] == 'modified':
                path = change['path']
                size = self._format_size(change.get('size', 0))
                log_message = f"[{monitor_name}] Modified: {path} ({size})"
                console.print(f"✏️  {log_message}")
                monitor_logger.info(log_message)
            
            elif change['type'] == 'deleted':
                path = change['path']
                log_message = f"[{monitor_name}] Deleted: {path}"
                console.print(f"🗑️  {log_message}")
                monitor_logger.info(log_message)
            
            elif change['type'] == 'new_commit':
                author = change.get('author', 'Unknown')
                message_full = change.get('message', 'No message')
                log_message = f"[{monitor_name}] New commit by {author}: {message_full[:50]}... ({change.get('sha', 'unknown')})"
                console.print(f"🔧 {log_message}")
                monitor_logger.info(f"[{monitor_name}] New commit by {author}: {message_full} ({change.get('sha', 'unknown')})")
            
            elif change['type'] == 'data_change':
                name = change.get('name', monitor_name)
                log_message = f"[{monitor_name}] API data changed for {name}"
                console.print(f"🌐 {log_message}")
                monitor_logger.info(log_message)
            
            # Execute action if configured
            monitor_logger.info(f"[{monitor_name}] Change detected: {change.get('type')} - executing action")
            self._execute_action(change, monitor_name, log_message)
    
    def _execute_action(self, change: Dict, monitor_name: str, log_message: str):
        """Execute configured action for a monitor change"""
        try:
            # Get monitor configuration to find action
            if monitor_name not in self.monitors:
                return
                
            monitor = self.monitors[monitor_name]
            config = getattr(monitor, 'config', {})
            
            # Check for new action framework configuration
            if 'actions' in config:
                self._execute_new_actions(change, monitor_name, log_message, config['actions'])
            elif 'action' in config and config['action'] is not None:
                self._execute_legacy_action(change, monitor_name, log_message, config['action'])
            else:
                return  # No action configured
                        
        except Exception as e:
            monitor_logger.error(f"[{monitor_name}] Action error: {str(e)}")
            console.print(f"❌ [{monitor_name}] Action error: {str(e)}")
    
    def _execute_new_actions(self, change: Dict, monitor_name: str, log_message: str, actions_config):
        """Execute actions using the new action framework"""
        try:
            action_registry = get_action_registry()
            monitor_logger.info(f"[{monitor_name}] Executing {len(actions_config)} new actions")
            
            # Prepare common context for all actions
            context = {
                'monitor_change_type': change.get('type', 'unknown'),
                'monitor_name': monitor_name,
                'monitor_timestamp': datetime.now().isoformat(),
                'monitor_log_message': log_message,
                'change_data': change
            }
            
            # Add change-specific context
            if change.get('type') in ['created', 'modified', 'deleted']:
                context.update({
                    'file_path': change.get('path', ''),
                    'file_size': str(change.get('size', 0))
                })
            elif change.get('type') == 'new_commit':
                context.update({
                    'commit_author': change.get('author', 'Unknown'),
                    'commit_message': change.get('message', 'No message'),
                    'commit_sha': change.get('sha', 'unknown')
                })
            elif change.get('type') == 'data_change':
                context.update({
                    'api_name': change.get('name', monitor_name),
                    'api_data': str(change.get('data', ''))
                })
            elif change.get('type') in ['new_comment', 'new_comments']:
                # GitHub comment context
                if 'comments' in change and change['comments']:
                    latest_comment = change['comments'][0]  # Most recent comment
                    context.update({
                        'comment_author': latest_comment.get('author', 'Unknown'),
                        'comment_body': latest_comment.get('body', 'No comment'),
                        'comment_url': latest_comment.get('url', ''),
                        'comment_id': latest_comment.get('id', ''),
                        'comment_created_at': latest_comment.get('created_at', ''),
                        'url': latest_comment.get('url', '') or change.get('url', '')  # For notification URL handling
                    })
                else:
                    # Fallback to single comment format
                    context.update({
                        'comment_author': change.get('author', 'Unknown'),
                        'comment_body': change.get('body', 'No comment'),
                        'comment_url': change.get('url', ''),
                        'comment_id': change.get('comment_id', ''),
                        'comment_created_at': change.get('created_at', ''),
                        'url': change.get('url', '')  # For notification URL handling
                    })
            elif change.get('type') == 'error':
                context['error_message'] = change.get('message', 'Unknown error')
            
            # Execute each configured action
            for i, action_config in enumerate(actions_config):
                monitor_logger.info(f"[{monitor_name}] Processing action {i}: {action_config}")
                if isinstance(action_config, str):
                    # Simple string - treat as action ID
                    action_id = action_config
                    action = action_registry.get_action(action_id)
                    if action:
                        result = action.execute(context)
                        self._log_action_result(monitor_name, action_id, result)
                    else:
                        monitor_logger.warning(f"[{monitor_name}] Action '{action_id}' not found")
                        
                elif isinstance(action_config, dict):
                    # Detailed action configuration
                    action_id = action_config.get('id')
                    action_type = action_config.get('type')
                    
                    if action_id and action_type:
                        # Create or get action
                        action = action_registry.get_action(action_id)
                        if not action:
                            action = create_action_from_config(action_config)
                            if action:
                                action_registry.register_action(action)
                        
                        if action:
                            # Merge action-specific config with context
                            action_context = {**context, **action_config}
                            result = action.execute(action_context)
                            self._log_action_result(monitor_name, action_id, result)
                        else:
                            monitor_logger.warning(f"[{monitor_name}] Could not create action '{action_id}'")
                            
        except Exception as e:
            monitor_logger.error(f"[{monitor_name}] New action execution error: {str(e)}")
            console.print(f"❌ [{monitor_name}] Action execution error: {str(e)}")
    
    def _execute_legacy_action(self, change: Dict, monitor_name: str, log_message: str, action_command):
        """Execute legacy shell command action"""
        try:
            # Prepare environment variables for action
            env_vars = os.environ.copy()
            env_vars.update({
                'MONITOR_CHANGE_TYPE': change.get('type', 'unknown'),
                'MONITOR_NAME': monitor_name,
                'MONITOR_TIMESTAMP': datetime.now().isoformat(),
                'MONITOR_LOG_MESSAGE': log_message
            })
            
            # Add change-specific variables
            if change.get('type') in ['created', 'modified', 'deleted']:
                env_vars['MONITOR_FILE_PATH'] = change.get('path', '')
                env_vars['MONITOR_FILE_SIZE'] = str(change.get('size', 0))
            elif change.get('type') == 'new_commit':
                env_vars['MONITOR_COMMIT_AUTHOR'] = change.get('author', 'Unknown')
                env_vars['MONITOR_COMMIT_MESSAGE'] = change.get('message', 'No message')
                env_vars['MONITOR_COMMIT_SHA'] = change.get('sha', 'unknown')
            elif change.get('type') == 'data_change':
                env_vars['MONITOR_API_NAME'] = change.get('name', monitor_name)
                env_vars['MONITOR_API_DATA'] = str(change.get('data', ''))
            elif change.get('type') == 'cli_action_change':
                # CLI Action Monitor variables
                result = change.get('result', {})
                env_vars['MONITOR_CLI_COMMAND'] = result.get('command', 'unknown')
                env_vars['MONITOR_CLI_OUTPUT'] = result.get('output', '')
                env_vars['MONITOR_CLI_TIMESTAMP'] = result.get('timestamp', '')
                
                # Try to extract structured data from common CLI commands
                output = result.get('output', '')
                if 'github-comments' in result.get('command', ''):
                    # Extract GitHub comment data
                    env_vars['MONITOR_GITHUB_COMMENTS'] = output
                    # Try to extract comment count
                    lines = output.split('\n')
                    for line in lines:
                        if 'Found' in line and 'comments' in line:
                            import re
                            match = re.search(r'Found (\d+) comments', line)
                            if match:
                                env_vars['MONITOR_COMMENT_COUNT'] = match.group(1)
                                break
                elif 'status' in result.get('command', ''):
                    # Extract status data
                    env_vars['MONITOR_STATUS_OUTPUT'] = output
                    # Try to extract task key
                    lines = output.split('\n')
                    for line in lines:
                        if 'Task Key:' in line:
                            task_key = line.split('Task Key:')[1].strip()
                            env_vars['MONITOR_TASK_KEY'] = task_key
                            break
            elif change.get('type') == 'error':
                env_vars['MONITOR_ERROR_MESSAGE'] = change.get('message', 'Unknown error')
            
            # Execute action with proper shell escaping
            result = subprocess.run(
                action_command,
                shell=True,
                env=env_vars,
                capture_output=True,
                text=True,
                timeout=30  # 30 second timeout for actions
            )
            
            self._log_action_result(monitor_name, action_command, {
                'success': result.returncode == 0,
                'return_code': result.returncode,
                'stdout': result.stdout,
                'stderr': result.stderr
            })
                        
        except subprocess.TimeoutExpired:
            monitor_logger.error(f"[{monitor_name}] Legacy action timeout: {action_command}")
            console.print(f"⏰ [{monitor_name}] Action timeout: {action_command}")
        except Exception as e:
            monitor_logger.error(f"[{monitor_name}] Legacy action error: {str(e)}")
            console.print(f"❌ [{monitor_name}] Action error: {str(e)}")
    
    def _log_action_result(self, monitor_name: str, action_identifier: str, result: Dict[str, Any]):
        """Log and display action execution result"""
        if result.get('success', False):
            monitor_logger.info(f"[{monitor_name}] Action executed successfully: {action_identifier}")
            console.print(f"🚀 [{monitor_name}] Action executed: {action_identifier}")
            
            # Log additional output if available
            if result.get('stdout'):
                monitor_logger.info(f"[{monitor_name}] Action output: {result['stdout']}")
        else:
            error_msg = result.get('error', result.get('stderr', 'Unknown error'))
            monitor_logger.error(f"[{monitor_name}] Action failed: {action_identifier} - {error_msg}")
            console.print(f"❌ [{monitor_name}] Action failed: {action_identifier}")
            if error_msg:
                console.print(f"   Error: {error_msg}")
    
    def _run_ephemeral_monitor(self, monitor, monitor_name, duration=None, interval=2, background=False):
        """Run an ephemeral monitor that is not saved to config"""
        if background:
            # Generate unique name and run in background
            import uuid
            import atexit
            
            unique_name = f"bg-{str(uuid.uuid4())[:8]}"
            self.monitors[unique_name] = monitor
            
            try:
                # Create background process
                proc = subprocess.Popen([
                    sys.executable, '-c', f'''
import sys
import os
import time
import subprocess
import signal
from pathlib import Path

# Add workflow path
sys.path.insert(0, str(Path("{Path(__file__).parent}")))

def signal_handler(signum, frame):
    print("\\n🛑 Background monitor stopped")
    sys.exit(0)

signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)

# Simple monitoring loop
monitor = {monitor.__class__.__name__}("{getattr(monitor, 'path', getattr(monitor, 'url', ''))}" if hasattr(monitor, 'path') else f'"{getattr(monitor, 'url', '')}"', "{getattr(monitor, 'name', '')}" if hasattr(monitor, 'name') else '""', {monitor.config})

monitor.running = True
start_time = time.time()

if {duration}:
    end_time = start_time + {duration}
    while time.time() < end_time:
        changes = monitor.check()
        if changes:
            for change in changes:
                if monitor.config.get('action'):
                    result = subprocess.run(monitor.config.get('action'), shell=True, capture_output=True, text=True, timeout=30)
                    if result.returncode == 0 and result.stdout:
                        print(f"🚀 Action output: {{result.stdout.strip()}}")
        time.sleep({interval})
else:
    try:
        while True:
            changes = monitor.check()
            if changes:
                for change in changes:
                    if monitor.config.get('action'):
                        result = subprocess.run(monitor.config.get('action'), shell=True, capture_output=True, text=True, timeout=30)
                        if result.returncode == 0 and result.stdout:
                            print(f"🚀 Action output: {{result.stdout.strip()}}")
            time.sleep({interval})
    except KeyboardInterrupt:
        print("\\n🛑 Background monitor stopped")
'''], start_new_session=True)
                
                # Cleanup function
                def cleanup():
                    try:
                        proc.terminate()
                        if unique_name in self.monitors:
                            del self.monitors[unique_name]
                    except:
                        pass
                
                atexit.register(cleanup)
                
                console.print(f"🚀 Ephemeral monitor started in background (PID: {proc.pid})")
                console.print(f"💡 Use 'kill {proc.pid}' to stop the monitor")
                return proc.pid
                
            except Exception as e:
                console.print(f"❌ Failed to start background monitor: {e}")
                if unique_name in self.monitors:
                    del self.monitors[unique_name]
                return
        
        console.print(f"🔍 Starting ephemeral monitor (interval: {interval}s)")
        
        if duration:
            console.print(f"⏱️  Will run for {duration} seconds")
        
        monitor.running = True
        start_time = time.time()
        
        try:
            if duration:
                # Fixed duration mode
                end_time = start_time + duration
                
                while time.time() < end_time:
                    changes = monitor.check()
                    if changes:
                        for change in changes:
                            change['monitor_name'] = monitor_name
                            console.print(f"🔄 [{monitor_name}] Change detected: {change.get('type', 'unknown')}")
                            
                            # Always show CLI results for cli_action_change
                            if change.get('type') == 'cli_action_change':
                                console.print(f"📋 [{monitor_name}] CLI output changed:")
                                result = change.get('result', {})
                                if result.get('output'):
                                    # Show the CLI output, truncated if too long
                                    output = result['output']
                                    max_lines = 10
                                    lines = output.split('\n')
                                    if len(lines) > max_lines:
                                        for line in lines[:max_lines]:
                                            console.print(f"   {line}")
                                        console.print(f"   ... ({len(lines) - max_lines} more lines)")
                                    else:
                                        for line in lines:
                                            console.print(f"   {line}")
                                else:
                                    console.print(f"   No output available")
                            
                            # Execute action if configured
                            console.print(f"🔍 Monitor config: {monitor.config}")
                            if monitor.config.get('actions'):
                                # Use new action framework
                                console.print(f"🎯 Executing {len(monitor.config.get('actions'))} actions for {monitor_name}")
                                self._execute_new_actions(change, monitor_name, f"CLI action change detected: {change.get('type', 'unknown')}", monitor.config.get('actions'))
                            elif monitor.config.get('action'):
                                # For ephemeral monitors, execute action with environment variables
                                action = monitor.config.get('action')
                                
                                # Prepare environment variables for action
                                env_vars = os.environ.copy()
                                env_vars.update({
                                    'MONITOR_CHANGE_TYPE': change.get('type', 'unknown'),
                                    'MONITOR_NAME': monitor_name,
                                    'MONITOR_TIMESTAMP': datetime.now().isoformat(),
                                    'MONITOR_LOG_MESSAGE': f"CLI action change detected: {change.get('type', 'unknown')}"
                                })
                                
                                # Add CLI action specific variables
                                if change.get('type') == 'cli_action_change':
                                    result_data = change.get('result', {})
                                    env_vars['MONITOR_CLI_COMMAND'] = result_data.get('command', 'unknown')
                                    env_vars['MONITOR_CLI_OUTPUT'] = result_data.get('output', '')
                                    env_vars['MONITOR_CLI_TIMESTAMP'] = result_data.get('timestamp', '')
                                    
                                    # Try to extract structured data from common CLI commands
                                    output = result_data.get('output', '')
                                    if 'github-comments' in result_data.get('command', ''):
                                        # Extract GitHub comment data
                                        env_vars['MONITOR_GITHUB_COMMENTS'] = output
                                        # Try to extract comment count
                                        lines = output.split('\n')
                                        for line in lines:
                                            if 'Found' in line and 'comments' in line:
                                                import re
                                                match = re.search(r'Found (\d+) comments', line)
                                                if match:
                                                    env_vars['MONITOR_COMMENT_COUNT'] = match.group(1)
                                                    break
                                    elif 'status' in result_data.get('command', ''):
                                        # Extract status data
                                        env_vars['MONITOR_STATUS_OUTPUT'] = output
                                        # Try to extract task key
                                        lines = output.split('\n')
                                        for line in lines:
                                            if 'Task Key:' in line:
                                                task_key = line.split('Task Key:')[1].strip()
                                                env_vars['MONITOR_TASK_KEY'] = task_key
                                                break
                                
                                result = subprocess.run(
                                    action,
                                    shell=True,
                                    env=env_vars,
                                    capture_output=True,
                                    text=True,
                                    timeout=30
                                )
                                if result.returncode == 0:
                                    if result.stdout:
                                        console.print(f"🚀 [{monitor_name}] Action output: {result.stdout.strip()}")
                                    else:
                                        console.print(f"🚀 [{monitor_name}] Action executed: {action}")
                                else:
                                    console.print(f"❌ [{monitor_name}] Action failed: {action}")
                            elif change.get('type') == 'error':
                                console.print(f"❌ [{monitor_name}] Error: {change.get('message', 'Unknown error')}")
                    
                    time.sleep(interval)
                
                console.print(f"⏱️  Ephemeral monitor stopped after {duration} seconds")
            else:
                # Indefinite mode
                try:
                    while True:
                        changes = monitor.check()
                        if changes:
                            for change in changes:
                                change['monitor_name'] = monitor_name
                                console.print(f"🔄 [{monitor_name}] Change detected: {change.get('type', 'unknown')}")
                                
                                # Always show CLI results for cli_action_change
                                if change.get('type') == 'cli_action_change':
                                    console.print(f"📋 [{monitor_name}] CLI output changed:")
                                    result = change.get('result', {})
                                    if result.get('output'):
                                        # Show the CLI output, truncated if too long
                                        output = result['output']
                                        max_lines = 10
                                        lines = output.split('\n')
                                        if len(lines) > max_lines:
                                            for line in lines[:max_lines]:
                                                console.print(f"   {line}")
                                            console.print(f"   ... ({len(lines) - max_lines} more lines)")
                                        else:
                                            for line in lines:
                                                console.print(f"   {line}")
                                    else:
                                        console.print(f"   No output available")
                                
                                # Execute action if configured
                                monitor_config = getattr(monitor, 'config', getattr(monitor, 'monitor_config', {}))
                                console.print(f"🔍 Monitor config (indefinite): {monitor_config}")
                                if monitor_config.get('actions'):
                                    # Use new action framework
                                    console.print(f"🎯 Executing {len(monitor_config.get('actions'))} actions for {monitor_name}")
                                    self._execute_new_actions(change, monitor_name, f"CLI action change detected: {change.get('type', 'unknown')}", monitor_config.get('actions'))
                                elif monitor_config.get('action'):
                                    action = monitor_config.get('action')
                                    
                                    # Prepare environment variables for action
                                    env_vars = os.environ.copy()
                                    env_vars.update({
                                        'MONITOR_CHANGE_TYPE': change.get('type', 'unknown'),
                                        'MONITOR_NAME': monitor_name,
                                        'MONITOR_TIMESTAMP': datetime.now().isoformat(),
                                        'MONITOR_LOG_MESSAGE': f"CLI action change detected: {change.get('type', 'unknown')}"
                                    })
                                    
                                    # Add CLI action specific variables
                                    if change.get('type') == 'cli_action_change':
                                        result_data = change.get('result', {})
                                        env_vars['MONITOR_CLI_COMMAND'] = result_data.get('command', 'unknown')
                                        env_vars['MONITOR_CLI_OUTPUT'] = result_data.get('output', '')
                                        env_vars['MONITOR_CLI_TIMESTAMP'] = result_data.get('timestamp', '')
                                        
                                        # Try to extract structured data from common CLI commands
                                        output = result_data.get('output', '')
                                        if 'github-comments' in result_data.get('command', ''):
                                            # Extract GitHub comment data
                                            env_vars['MONITOR_GITHUB_COMMENTS'] = output
                                            # Try to extract comment count
                                            lines = output.split('\n')
                                            for line in lines:
                                                if 'Found' in line and 'comments' in line:
                                                    import re
                                                    match = re.search(r'Found (\d+) comments', line)
                                                    if match:
                                                        env_vars['MONITOR_COMMENT_COUNT'] = match.group(1)
                                                        break
                                        elif 'status' in result_data.get('command', ''):
                                            # Extract status data
                                            env_vars['MONITOR_STATUS_OUTPUT'] = output
                                            # Try to extract task key
                                            lines = output.split('\n')
                                            for line in lines:
                                                if 'Task Key:' in line:
                                                    task_key = line.split('Task Key:')[1].strip()
                                                    env_vars['MONITOR_TASK_KEY'] = task_key
                                                    break
                                    
                                    result = subprocess.run(
                                        action,
                                        shell=True,
                                        env=env_vars,
                                        capture_output=True,
                                        text=True,
                                        timeout=30
                                    )
                                    if result.returncode == 0:
                                        if result.stdout:
                                            console.print(f"🚀 [{monitor_name}] Action output: {result.stdout.strip()}")
                                        else:
                                            console.print(f"🚀 [{monitor_name}] Action executed: {action}")
                                    else:
                                        console.print(f"❌ [{monitor_name}] Action failed: {action}")
                                elif change.get('type') == 'error':
                                    console.print(f"❌ [{monitor_name}] Error: {change.get('message', 'Unknown error')}")
                        
                        time.sleep(interval)
                except KeyboardInterrupt:
                    console.print("\n🛑 Ephemeral monitor stopped by user")
                    
        except KeyboardInterrupt:
            console.print("\n🛑 Ephemeral monitor stopped by user")
        
        finally:
            monitor.running = False
    
    def _format_size(self, size: int) -> str:
        """Format file size"""
        for unit in ['B', 'KB', 'MB', 'GB']:
            if size < 1024.0:
                return f"{size:.1f}{unit}"
            size /= 1024.0
        return f"{size:.1f}TB"
    
    def load_saved_monitors(self):
        """Load monitors from saved configuration"""
        for name, monitor_config in self.config['monitors'].items():
            monitor_type = monitor_config['type']
            # Extract config from monitor_config (excluding name and type)
            config = {k: v for k, v in monitor_config.items() if k not in ['name', 'type']}
            
            if monitor_type == 'file':
                if os.path.exists(config['path']):
                    self.monitors[name] = FileMonitor(config['path'], config)
            
            elif monitor_type == 'github_pr':
                # Get GitHub token from workflow config
                github_token = self.wf_config.get("github", {}).get("token")
                if github_token and not config.get('github_token'):
                    config['github_token'] = github_token
                
                self.monitors[name] = GitHubPRMonitor(
                    config['owner'], 
                    config['repo'], 
                    config['pr_number'], 
                    config
                )
            
            elif monitor_type == 'github_pr_comment':
                # Get GitHub token from workflow config
                github_token = self.wf_config.get("github", {}).get("token")
                if github_token and not config.get('github_token'):
                    config['github_token'] = github_token
                
                self.monitors[name] = GitHubPRCommentMonitor(
                    config['owner'], 
                    config['repo'], 
                    config['pr_number'], 
                    config
                )
            
            elif monitor_type == 'api':
                self.monitors[name] = APIMonitor(name, config['url'], config)
            
            elif monitor_type == 'cli_action':
                action_command = config.get('config', {}).get('action_command')
                if action_command:
                    actual_config = config.get('config', {})
                    self.monitors[name] = CLIActionMonitor(name, action_command, actual_config)
            
            elif monitor_type == 'github_comment_action':
                owner = monitor_config.get('owner')
                repo = monitor_config.get('repo')
                pr_number = monitor_config.get('pr_number')
                
                if not all([owner, repo, pr_number]):
                    console.print(f"❌ GitHub comment action monitor '{name}' requires owner, repo, and pr_number")
                    continue
                
                # Create action-based monitor using GitHub comment lookup action
                action_config = {
                    'owner': owner,
                    'repo': repo,
                    'pr_number': pr_number,
                    'limit': monitor_config.get('limit', 10),
                    'since': monitor_config.get('since')
                }
                
                monitor_config_data = {k: v for k, v in monitor_config.items() if k not in ['name', 'type']}
                
                self.monitors[name] = ActionBasedMonitor(
                    name=name,
                    action_type='github_comment_lookup',
                    action_config=action_config,
                    monitor_config=monitor_config_data
                )
        
        if self.monitors:
            console.print(f"✅ Loaded {len(self.monitors)} saved monitors")


# Global monitor instance
_monitor_instance = None

def get_monitor() -> WorkflowMonitor:
    """Get or create global monitor instance"""
    global _monitor_instance
    if _monitor_instance is None:
        _monitor_instance = WorkflowMonitor()
        _monitor_instance.load_saved_monitors()
    return _monitor_instance
