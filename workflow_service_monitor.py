#!/usr/bin/env python3
"""
Simple service commands for workflow monitor service management
"""
import subprocess
import sys
from pathlib import Path

def main():
    if len(sys.argv) < 2:
        print("Usage: python3 workflow_service_monitor.py <command>")
        print("Commands: start, stop, restart, status")
        sys.exit(1)
    
    command = sys.argv[1]
    
    # Ensure we're in the right directory
    script_path = Path(__file__).parent
    service_script = script_path / 'workflow' / 'monitor_service.py'
    
    if not service_script.exists():
        print(f"❌ Service script not found: {service_script}")
        sys.exit(1)
    
    try:
        if command == "start":
            result = subprocess.run([
                sys.executable, str(service_script), 'start'
            ], capture_output=True, text=True)
            print(result.stdout)
            
        elif command == "stop":
            result = subprocess.run([
                sys.executable, str(service_script), 'stop'
            ], capture_output=True, text=True)
            print(result.stdout)
            
        elif command == "restart":
            # Stop first
            result = subprocess.run([
                sys.executable, str(service_script), 'stop'
            ], capture_output=True, text=True)
            
            if result.returncode == 0:
                # Start again
                result = subprocess.run([
                    sys.executable, str(service_script), 'start'
                ], capture_output=True, text=True)
                print(result.stdout)
            else:
                print(f"❌ Failed to stop service: {result.stderr}")
                
        elif command == "status":
            result = subprocess.run([
                sys.executable, str(service_script), 'status'
            ], capture_output=True, text=True)
            print(result.stdout)
            
        else:
            print(f"❌ Unknown command: {command}")
            print("Available commands: start, stop, restart, status")
            
    except Exception as e:
        print(f"❌ Error: {e}")

if __name__ == "__main__":
    main()