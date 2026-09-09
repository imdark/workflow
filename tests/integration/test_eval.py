"""
Tests for JavaScript evaluation.
"""
import pytest
import requests
import time


# Every endpoint here drives a real page, so the whole module needs a
# Chrome with remote debugging (see the `chrome` fixture in conftest.py).
pytestmark = pytest.mark.usefixtures("chrome")


class TestEvalCommand:
    """Test JavaScript evaluation"""
    
    def test_eval_document_title(self, daemon_url):
        """Should evaluate document.title"""
        requests.put(
            f"{daemon_url}/open",
            json={"url": "https://example.com"},
            timeout=60
        )
        time.sleep(1)
        
        response = requests.post(
            f"{daemon_url}/eval",
            json={"script": "document.title"},
            timeout=30
        )
        assert response.status_code == 200
        result = response.json()
        assert "Example" in result["result"]
    
    def test_eval_arithmetic(self, daemon_url):
        """Should evaluate arithmetic expressions"""
        requests.put(
            f"{daemon_url}/open",
            json={"url": "https://example.com"},
            timeout=60
        )
        time.sleep(1)
        
        response = requests.post(
            f"{daemon_url}/eval",
            json={"script": "2 + 2"},
            timeout=30
        )
        result = response.json()
        assert result["result"] == "4"
    
    def test_eval_returns_empty_for_no_return(self, daemon_url):
        """Should handle scripts with no return value"""
        requests.put(
            f"{daemon_url}/open",
            json={"url": "https://example.com"},
            timeout=60
        )
        time.sleep(1)
        
        response = requests.post(
            f"{daemon_url}/eval",
            json={"script": "console.log('test')"},
            timeout=30
        )
        assert response.status_code == 200
    
    def test_eval_returns_result_key(self, daemon_url):
        """Should return result in response"""
        requests.put(
            f"{daemon_url}/open",
            json={"url": "https://example.com"},
            timeout=60
        )
        time.sleep(1)
        
        response = requests.post(
            f"{daemon_url}/eval",
            json={"script": "'hello'"},
            timeout=30
        )
        result = response.json()
        assert "result" in result
    
    def test_eval_dom_access(self, daemon_url):
        """Should evaluate DOM access"""
        requests.put(
            f"{daemon_url}/open",
            json={"url": "https://example.com"},
            timeout=60
        )
        time.sleep(1)
        
        response = requests.post(
            f"{daemon_url}/eval",
            json={"script": "document.querySelector('h1').innerText"},
            timeout=30
        )
        assert response.status_code == 200
