"""
Tests for click functionality.
"""
import pytest
import requests
import time


# Every endpoint here drives a real page, so the whole module needs a
# Chrome with remote debugging (see the `chrome` fixture in conftest.py).
pytestmark = pytest.mark.usefixtures("chrome")


class TestClickCommand:
    """Test click functionality"""
    
    def test_click_element(self, daemon_url):
        """Should be able to click elements"""
        requests.put(
            f"{daemon_url}/open",
            json={"url": "https://example.com"},
            timeout=60
        )
        time.sleep(1)
        
        # `uid` is a CSS selector (BrowserClient.click names it `selector`);
        # example.com's only link is the "More information..." anchor.
        response = requests.post(
            f"{daemon_url}/click",
            json={"uid": "a"},
            timeout=30
        )
        assert response.status_code == 200
    
    def test_click_returns_status(self, daemon_url):
        """Should return status in response"""
        requests.put(
            f"{daemon_url}/open",
            json={"url": "https://example.com"},
            timeout=60
        )
        time.sleep(1)
        
        response = requests.post(
            f"{daemon_url}/click",
            json={"uid": "test"},
            timeout=30
        )
        result = response.json()
        assert "status" in result
    
    def test_click_nonexistent_element(self, daemon_url):
        """Should handle click on nonexistent element"""
        requests.put(
            f"{daemon_url}/open",
            json={"url": "https://example.com"},
            timeout=60
        )
        time.sleep(1)
        
        response = requests.post(
            f"{daemon_url}/click",
            json={"uid": "nonexistent-element-12345"},
            timeout=30
        )
        assert response.status_code == 500
        # Errors must stay JSON -- BrowserClient calls resp.json() on every reply.
        assert response.json()["status"] == "error"
