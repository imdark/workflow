"""
Tests for pages listing.
"""
import pytest
import requests
import time


# Every endpoint here drives a real page, so the whole module needs a
# Chrome with remote debugging (see the `chrome` fixture in conftest.py).
pytestmark = pytest.mark.usefixtures("chrome")


class TestPagesCommand:
    """Test pages listing"""
    
    def test_pages_returns_list(self, daemon_url):
        """Should return list of pages"""
        requests.put(
            f"{daemon_url}/open",
            json={"url": "https://example.com"},
            timeout=60
        )
        time.sleep(1)
        
        response = requests.post(
            f"{daemon_url}/pages",
            json={},
            timeout=30
        )
        assert response.status_code == 200
        result = response.json()
        assert "pages" in result
    
    def test_pages_returns_pages_key(self, daemon_url):
        """Should return pages array"""
        requests.put(
            f"{daemon_url}/open",
            json={"url": "https://example.com"},
            timeout=60
        )
        time.sleep(1)
        
        response = requests.post(
            f"{daemon_url}/pages",
            json={},
            timeout=30
        )
        result = response.json()
        assert isinstance(result.get("pages"), list)
    
    def test_pages_with_multiple_pages(self, daemon_url):
        """Should handle multiple pages"""
        requests.put(
            f"{daemon_url}/open",
            json={"url": "https://example.com"},
            timeout=60
        )
        time.sleep(1)
        
        requests.put(
            f"{daemon_url}/open",
            json={"url": "https://www.w3.org/"},
            timeout=60
        )
        time.sleep(1)
        
        response = requests.post(
            f"{daemon_url}/pages",
            json={},
            timeout=30
        )
        result = response.json()
        assert "pages" in result
