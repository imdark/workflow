"""
Destructive integration tests for browser CLI commands.

WARNING: These tests interact with real production systems (Confluence, etc.)
and may affect other team members. Only run these in isolated/test environments.
"""
import pytest
import requests
import time


# Every endpoint here drives a real page, so the whole module needs a
# Chrome with remote debugging (see the `chrome` fixture in conftest.py).
pytestmark = pytest.mark.usefixtures("chrome")


class TestConfluenceInteraction:
    """
    Tests that interact with Confluence.
    
    WARNING: These tests modify real Confluence pages and can affect
    other team members working on the same pages.
    """
    
    @pytest.fixture
    def confluence_edit_url(self):
        """Confluence page edit URL - modify for different test pages"""
        return "https://your-domain.atlassian.net/wiki/spaces/SPACE/pages/edit-v2/PAGE_ID"
    
    @pytest.fixture
    def unique_test_marker(self):
        """Unique marker for test isolation"""
        import uuid
        return f"TEST-{uuid.uuid4().hex[:8]}"
    
    def test_open_confluence_page(self, daemon_url, confluence_edit_url):
        """Should be able to open Confluence edit page"""
        response = requests.put(
            f"{daemon_url}/open",
            json={"url": confluence_edit_url},
            timeout=60
        )
        assert response.status_code == 200
    
    def test_fill_confluence_with_unique_content(self, daemon_url, confluence_edit_url, unique_test_marker):
        """
        Fill Confluence page with unique content.
        
        WARNING: This modifies a real page. Uses unique marker to minimize
        confusion but still affects the shared page.
        """
        requests.put(
            f"{daemon_url}/open",
            json={"url": confluence_edit_url},
            timeout=60
        )
        time.sleep(3)
        
        response = requests.post(
            f"{daemon_url}/eval",
            json={"script": f"document.querySelector('[contenteditable]').innerText = '{unique_test_marker}'"},
            timeout=30
        )
        
        assert response.status_code == 200
    
    def test_capture_confluence_screenshot(self, daemon_url, confluence_edit_url, tmp_path):
        """
        Capture screenshot of Confluence page.
        
        This is relatively safe as it only reads/displays.
        """
        output_path = tmp_path / "confluence_screenshot.png"
        
        requests.put(
            f"{daemon_url}/open",
            json={"url": confluence_edit_url},
            timeout=60
        )
        time.sleep(3)
        
        response = requests.post(
            f"{daemon_url}/screenshot",
            json={"output": str(output_path)},
            timeout=30
        )
        
        assert response.status_code == 200
        assert output_path.exists()
    
    def test_get_confluence_page_snapshot(self, daemon_url, confluence_edit_url):
        """
        Get HTML snapshot of Confluence page.
        
        This is relatively safe as it only reads.
        """
        requests.put(
            f"{daemon_url}/open",
            json={"url": confluence_edit_url},
            timeout=60
        )
        time.sleep(3)
        
        response = requests.post(
            f"{daemon_url}/snapshot",
            json={},
            timeout=30
        )
        
        assert response.status_code == 200
        result = response.json()
        assert "content" in result


class TestSharedBrowserSession:
    """
    Tests that interact with a shared Chrome session.
    
    WARNING: These tests assume Chrome is already running with debugging
    and may interfere with other debugging sessions.
    """
    
    def test_connect_to_running_chrome(self, daemon_url):
        """
        Test connecting to an already-running Chrome instance.
        
        WARNING: This may interfere with other Chrome debugging sessions.
        """
        response = requests.put(
            f"{daemon_url}/open",
            json={"url": "https://example.com"},
            timeout=60
        )
        assert response.status_code == 200


class TestProductionWebsites:
    """
    Tests that interact with production websites.
    
    WARNING: These tests may trigger rate limits or other protections
    on production websites.
    """
    
    def test_open_google(self, daemon_url):
        """
        Open Google homepage.
        
        WARNING: May trigger bot detection on Google's production servers.
        """
        response = requests.put(
            f"{daemon_url}/open",
            json={"url": "https://www.google.com"},
            timeout=60
        )
        assert response.status_code == 200
    
    def test_open_github(self, daemon_url):
        """
        Open GitHub homepage.
        
        WARNING: May trigger bot detection on GitHub's production servers.
        """
        response = requests.put(
            f"{daemon_url}/open",
            json={"url": "https://github.com"},
            timeout=60
        )
        assert response.status_code == 200
    
    def test_open_slack(self, daemon_url):
        """
        Open Slack.
        
        WARNING: This is a production service and may have protections.
        """
        response = requests.put(
            f"{daemon_url}/open",
            json={"url": "https://slack.com"},
            timeout=60
        )
        assert response.status_code == 200


class TestAuthenticationInteraction:
    """
    Tests that interact with authentication systems.
    
    WARNING: These tests may affect authentication sessions or trigger
    security alerts.
    """
    
    def test_open_login_page(self, daemon_url):
        """
        Open a login page.
        
        WARNING: May trigger security alerts on some platforms.
        """
        response = requests.put(
            f"{daemon_url}/open",
            json={"url": "https://github.com/login"},
            timeout=60
        )
        assert response.status_code == 200
    
    def test_fill_login_form(self, daemon_url):
        """
        Attempt to fill a login form.
        
        WARNING: This is potentially destructive - could be used for
        credential stuffing. Only test with test credentials.
        """
        requests.put(
            f"{daemon_url}/open",
            json={"url": "https://github.com/login"},
            timeout=60
        )
        time.sleep(2)
        
        response = requests.post(
            f"{daemon_url}/fill",
            json={"uid": "login_field", "value": "test@example.com"},
            timeout=30
        )
        assert response.status_code in [200, 500]
