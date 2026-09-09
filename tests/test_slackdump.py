"""Tests for SlackDumper.

SlackDumper authenticates with browser cookies and/or a token over a
requests.Session (see workflow/cli.py, which builds it as
`SlackDumper(cookies=cookies, token=token)`). These tests stub that
session's get/post rather than the requests module, so no test touches the
network.
"""

import tempfile
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from workflow.slackdump import SlackDumper, set_slack_token, get_slack_token


def json_response(payload, status_code=200):
    """A stand-in for a requests Response carrying a JSON body."""
    response = Mock()
    response.status_code = status_code
    response.json.return_value = payload
    return response


def make_dumper(*get_responses, post_responses=(), cookies=None, token="xoxp-test"):
    """A SlackDumper whose session replays canned responses in order."""
    dumper = SlackDumper(cookies=cookies, token=token)

    if len(get_responses) == 1:
        dumper.session.get = Mock(return_value=get_responses[0])
    else:
        dumper.session.get = Mock(side_effect=list(get_responses))

    if post_responses:
        dumper.session.post = Mock(side_effect=list(post_responses))
    return dumper


@pytest.fixture(autouse=True)
def no_sleeping():
    """dump_channel paces itself between API calls; don't wait in tests."""
    with patch("workflow.slackdump.time.sleep"):
        yield


class TestAuth:
    def test_token_becomes_a_bearer_header_on_the_session(self):
        dumper = SlackDumper(token="xoxp-test-token")
        assert dumper.token == "xoxp-test-token"
        assert dumper.session.headers["Authorization"] == "Bearer xoxp-test-token"

    def test_cookies_are_loaded_into_the_session_jar(self):
        dumper = SlackDumper(cookies={"d": "xoxd-abc"})
        assert dumper.session.cookies.get("d") == "xoxd-abc"

    def test_works_with_neither_cookies_nor_token(self):
        dumper = SlackDumper()
        assert dumper.cookies == {}
        assert dumper.token is None
        assert "Authorization" not in dumper.session.headers

    def test_d_cookie_prefers_the_explicit_dict(self):
        dumper = SlackDumper(cookies={"d": "from-dict"})
        dumper.session.cookies.set("d", "from-jar")
        assert dumper._get_d_cookie() == "from-dict"

    def test_d_cookie_falls_back_to_the_session_jar(self):
        # This is the path the browser-cookie flow uses when cookies were
        # loaded into the session rather than passed in.
        dumper = SlackDumper(token="xoxp-test")
        dumper.session.cookies.set("d", "from-jar")
        assert dumper._get_d_cookie() == "from-jar"

    def test_no_d_cookie_at_all(self):
        assert SlackDumper(token="xoxp-test")._get_d_cookie() is None


class TestApiCall:
    def test_returns_the_payload(self):
        dumper = make_dumper(json_response({"ok": True, "channels": []}))
        assert dumper._api_call("conversations.list") == {"ok": True, "channels": []}
        dumper.session.get.assert_called_once()

    def test_sends_the_method_as_the_url_and_kwargs_as_params(self):
        dumper = make_dumper(json_response({"ok": True}))
        dumper._api_call("conversations.history", channel="C001", limit=5)
        url, kwargs = dumper.session.get.call_args[0][0], dumper.session.get.call_args[1]
        assert url == "https://slack.com/api/conversations.history"
        assert kwargs["params"] == {"channel": "C001", "limit": 5}

    def test_attaches_the_d_cookie_as_a_header(self):
        dumper = make_dumper(json_response({"ok": True}), cookies={"d": "xoxd-abc"})
        dumper._api_call("conversations.list")
        assert dumper.session.get.call_args[1]["headers"]["Cookie"] == "d=xoxd-abc"

    def test_omits_the_cookie_header_when_there_is_no_d_cookie(self):
        dumper = make_dumper(json_response({"ok": True}))
        dumper._api_call("conversations.list")
        assert "Cookie" not in dumper.session.get.call_args[1]["headers"]

    def test_a_not_ok_response_raises_with_slacks_error(self):
        dumper = make_dumper(json_response({"ok": False, "error": "invalid_auth"}))
        with pytest.raises(Exception, match="invalid_auth"):
            dumper._api_call("conversations.list")

    def test_a_not_ok_response_without_an_error_still_raises(self):
        dumper = make_dumper(json_response({"ok": False}))
        with pytest.raises(Exception, match="Unknown error"):
            dumper._api_call("conversations.list")


class TestApiPostCall:
    def test_posts_kwargs_as_json(self):
        dumper = SlackDumper(token="xoxp-test")
        dumper.session.post = Mock(return_value=json_response({"ok": True, "ts": "1.2"}))
        result = dumper._api_post_call("chat.postMessage", channel="C001", text="hi")
        assert result["ts"] == "1.2"
        assert dumper.session.post.call_args[1]["json"] == {"channel": "C001", "text": "hi"}

    def test_post_message_threads_a_reply(self):
        dumper = SlackDumper(token="xoxp-test")
        dumper.session.post = Mock(return_value=json_response({"ok": True}))
        dumper.post_message("C001", "reply", thread_ts="1234567890.123456")
        assert dumper.session.post.call_args[1]["json"]["thread_ts"] == "1234567890.123456"

    def test_post_message_without_a_thread_omits_thread_ts(self):
        dumper = SlackDumper(token="xoxp-test")
        dumper.session.post = Mock(return_value=json_response({"ok": True}))
        dumper.post_message("C001", "top level")
        assert "thread_ts" not in dumper.session.post.call_args[1]["json"]

    def test_a_not_ok_post_raises(self):
        dumper = SlackDumper(token="xoxp-test")
        dumper.session.post = Mock(return_value=json_response({"ok": False, "error": "not_in_channel"}))
        with pytest.raises(Exception, match="not_in_channel"):
            dumper.post_message("C001", "hi")


class TestReads:
    def test_get_channels(self):
        dumper = make_dumper(json_response({
            "ok": True,
            "channels": [
                {"id": "C001", "name": "general"},
                {"id": "C002", "name": "random", "is_private": True},
            ],
        }))
        channels = dumper.get_channels()
        assert len(channels) == 2
        assert channels[0]["name"] == "general"
        assert channels[1]["is_private"]

    def test_get_channels_on_an_empty_workspace(self):
        dumper = make_dumper(json_response({"ok": True}))
        assert dumper.get_channels() == []

    def test_get_channel_messages(self):
        dumper = make_dumper(json_response({
            "ok": True,
            "messages": [
                {"ts": "1234567890.123456", "text": "Hello", "user": "U001"},
                {"ts": "1234567891.123456", "text": "World", "user": "U002"},
            ],
            "has_more": False,
        }))
        messages = dumper.get_channel_messages("C001")
        assert [m["text"] for m in messages] == ["Hello", "World"]

    def test_get_channel_messages_follows_the_cursor(self):
        page1 = json_response({
            "ok": True,
            "messages": [{"ts": "1.1", "text": "page 1"}],
            "has_more": True,
            "response_metadata": {"next_cursor": "cur-2"},
        })
        page2 = json_response({
            "ok": True,
            "messages": [{"ts": "2.2", "text": "page 2"}],
            "has_more": False,
        })
        dumper = make_dumper(page1, page2)
        messages = dumper.get_channel_messages("C001")
        assert [m["text"] for m in messages] == ["page 1", "page 2"]
        assert dumper.session.get.call_args_list[1][1]["params"]["cursor"] == "cur-2"

    def test_get_channel_messages_passes_the_time_window(self):
        dumper = make_dumper(json_response({"ok": True, "messages": [], "has_more": False}))
        dumper.get_channel_messages("C001", oldest="100.0", latest="200.0")
        params = dumper.session.get.call_args[1]["params"]
        assert params["oldest"] == "100.0"
        assert params["latest"] == "200.0"

    def test_get_thread_replies(self):
        dumper = make_dumper(json_response({
            "ok": True,
            "messages": [
                {"ts": "1234567890.123456", "text": "Parent", "user": "U001"},
                {"ts": "1234567891.123456", "text": "Reply 1", "user": "U002"},
                {"ts": "1234567892.123456", "text": "Reply 2", "user": "U003"},
            ],
        }))
        assert len(dumper.get_thread_replies("C001", "1234567890.123456")) == 3

    def test_get_message_returns_the_single_hit(self):
        dumper = make_dumper(json_response({"ok": True, "messages": [{"ts": "1.1", "text": "one"}]}))
        assert dumper.get_message("C001", "1.1")["text"] == "one"

    def test_get_message_returns_none_when_nothing_matches(self):
        dumper = make_dumper(json_response({"ok": True, "messages": []}))
        assert dumper.get_message("C001", "1.1") is None

    def test_get_permalink(self):
        dumper = make_dumper(json_response({"ok": True, "permalink": "https://slack.com/archives/C001/p1"}))
        assert dumper.get_permalink("C001", "1.1") == "https://slack.com/archives/C001/p1"

    def test_get_permalink_swallows_errors(self):
        # Callers treat a missing permalink as "no link", not a failure.
        dumper = make_dumper(json_response({"ok": False, "error": "message_not_found"}))
        assert dumper.get_permalink("C001", "1.1") is None

    def test_get_user_info(self):
        dumper = make_dumper(json_response({"ok": True, "user": {"id": "U001", "name": "testuser"}}))
        assert dumper.get_user_info("U001")["user"]["name"] == "testuser"

    def test_get_user_info_returns_empty_on_error(self):
        dumper = make_dumper(json_response({"ok": False, "error": "user_not_found"}))
        assert dumper.get_user_info("U404") == {}

    def test_get_file_info_returns_empty_on_error(self):
        dumper = make_dumper(json_response({"ok": False, "error": "file_not_found"}))
        assert dumper.get_file_info("F404") == {}


class TestFindBotUserId:
    def _members(self, members, next_cursor=None):
        payload = {"ok": True, "members": members}
        if next_cursor:
            payload["response_metadata"] = {"next_cursor": next_cursor}
        return json_response(payload)

    def test_matches_a_bot_by_partial_name(self):
        dumper = make_dumper(self._members([
            {"id": "U001", "name": "someone", "is_bot": False},
            {"id": "B001", "name": "linear", "is_bot": True},
        ]))
        assert dumper.find_bot_user_id("Linear") == "B001"

    def test_matches_on_real_name_too(self):
        dumper = make_dumper(self._members([
            {"id": "B001", "name": "lnr", "real_name": "Linear App", "is_bot": True},
        ]))
        assert dumper.find_bot_user_id("linear") == "B001"

    def test_ignores_human_users_with_a_matching_name(self):
        dumper = make_dumper(self._members([
            {"id": "U001", "name": "linear-fan", "is_bot": False},
        ]))
        assert dumper.find_bot_user_id("linear") is None

    def test_pages_through_the_member_list(self):
        dumper = make_dumper(
            self._members([{"id": "U001", "name": "someone", "is_bot": False}], next_cursor="cur-2"),
            self._members([{"id": "B001", "name": "linear", "is_bot": True}]),
        )
        assert dumper.find_bot_user_id("linear") == "B001"


class TestDownloadFile:
    def test_writes_the_body_to_disk(self):
        response = Mock()
        response.status_code = 200
        response.content = b"fake file content"
        dumper = make_dumper(response)

        with tempfile.TemporaryDirectory() as tmpdir:
            result = dumper.download_file("https://example.com/file.txt", Path(tmpdir), "test_file.txt")
            assert result is not None
            assert result.name == "test_file.txt"
            assert result.read_bytes() == b"fake file content"

    def test_sends_the_d_cookie_for_private_files(self):
        response = Mock()
        response.status_code = 200
        response.content = b"x"
        dumper = make_dumper(response, cookies={"d": "xoxd-abc"})

        with tempfile.TemporaryDirectory() as tmpdir:
            dumper.download_file("https://files.slack.com/private", Path(tmpdir), "f.bin")
        assert dumper.session.get.call_args[1]["headers"]["Cookie"] == "d=xoxd-abc"

    def test_returns_none_on_a_non_200(self):
        response = Mock()
        response.status_code = 403
        dumper = make_dumper(response)

        with tempfile.TemporaryDirectory() as tmpdir:
            assert dumper.download_file("https://example.com/f.txt", Path(tmpdir), "f.txt") is None

    def test_returns_none_on_a_transport_error(self):
        dumper = SlackDumper(token="xoxp-test")
        dumper.session.get = Mock(side_effect=Exception("Network error"))

        with tempfile.TemporaryDirectory() as tmpdir:
            assert dumper.download_file("https://example.com/f.txt", Path(tmpdir), "f.txt") is None


class TestDumpChannel:
    CHANNEL_INFO = {"ok": True, "channel": {"id": "C001", "name": "test-channel"}}

    def test_writes_one_json_export(self):
        dumper = make_dumper(
            json_response(self.CHANNEL_INFO),
            json_response({
                "ok": True,
                "messages": [{"ts": "1234567890.123456", "text": "Test message",
                              "user": "U001", "reply_count": 0}],
                "has_more": False,
            }),
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            result = dumper.dump_channel("C001", Path(tmpdir), include_threads=False,
                                         include_files=False, progress_callback=None)
            assert result["channel_id"] == "C001"
            assert result["channel_name"] == "test-channel"
            assert len(result["messages"]) == 1
            assert len(list(Path(tmpdir).glob("*.json"))) == 1

    def test_collects_thread_replies_excluding_the_parent(self):
        dumper = make_dumper(
            json_response(self.CHANNEL_INFO),
            json_response({
                "ok": True,
                "messages": [{"ts": "1234567890.123456", "text": "Thread parent",
                              "user": "U001", "reply_count": 2}],
                "has_more": False,
            }),
            json_response({
                "ok": True,
                "messages": [
                    {"ts": "1234567890.123456", "text": "Thread parent", "user": "U001"},
                    {"ts": "1234567891.123456", "text": "Reply 1", "user": "U002"},
                    {"ts": "1234567892.123456", "text": "Reply 2", "user": "U003"},
                ],
            }),
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            result = dumper.dump_channel("C001", Path(tmpdir), include_threads=True,
                                         include_files=False, progress_callback=None)
            assert len(result["threads"]) == 1
            thread_ts = next(iter(result["threads"]))
            assert len(result["threads"][thread_ts]["replies"]) == 2

    def test_skips_threads_when_not_requested(self):
        dumper = make_dumper(
            json_response(self.CHANNEL_INFO),
            json_response({
                "ok": True,
                "messages": [{"ts": "1.1", "text": "parent", "user": "U001", "reply_count": 2}],
                "has_more": False,
            }),
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            result = dumper.dump_channel("C001", Path(tmpdir), include_threads=False,
                                         include_files=False, progress_callback=None)
            assert result["threads"] == {}

    def test_bot_messages_are_dropped(self):
        dumper = make_dumper(
            json_response(self.CHANNEL_INFO),
            json_response({
                "ok": True,
                "messages": [
                    {"ts": "1.1", "text": "from a bot", "subtype": "bot_message"},
                    {"ts": "2.2", "text": "from a person", "user": "U001"},
                ],
                "has_more": False,
            }),
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            result = dumper.dump_channel("C001", Path(tmpdir), include_threads=False,
                                         include_files=False, progress_callback=None)
            assert [m["text"] for m in result["messages"]] == ["from a person"]

    def test_falls_back_to_the_channel_id_when_info_fails(self):
        dumper = make_dumper(
            json_response({"ok": False, "error": "channel_not_found"}),
            json_response({"ok": True, "messages": [], "has_more": False}),
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            result = dumper.dump_channel("C001", Path(tmpdir), include_threads=False,
                                         include_files=False, progress_callback=None)
            assert result["channel_name"] == "C001"

    def test_reports_progress(self):
        dumper = make_dumper(
            json_response(self.CHANNEL_INFO),
            json_response({"ok": True, "messages": [], "has_more": False}),
        )
        messages = []

        with tempfile.TemporaryDirectory() as tmpdir:
            dumper.dump_channel("C001", Path(tmpdir), include_threads=False,
                                include_files=False, progress_callback=messages.append)
        assert any("test-channel" in m for m in messages)
        assert any("Export complete" in m for m in messages)


class TestSlackDumperConfig:
    @patch('workflow.config.load_config')
    @patch('workflow.config.save_config')
    def test_set_slack_token(self, mock_save, mock_load):
        mock_load.return_value = {"slack": {}}
        assert set_slack_token("xoxp-new-token")
        mock_save.assert_called_once()

    @patch('workflow.config.load_config')
    def test_get_slack_token_from_config(self, mock_load):
        mock_load.return_value = {"slack": {"api_token": "xoxp-config-token"}}
        assert get_slack_token() == "xoxp-config-token"

    @patch.dict('os.environ', {'SLACK_API_TOKEN': 'xoxp-env-token'})
    @patch('workflow.config.load_config')
    def test_get_slack_token_from_env(self, mock_load):
        mock_load.return_value = {"slack": {}}
        assert get_slack_token() == "xoxp-env-token"

    @patch('workflow.config.load_config')
    def test_config_token_wins_over_the_environment(self, mock_load):
        mock_load.return_value = {"slack": {"api_token": "xoxp-config-token"}}
        with patch.dict('os.environ', {'SLACK_API_TOKEN': 'xoxp-env-token'}):
            assert get_slack_token() == "xoxp-config-token"
