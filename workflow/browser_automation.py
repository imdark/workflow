import socket
import json
import subprocess
import threading
import time
import os
from pathlib import Path
from typing import Dict, Any, Optional, List
import typer
import select

BROWSER_STATE_FILE = Path.home() / ".wf" / "browser_state.yaml"
DEFAULT_DEBUG_PORT = 9222


def _load_browser_state() -> Dict[str, Any]:
    try:
        import yaml
        if BROWSER_STATE_FILE.exists():
            with open(BROWSER_STATE_FILE) as f:
                return yaml.safe_load(f) or {}
    except Exception:
        pass
    return {}


def _save_browser_state(state: Dict[str, Any]) -> None:
    try:
        import yaml
        BROWSER_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(BROWSER_STATE_FILE, 'w') as f:
            yaml.dump(state, f)
    except Exception:
        pass


def is_port_listening(port: int, host: str = 'localhost') -> bool:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(1)
    try:
        result = sock.connect_ex((host, port))
        sock.close()
        return result == 0
    except Exception:
        return False


def get_websocket_url(port: int) -> Optional[str]:
    try:
        import urllib.request
        url = f"http://localhost:{port}/json/version"
        with urllib.request.urlopen(url, timeout=2) as response:
            data = json.loads(response.read())
            return data.get('webSocketDebuggerUrl')
    except Exception:
        pass
    
    if is_port_listening(port):
        return f"ws://127.0.0.1:{port}/devtools/browser/"
    return None


def find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(('', 0))
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        return s.getsockname()[1]


def run_mcp_command(ws_url: str, method: str, params: Dict = None) -> Optional[Dict]:
    """Run a single MCP command via shell - returns response dict"""
    import subprocess
    import time
    
    request = {
        "jsonrpc": "2.0",
        "id": 2,
        "method": method,
        "params": params or {}
    }
    
    request_json = json.dumps(request)
    
    print(f"[MCP] Running with stdin...", file=__import__('sys').stderr)
    
    # Use subprocess.Popen with explicit stdin/stdout
    cmd = ['npx', '-y', 'chrome-devtools-mcp@latest', f'--wsEndpoint={ws_url}', '--transport=stdio']
    
    print(f"[MCP] Cmd: {' '.join(cmd)}", file=__import__('sys').stderr)
    
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1
    )
    
    # Write to stdin - send request_json followed by newline
    proc.stdin.write(request_json + '\n')
    proc.stdin.flush()
    
    # Close stdin so server knows we're done sending
    proc.stdin.close()
    
    # Read stdout
    output = ''
    start = time.time()
    
    while time.time() - start < 120:
        line = proc.stdout.readline()
        if line:
            output += line
            print(f"[MCP] Line: {line[:60]}...", file=__import__('sys').stderr)
            
            if '"jsonrpc"' in line:
                try:
                    return json.loads(line.strip())
                except:
                    pass
        
        # Check if process ended
        if proc.poll() is not None:
            break
    
    stderr = proc.stderr.read()
    if stderr:
        print(f"[MCP] Stderr: {stderr[:200]}", file=__import__('sys').stderr)
    
    return None


class MCPClient:
    def __init__(self):
        self.process = None
        self.message_id = 1
        self.pipe_port = None
    
    def start(self, ws_url: str) -> bool:
        self.pipe_port = find_free_port()
        
        try:
            # Build init command
            init_cmd = '{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "manual-test", "version": "1.0"}}}'
            
            # Use process substitution to run initialize and capture output
            cmd = f'echo {init_cmd} | npx -y chrome-devtools-mcp@latest --wsEndpoint={ws_url} --transport=stdio'
            
            self.process = subprocess.Popen(
                cmd,
                shell=True,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                executable='/bin/bash'
            )
            
            # Read init response
            response = self.process.stdout.readline()
            print(f"[MCP] Init: {response[:200] if response else 'none'}", file=__import__('sys').stderr)
            
            return True
        except Exception as e:
            print(f"Failed to start MCP: {e}")
            return False
    
    def send_json(self, request: Dict) -> Optional[Dict]:
        """Send a raw JSON-RPC request and return response"""
        # This client is single-shot, so just return empty
        return None
    
    def is_running(self) -> bool:
        return self.process is not None and self.process.poll() is None


class BrowserDaemon:
    def __init__(self, port: int = DEFAULT_DEBUG_PORT):
        self.port = port
        self.ws_url = None
        self.mcp_client = None
        self.running = False
        self._restore_state()
    
    def _restore_state(self):
        state = _load_browser_state()
        if state.get('daemon_running'):
            self.port = state.get('port', self.port)
            self.ws_url = state.get('ws_url') or get_websocket_url(self.port)
            self.running = bool(self.ws_url)
        
    def connect(self) -> bool:
        if not is_port_listening(self.port):
            return False
            
        ws_url = get_websocket_url(self.port)
        if not ws_url:
            return False
            
        self.ws_url = ws_url
        
        self.mcp_client = MCPClient()
        if self.mcp_client.start(ws_url):
            self.running = True
            _save_browser_state({'daemon_running': True, 'port': self.port, 'ws_url': self.ws_url})
            return True
        return False
    
    def start_daemon(self) -> bool:
        return self.connect()
    
    def stop(self):
        self.running = False
        if self.mcp_client and self.mcp_client.process:
            try:
                self.mcp_client.process.terminate()
                self.mcp_client.process.wait(timeout=2)
            except Exception:
                try:
                    self.mcp_client.process.kill()
                except Exception:
                    pass
        _save_browser_state({})
    
    def is_connected(self) -> bool:
        if not self.running or not self.ws_url:
            self._restore_state()
        
        if self.running and self.ws_url:
            if self.mcp_client and self.mcp_client.is_running():
                return True
            self.mcp_client = MCPClient()
            if self.mcp_client.start(self.ws_url):
                return True
        
        return False
    
    def send_command(self, method: str, params: Optional[Dict] = None) -> tuple[Optional[Dict], Optional[str]]:
        if not self.is_connected():
            return None, "Browser not connected. Run 'wf browser connect' first."
        
        try:
            result = self.mcp_client.call_tool(method, params)
            return result, None
        except Exception as e:
            return None, f"Command failed: {str(e)}"


_daemon_instance: Optional[BrowserDaemon] = None


def get_browser_daemon() -> BrowserDaemon:
    global _daemon_instance
    if _daemon_instance is None:
        _daemon_instance = BrowserDaemon()
    return _daemon_instance


def close_browser():
    global _daemon_instance
    if _daemon_instance:
        _daemon_instance.stop()
        _daemon_instance = None
    _save_browser_state({})
