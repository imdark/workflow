"""
Tests for screenshot functionality.
"""
import pytest
import requests
import time
from pathlib import Path


# Every endpoint here drives a real page, so the whole module needs a
# Chrome with remote debugging (see the `chrome` fixture in conftest.py).
pytestmark = pytest.mark.usefixtures("chrome")


class TestScreenshotCommand:
    """Test screenshot functionality"""
    
    def test_screenshot_creates_file(self, daemon_url, tmp_path):
        """Screenshot should create a file"""
        output_path = tmp_path / "test_screenshot.png"
        
        requests.put(
            f"{daemon_url}/open",
            json={"url": "https://example.com"},
            timeout=60
        )
        time.sleep(1)
        
        response = requests.post(
            f"{daemon_url}/screenshot",
            json={"output": str(output_path)},
            timeout=30
        )
        assert response.status_code == 200
        assert output_path.exists()
        assert output_path.stat().st_size > 0
    
    def test_screenshot_with_custom_filename(self, daemon_url, tmp_path):
        """Screenshot should use custom filename"""
        output_path = tmp_path / "custom_name.png"
        
        requests.put(
            f"{daemon_url}/open",
            json={"url": "https://example.com"},
            timeout=60
        )
        time.sleep(1)
        
        response = requests.post(
            f"{daemon_url}/screenshot",
            json={"output": str(output_path)},
            timeout=30
        )
        result = response.json()
        assert result["saved_path"] == str(output_path)
    
    def test_screenshot_returns_saved_path(self, daemon_url, tmp_path):
        """Screenshot should return saved path in response"""
        output_path = tmp_path / "response_test.png"
        
        requests.put(
            f"{daemon_url}/open",
            json={"url": "https://example.com"},
            timeout=60
        )
        time.sleep(1)
        
        response = requests.post(
            f"{daemon_url}/screenshot",
            json={"output": str(output_path)},
            timeout=30
        )
        result = response.json()
        assert "saved_path" in result
    
    def test_screenshot_different_formats(self, daemon_url, tmp_path):
        """Screenshot should work with the formats pyppeteer supports"""
        for filename in ["test1.png", "test2.jpg", "test3.jpeg"]:
            output_path = tmp_path / filename
            
            requests.put(
                f"{daemon_url}/open",
                json={"url": "https://example.com"},
                timeout=60
            )
            time.sleep(0.5)
            
            response = requests.post(
                f"{daemon_url}/screenshot",
                json={"output": str(output_path)},
                timeout=30
            )
            assert response.status_code == 200

    def test_screenshot_unsupported_format_errors_as_json(self, daemon_url, tmp_path):
        """An unsupported format must fail as JSON, not a bodyless 500"""
        requests.put(
            f"{daemon_url}/open",
            json={"url": "https://example.com"},
            timeout=60
        )
        time.sleep(0.5)

        response = requests.post(
            f"{daemon_url}/screenshot",
            json={"output": str(tmp_path / "shot.webp")},
            timeout=30
        )
        assert response.status_code == 500
        assert response.json()["status"] == "error"
