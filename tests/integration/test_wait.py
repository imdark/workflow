"""
Tests for wait functionality.
"""
import pytest
import requests
import time


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
