"""
Tests for wait functionality.
"""
import pytest
import requests
import time


# Every endpoint here drives a real page, so the whole module needs a
# Chrome with remote debugging (see the `chrome` fixture in conftest.py).
pytestmark = pytest.mark.usefixtures("chrome")


class TestWaitCommand:
    """Test wait functionality"""
    
    def test_wait_for_existing_element(self, daemon_url):
        """Should handle wait for existing element"""
        requests.put(
            f"{daemon_url}/open",
            json={"url": "https://example.com"},
            timeout=60
        )
        time.sleep(1)
        
        response = requests.post(
            f"{daemon_url}/wait",
            json={"text": "Example", "timeout": 5000},
            timeout=10
        )
        assert response.status_code == 200
    
    def test_wait_returns_status(self, daemon_url):
        """Should return status in response"""
        requests.put(
            f"{daemon_url}/open",
            json={"url": "https://example.com"},
            timeout=60
        )
        time.sleep(1)
        
        response = requests.post(
            f"{daemon_url}/wait",
            json={"text": "Example", "timeout": 5000},
            timeout=10
        )
        result = response.json()
        assert "status" in result
    
    def test_wait_with_custom_timeout(self, daemon_url):
        """Should handle custom timeout"""
        requests.put(
            f"{daemon_url}/open",
            json={"url": "https://example.com"},
            timeout=60
        )
        time.sleep(1)
        
        response = requests.post(
            f"{daemon_url}/wait",
            json={"text": "Example", "timeout": 1000},
            timeout=10
        )
        assert response.status_code in [200, 500]

    def test_wait_for_selector(self, daemon_url):
        """Should wait on a CSS selector when given one"""
        requests.put(
            f"{daemon_url}/open",
            json={"url": "https://example.com"},
            timeout=60
        )
        time.sleep(1)

        response = requests.post(
            f"{daemon_url}/wait",
            json={"selector": "a", "timeout": 5000},
            timeout=10
        )
        assert response.status_code == 200
        assert response.json()["status"] == "ok"

    def test_wait_for_absent_text_errors_as_json(self, daemon_url):
        """A timeout must come back as JSON, not aiohttp's bodyless 504"""
        requests.put(
            f"{daemon_url}/open",
            json={"url": "https://example.com"},
            timeout=60
        )
        time.sleep(1)

        response = requests.post(
            f"{daemon_url}/wait",
            json={"text": "NoSuchTextOnThisPage", "timeout": 1000},
            timeout=15
        )
        assert response.status_code == 500
        assert response.json()["status"] == "error"

    def test_wait_without_text_or_selector_is_a_bad_request(self, daemon_url):
        """Neither argument given is the caller's mistake, not a page error"""
        response = requests.post(f"{daemon_url}/wait", json={}, timeout=10)
        assert response.status_code == 400
        assert response.json()["status"] == "error"
