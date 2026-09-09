#!/usr/bin/env python3
"""
Workflow Monitor Service - System service for continuous monitoring
"""
import sys
import os
import time
import signal
import subprocess
from pathlib import Path

# Add workflow path
sys.path.insert(0, str(Path(__file__).parent))

from workflow.monitor import WorkflowMonitor
from rich.console import Console

console = Console(color_system=None)

class MonitorService:
    def __init__(self):
        self.monitor = WorkflowMonitor()
        self.running = False
        self.shutdown_requested = False
    
    def signal_handler(self, signum, frame):
        console.print("\n🛑 Monitor service shutting down...")
        self.shutdown_requested = True
    
    def start(self):
        """Start the monitor service"""
        self.running = True
        
        # Set up signal handlers
        signal.signal(signal.SIGINT, self.signal_handler)
        signal.signal(signal.SIGTERM, self.signal_handler)
        
        console.print("🚀 Workflow Monitor Service starting...")
        
        # Load and display configured monitors
        self.monitor.load_saved_monitors()
        if not self.monitor.monitors:
            console.print("⚠️  No monitors configured. Use 'wf monitor add' commands to add monitors.")
            return
        
        console.print(f"📋 Loaded {len(self.monitor.monitors)} monitor(s):")
        for name, monitor in self.monitor.monitors.items():
            monitor_type = type(monitor).__name__
            if hasattr(monitor, 'path'):
                target = monitor.path
            elif hasattr(monitor, 'url'):
                target = monitor.url
            else:
                target = "Unknown"
            console.print(f"  • {name} ({monitor_type}) → {target}")
        
        console.print("🔍 Starting continuous monitoring...")
        console.print("💡 Press Ctrl+C to stop")
        
        # Main monitoring loop
        try:
            while not self.shutdown_requested:
                start_time = time.time()
                
                # Check all monitors
                all_changes = []
                for name, monitor in self.monitor.monitors.items():
                    if monitor.running:
                        changes = monitor.check()
                        for change in changes:
                            change['monitor_name'] = name
                            all_changes.append(change)
                
                # Display and execute actions for changes
                if all_changes:
                    self.monitor._display_changes(all_changes)
                
                # Sleep with interrupt check
                elapsed = time.time() - start_time
                remaining_sleep = max(0, 2 - elapsed)  # 2-second interval
                
                # Sleep in small chunks to allow faster shutdown
                for _ in range(int(remaining_sleep * 10)):
                    if self.shutdown_requested:
                        break
                    time.sleep(0.1)
                    
        except KeyboardInterrupt:
            pass
        
        console.print("🛑 Monitor service stopped")
    
    def status(self):
        """Show service status"""
        self.monitor.load_saved_monitors()
        
        if not self.monitor.monitors:
            console.print("❌ No monitors configured")
            return
        
        console.print("📋 Configured monitors:")
        for name, monitor in self.monitor.monitors.items():
            monitor_type = type(monitor).__name__
            if hasattr(monitor, 'path'):
                target = monitor.path
            elif hasattr(monitor, 'url'):
                target = monitor.url
            else:
                target = "Unknown"
            
            status = "🟢 Running" if monitor.running else "🔴 Stopped"
            console.print(f"  • {name} ({monitor_type}) → {target} [{status}]")
    
    def reload(self):
        """Reload configuration"""
        console.print("🔄 Reloading monitor configuration...")
        self.monitor.load_saved_monitors()
        console.print(f"✅ Reloaded {len(self.monitor.monitors)} monitor(s)")

def main():
    if len(sys.argv) < 2:
        print("Usage: workflow-monitor-service <command>")
        print("Commands:")
        print("  start   - Start the monitor service")
        print("  status  - Show service status") 
        print("  reload  - Reload configuration")
        sys.exit(1)
    
    command = sys.argv[1]
    service = MonitorService()
    
    if command == "start":
        service.start()
    elif command == "status":
        service.status()
    elif command == "reload":
        service.reload()
    else:
        print(f"Unknown command: {command}")
        sys.exit(1)

if __name__ == "__main__":
    main()