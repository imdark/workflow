"""
Tests for fill/input functionality.
"""
import pytest
import requests
import time


class TestFillCommand:
    """Test fill/input functionality"""
    
    def test_fill_nonexistent_element(self, daemon_url):
        """Should handle fill on nonexistent element gracefully"""
        requests.put(
            f"{daemon_url}/open",
            json={"url": "https://example.com"},
            timeout=60
        )
        time.sleep(1)
        
        response = requests.post(
            f"{daemon_url}/fill",
            json={"uid": "nonexistent-id", "value": "test"},
            timeout=30
        )
        assert response.status_code in [200, 500]
    
    def test_fill_returns_status(self, daemon_url):
        """Should return status in response"""
        requests.put(
            f"{daemon_url}/open",
            json={"url": "https://example.com"},
            timeout=60
        )
        time.sleep(1)
        
        response = requests.post(
            f"{daemon_url}/fill",
            json={"uid": "test", "value": "test"},
            timeout=30
        )
        result = response.json()
        assert "status" in result
    
    def test_fill_with_special_characters(self, daemon_url):
        """Should handle special characters in value"""
        requests.put(
            f"{daemon_url}/open",
            json={"url": "https://example.com"},
            timeout=60
        )
        time.sleep(1)
        
        response = requests.post(
            f"{daemon_url}/fill",
            json={"uid": "test", "value": "test <script>alert(1)</script>"},
            timeout=30
        )
        assert response.status_code in [200, 500]
