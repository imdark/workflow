"""
Markdown-based task backend that emulates JIRA fields
"""
import os
import re
import yaml
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Any
from workflow.backends.base import Issue, TaskBackend


class MarkdownTask:
    """Represents a task in markdown format"""

    def __init__(self, key: str, data: Dict[str, Any], file_path: Path):
        self.key = key
        self.data = data
        self.file_path = file_path

    @property
    def title(self) -> str:
        return self.data.get('title', '')

    @property
    def description(self) -> str:
        return self.data.get('description', '')

    @property
    def status(self) -> str:
        return self.data.get('status', 'To Do')

    @property
    def issue_type(self) -> str:
        return self.data.get('type', 'Task')

    @property
    def priority(self) -> str:
        return self.data.get('priority', 'Medium')

    @property
    def assignee(self) -> Optional[str]:
        return self.data.get('assignee')

    @property
    def reporter(self) -> Optional[str]:
        return self.data.get('reporter')

    @property
    def labels(self) -> List[str]:
        return self.data.get('labels', [])

    @property
    def components(self) -> List[str]:
        return self.data.get('components', [])

    @property
    def fix_versions(self) -> List[str]:
        return self.data.get('fix_versions', [])

    @property
    def created_date(self) -> str:
        return self.data.get('created', '')

    @property
    def updated_date(self) -> str:
        return self.data.get('updated', '')

    @property
    def due_date(self) -> Optional[str]:
        return self.data.get('due_date')

    @property
    def custom_fields(self) -> Dict[str, Any]:
        return self.data.get('custom_fields', {})

    def to_issue(self) -> Issue:
        """Convert to Issue object for compatibility"""
        return Issue(
            key=self.key,
            title=self.title,
            description=self.description,
            updated=self.updated_date
        )

    def update_data(self, updates: Dict[str, Any]):
        """Update task data"""
        self.data.update(updates)
        self.data['updated'] = datetime.now().isoformat()


class MarkdownBackend(TaskBackend):
    """Markdown-based task backend"""

    def __init__(self, cfg):
        self.cfg = cfg
        self.base_dir = Path.home() / '.wf' / 'tasks'
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self._current_project = None
        self._tasks_cache = {}
        self._cache_time = {}
        self._cache_ttl = 60  # Cache for 60 seconds

    def _get_project_dir(self, project_name: Optional[str] = None) -> Path:
        """Get the directory for a project's tasks"""
        if project_name is None:
            # Try to get current project
            from workflow.projects import get_current_project
            project_name = get_current_project()

        if not project_name:
            project_name = 'default'

        project_dir = self.base_dir / project_name.lower()
        project_dir.mkdir(parents=True, exist_ok=True)
        return project_dir

    def _get_task_file_path(self, key: str, project_name: Optional[str] = None) -> Path:
        """Get the file path for a task"""
        project_dir = self._get_project_dir(project_name)
        return project_dir / f"{key.lower()}.md"

    def _parse_task_file(self, file_path: Path) -> Optional[MarkdownTask]:
        """Parse a markdown task file"""
        try:
            content = file_path.read_text(encoding='utf-8')

            # Extract frontmatter
            frontmatter_match = re.match(r'^---\n(.*?)\n---\n(.*)$', content, re.DOTALL)
            if not frontmatter_match:
                return None

            frontmatter_text, description = frontmatter_match.groups()
            data = yaml.safe_load(frontmatter_text)

            if not data:
                return None

            # Use filename as key if not in data
            key = data.get('key', file_path.stem.upper())

            # Update description from markdown content
            data['description'] = description.strip()

            return MarkdownTask(key, data, file_path)

        except Exception as e:
            print(f"Error parsing task file {file_path}: {e}")
            return None

    def _write_task_file(self, task: MarkdownTask):
        """Write a task to markdown file"""
        # Separate frontmatter from description
        frontmatter_data = task.data.copy()
        description = frontmatter_data.pop('description', '')

        # Create frontmatter
        frontmatter = yaml.dump(frontmatter_data, default_flow_style=False)

        # Write file
        content = f"---\n{frontmatter}---\n{description}\n"
        task.file_path.write_text(content, encoding='utf-8')

    def _get_next_key(self, project_name: Optional[str] = None) -> str:
        """Get the next available task key for a project"""
        project_dir = self._get_project_dir(project_name)
        if not project_name:
            project_name = 'default'

        # Get project prefix or use 'TASK'
        prefix = project_name.upper() if project_name != 'default' else 'TASK'

        # Find existing tasks
        existing_keys = []
        for file_path in project_dir.glob("*.md"):
            task = self._parse_task_file(file_path)
            if task and task.key.startswith(prefix):
                # Extract number part
                match = re.match(rf'{prefix}-(\d+)', task.key)
                if match:
                    existing_keys.append(int(match.group(1)))

        # Generate next number
        next_num = max(existing_keys, default=0) + 1
        return f"{prefix}-{next_num}"

    def _load_tasks(self, project_name: Optional[str] = None, force_reload: bool = False):
        """Load all tasks for a project"""
        project_dir = self._get_project_dir(project_name)
        cache_key = str(project_dir)

        # Check cache
        if not force_reload and cache_key in self._tasks_cache:
            cache_time = self._cache_time.get(cache_key, 0)
            if time.time() - cache_time < self._cache_ttl:
                return self._tasks_cache[cache_key]

        tasks = {}
        for file_path in project_dir.glob("*.md"):
            task = self._parse_task_file(file_path)
            if task:
                tasks[task.key] = task

        # Update cache
        self._tasks_cache[cache_key] = tasks
        self._cache_time[cache_key] = time.time()

        return tasks

    def get(self, key: str) -> Issue:
        """Get a task by key"""
        # Try to find which project this task belongs to
        for project_dir in self.base_dir.iterdir():
            if not project_dir.is_dir():
                continue

            file_path = project_dir / f"{key.lower()}.md"
            if file_path.exists():
                task = self._parse_task_file(file_path)
                if task:
                    return task.to_issue()

        raise Exception(f"Task '{key}' not found")

    def get_or_create(self, q) -> Optional[Issue]:
        """Get existing task or create new one"""
        # First try to get existing task
        try:
            issue = self.get(q)
            if issue:
                return issue
        except Exception:
            pass

        # Check if query looks like a task key (PROJECT-123 format)
        if re.match(r'^[A-Z][A-Z0-9]*-\d+$', q.upper()):
            # Create task with this key
            return self._create_from_key(q.upper())
        else:
            # Create new task with auto-generated key
            return self._create_from_text(q)

    def _create_from_key(self, key: str, title: str = None) -> Optional[Issue]:
        """Create a new task with a specific key"""
        project_name = self._extract_project_from_key(key)
        file_path = self._get_task_file_path(key, project_name)

        if file_path.exists():
            return self.get(key)

        # Create new task
        now = datetime.now().isoformat()
        data = {
            'key': key,
            'title': title if title else key,
            'description': '',
            'status': 'To Do',
            'type': 'Task',
            'priority': 'Medium',
            'created': now,
            'updated': now,
            'labels': [],
            'components': [],
            'fix_versions': [],
            'custom_fields': {}
        }

        task = MarkdownTask(key, data, file_path)
        self._write_task_file(task)

        # Clear cache
        self._clear_cache()

        return task.to_issue()

    def _create_from_text(self, text: str) -> Optional[Issue]:
        """Create a new task from text"""
        key = self._get_next_key()
        return self._create_from_key(key, title=text)

    def _extract_project_from_key(self, key: str) -> Optional[str]:
        """Extract project name from task key"""
        # Remove numbers and dash, get the prefix
        match = re.match(r'^([A-Z]+)-\d+', key)
        if match:
            prefix = match.group(1).lower()

            # Check if a project directory exists with this name
            if (self.base_dir / prefix).exists():
                return prefix

        return None

    def _clear_cache(self):
        """Clear task cache"""
        self._tasks_cache.clear()
        self._cache_time.clear()

    def list_tasks(self, project_name: Optional[str] = None, status: Optional[str] = None) -> List[MarkdownTask]:
        """List tasks with optional filtering"""
        tasks = self._load_tasks(project_name)
        task_list = list(tasks.values())

        if status:
            task_list = [t for t in task_list if t.status.lower() == status.lower()]

        # Sort by updated date (newest first)
        task_list.sort(key=lambda t: t.updated_date, reverse=True)
        return task_list

    def update_task(self, key: str, updates: Dict[str, Any]) -> bool:
        """Update a task"""
        # Find the task file
        for project_dir in self.base_dir.iterdir():
            if not project_dir.is_dir():
                continue

            file_path = project_dir / f"{key.lower()}.md"
            if file_path.exists():
                task = self._parse_task_file(file_path)
                if task:
                    task.update_data(updates)
                    self._write_task_file(task)

                    # Clear cache
                    self._clear_cache()
                    return True

        return False

    def transition_task(self, key: str, new_status: str) -> bool:
        """Transition task to new status"""
        return self.update_task(key, {'status': new_status})

    def delete_task(self, key: str) -> bool:
        """Delete a task"""
        for project_dir in self.base_dir.iterdir():
            if not project_dir.is_dir():
                continue

            file_path = project_dir / f"{key.lower()}.md"
            if file_path.exists():
                file_path.unlink()
                self._clear_cache()
                return True

        return False

    def get_projects(self) -> List[str]:
        """Get list of all projects with tasks"""
        projects = []
        for project_dir in self.base_dir.iterdir():
            if project_dir.is_dir() and any(project_dir.glob("*.md")):
                projects.append(project_dir.name)
        return projects

    def create_project(self, project_name: str, config: Optional[Dict[str, Any]] = None):
        """Create a new project directory with optional config"""
        project_dir = self.base_dir / project_name.lower()
        project_dir.mkdir(parents=True, exist_ok=True)

        if config:
            config_file = project_dir / 'project_config.yaml'
            config_file.write_text(yaml.dump(config), encoding='utf-8')

    def get_project_config(self, project_name: str) -> Dict[str, Any]:
        """Get project configuration"""
        project_dir = self.base_dir / project_name.lower()
        config_file = project_dir / 'project_config.yaml'

        if config_file.exists():
            return yaml.safe_load(config_file.read_text(encoding='utf-8')) or {}

        return {}

    def select_interactively(self, include_others=False):
        """Select a task interactively"""
        from prompt_toolkit import prompt
        from prompt_toolkit.completion import FuzzyWordCompleter

        tasks = self.list_tasks()
        if not tasks:
            print("No tasks found")
            return None

        # Create choices
        choices = [f"{t.key}: {t.title}" for t in tasks]
        completer = FuzzyWordCompleter(choices)

        try:
            result = prompt("Select task: ", completer=completer)
            # Extract key from selection
            key = result.split(':')[0].strip()
            return self.get(key)
        except (EOFError, KeyboardInterrupt):
            return None

    def move_to_in_progress(self, issue, custom_fields=None):
        """Move task to In Progress status"""
        self.transition_task(issue.key, 'In Progress')

    def move_to_done(self, issue):
        """Move task to Done status"""
        self.transition_task(issue.key, 'Done')

    def move_to_review(self, issue):
        """Move task to In Review status"""
        self.transition_task(issue.key, 'In Review')

    def create_issue(self, summary, description, issue_type="Task", project_key=None, custom_fields=None):
        """Create a new issue/task"""
        # Get the project
        from workflow.projects import get_current_project
        current_project = project_key or get_current_project()

        # Generate next key
        key = self._get_next_key(current_project)

        # Create the task
        now = datetime.now().isoformat()
        data = {
            'key': key,
            'title': summary,
            'description': description or '',
            'status': 'To Do',
            'type': issue_type,
            'priority': 'Medium',
            'created': now,
            'updated': now,
            'labels': [],
            'components': [],
            'fix_versions': [],
            'custom_fields': custom_fields or {}
        }

        file_path = self._get_task_file_path(key, current_project)
        task = MarkdownTask(key, data, file_path)
        self._write_task_file(task)
        self._clear_cache()

        return task.to_issue()

    def get_assigned_tasks(self):
        """Get all tasks (for compatibility with Jira backend)"""
        return [t.to_issue() for t in self.list_tasks()]

    def is_done(self, issue):
        """Check if a task is done"""
        try:
            # Find task file
            for project_dir in self.base_dir.iterdir():
                if not project_dir.is_dir():
                    continue
                file_path = project_dir / f"{issue.key.lower()}.md"
                if file_path.exists():
                    task = self._parse_task_file(file_path)
                    if task:
                        return task.status.lower() in ['done', 'closed', 'completed']
        except Exception:
            pass
        return False

    def get_task_templates(self, project_name: Optional[str] = None) -> Dict[str, Any]:
        """Get task templates for a project"""
        project_dir = self._get_project_dir(project_name)
        templates_file = project_dir / 'templates.yaml'

        if templates_file.exists():
            return yaml.safe_load(templates_file.read_text(encoding='utf-8')) or {}

        # Return default templates
        return {
            'bug': {
                'type': 'Bug',
                'priority': 'High',
                'labels': ['bug'],
                'description_template': '## Bug Description\n\n## Steps to Reproduce\n1. \n2. \n3. \n\n## Expected Behavior\n\n## Actual Behavior\n'
            },
            'feature': {
                'type': 'Story',
                'priority': 'Medium',
                'labels': ['feature'],
                'description_template': '## Feature Description\n\n## Acceptance Criteria\n- [ ] \n- [ ] \n- [ ] \n\n## Technical Notes\n'
            },
            'task': {
                'type': 'Task',
                'priority': 'Medium',
                'labels': [],
                'description_template': '## Description\n\n## Notes\n'
            }
        }

    def create_from_template(self, template_name: str, title: str, project_name: Optional[str] = None, **kwargs) -> Optional[Issue]:
        """Create a task from a template"""
        templates = self.get_task_templates(project_name)

        if template_name not in templates:
            print(f"Template '{template_name}' not found")
            return None

        template = templates[template_name]

        # Build task data from template
        key = self._get_next_key(project_name)
        now = datetime.now().isoformat()

        data = {
            'key': key,
            'title': title,
            'description': template.get('description_template', ''),
            'status': 'To Do',
            'type': template.get('type', 'Task'),
            'priority': template.get('priority', 'Medium'),
            'created': now,
            'updated': now,
            'labels': template.get('labels', []),
            'components': [],
            'fix_versions': [],
            'custom_fields': {}
        }

        # Override with any provided kwargs
        data.update(kwargs)

        file_path = self._get_task_file_path(key, project_name)
        task = MarkdownTask(key, data, file_path)
        self._write_task_file(task)
        self._clear_cache()

        return task.to_issue()

    def get_workflow(self, project_name: Optional[str] = None) -> Dict[str, List[str]]:
        """Get workflow transitions for a project"""
        project_dir = self._get_project_dir(project_name)
        workflow_file = project_dir / 'workflow.yaml'

        if workflow_file.exists():
            return yaml.safe_load(workflow_file.read_text(encoding='utf-8')) or {}

        # Return default workflow
        return {
            'To Do': ['In Progress'],
            'In Progress': ['In Review', 'To Do'],
            'In Review': ['Done', 'In Progress'],
            'Done': ['In Progress']
        }

    def get_available_transitions(self, key: str) -> List[str]:
        """Get available status transitions for a task"""
        task = None
        for project_dir in self.base_dir.iterdir():
            if not project_dir.is_dir():
                continue
            file_path = project_dir / f"{key.lower()}.md"
            if file_path.exists():
                task = self._parse_task_file(file_path)
                break

        if not task:
            return []

        workflow = self.get_workflow()
        return workflow.get(task.status, [])

    def save_task_template(self, name: str, template: Dict[str, Any], project_name: Optional[str] = None):
        """Save a task template for a project"""
        project_dir = self._get_project_dir(project_name)
        templates_file = project_dir / 'templates.yaml'

        templates: Dict[str, Any] = {}
        if templates_file.exists():
            templates = yaml.safe_load(templates_file.read_text(encoding='utf-8')) or {}

        templates[name] = template
        templates_file.write_text(yaml.dump(templates), encoding='utf-8')

    def save_workflow(self, workflow: Dict[str, List[str]], project_name: Optional[str] = None):
        """Save workflow for a project"""
        project_dir = self._get_project_dir(project_name)
        workflow_file = project_dir / 'workflow.yaml'
        workflow_file.write_text(yaml.dump(workflow), encoding='utf-8')

    def issue_url(self, key) -> Optional[str]:
        """Local tasks are files, not web pages -- the CLI prints the file
        path for this backend instead."""
        return None

    # Stub methods for Jira compatibility
    def find_field_by_name(self, name: str) -> Optional[Dict[str, Any]]:
        """Find a field by name (Jira-specific, returns None for markdown backend)"""
        return None

    def search_fields(self, query: str = "") -> Dict[str, Dict[str, Any]]:
        """Search fields (Jira-specific, returns empty dict for markdown backend)"""
        return {}

    def get_field_metadata(self, field_id: str) -> Optional[Dict[str, Any]]:
        """Get field metadata (Jira-specific, returns None for markdown backend)"""
        return None

    def get_field_options(self, field_id: str) -> List[Dict[str, Any]]:
        """Get field options (Jira-specific, returns empty list for markdown backend)"""
        return []

    def _get_fields(self) -> Dict[str, Dict[str, Any]]:
        """Get all fields (Jira-specific, returns empty dict for markdown backend)"""
        return {}

    def _process_field_value(self, value, field_type, field_schema):
        """Process a field value (Jira-specific, returns value as-is for markdown backend)"""
        return value

    def _get_task_by_key(self, key: str) -> Optional[MarkdownTask]:
        """Find and return a task by key"""
        for project_dir in self.base_dir.iterdir():
            if not project_dir.is_dir():
                continue
            file_path = project_dir / f"{key.lower()}.md"
            if file_path.exists():
                return self._parse_task_file(file_path)
        return None

    def get_comments(self, key: str) -> List[Dict[str, Any]]:
        """Get all comments for a task"""
        task = self._get_task_by_key(key)
        if not task:
            return []
        return task.data.get('comments', [])

    def add_comment(self, key: str, text: str, author: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """Add a comment to a task"""
        task = self._get_task_by_key(key)
        if not task:
            return None

        comments = task.data.get('comments', [])
        comment_id = len(comments) + 1
        now = datetime.now().isoformat()

        new_comment = {
            'id': comment_id,
            'author': author or 'unknown',
            'text': text,
            'created': now,
            'updated': now
        }

        comments.append(new_comment)
        task.data['comments'] = comments
        task.data['updated'] = now
        self._write_task_file(task)
        self._clear_cache()

        return new_comment

    def update_comment(self, key: str, comment_id: int, text: str) -> bool:
        """Update a comment on a task"""
        task = self._get_task_by_key(key)
        if not task:
            return False

        comments = task.data.get('comments', [])
        for comment in comments:
            if comment.get('id') == comment_id:
                comment['text'] = text
                comment['updated'] = datetime.now().isoformat()
                task.data['comments'] = comments
                task.data['updated'] = datetime.now().isoformat()
                self._write_task_file(task)
                self._clear_cache()
                return True

        return False

    def delete_comment(self, key: str, comment_id: int) -> bool:
        """Delete a comment from a task"""
        task = self._get_task_by_key(key)
        if not task:
            return False

        comments = task.data.get('comments', [])
        original_len = len(comments)
        comments = [c for c in comments if c.get('id') != comment_id]

        if len(comments) < original_len:
            task.data['comments'] = comments
            task.data['updated'] = datetime.now().isoformat()
            self._write_task_file(task)
            self._clear_cache()
            return True

        return False
