"""
Tests for the health endpoint.
"""
import pytest
import requests


class TestHealthEndpoint:
    """Test the health check endpoint"""
    
    def test_health_returns_ok(self, daemon_url):
        """Health endpoint should return ok status"""
        response = requests.get(f"{daemon_url}/health")
        assert response.status_code == 200
        assert response.json()["status"] == "ok"
    
    def test_health_is_json(self, daemon_url):
        """Health endpoint should return valid JSON"""
        response = requests.get(f"{daemon_url}/health")
        data = response.json()
        assert "status" in data
