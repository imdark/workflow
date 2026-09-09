import os
import json
from typing import Optional, List, Dict, Any
from workflow.config import load_config, get_jira_token_from_keychain


class ConfluenceClient:
    def __init__(self):
        self.config = load_config()
        jira_config = self.config.get("jira", {})
        
        # Get Confluence URL from jira config
        jira_url = jira_config.get("url", "")
        if not jira_url:
            raise ValueError("Jira/Confluence URL not configured")
        
        # Convert jira URL to confluence URL
        # e.g., https://your-domain.atlassian.net -> https://your-domain.atlassian.net/wiki
        self.base_url = jira_url.replace("atlassian.net", "atlassian.net/wiki").rstrip("/")
        
        # Get credentials
        self.email = jira_config.get("email")
        self.token = get_jira_token_from_keychain()
        
        if not self.email or not self.token:
            raise ValueError("Jira credentials not configured")
        
        self.session = self._create_session()
    
    def _create_session(self):
        import requests
        session = requests.Session()
        session.auth = (self.email, self.token)
        session.headers.update({
            "Content-Type": "application/json",
            "Accept": "application/json"
        })
        return session
    
    def _request(self, method: str, endpoint: str, **kwargs) -> Dict:
        # Handle search endpoint differently
        if endpoint.startswith("search"):
            url = f"{self.base_url}/rest/api/{endpoint}"
        else:
            url = f"{self.base_url}/rest/api/{endpoint}"
        response = self.session.request(method, url, **kwargs)
        response.raise_for_status()
        return response.json() if response.content else {}
    
    def search(self, cql: str, limit: int = 25) -> List[Dict]:
        """Search using CQL (Confluence Query Language)"""
        url = f"{self.base_url}/rest/api/search"
        params = {"cql": cql, "limit": limit}
        response = self.session.get(url, params=params)
        response.raise_for_status()
        result = response.json()
        return result.get("results", [])
    
    def search_pages(self, query: str, space_key: Optional[str] = None, limit: int = 25) -> List[Dict]:
        """Search for pages"""
        cql = f"text ~ '{query}' AND type=page"
        if space_key:
            cql += f" AND space.key='{space_key}'"
        return self.search(cql, limit)
    
    def get_page(self, page_id: str) -> Dict:
        """Get page by ID"""
        return self._request("GET", f"content/{page_id}?expand=body.storage,version")
    
    def get_page_by_title(self, space_key: str, title: str) -> Optional[Dict]:
        """Get page by title in a space"""
        cql = f"space.key='{space_key}' AND title='{title}' AND type=page"
        results = self.search(cql, limit=1)
        if results:
            # Search results have nested content.id
            result = results[0]
            content = result.get("content", {})
            if content:
                return content
            return result
        return None
    
    def create_page(self, space_key: str, title: str, content: str = "", parent_id: Optional[str] = None) -> Dict:
        """Create a new page"""
        data = {
            "type": "page",
            "title": title,
            "space": {"key": space_key},
            "body": {
                "storage": {
                    "value": content,
                    "representation": "storage"
                }
            }
        }
        if parent_id:
            data["ancestors"] = [{"id": parent_id}]
        
        return self._request("POST", "content", json=data)
    
    def update_page(self, page_id: str, title: Optional[str] = None, content: Optional[str] = None, minor_edit: bool = False) -> Dict:
        """Update an existing page"""
        # Get current version
        current = self.get_page(page_id)
        version = current.get("version", {}).get("number", 0)
        
        update_data = {
            "id": page_id,
            "type": "page",
            "title": title or current.get("title"),
            "body": {
                "storage": {
                    "value": content or current.get("body", {}).get("storage", {}).get("value", ""),
                    "representation": "storage"
                }
            },
            "version": {
                "number": version + 1,
                "minorEdit": minor_edit
            }
        }
        
        return self._request("PUT", f"content/{page_id}", json=update_data)
    
    def add_comment(self, page_id: str, content: str) -> Dict:
        """Add a comment to a page"""
        data = {
            "type": "comment",
            "body": {
                "storage": {
                    "value": content,
                    "representation": "storage"
                }
            },
            "container": {"id": page_id, "type": "page"}
        }
        
        return self._request("POST", "content", json=data)
    
    def get_page_comments(self, page_id: str, limit: int = 25) -> List[Dict]:
        """Get comments on a page"""
        result = self._request("GET", f"content/{page_id}/child/comment?limit={limit}")
        return result.get("results", [])
    
    def get_page_history(self, page_id: str) -> Dict:
        """Get page history/versions"""
        return self._request("GET", f"content/{page_id}/history")
    
    def get_page_ancestors(self, page_id: str) -> List[Dict]:
        """Get page ancestors (breadcrumb)"""
        page = self.get_page(page_id)
        return page.get("ancestors", [])
    
    def get_child_pages(self, page_id: str, limit: int = 25) -> List[Dict]:
        """Get child pages"""
        result = self._request("GET", f"content/{page_id}/child/page?limit={limit}")
        return result.get("results", [])


_client: Optional[ConfluenceClient] = None


def get_confluence_client() -> ConfluenceClient:
    global _client
    if _client is None:
        _client = ConfluenceClient()
    return _client


def reset_confluence_client():
    global _client
    _client = None
