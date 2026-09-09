"""
Tests for opening URLs in browser.
"""
import pytest
import requests


class TestOpenCommand:
    """Test opening URLs in browser"""
    
    def test_open_example_dot_com(self, daemon_url):
        """Should be able to open example.com"""
        response = requests.put(
            f"{daemon_url}/open",
            json={"url": "https://example.com"},
            timeout=60
        )
        assert response.status_code == 200
        assert response.json()["status"] == "ok"
    
    def test_open_w3_org(self, daemon_url):
        """Should be able to open w3.org"""
        response = requests.put(
            f"{daemon_url}/open",
            json={"url": "https://www.w3.org/"},
            timeout=60
        )
        assert response.status_code == 200
    
    def test_open_responds_with_url(self, daemon_url):
        """Open should respond with the URL"""
        url = "https://example.com"
        response = requests.put(
            f"{daemon_url}/open",
            json={"url": url},
            timeout=60
        )
        result = response.json()
        assert result.get("url") == url
    
    def test_open_invalid_url(self, daemon_url):
        """Should handle invalid URLs gracefully"""
        response = requests.put(
            f"{daemon_url}/open",
            json={"url": "not-a-valid-url"},
            timeout=10
        )
        assert response.status_code == 500
    
    def test_open_with_special_characters(self, daemon_url):
        """Should handle URLs with special characters"""
        response = requests.put(
            f"{daemon_url}/open",
            json={"url": "https://example.com/?q=test+case"},
            timeout=60
        )
        assert response.status_code == 200
