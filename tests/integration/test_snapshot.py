"""
Tests for page snapshot/HTML retrieval.
"""
import pytest
import requests
import time


# Every endpoint here drives a real page, so the whole module needs a
# Chrome with remote debugging (see the `chrome` fixture in conftest.py).
pytestmark = pytest.mark.usefixtures("chrome")


class TestSnapshotCommand:
    """Test page snapshot/HTML retrieval"""
    
    def test_snapshot_returns_html(self, daemon_url):
        """Should return page HTML content"""
        requests.put(
            f"{daemon_url}/open",
            json={"url": "https://example.com"},
            timeout=60
        )
        time.sleep(1)
        
        response = requests.post(
            f"{daemon_url}/snapshot",
            json={},
            timeout=30
        )
        assert response.status_code == 200
        result = response.json()
        assert "Example Domain" in result["content"]
    
    def test_snapshot_returns_content_key(self, daemon_url):
        """Should return content in response"""
        requests.put(
            f"{daemon_url}/open",
            json={"url": "https://example.com"},
            timeout=60
        )
        time.sleep(1)
        
        response = requests.post(
            f"{daemon_url}/snapshot",
            json={},
            timeout=30
        )
        result = response.json()
        assert "content" in result
    
    def test_snapshot_contains_html_tags(self, daemon_url):
        """Should return full HTML"""
        requests.put(
            f"{daemon_url}/open",
            json={"url": "https://example.com"},
            timeout=60
        )
        time.sleep(1)
        
        response = requests.post(
            f"{daemon_url}/snapshot",
            json={},
            timeout=30
        )
        result = response.json()
        assert "<html" in result["content"].lower() or "<!doctype" in result["content"].lower()
    
    def test_snapshot_contains_body(self, daemon_url):
        """Should return body content"""
        requests.put(
            f"{daemon_url}/open",
            json={"url": "https://example.com"},
            timeout=60
        )
        time.sleep(1)
        
        response = requests.post(
            f"{daemon_url}/snapshot",
            json={},
            timeout=30
        )
        result = response.json()
        assert "<body" in result["content"].lower() or "example" in result["content"].lower()
