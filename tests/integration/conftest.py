import pytest
import subprocess
import time
import os
import socket
import requests
from pathlib import Path

DAEMON_PORT = 18765
CHROME_PORT = 9222
TEST_STATE_DIR = Path.home() / ".wf" / "browser" / "test"


@pytest.fixture(scope="function")
def daemon_port():
    """Unique port for each test to ensure isolation"""
    port = DAEMON_PORT + hash(os.environ.get("PYTEST_CURRENT_TEST", "")) % 1000
    return port


@pytest.fixture(scope="function")
def test_state_dir(tmp_path):
    """Isolated state directory for each test"""
    state_dir = tmp_path / "browser_state"
    state_dir.mkdir(parents=True, exist_ok=True)
    return state_dir


def is_port_open(port):
    """Check if a port is open"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(1)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def wait_for_port(port, timeout=10):
    """Wait for a port to become available"""
    start = time.time()
    while time.time() - start < timeout:
        if is_port_open(port):
            return True
        time.sleep(0.5)
    return False


def stop_daemon_on_port(port):
    """Stop any daemon running on the given port"""
    try:
        response = requests.get(f"http://127.0.0.1:{port}/health", timeout=2)
        if response.status_code == 200:
            import subprocess
            subprocess.run(["lsof", "-t", "-i", f":{port}"], capture_output=True, text=True)
    except:
        pass


@pytest.fixture(scope="function")
def clean_port(daemon_port):
    """Ensure port is clean before test"""
    stop_daemon_on_port(daemon_port)
    yield
    stop_daemon_on_port(daemon_port)


@pytest.fixture(scope="function")
def browser_daemon(clean_port, daemon_port, monkeypatch):
    """Start browser daemon for testing"""
    monkeypatch.setenv("BROWSER_DAEMON_PORT", str(daemon_port))
    
    import sys
    proc = subprocess.Popen(
        [sys.executable, "-m", "workflow.browser_daemon"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env={**os.environ, "BROWSER_DAEMON_PORT": str(daemon_port)}
    )
    
    wait_for_port(daemon_port, timeout=10)
    time.sleep(1)
    
    yield daemon_port
    
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()


@pytest.fixture(scope="function")
def daemon_url(browser_daemon):
    """Get the daemon URL"""
    return f"http://127.0.0.1:{browser_daemon}"
