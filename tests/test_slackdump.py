import pytest
from unittest.mock import Mock, patch
from pathlib import Path
import tempfile

from workflow.slackdump import SlackDumper, set_slack_token, get_slack_token


class TestSlackDumper:
    def test_init(self):
        dumper = SlackDumper("xoxp-test-token")
        assert dumper.token == "xoxp-test-token"
        assert "Bearer xoxp-test-token" in dumper.headers["Authorization"]

    @patch('workflow.slackdump.requests')
    def test_api_call_success(self, mock_requests):
        mock_response = Mock()
        mock_response.json.return_value = {"ok": True, "channels": []}
        mock_requests.get.return_value = mock_response

        dumper = SlackDumper("xoxp-test")
        result = dumper._api_call("conversations.list")

        assert result == {"ok": True, "channels": []}
        mock_requests.get.assert_called_once()

    @patch('workflow.slackdump.requests')
    def test_api_call_error(self, mock_requests):
        mock_response = Mock()
        mock_response.json.return_value = {"ok": False, "error": "invalid_auth"}
        mock_requests.get.return_value = mock_response

        dumper = SlackDumper("xoxp-invalid")
        
        with pytest.raises(Exception) as exc_info:
            dumper._api_call("conversations.list")
        
        assert "invalid_auth" in str(exc_info.value)

    @patch('workflow.slackdump.requests')
    def test_get_channels(self, mock_requests):
        mock_response = Mock()
        mock_response.json.return_value = {
            "ok": True,
            "channels": [
                {"id": "C001", "name": "general"},
                {"id": "C002", "name": "random", "is_private": True}
            ]
        }
        mock_requests.get.return_value = mock_response

        dumper = SlackDumper("xoxp-test")
        channels = dumper.get_channels()

        assert len(channels) == 2
        assert channels[0]["name"] == "general"
        assert channels[1]["is_private"]

    @patch('workflow.slackdump.requests')
    def test_get_channel_messages(self, mock_requests):
        mock_response = Mock()
        mock_response.json.return_value = {
            "ok": True,
            "messages": [
                {"ts": "1234567890.123456", "text": "Hello", "user": "U001"},
                {"ts": "1234567891.123456", "text": "World", "user": "U002"}
            ],
            "has_more": False
        }
        mock_requests.get.return_value = mock_response

        dumper = SlackDumper("xoxp-test")
        messages = dumper.get_channel_messages("C001")

        assert len(messages) == 2
        assert messages[0]["text"] == "Hello"

    @patch('workflow.slackdump.requests')
    def test_get_thread_replies(self, mock_requests):
        mock_response = Mock()
        mock_response.json.return_value = {
            "ok": True,
            "messages": [
                {"ts": "1234567890.123456", "text": "Parent", "user": "U001"},
                {"ts": "1234567891.123456", "text": "Reply 1", "user": "U002"},
                {"ts": "1234567892.123456", "text": "Reply 2", "user": "U003"}
            ]
        }
        mock_requests.get.return_value = mock_response

        dumper = SlackDumper("xoxp-test")
        replies = dumper.get_thread_replies("C001", "1234567890.123456")

        assert len(replies) == 3

    @patch('workflow.slackdump.requests')
    def test_dump_channel_basic(self, mock_requests):
        channel_info_response = Mock()
        channel_info_response.json.return_value = {
            "ok": True,
            "channel": {"id": "C001", "name": "test-channel"}
        }

        messages_response = Mock()
        messages_response.json.return_value = {
            "ok": True,
            "messages": [
                {"ts": "1234567890.123456", "text": "Test message", "user": "U001", "reply_count": 0}
            ],
            "has_more": False
        }

        mock_requests.get.side_effect = [channel_info_response, messages_response]

        dumper = SlackDumper("xoxp-test")
        
        with tempfile.TemporaryDirectory() as tmpdir:
            result = dumper.dump_channel(
                "C001",
                Path(tmpdir),
                include_threads=False,
                include_files=False,
                progress_callback=None
            )

            assert result["channel_id"] == "C001"
            assert result["channel_name"] == "test-channel"
            assert len(result["messages"]) == 1

            output_files = list(Path(tmpdir).glob("*.json"))
            assert len(output_files) == 1

    @patch('workflow.slackdump.requests')
    def test_dump_channel_with_threads(self, mock_requests):
        channel_info_response = Mock()
        channel_info_response.json.return_value = {
            "ok": True,
            "channel": {"id": "C001", "name": "test-channel"}
        }

        messages_response = Mock()
        messages_response.json.return_value = {
            "ok": True,
            "messages": [
                {"ts": "1234567890.123456", "text": "Thread parent", "user": "U001", "reply_count": 2}
            ],
            "has_more": False
        }

        thread_response = Mock()
        thread_response.json.return_value = {
            "ok": True,
            "messages": [
                {"ts": "1234567890.123456", "text": "Thread parent", "user": "U001"},
                {"ts": "1234567891.123456", "text": "Reply 1", "user": "U002"},
                {"ts": "1234567892.123456", "text": "Reply 2", "user": "U003"}
            ]
        }

        mock_requests.get.side_effect = [channel_info_response, messages_response, thread_response]

        dumper = SlackDumper("xoxp-test")
        
        with tempfile.TemporaryDirectory() as tmpdir:
            result = dumper.dump_channel(
                "C001",
                Path(tmpdir),
                include_threads=True,
                include_files=False,
                progress_callback=None
            )

            assert len(result["threads"]) == 1
            thread_ts = list(result["threads"].keys())[0]
            assert len(result["threads"][thread_ts]["replies"]) == 2

    @patch('workflow.slackdump.requests')
    def test_get_user_info(self, mock_requests):
        mock_response = Mock()
        mock_response.json.return_value = {
            "ok": True,
            "user": {"id": "U001", "name": "testuser"}
        }
        mock_requests.get.return_value = mock_response

        dumper = SlackDumper("xoxp-test")
        user_info = dumper.get_user_info("U001")

        assert user_info["user"]["name"] == "testuser"

    @patch('workflow.slackdump.requests')
    def test_download_file(self, mock_requests):
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.content = b"fake file content"
        mock_requests.get.return_value = mock_response

        dumper = SlackDumper("xoxp-test")
        
        with tempfile.TemporaryDirectory() as tmpdir:
            result = dumper.download_file(
                "https://example.com/file.txt",
                Path(tmpdir),
                "test_file.txt"
            )

            assert result is not None
            assert result.name == "test_file.txt"
            assert result.read_bytes() == b"fake file content"

    @patch('workflow.slackdump.requests')
    def test_download_file_error(self, mock_requests):
        mock_requests.get.side_effect = Exception("Network error")

        dumper = SlackDumper("xoxp-test")
        
        with tempfile.TemporaryDirectory() as tmpdir:
            result = dumper.download_file(
                "https://example.com/file.txt",
                Path(tmpdir),
                "test_file.txt"
            )

            assert result is None


class TestSlackDumperConfig:
    @patch('workflow.config.load_config')
    @patch('workflow.config.save_config')
    def test_set_slack_token(self, mock_save, mock_load):
        mock_load.return_value = {"slack": {}}
        
        result = set_slack_token("xoxp-new-token")
        
        assert result
        mock_save.assert_called_once()

    @patch('workflow.config.load_config')
    def test_get_slack_token_from_config(self, mock_load):
        mock_load.return_value = {"slack": {"api_token": "xoxp-config-token"}}
        
        token = get_slack_token()
        
        assert token == "xoxp-config-token"

    @patch.dict('os.environ', {'SLACK_API_TOKEN': 'xoxp-env-token'})
    @patch('workflow.config.load_config')
    def test_get_slack_token_from_env(self, mock_load):
        mock_load.return_value = {"slack": {}}
        
        token = get_slack_token()
        
        assert token == "xoxp-env-token"
