import json
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, List

try:
    import requests
    REQUESTS_AVAILABLE = True
except ImportError:
    REQUESTS_AVAILABLE = False


SLACK_API_BASE = "https://slack.com/api"


class SlackDumper:
    def __init__(self, cookies: Optional[Dict[str, str]] = None, token: Optional[str] = None):
        self.cookies: Dict[str, str] = cookies if cookies else {}
        self.token = token
        self.session = requests.Session()
        
        if self.cookies:
            self.session.cookies.update(self.cookies)
        
        if token:
            self.session.headers.update({"Authorization": f"Bearer {token}"})

    def _get_d_cookie(self) -> Optional[str]:
        return self.cookies.get("d") or self.session.cookies.get("d")

    def _api_call(self, method: str, **kwargs) -> dict:
        url = f"{SLACK_API_BASE}/{method}"

        headers = kwargs.pop("headers", {})
        d_cookie = self._get_d_cookie()
        if d_cookie:
            headers["Cookie"] = f"d={d_cookie}"

        response = self.session.get(url, headers=headers, params=kwargs, timeout=30)
        data = response.json()
        if not data.get("ok"):
            raise Exception(f"Slack API error: {data.get('error', 'Unknown error')}")
        return data

    def _api_post_call(self, method: str, **kwargs) -> dict:
        url = f"{SLACK_API_BASE}/{method}"

        headers = kwargs.pop("headers", {})
        d_cookie = self._get_d_cookie()
        if d_cookie:
            headers["Cookie"] = f"d={d_cookie}"

        response = self.session.post(url, headers=headers, json=kwargs, timeout=30)
        data = response.json()
        if not data.get("ok"):
            raise Exception(f"Slack API error: {data.get('error', 'Unknown error')}")
        return data

    def get_channels(self) -> list:
        data = self._api_call("conversations.list", types="public_channel,private_channel")
        return data.get("channels", [])

    def get_channel_messages(self, channel_id: str, oldest: Optional[str] = None, 
                             latest: Optional[str] = None, limit: int = 200) -> list:
        messages = []
        cursor = None
        while True:
            params = {
                "channel": channel_id,
                "limit": limit,
                "inclusive": True
            }
            if cursor:
                params["cursor"] = cursor
            if oldest:
                params["oldest"] = oldest
            if latest:
                params["latest"] = latest

            data = self._api_call("conversations.history", **params)
            batch = data.get("messages", [])
            messages.extend(batch)
            
            has_more = data.get("has_more", False)
            cursor = data.get("response_metadata", {}).get("next_cursor")
            
            if not has_more and not cursor:
                break
            time.sleep(0.5)
        return messages

    def get_thread_replies(self, channel_id: str, thread_ts: str) -> list:
        data = self._api_call(
            "conversations.replies",
            channel=channel_id,
            thread_ts=thread_ts,
            limit=200
        )
        return data.get("messages", [])

    def get_message(self, channel_id: str, ts: str) -> Optional[dict]:
        """Fetch a single message by its timestamp."""
        data = self._api_call(
            "conversations.history",
            channel=channel_id,
            latest=ts,
            oldest=ts,
            inclusive=True,
            limit=1,
        )
        messages = data.get("messages", [])
        return messages[0] if messages else None

    def get_permalink(self, channel_id: str, message_ts: str) -> Optional[str]:
        try:
            data = self._api_call(
                "chat.getPermalink",
                channel=channel_id,
                message_ts=message_ts,
            )
            return data.get("permalink")
        except Exception:
            return None

    def post_message(self, channel_id: str, text: str, thread_ts: Optional[str] = None) -> dict:
        """Post a message to a channel, optionally as a threaded reply."""
        kwargs = {"channel": channel_id, "text": text}
        if thread_ts:
            kwargs["thread_ts"] = thread_ts
        return self._api_post_call("chat.postMessage", **kwargs)

    def find_bot_user_id(self, name: str) -> Optional[str]:
        """Look up a bot/app's Slack user id by (partial, case-insensitive) name."""
        needle = name.strip().lower()
        cursor = None
        while True:
            params = {"limit": 200}
            if cursor:
                params["cursor"] = cursor
            data = self._api_call("users.list", **params)
            for user in data.get("members", []):
                if not user.get("is_bot") and not user.get("is_app_user"):
                    continue
                candidates = [
                    user.get("name", ""),
                    user.get("real_name", ""),
                    user.get("profile", {}).get("real_name", ""),
                ]
                if any(needle in c.lower() for c in candidates if c):
                    return user.get("id")
            cursor = data.get("response_metadata", {}).get("next_cursor")
            if not cursor:
                break
        return None

    def get_user_info(self, user_id: str) -> dict:
        try:
            return self._api_call("users.info", user=user_id)
        except Exception:
            return {}

    def get_file_info(self, file_id: str) -> dict:
        try:
            return self._api_call("files.info", file=file_id, limit=200)
        except Exception:
            return {}

    def download_file(self, file_url: str, output_dir: Path, filename: str) -> Optional[Path]:
        try:
            headers = {}
            d_cookie = self._get_d_cookie()
            if d_cookie:
                headers["Cookie"] = f"d={d_cookie}"
            
            response = self.session.get(file_url, headers=headers, timeout=60)
            if response.status_code == 200:
                output_dir.mkdir(parents=True, exist_ok=True)
                filepath = output_dir / filename
                filepath.write_bytes(response.content)
                return filepath
        except Exception:
            pass
        return None

    def dump_channel(self, channel_id: str, output_dir: Path, 
                     include_threads: bool = True, include_files: bool = True,
                     oldest: Optional[str] = None, latest: Optional[str] = None,
                     progress_callback=None) -> dict:
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        try:
            channel_info = self._api_call("conversations.info", channel=channel_id)
            channel_name = channel_info.get("channel", {}).get("name", "unknown")
        except Exception:
            channel_name = channel_id

        export_data = {
            "channel_id": channel_id,
            "channel_name": channel_name,
            "exported_at": datetime.now().isoformat(),
            "messages": [],
            "threads": {},
            "files": []
        }

        if progress_callback:
            progress_callback(f"Fetching messages from #{channel_name}...")

        try:
            messages = self.get_channel_messages(channel_id, oldest, latest)
        except Exception as e:
            if progress_callback:
                progress_callback(f"Error fetching messages: {e}")
            messages = []

        if progress_callback:
            progress_callback(f"Found {len(messages)} messages. Processing...")

        for msg in messages:
            if msg.get("subtype") == "bot_message":
                continue

            msg_data = {
                "ts": msg.get("ts"),
                "user": msg.get("user"),
                "text": msg.get("text", ""),
                "reply_count": msg.get("reply_count", 0),
                "reactions": msg.get("reactions", []),
                "files": msg.get("files", []),
                "thread_ts": msg.get("thread_ts"),
                "type": msg.get("type"),
            }

            if include_threads and msg.get("reply_count", 0) > 0:
                thread_ts = msg.get("ts")
                if progress_callback:
                    progress_callback(f"  Fetching thread {thread_ts} ({msg['reply_count']} replies)...")
                
                try:
                    replies = self.get_thread_replies(channel_id, thread_ts)
                    export_data["threads"][thread_ts] = {
                        "parent": msg_data,
                        "replies": [{
                            "ts": r.get("ts"),
                            "user": r.get("user"),
                            "text": r.get("text", ""),
                            "reactions": r.get("reactions", []),
                            "files": r.get("files", [])
                        } for r in replies if r.get("ts") != thread_ts]
                    }
                except Exception:
                    pass
                time.sleep(0.5)

            if include_files and msg.get("files"):
                for f in msg.get("files", []):
                    file_data = {
                        "id": f.get("id"),
                        "name": f.get("name"),
                        "url": f.get("url_private"),
                        "mime_type": f.get("mimetype"),
                        "message_ts": msg.get("ts")
                    }
                    export_data["files"].append(file_data)

            export_data["messages"].append(msg_data)
            time.sleep(0.1)

        output_file = output_dir / f"{channel_name}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        with open(output_file, "w") as f:
            json.dump(export_data, f, indent=2)

        if include_files and export_data["files"]:
            files_dir = output_dir / f"{channel_name}_files"
            if progress_callback:
                progress_callback(f"Downloading {len(export_data['files'])} files...")
            
            for file_info in export_data["files"]:
                if file_info.get("url"):
                    downloaded = self.download_file(
                        file_info["url"], 
                        files_dir, 
                        file_info.get("name", file_info.get("id", "unknown"))
                    )
                    if downloaded:
                        file_info["local_path"] = str(downloaded)
                        if progress_callback:
                            progress_callback(f"  Downloaded: {file_info['name']}")
                    time.sleep(0.5)

        if progress_callback:
            progress_callback(f"Export complete: {output_file}")

        return export_data

    def dump_all_channels(self, output_dir: Path, channel_filter: Optional[list] = None,
                          include_threads: bool = True, include_files: bool = True,
                          oldest: Optional[str] = None, latest: Optional[str] = None,
                          progress_callback=None) -> list:
        channels = self.get_channels()
        results = []

        for channel in channels:
            if channel_filter and channel["name"] not in channel_filter:
                continue

            if progress_callback:
                progress_callback(f"\n{'='*50}")
                progress_callback(f"Processing: #{channel['name']}")

            try:
                result = self.dump_channel(
                    channel["id"], 
                    output_dir, 
                    include_threads,
                    include_files,
                    oldest,
                    latest,
                    progress_callback
                )
                results.append(result)
            except Exception as e:
                if progress_callback:
                    progress_callback(f"Error: {e}")

        return results


def get_browser_cookies() -> Dict[str, str]:
    import urllib.request
    
    try:
        req = urllib.request.Request(
            "http://127.0.0.1:18765/cookies",
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=30) as response:
            result = json.loads(response.read().decode())
            all_cookies = result.get("cookies", [])
            
            slack_cookies = {
                c["name"]: c["value"] 
                for c in all_cookies 
                if "slack" in c.get("domain", "").lower()
            }
            return slack_cookies
    except Exception:
        return {}


def save_slack_cookies(cookies: Dict[str, str]) -> bool:
    from .config import load_config, save_config
    cfg = load_config()
    if "slack" not in cfg:
        cfg["slack"] = {}
    cfg["slack"]["cookies"] = json.dumps(cookies)
    save_config(cfg)
    return True


def get_slack_cookies() -> Dict[str, str]:
    from .config import load_config
    cfg = load_config()
    slack_cfg = cfg.get("slack", {})
    cookies_str = slack_cfg.get("cookies", "{}")
    if cookies_str:
        try:
            return json.loads(cookies_str)
        except Exception:
            pass
    return {}


def set_slack_token(token: str) -> bool:
    from .config import load_config, save_config
    cfg = load_config()
    if "slack" not in cfg:
        cfg["slack"] = {}
    cfg["slack"]["api_token"] = token
    save_config(cfg)
    return True


def get_slack_token() -> Optional[str]:
    from .config import load_config
    cfg = load_config()
    slack_cfg = cfg.get("slack", {})
    token = slack_cfg.get("api_token")
    if not token:
        token = os.environ.get("SLACK_API_TOKEN")
    return token


def dump_to_keychain(token: str) -> bool:
    try:
        import keyring
        keyring.set_password("workflow.slack", "api_token", token)
        return True
    except Exception:
        return False


def get_token_from_keychain() -> Optional[str]:
    try:
        import keyring
        return keyring.get_password("workflow.slack", "api_token")
    except Exception:
        return None
