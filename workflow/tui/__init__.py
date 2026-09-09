from textual.app import App, ComposeResult
from textual.containers import Container, Horizontal, Vertical, ScrollableContainer
from textual.widgets import Header, Footer, Static, Button, Input, TextArea
from textual.screen import Screen, ModalScreen
from textual.binding import Binding
from textual import work
from textual.reactive import reactive
from textual.events import Click
from datetime import datetime
import asyncio
import subprocess

from workflow.backends.markdown import MarkdownBackend
from workflow.config import load_effective_config, get_jira_config
from workflow.projects import list_projects, get_current_project, set_current_project
from workflow.state import _load as load_state, _save as save_state, set_current_task, get_all_ai_sessions, set_ai_session, has_ai_session, get_task_dir
from workflow.ai import launch_ai_session


def get_type_icon(issue_type: str) -> str:
    icons = {
        "Bug": "\U0001F41B",
        "Story": "\U0001F4C4",
        "Task": "\U0001F4DD",
        "Epic": "\U0001F4C8",
    }
    return icons.get(issue_type, "\U0001F4DD")


class TaskItem(Static):
    def __init__(self, task, has_ai=False, **kwargs):
        super().__init__(**kwargs)
        self._task_obj = task
        self._selected = False
        self._has_ai = has_ai

    @property
    def selected(self):
        return self._selected

    @selected.setter
    def selected(self, value):
        self._selected = value
        self.update_classes()

    def update_classes(self):
        if self._selected:
            self.add_class("selected")
        else:
            self.remove_class("selected")

    @property
    def task(self):
        return self._task_obj

    def set_has_ai(self, has_ai: bool):
        self._has_ai = has_ai
        self.refresh()

    def render(self) -> str:
        key = getattr(self._task_obj, 'key', 'N/A')
        title = getattr(self._task_obj, 'title', 'No title')
        issue_type = getattr(self._task_obj, 'issue_type', 'Task')
        
        icon = get_type_icon(issue_type)
        ai_icon = " \U0001F916" if self._has_ai else ""
        return f"{icon} [bold]{key}[/bold] {title}{ai_icon}"


class TaskColumn(Vertical):
    def __init__(self, title: str, status_filter: str, column_id: str, **kwargs):
        super().__init__(**kwargs)
        self.title = title
        self.status_filter = status_filter
        self.column_id = column_id

    def compose(self) -> ComposeResult:
        yield Static(self.title, classes="column-header", id=f"header-{self.column_id}")
        yield ScrollableContainer(id="task-list")


class BoardView(Container):
    tasks = reactive([])
    
    def __init__(self, backend, **kwargs):
        super().__init__(**kwargs)
        self.backend = backend
        self.selected_tasks = []
        self.focused_column = None
        self.current_column_index = 0
        self.current_task_index = -1
        self.column_ids = ["col-todo", "col-inprogress", "col-done"]

    def compose(self) -> ComposeResult:
        yield Horizontal(
            TaskColumn("To Do", "To Do", "col-todo", id="col-todo"),
            TaskColumn("In Progress", "In Progress", "col-inprogress", id="col-inprogress"),
            TaskColumn("Done", "Done", "col-done", id="col-done"),
            id="board"
        )

    def load_tasks(self):
        if not self.backend:
            return []
        
        try:
            if hasattr(self.backend, 'list_tasks'):
                tasks = self.backend.list_tasks()
                # Debug: log what we're getting
                try:
                    app = self.app
                    if app:
                        app.log_to_console(f"Loaded {len(tasks)} tasks")
                        if tasks:
                            app.log_to_console(f"First task type: {type(tasks[0]).__name__}")
                            app.log_to_console(f"First task: {tasks[0]}")
                except:
                    pass
                return tasks
            elif hasattr(self.backend, 'get_assigned_tasks'):
                return self.backend.get_assigned_tasks()
            return []
        except Exception as e:
            print(f"Error loading tasks: {e}")
            return []

    def refresh_board(self):
        tasks = self.load_tasks()
        
        todo_tasks = [t for t in tasks if self._get_status_category(t) == "To Do"]
        inprogress_tasks = [t for t in tasks if self._get_status_category(t) == "In Progress"]
        done_tasks = [t for t in tasks if self._get_status_category(t) == "Done"]
        
        self.selected_tasks = []
        
        ai_sessions = get_all_ai_sessions()
        
        for col_id, task_list in [("col-todo", todo_tasks), ("col-inprogress", inprogress_tasks), ("col-done", done_tasks)]:
            try:
                col = self.query_one(f"#{col_id} #task-list", ScrollableContainer)
                col.remove_children()
                for task in task_list:
                    task_key = getattr(task, 'key', None)
                    has_ai = bool(task_key and task_key in ai_sessions)
                    item = TaskItem(task, has_ai=has_ai, classes="task-card")
                    col.mount(item)
            except Exception:
                pass

    def focus_column(self, column_id: str):
        self.focused_column = column_id
        try:
            col = self.query_one(f"#{column_id} #task-list", ScrollableContainer)
            col.scroll_visible()
        except Exception:
            pass

    def toggle_task_selection(self, task_item: TaskItem, extend: bool = False):
        if extend:
            if task_item in self.selected_tasks:
                self.selected_tasks.remove(task_item)
                task_item.selected = False
            else:
                self.selected_tasks.append(task_item)
                task_item.selected = True
        else:
            if task_item in self.selected_tasks:
                self.selected_tasks.remove(task_item)
                task_item.selected = False
            else:
                for item in self.selected_tasks:
                    item.selected = False
                self.selected_tasks = [task_item]
                task_item.selected = True

    def move_task_status(self, task_item: TaskItem, new_status: str):
        task = task_item.task
        try:
            import sys
            print(f"DEBUG: task={task}", file=sys.stderr)
            print(f"DEBUG: type={type(task)}", file=sys.stderr)
            
            task_key = getattr(task, 'key', None)
            print(f"DEBUG: getattr key={task_key}", file=sys.stderr)
            
            if not task_key and hasattr(task, 'data') and task.data:
                task_key = task.data.get('key')
                print(f"DEBUG: data.get key={task_key}", file=sys.stderr)
            
            if not task_key and isinstance(task, dict):
                task_key = task.get('key')
                print(f"DEBUG: dict.get key={task_key}", file=sys.stderr)
            
            # Try to log to console
            try:
                app = self.app
                if app:
                    app.log_to_console(f"task={task}, type={type(task).__name__}, key={task_key}")
            except Exception as e:
                print(f"DEBUG: console error: {e}", file=sys.stderr)
            
            if not task_key:
                app = self.app
                if app:
                    app.notify("Could not find task key")
                return
                
            if hasattr(self.backend, 'move_to_in_progress') and new_status == "In Progress":
                self.backend.move_to_in_progress(task)
            elif hasattr(self.backend, 'move_to_done') and new_status == "Done":
                self.backend.move_to_done(task)
            elif hasattr(self.backend, 'transition_task'):
                self.backend.transition_task(task_key, new_status)
            
            self.refresh_board()
            app = self.app
            if app:
                app.log_to_console(f"Moved {task_key} to {new_status}")
                app.notify(f"Moved to {new_status}")
        except Exception as e:
            app = self.app
            if app:
                app.notify(f"Error moving task: {e}")

    def _get_status_category(self, task) -> str:
        try:
            status = getattr(task, 'status', '')
            status_lower = status.lower()
            if "done" in status_lower or "closed" in status_lower or "completed" in status_lower:
                return "Done"
            elif "progress" in status_lower or "in progress" in status_lower:
                return "In Progress"
            else:
                return "To Do"
        except:
            return "To Do"

    def get_column_tasks(self, column_id: str):
        try:
            col = self.query_one(f"#{column_id} #task-list", ScrollableContainer)
            return list(col.query(TaskItem))
        except Exception:
            return []

    def get_task_count_in_column(self, column_index: int) -> int:
        if 0 <= column_index < len(self.column_ids):
            return len(self.get_column_tasks(self.column_ids[column_index]))
        return 0

    def select_task_at_index(self, column_index: int, task_index: int):
        if 0 <= column_index < len(self.column_ids):
            self.current_column_index = column_index
            self.focused_column = self.column_ids[column_index]
            tasks = self.get_column_tasks(self.column_ids[column_index])
            if 0 <= task_index < len(tasks):
                self.current_task_index = task_index
                for item in self.selected_tasks:
                    item.selected = False
                self.selected_tasks = []
                task_item = tasks[task_index]
                self.selected_tasks.append(task_item)
                task_item.selected = True
                task_item.scroll_visible()
                self.focus_column(self.column_ids[column_index])

    def focus_task_at_index(self, column_index: int, task_index: int):
        if 0 <= column_index < len(self.column_ids):
            self.current_column_index = column_index
            self.focused_column = self.column_ids[column_index]
            tasks = self.get_column_tasks(self.column_ids[column_index])
            if 0 <= task_index < len(tasks):
                for item in self.query(TaskItem):
                    item.remove_class("focused")
                self.current_task_index = task_index
                task_item = tasks[task_index]
                task_item.add_class("focused")
                task_item.scroll_visible()
                self.focus_column(self.column_ids[column_index])


class TaskBoardApp(App):
    CSS = """
    Screen {
        background: $surface;
    }
    
    #board {
        height: 100%;
    }
    
    TaskColumn {
        width: 1fr;
        height: 100%;
        border: solid $primary;
        padding: 1;
        margin: 1;
    }
    
    .column-header {
        text-align: center;
        text-style: bold;
        background: $primary-darken-1;
        color: $text;
        padding: 1;
        margin-bottom: 1;
    }
    
    #col-todo .column-header { background: $primary; }
    #col-inprogress .column-header { background: $warning; }
    #col-done .column-header { background: $success; }
    
    .task-card {
        background: $surface-darken-1;
        border: solid $primary-darken-1;
        padding: 1;
        margin: 1;
    }
    
    .task-card:hover {
        background: $primary-darken-2;
        border: solid $primary;
    }
    
    .task-card.selected {
        background: #f59e0b;
        border: solid #d97706;
    }

    .task-card.focused {
        background: $primary-darken-2;
        border: solid $accent;
    }

    .task-card.selected.focused {
        background: #f59e0b;
        border: heavy #3b82f6;
    }
    
    .task-item-title {
        color: $text;
    }
    
    #toolbar {
        height: 3;
        background: $surface;
        dock: bottom;
    }
    
    #console-area {
        height: 0;
        background: $panel;
        dock: bottom;
    }
    
    #console-area.visible {
        height: 30%;
    }
    
    #console-header {
        background: $primary-darken-1;
        color: $text;
        padding: 0 1;
        text-style: bold;
    }
    
    #console-content {
        height: 100%;
        background: $panel;
    }
    
    .console-line {
        color: $text;
        padding: 0 1;
    }
    
    .modal-title {
        text-style: bold;
        text-align: center;
        margin-bottom: 1;
    }
    
    #delete-modal, #project-modal, #new-task-modal {
        width: 40;
        height: auto;
        background: $surface;
        border: solid $primary;
        padding: 1 2;
        align: center middle;
    }
    
    #delete-warning {
        color: $error;
        text-align: center;
        margin: 1 0;
    }
    """

    BINDINGS = [
        Binding("n", "new_task", "New Task"),
        Binding("r", "refresh", "Refresh"),
        Binding("p", "switch_project", "Switch Project"),
        Binding("f", "focus_terminal", "Focus Terminal"),
        Binding("escape", "close_modal", "Close"),
        Binding("space", "toggle_selection", "Select"),
        Binding("a", "start_ai", "Start AI"),
        Binding("`", "toggle_console", "Console"),
        Binding("t", "move_todo", "To Do"),
        Binding("i", "move_inprogress", "In Progress"),
        Binding("o", "move_done", "Done"),
        Binding("y", "copy_console", "Copy Console"),
        Binding("x", "delete_task", "Delete"),
        Binding("up", "nav_up", "Up"),
        Binding("down", "nav_down", "Down"),
        Binding("left", "nav_left", "Left"),
        Binding("right", "nav_right", "Right"),
    ]

    def __init__(self):
        super().__init__()
        self.backend = None
        self.current_project = None
        self.projects = {}
        self.current_tasks = []
        self.console_visible = False

    def on_mount(self):
        self.load_config()
        board = self.query_one("#board-view", BoardView)
        board.backend = self.backend
        board.refresh_board()

    def on_key(self, event):
        self.log_to_console(f"DEBUG on_key: {event.key}", show_console=True)
        if event.key == "up":
            event.prevent_default()
            self.action_nav_up()
        elif event.key == "down":
            event.prevent_default()
            self.action_nav_down()
        elif event.key == "left":
            event.prevent_default()
            self.action_nav_left()
        elif event.key == "right":
            event.prevent_default()
            self.action_nav_right()
        elif event.key == "space":
            event.prevent_default()
            self.action_toggle_selection()

    def load_config(self):
        config = load_effective_config()
        self.projects = list_projects()
        self.current_project = get_current_project() or "flow"
        
        task_backend = "jira"
        
        if self.current_project and self.current_project in self.projects:
            project_cfg = self.projects[self.current_project]
            task_backend = project_cfg.get("task_backend", task_backend)
        
        from workflow.backends import get_backend
        # get_backend() covers jira/linear/markdown and prints setup
        # guidance for an unconfigured one; fall back to local tasks so the
        # TUI still opens rather than crashing on a missing API key.
        self.backend = get_backend(config, force_type=task_backend) or MarkdownBackend(config)

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield BoardView(self.backend, id="board-view")
        yield Container(
            Static("[bold]Console Output[/bold] (click to select text, Ctrl+C to copy)", id="console-header"),
            TextArea("", id="console-content", read_only=True),
            id="console-area"
        )
        yield Footer()

    def on_click(self, event: Click):
        widget = event.widget
        
        self.log_to_console(f"Click on: {widget}")
        
        if hasattr(widget, 'id') and widget.id and widget.id.startswith('btn-'):
            self.handle_toolbar_click(widget.id)
            return
        
        if hasattr(event, 'style') and 'column-header' in str(event.style):
            return
        
        if widget and hasattr(widget, 'id'):
            target_id = widget.id
            if target_id and target_id.startswith('col-'):
                board = self.query_one("#board-view", BoardView)
                board.focus_column(target_id)
                return
        
        board = self.query_one("#board-view", BoardView)
        
        for item in board.query(TaskItem):
            if widget == item or (hasattr(widget, 'parent') and widget.parent == item):
                if event.shift:
                    board.toggle_task_selection(item, extend=True)
                else:
                    board.toggle_task_selection(item, extend=False)
                return

    def handle_toolbar_click(self, button_id: str):
        board = self.query_one("#board-view", BoardView)
        selected = board.selected_tasks
        
        if not selected:
            self.notify("No task selected")
            return
        
        task_item = selected[0]
        
        if button_id == "btn-todo-move":
            board.move_task_status(task_item, "To Do")
        elif button_id == "btn-inprogress-move":
            board.move_task_status(task_item, "In Progress")
        elif button_id == "btn-done-move":
            board.move_task_status(task_item, "Done")
        elif button_id == "btn-start-ai":
            self._start_ai_on_task(task_item.task, task_item)

    def _start_ai_on_task(self, task, task_item=None):
        try:
            from workflow.backends.base import Issue
            import os
            import subprocess
            
            task_key = getattr(task, 'key', 'N/A')
            task_title = getattr(task, 'title', '')
            task_desc = getattr(task, 'description', '')
            
            task_dir = get_task_dir(task_key)
            
            issue = Issue(task_key, task_title, task_desc)
            set_current_task(issue)
            
            wf_cmd = f"wf ai"
            
            script = f'''
tell application "Terminal"
    activate
    do script "cd {os.getcwd()} && {wf_cmd}"
end tell
'''
            result = subprocess.run(
                ["osascript", "-e", script],
                capture_output=True,
                text=True
            )
            
            import time
            time.sleep(0.5)
            
            app_list = subprocess.run(
                ["osascript", "-e", 'tell application "System Events" to get unix id of every process whose name contains "Terminal"'],
                capture_output=True,
                text=True
            )
            
            if app_list.returncode == 0 and app_list.stdout.strip():
                pids = [int(p.strip()) for p in app_list.stdout.strip().split(", ") if p.strip().isdigit()]
                if pids:
                    latest_pid = max(pids)
                    set_ai_session(task_key, latest_pid)
                    
                    if task_item:
                        task_item.set_has_ai(True)
                    
                    self.notify(f"Started AI for {task_key} (PID: {latest_pid})")
                    self.log_to_console(f"AI session started for {task_key} with PID {latest_pid}")
                    return
            
            self.notify(f"AI started for {task_key} (could not track PID)")
            self.log_to_console(f"AI session started for {task_key}")
            
        except Exception as e:
            self.notify(f"Error starting AI: {e}")
            import traceback
            traceback.print_exc()

    def action_new_task(self):
        self.push_screen(CreateTaskModal(self.backend, self.on_task_created))

    def on_task_created(self, task):
        self.notify(f"Created task: {task.key if hasattr(task, 'key') else task}")
        self.action_refresh()

    def action_refresh(self):
        try:
            board = self.query_one("#board-view", BoardView)
            board.backend = self.backend
            board.refresh_board()
            self.log_to_console("Tasks refreshed")
            self.notify("Tasks refreshed")
        except Exception as e:
            self.notify(f"Error: {e}")

    def action_switch_project(self):
        project_list = list(self.projects.keys())
        if not project_list:
            self.notify("No projects configured")
            return
        self.push_screen(ProjectSwitchModal(project_list, self.current_project, self.on_project_selected))

    def on_project_selected(self, project_name: str):
        set_current_project(project_name)
        self.load_config()
        self.action_refresh()
        self.notify(f"Switched to project: {project_name}")

    def action_focus_terminal(self):
        try:
            subprocess.run([
                "osascript", "-e",
                'tell application "Terminal" to activate'
            ], check=True)
            self.notify("Terminal focused")
        except Exception:
            try:
                subprocess.run(["open", "-a", "Terminal"], check=True)
                self.notify("Terminal opened")
            except Exception as e:
                self.notify(f"Could not focus terminal: {e}")

    def action_focus_column_1(self):
        board = self.query_one("#board-view", BoardView)
        board.focus_column("col-todo")

    def action_focus_column_2(self):
        board = self.query_one("#board-view", BoardView)
        board.focus_column("col-inprogress")

    def action_focus_column_3(self):
        board = self.query_one("#board-view", BoardView)
        board.focus_column("col-done")

    def action_toggle_selection(self):
        self.log_to_console("DEBUG: space pressed - toggle_selection", show_console=True)
        board = self.query_one("#board-view", BoardView)
        # Always toggle the currently focused task, not the first selected task
        if board.current_task_index >= 0:
            col_id = board.focused_column or board.column_ids[0]
            tasks = board.get_column_tasks(col_id)
            if 0 <= board.current_task_index < len(tasks):
                task_item = tasks[board.current_task_index]
                board.toggle_task_selection(task_item, extend=True)

    def action_start_ai(self):
        board = self.query_one("#board-view", BoardView)
        if not board.selected_tasks:
            if board.current_task_index >= 0:
                col_id = board.focused_column or board.column_ids[0]
                tasks = board.get_column_tasks(col_id)
                if 0 <= board.current_task_index < len(tasks):
                    board.toggle_task_selection(tasks[board.current_task_index], extend=False)
                else:
                    self.notify("No task focused or selected")
                    return
            else:
                self.notify("No task focused or selected")
                return
        
        task_item = board.selected_tasks[0]
        task = task_item.task
        task_key = getattr(task, 'key', None)
        
        if not task_key:
            self.notify("Cannot get task key")
            return
        
        if has_ai_session(task_key):
            self.notify(f"AI already running for {task_key}")
            return
        
        self._start_ai_on_task(task, task_item)

    def action_toggle_console(self):
        self.console_visible = not self.console_visible
        console = self.query_one("#console-area")
        if self.console_visible:
            console.add_class("visible")
            self.log_to_console("Console opened - press ` to toggle")
        else:
            console.remove_class("visible")

    def action_move_todo(self):
        board = self.query_one("#board-view", BoardView)
        if not board.selected_tasks:
            self.notify("No task selected")
            return
        board.move_task_status(board.selected_tasks[0], "To Do")

    def action_move_inprogress(self):
        board = self.query_one("#board-view", BoardView)
        if not board.selected_tasks:
            self.notify("No task selected")
            return
        board.move_task_status(board.selected_tasks[0], "In Progress")

    def action_move_done(self):
        board = self.query_one("#board-view", BoardView)
        if not board.selected_tasks:
            self.notify("No task selected")
            return
        board.move_task_status(board.selected_tasks[0], "Done")

    def action_delete_task(self):
        board = self.query_one("#board-view", BoardView)
        if not board.selected_tasks:
            self.notify("No task selected")
            return
        
        task_item = board.selected_tasks[0]
        task = task_item.task
        task_key = getattr(task, 'key', None)
        if not task_key and hasattr(task, 'data') and task.data:
            task_key = task.data.get('key')
        if not task_key and isinstance(task, dict):
            task_key = task.get('key')
        
        task = board.selected_tasks[0]
        task_key = getattr(task, 'key', None)
        if not task_key:
            self.notify("Cannot get task key")
            return
        
        self.push_screen(ConfirmDeleteModal(task_key, self.backend, self.on_task_deleted, self.on_delete_cancelled, task_item))

    def on_task_deleted(self, task_key: str, success: bool):
        if success:
            self.notify(f"Task {task_key} deleted")
            board = self.query_one("#board-view", BoardView)
            board.refresh_board()
        else:
            self.notify(f"Failed to delete {task_key}")
    
    def on_delete_cancelled(self, task_item):
        if task_item:
            task_item.focus()

    def log_to_console(self, message: str, show_console: bool = False):
        try:
            console = self.query_one("#console-content", TextArea)
            timestamp = datetime.now().strftime("%H:%M:%S")
            current = console.text or ""
            new_text = current + f"[{timestamp}] {message}\n"
            console.text = new_text
        except Exception as e:
            import sys
            print(f"Console error: {e}", file=sys.stderr)

    def on_text_area_selected(self, event):
        if event.text_area.id == "console-content":
            selected = event.text_area.selected_text
            if selected and selected.strip():
                try:
                    import subprocess
                    subprocess.run(
                        ["pbcopy"],
                        input=selected,
                        text=True,
                        check=True
                    )
                except Exception:
                    pass

    def action_copy_console(self):
        try:
            console = self.query_one("#console-content", TextArea)
            text = console.text
            if text:
                import subprocess
                subprocess.run(["pbcopy"], input=text, text=True, check=True)
                self.notify("Console copied to clipboard")
        except Exception as e:
            self.notify(f"Error copying: {e}")

    def action_close_modal(self):
        # Check if we're on a modal screen (not the main screen)
        if len(self.screen_stack) > 1:
            # Close the modal
            self.pop_screen()
        else:
            # We're on the main board, deselect any selected tasks
            try:
                board = self.query_one("#board-view", BoardView)
                if board.selected_tasks:
                    # Deselect all tasks
                    for item in board.selected_tasks:
                        item.selected = False
                    board.selected_tasks = []
                    self.log_to_console("Deselected all tasks")
            except:
                pass

    async def action_quit(self):
        self.exit()

    def action_nav_up(self):
        board = self.query_one("#board-view", BoardView)
        current_idx = board.current_task_index
        if current_idx > 0:
            board.focus_task_at_index(board.current_column_index, current_idx - 1)

    def action_nav_down(self):
        board = self.query_one("#board-view", BoardView)
        current_idx = board.current_task_index
        max_idx = board.get_task_count_in_column(board.current_column_index) - 1
        if max_idx < 0:
            return
        if current_idx < 0:
            board.focus_task_at_index(board.current_column_index, 0)
        elif current_idx < max_idx:
            board.focus_task_at_index(board.current_column_index, current_idx + 1)

    def action_nav_left(self):
        board = self.query_one("#board-view", BoardView)
        current_col = board.current_column_index
        if current_col > 0:
            new_col = current_col - 1
            max_task_idx = board.get_task_count_in_column(new_col) - 1
            if max_task_idx < 0:
                return
            task_idx = board.current_task_index
            if task_idx < 0:
                task_idx = 0
            else:
                task_idx = min(task_idx, max_task_idx)
            board.focus_task_at_index(new_col, task_idx)

    def action_nav_right(self):
        board = self.query_one("#board-view", BoardView)
        current_col = board.current_column_index
        if current_col < len(board.column_ids) - 1:
            new_col = current_col + 1
            max_task_idx = board.get_task_count_in_column(new_col) - 1
            if max_task_idx < 0:
                return
            task_idx = board.current_task_index
            if task_idx < 0:
                task_idx = 0
            else:
                task_idx = min(task_idx, max_task_idx)
            board.focus_task_at_index(new_col, task_idx)


class TaskDetailModal(ModalScreen):
    def __init__(self, task, backend, **kwargs):
        super().__init__(**kwargs)
        self._task = task
        self.backend = backend

    def compose(self) -> ComposeResult:
        key = getattr(self._task, 'key', 'N/A')
        title = getattr(self._task, 'title', 'No title')
        description = getattr(self._task, 'description', 'No description')
        status = getattr(self._task, 'status', 'Unknown')
        issue_type = getattr(self._task, 'issue_type', 'Task')
        
        yield Container(
            Static(f"[bold]{key}[/bold] - {issue_type}", classes="detail-key"),
            Static(f"Status: {status}", classes="detail-status"),
            Static(f"[bold]{title}[/bold]", classes="detail-title"),
            ScrollableContainer(
                Static(description or "No description", classes="detail-desc"),
                id="desc-scroll"
            ),
            Horizontal(
                Button("Start AI", variant="primary", id="btn-start-ai"),
                Button("To Do", id="btn-todo"),
                Button("In Progress", id="btn-inprogress"),
                Button("Done", id="btn-done"),
                Button("Close", variant="error", id="btn-close"),
            ),
            id="detail-modal"
        )

    def on_button_pressed(self, event):
        if event.button.id == "btn-close":
            self.app.pop_screen()
        elif event.button.id == "btn-start-ai":
            self.start_ai_on_task()
        elif event.button.id == "btn-todo":
            self.move_task("To Do")
        elif event.button.id == "btn-inprogress":
            self.move_task("In Progress")
        elif event.button.id == "btn-done":
            self.move_task("Done")

    def start_ai_on_task(self):
        try:
            from workflow.backends.base import Issue
            import os
            import subprocess
            
            task_key = self._task.key
            task_title = self._task.title
            task_desc = self._task.description
            
            issue = Issue(task_key, task_title, task_desc)
            set_current_task(issue)
            set_ai_session(task_key, 0)
            
            self.app.pop_screen()
            
            wf_cmd = "wf ai"
            
            script = f'''
tell application "Terminal"
    activate
    do script "cd {os.getcwd()} && {wf_cmd}"
end tell
'''
            subprocess.run(["osascript", "-e", script], capture_output=True)
            
            board = self.app.query_one("#board-view", BoardView)
            board.refresh_board()
            
        except Exception as e:
            self.app.notify(f"Error starting AI: {e}")

    def move_task(self, status: str):
        try:
            if hasattr(self.backend, 'move_to_in_progress') and status == "In Progress":
                self.backend.move_to_in_progress(self._task)
            elif hasattr(self.backend, 'move_to_done') and status == "Done":
                self.backend.move_to_done(self._task)
            elif hasattr(self.backend, f'move_to_{status.lower().replace(" ", "_")}'):
                getattr(self.backend, f'move_to_{status.lower().replace(" ", "_")}')(self._task)
            
            self.app.pop_screen()
            board = self.app.query_one("#board-view", BoardView)
            board.refresh_board()
            self.notify(f"Moved to {status}")
        except Exception as e:
            self.notify(f"Error moving task: {e}")


class ProjectSwitchModal(ModalScreen):
    def __init__(self, projects, current_project, callback, **kwargs):
        super().__init__(**kwargs)
        self.projects = projects
        self.current_project = current_project
        self.callback = callback

    def compose(self) -> ComposeResult:
        yield Container(
            Static("[bold]Switch Project[/bold]", classes="modal-title"),
            Vertical(id="project-list"),
            Button("Cancel", id="btn-cancel"),
            id="project-modal"
        )

    def on_mount(self):
        list_view = self.query_one("#project-list", Vertical)
        for proj in self.projects:
            is_current = " [current]" if proj == self.current_project else ""
            btn = Button(f"{proj}{is_current}", id=f"proj-{proj}")
            list_view.mount(btn)

    def on_button_pressed(self, event):
        if event.button.id == "btn-cancel":
            self.app.pop_screen()
        elif event.button.id.startswith("proj-"):
            project_name = event.button.id[5:]
            self.app.pop_screen()
            self.callback(project_name)


class CreateTaskModal(ModalScreen):
    def __init__(self, backend, callback, **kwargs):
        super().__init__(**kwargs)
        self.backend = backend
        self.callback = callback

    def compose(self) -> ComposeResult:
        yield Container(
            Static("[bold]Create New Task[/bold]", classes="modal-title"),
            Input(placeholder="Task title", id="input-title"),
            TextArea(placeholder="Description (optional)", id="input-desc"),
            Horizontal(
                Button("Create", variant="primary", id="btn-create"),
                Button("Cancel", id="btn-cancel"),
            ),
            id="new-task-modal"
        )

    def on_button_pressed(self, event):
        if event.button.id == "btn-cancel":
            self.app.pop_screen()
        elif event.button.id == "btn-create":
            self.create_task()

    def create_task(self):
        title_input = self.query_one("#input-title", Input)
        desc_input = self.query_one("#input-desc", TextArea)
        
        title = title_input.value.strip()
        if not title:
            self.notify("Title is required")
            return
        
        description = desc_input.text
        
        try:
            if hasattr(self.backend, 'create_issue'):
                new_task = self.backend.create_issue(title, description)
                self.app.pop_screen()
                self.callback(new_task)
            else:
                self.notify("Backend does not support creating tasks")
        except Exception as e:
            self.notify(f"Error creating task: {e}")


class ConfirmDeleteModal(ModalScreen):
    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
    ]
    
    def __init__(self, task_key, backend, callback, cancel_callback=None, task_item=None, **kwargs):
        super().__init__(**kwargs)
        self.task_key = task_key
        self.backend = backend
        self.callback = callback
        self.cancel_callback = cancel_callback
        self.task_item = task_item

    def compose(self) -> ComposeResult:
        yield Container(
            Static(f"[bold]Delete Task {self.task_key}?[/bold]", classes="modal-title"),
            Static("This action cannot be undone.", id="delete-warning"),
            Horizontal(
                Button("Delete", variant="error", id="btn-delete"),
                Button("Cancel", id="btn-cancel"),
            ),
            id="delete-modal"
        )

    def on_mount(self):
        self.query_one("#btn-delete", Button).focus()

    def action_cancel(self):
        self.app.pop_screen()
        if self.cancel_callback:
            self.cancel_callback(self.task_item)

    def on_key(self, event):
        if event.key == "enter":
            focused = self.focused
            if focused and hasattr(focused, 'id') and focused.id == "btn-delete":
                self.delete_task()
            else:
                self.action_cancel()
            return
        if event.key in ("tab", "right"):
            buttons = list(self.query(Button))
            focused = self.focused
            if focused in buttons:
                idx = buttons.index(focused)
                next_idx = (idx + 1) % len(buttons)
                buttons[next_idx].focus()
            event.prevent_default()
            return
        if event.key == "shift+tab":
            buttons = list(self.query(Button))
            focused = self.focused
            if focused in buttons:
                idx = buttons.index(focused)
                prev_idx = (idx - 1) % len(buttons)
                buttons[prev_idx].focus()
            event.prevent_default()
            return
        if event.key == "left":
            buttons = list(self.query(Button))
            focused = self.focused
            if focused in buttons:
                idx = buttons.index(focused)
                prev_idx = (idx - 1) % len(buttons)
                buttons[prev_idx].focus()
            event.prevent_default()
            return

    def on_button_pressed(self, event):
        if event.button.id == "btn-cancel":
            task_item = self.task_item
            cancel_callback = self.cancel_callback
            self.app.pop_screen()
            if cancel_callback:
                cancel_callback(task_item)
        elif event.button.id == "btn-delete":
            self.delete_task()

    def delete_task(self):
        success = False
        try:
            if hasattr(self.backend, 'delete_task'):
                success = self.backend.delete_task(self.task_key)
            elif hasattr(self.backend, 'delete'):
                success = self.backend.delete(self.task_key)
            else:
                self.notify("Backend does not support deleting tasks")
                self.app.pop_screen()
                return
        except Exception as e:
            self.notify(f"Error deleting task: {e}")
        
        self.app.pop_screen()
        self.callback(self.task_key, success)


if __name__ == "__main__":
    app = TaskBoardApp()
    app.run()
