"""
macOS Desktop Notifications with inline response support using desktop-notifier
"""

import asyncio
import sys
import os
import subprocess
import threading
import tempfile
import time
from typing import Optional, Dict, Any, Callable
from pathlib import Path
import asyncio
import platform
import signal

from desktop_notifier import DEFAULT_SOUND, Button, DesktopNotifier, ReplyField, Urgency
import webbrowser

def open_github_url():
    """Open GitHub URL for testing"""
    url = "https://github.com/example-org/example-repo/pull/123"
    print(f"🌐 Opening GitHub URL: {url}")
    webbrowser.open(url)

# Integrate with Core Foundation event loop on macOS to allow receiving callbacks.
if platform.system() == "Darwin":
    from rubicon.objc.runtime import load_library

    # rubicon's eventloop looks up NSEvent via ObjCClass, which requires
    # AppKit to already be loaded into the process.
    load_library("AppKit")

    from rubicon.objc.eventloop import RubiconEventLoop

    loop = RubiconEventLoop()
    asyncio.set_event_loop(loop)


async def desktop_notify(
        title: str,
        message: str,
        response_handler: Optional[Callable] = None,
) -> None:

    notifier = DesktopNotifier(app_name="Sample App")

    await notifier.send(
        title="title",
        message="message",
        urgency=Urgency.Critical,
       #buttons=[
       #    Button(
       #        title="Mark as read",
       #        on_pressed=lambda: print("Button 'Mark as read' was clicked"),
       #    ),
       #    Button(
       #        title="Click me!!",
       #        on_pressed=lambda: open_github_url(),
       #    ),
       #],
        reply_field=ReplyField(
            title="Reply",
            button_title="Reply",
            on_replied=lambda text: response_handler(),
        ),
        on_dispatched=lambda: print("Notification is showing now"),
        on_clicked=lambda: response_handler(),
        on_dismissed=lambda: print("Notification was dismissed"),
        sound=DEFAULT_SOUND,
    )

    # Run the event loop forever to respond to user interactions with the notification.
    event = asyncio.Event()

    if platform.system() != "Windows":
        # Handle SIGINT and SIGTERM gracefully on Unix.
        loop = asyncio.get_running_loop()
        loop.add_signal_handler(signal.SIGINT, event.set)
        loop.add_signal_handler(signal.SIGTERM, event.set)

    await event.wait()


# Add path to desktop-notifier venv
venv_path = os.path.join(Path(__file__).parent.parent, 'pyobjc_env/lib/python3.13/site-packages')
sys.path.insert(0, venv_path)

try:
    from desktop_notifier import DesktopNotifier, ReplyField, Button
    DESKTOP_NOTIFIER_AVAILABLE = True
except ImportError:
    DESKTOP_NOTIFIER_AVAILABLE = False
    print("Warning: desktop-notifier not available, falling back to basic notifications")


def open_github_url():
    """Open GitHub URL for testing"""
    url = "https://github.com/example-org/example-repo/pull/123"
    print(f"🌐 Opening GitHub URL: {url}")
    import webbrowser
    webbrowser.open(url)


class NotificationService:
    def __init__(self):
        self.response_handlers: Dict[str, Callable] = {}
        self.temp_dir = Path(tempfile.gettempdir()) / "workflow_notifications"
        self.temp_dir.mkdir(exist_ok=True)
        self.notifier = None
        self.ReplyField = None
        self._cf_runloop_thread = None
        self._cf_runloop_started = False
        
        # Ensure asyncio is initialized
        self._ensure_asyncio_initialized()
        
        # Set up CFRunLoop event loop policy for desktop-notifier callbacks
        if DESKTOP_NOTIFIER_AVAILABLE:
            try:
                from rubicon.objc.eventloop import RubiconEventLoop
                loop = RubiconEventLoop()
                asyncio.set_event_loop(loop)
                print("CFRunLoop event loop policy installed for desktop-notifier callbacks")
            except ImportError:
                print("Failed to import CFRunLoop event loop policy")
            except Exception as e:
                print(f"Failed to set CFRunLoop event loop policy: {e}")
                
            from desktop_notifier import DesktopNotifier, ReplyField as DesktopReplyField
            self.notifier = DesktopNotifier()
            self.ReplyField = DesktopReplyField
            print("after from desktop_notifier")

    def _ensure_asyncio_initialized(self):
        """Ensure asyncio is properly initialized for callbacks"""
        try:
            if not asyncio._get_running_loop():  # type: ignore
                from rubicon.objc.eventloop import RubiconEventLoop
                loop = RubiconEventLoop()
                asyncio.set_event_loop(loop)
                print("Ensured asyncio event loop for callbacks")
        except:
            pass

    async def send_notification_async(
        self,
        title: str,
        message: str,
        response_options: Optional[list] = None,
        response_handler: Optional[Callable] = None,
        notification_id: Optional[str] = None,
        use_reply_field: bool = True
    ) -> str:
        """
        Send a macOS notification with optional inline response buttons or reply field
        
        Args:
            title: Notification title
            message: Notification message
            response_options: List of button labels for inline responses
            response_handler: Function to call when button is clicked
            notification_id: Unique ID for notification
            use_reply_field: Use desktop-notifier ReplyField if available
        
        Returns:
            notification_id: The ID of sent notification
        """
        if notification_id is None:
            import uuid
            notification_id = str(uuid.uuid4())
        
        # Ensure asyncio is initialized before sending notifications
        self._ensure_asyncio_initialized()
        
        # Use desktop-notifier for all notifications when available
        if DESKTOP_NOTIFIER_AVAILABLE and self.notifier and use_reply_field:
            try:
                buttons = []
                if response_options:
                    for i, option in enumerate(response_options):
                        buttons.append(
                            Button(
                                title=option,
                                on_pressed=lambda text, opt=option: self._handle_button_click(opt, response_handler)
                            )
                        )
                
                clicked = False
                def handle_click():
                    print(clicked)
                    clicked = True

                await self.notifier.send(
                    title=title,
                    message=message,
                    buttons=buttons,
                    reply_field=self.ReplyField(
                        title=response_options[0] if response_options else "Reply",
                        button_title=response_options[0] if response_options else "Send",
                        on_replied=handle_click
                    ) if response_handler else None,
                    on_dispatched=lambda: print(f"Desktop notification sent: {title}")
                )
                while not clicked:
                   await asyncio.sleep(1) 
                return notification_id
                
            except Exception as e:
                print(f"Desktop-notifier failed, falling back to basic notification: {e}")
        
        # Always fallback to AppleScript approach if desktop-notifier fails or isn't used
        # Fallback to AppleScript approach
        if response_handler and response_options:
            self.response_handlers[notification_id] = response_handler
            script = self._create_interactive_notification_script(
                title, message, response_options, notification_id
            )
        else:
            script = self._create_basic_notification_script(title, message)
        
        try:
            result = subprocess.run(["osascript", "-e", script], check=True, capture_output=True, text=True)
            print(f"Basic notification sent: {title}")
            if result.stdout:
                print(f"Dialog result: {result.stdout.strip()}")
                # Parse the response and call the handler
                if response_handler and result.stdout.strip():
                    try:
                        # Expected format: "notification_id:response"
                        if ':' in result.stdout:
                            response_id, response_text = result.stdout.strip().split(':', 1)
                            if response_id == notification_id:
                                response_handler(response_text)
                    except Exception as e:
                        print(f"Error handling response: {e}")
        except subprocess.CalledProcessError as e:
            print(f"Failed to send notification 1: {e}")
            print(f"Script output: {e.stderr}")
        
        return notification_id

    def _handle_button_click(self, button_title: str, response_handler: Optional[Callable] = None):
        """Handle button click event"""
        print(f"Button clicked: {button_title}")
        if response_handler:
            try:
                import webbrowser
                url = "https://github.com/example-org/example-repo/pull/123"
                print(f"🌐 Opening GitHub URL: {url}")
                webbrowser.open(url)
                print("Browser opened successfully!")
            except Exception as e:
                print(f"Failed to open URL: {e}")
                
            except Exception as e:
                print(f"Desktop-notifier failed, falling back to basic notification: {e}")
        
        # Always fallback to AppleScript approach if desktop-notifier fails or isn't used
        # Fallback to AppleScript approach
        if response_handler and response_options:
            self.response_handlers[notification_id] = response_handler
            script = self._create_interactive_notification_script(
                title, message, response_options, notification_id
            )
        else:
            script = self._create_basic_notification_script(title, message)
        
        try:
            result = subprocess.run(["osascript", "-e", script], check=True, capture_output=True, text=True)
            print(f"Basic notification sent: {title}")
            if result.stdout:
                print(f"Dialog result: {result.stdout.strip()}")
                # Parse the response and call the handler
                if response_handler and result.stdout.strip():
                    try:
                        # Expected format: "notification_id:response"
                        if ':' in result.stdout:
                            response_id, response_text = result.stdout.strip().split(':', 1)
                            if response_id == notification_id:
                                response_handler(response_text)
                    except Exception as e:
                        print(f"Error handling response: {e}")
        except subprocess.CalledProcessError as e:
            print(f"Failed to send notification 2: {e}")
            print(f"Script output: {e.stderr}")
        
        return notification_id

    def _handle_button_click(self, button_title: str, response_handler: Optional[Callable] = None):
        """Handle button click event"""
        print(f"Button clicked: {button_title}")
        if response_handler:
            try:
                import webbrowser
                url = "https://github.com/example-org/example-repo/pull/123"
                print(f"🌐 Opening GitHub URL: {url}")
                webbrowser.open(url)
                print("Browser opened successfully!")
            except Exception as e:
                print(f"Failed to open URL: {e}")

    def _handle_notification_click(self, response_handler: Optional[Callable] = None):
        """Handle notification click event"""
        print("Notification was clicked!")
        if response_handler:
            try:
                import webbrowser
                url = "https://github.com/example-org/example-repo/pull/123"
                print(f"🌐 Opening GitHub URL: {url}")
                webbrowser.open(url)
                print("Browser opened successfully!")
            except Exception as e:
                print(f"Failed to open URL: {e}")
        else:
            print("No response handler provided")
                
        # Always fallback to AppleScript approach if desktop-notifier fails or isn't used
        # Fallback to AppleScript approach
        if response_handler and response_options:
            self.response_handlers[notification_id] = response_handler
            script = self._create_interactive_notification_script(
                title, message, response_options, notification_id
            )
        else:
            script = self._create_basic_notification_script(title, message)
        
        try:
            result = subprocess.run(["osascript", "-e", script], check=True, capture_output=True, text=True)
            print(f"Basic notification sent: {title}")
            if result.stdout:
                print(f"Dialog result: {result.stdout.strip()}")
                # Parse the response and call the handler
                if response_handler and result.stdout.strip():
                    try:
                        # Expected format: "notification_id:response"
                        if ':' in result.stdout:
                            response_id, response_text = result.stdout.strip().split(':', 1)
                            if response_id == notification_id:
                                response_handler(response_text)
                    except Exception as e:
                        print(f"Error handling response: {e}")
        except subprocess.CalledProcessError as e:
            print(f"Failed to send notification 3: {e}")
            print(f"Script output: {e.stderr}")
        
        return notification_id

    def _ensure_cf_runloop(self):
        """Ensure CFRunLoop is running for desktop-notifier callbacks"""
        if self._cf_runloop_started:
            return
        
        def run_cf_runloop():
            import sys
            import os
            venv_path = os.path.join(Path(__file__).parent.parent, 'pyobjc_env/lib/python3.13/site-packages')
            sys.path.insert(0, venv_path)
            
            from rubicon.objc.runtime import load_library
            import ctypes
            
            foundation = load_library('Foundation')
            
            # CFRunLoop functions
            CFRunLoopRun = foundation.CFRunLoopRun
            CFRunLoopRun.restype = None
            CFRunLoopRun.argtypes = []
            
            try:
                CFRunLoopRun()
            except:
                pass
        
        self._cf_runloop_thread = threading.Thread(target=run_cf_runloop, daemon=True)
        self._cf_runloop_thread.start()
        self._cf_runloop_started = True
        time.sleep(0.5)  # Give runloop time to start


    def send_notification(
        self,
        title: str,
        message: str,
        response_options: Optional[list] = None,
        response_handler: Optional[Callable] = None,
        notification_id: Optional[str] = None,
        use_reply_field: bool = True
    ) -> str:
        """
        Synchronous wrapper for send_notification_async
        """

        import asyncio
        asyncio.run(desktop_notify(title, message, send_task_notification))
        return ""

        print("self._cf_runloop_started and response_handler")
        if self._cf_runloop_started and response_handler:
            # For callbacks, we need to keep the event loop alive
            import platform
            import asyncio
            import signal
            if platform.system() == "Darwin":
                
                async def run_with_callbacks():
                    result = await self.send_notification_async(
                        title, message, response_options, response_handler, notification_id, use_reply_field
                    )
                    
                    # Wait for callback or timeout
                    event = asyncio.Event()
                    
                    def handle_signal():
                        event.set()
                    
                    loop = asyncio.get_running_loop()
                    loop.add_signal_handler(signal.SIGINT, handle_signal)
                    loop.add_signal_handler(signal.SIGTERM, handle_signal)
                    
                    # Wait 30 seconds for callback
                    try:
                        await asyncio.wait_for(event.wait(), timeout=30.0)
                    except asyncio.TimeoutError:
                        print("⏰ Callback timeout after 30 seconds")
                    
                    return result
                
                return asyncio.run(run_with_callbacks())
            else:
                return asyncio.run(self.send_notification_async(
                    title, message, response_options, response_handler, notification_id, use_reply_field
                ))
        else:
            import asyncio
            return asyncio.run(self.send_notification_async(
                title, message, response_options, response_handler, notification_id, use_reply_field
            ))

    def _create_basic_notification_script(self, title: str, message: str) -> str:
        """Create AppleScript for basic notification"""
        # Escape any quotes in message
        safe_message = message.replace('"', '\\"')
        safe_title = title.replace('"', '\\"')
        # Use desktop notification instead of alert
        return f'''display notification "{safe_title}" with title "{safe_title}" subtitle "" sound name "Frog"'''

    def _create_interactive_notification_script(
        self, 
        title: str, 
        message: str, 
        buttons: list, 
        notification_id: str
    ) -> str:
        """Create AppleScript for interactive notification with buttons"""
        # Create button list (ensure Dismiss is not duplicated)
        all_buttons = buttons.copy()
        if "Dismiss" not in all_buttons:
            all_buttons.append("Dismiss")
        button_list = '", "'.join(all_buttons)
        response_file = str(self.temp_dir / "responses")
        
        return f'''try
    set buttonResult to button returned of (display dialog "{message}" buttons {{"{button_list}"}} default button "{buttons[0] if buttons else 'Dismiss'}" with title "{title}")
    if buttonResult is not "Dismiss" then
        return "{notification_id}:" & buttonResult
    end if
end try'''

    def start_response_listener(self):
        """Start background thread to listen for notification responses"""
        def listener():
            response_file = self.temp_dir / "responses"
            while True:
                if response_file.exists():
                    try:
                        with open(response_file, 'r') as f:
                            for line in f:
                                line = line.strip()
                                if line and ':' in line:
                                    notification_id, response = line.split(':', 1)
                                    self._handle_response(notification_id, response)
                        
                        # Clear file after processing
                        response_file.unlink()
                        response_file.touch()
                    except Exception as e:
                        print(f"Error processing responses: {e}")
                
                threading.Event().wait(1)  # Check every second
        
        listener_thread = threading.Thread(target=listener, daemon=True)
        listener_thread.start()
        return listener_thread

    def _handle_response(self, notification_id: str, response: str):
        """Handle incoming notification response"""
        if notification_id in self.response_handlers:
            try:
                self.response_handlers[notification_id](response)
            except Exception as e:
                print(f"Error in response handler: {e}")
            finally:
                # Clean up the handler after use
                del self.response_handlers[notification_id]

    def send_task_notification(
        self,
        task_key: str,
        task_title: str,
        action: str = "update",
        response_handler: Optional[Callable] = None
    ) -> str:
        """
        Send a task-related notification with common response options
        
        Args:
            task_key: Task identifier (e.g., JIRA ticket)
            task_title: Task title
            action: Type of action (start, complete, update, etc.)
            response_handler: Handler for response
        """
        title = f"Task {action.title()}: {task_key}"
        message = task_title
        
        if action == "start":
            response_options = ["Start Working", "View Details"]
        elif action == "complete":
            response_options = ["Mark Complete", "Add Comment", "View Details"]
        else:
            response_options = ["View Details", "Dismiss"]
        
        return self.send_notification(
            title=title,
            message=message,
            response_options=response_options,
            response_handler=response_handler,
            notification_id=f"task_{task_key}_{action}"
        )

    def send_error_notification(
        self,
        error_message: str,
        context: str = "",
        response_handler: Optional[Callable] = None
    ) -> str:
        """Send error notification with debugging options"""
        title = "Workflow Error"
        message = f"{context}: {error_message}" if context else error_message
        response_options = ["View Logs", "Retry", "Dismiss"]
        
        return self.send_notification(
            title=title,
            message=message,
            response_options=response_options,
            response_handler=response_handler,
            notification_id=f"error_{hash(error_message)}"
        )


# Global notification service instance
_notification_service = None
_response_listener_thread = None

def get_notification_service() -> NotificationService:
    """Get or create the global notification service instance"""
    global _notification_service, _response_listener_thread
    
    if _notification_service is None:
        _notification_service = NotificationService()
        _response_listener_thread = _notification_service.start_response_listener()
    
    return _notification_service

def send_notification(
    title: str,
    message: str,
    response_options: Optional[list] = None,
    response_handler: Optional[Callable] = None,
    use_reply_field: bool = True
) -> str:
    """Convenience function to send a notification with inline reply support"""
    service = get_notification_service()
    #return service.send_notification(title, message, response_options, response_handler, use_reply_field=use_reply_field)

def send_task_notification(
    task_key: str,
    task_title: str,
    action: str = "update",
    response_handler: Optional[Callable] = None
) -> str:
    """Convenience function to send task notifications with inline reply"""
    service = get_notification_service()
    # return service.send_task_notification(task_key, task_title, action, response_handler)
