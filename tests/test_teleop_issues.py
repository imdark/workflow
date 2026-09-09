from unittest.mock import Mock

from workflow.teleop_issues import (
    is_teleop_new_issue,
    find_new_issue_messages,
    parse_new_issue,
    build_linear_mention_comment,
    resolve_channel_id,
    thread_has_linear_conversion,
)


class TestIsTeleopNewIssue:
    def test_matches_username_and_prefix(self):
        msg = {"username": "Tele-op Issue", "text": "NEW ISSUE\nServo chain preflight failed"}
        assert is_teleop_new_issue(msg)

    def test_matches_bot_profile_name(self):
        msg = {"bot_profile": {"name": "Tele-op Issue"}, "text": "new issue\nsomething broke"}
        assert is_teleop_new_issue(msg)

    def test_rejects_wrong_workflow(self):
        msg = {"username": "Some Other Bot", "text": "NEW ISSUE\nfoo"}
        assert not is_teleop_new_issue(msg)

    def test_rejects_missing_prefix(self):
        msg = {"username": "Tele-op Issue", "text": "just a regular update"}
        assert not is_teleop_new_issue(msg)

    def test_rejects_human_message(self):
        msg = {"user": "U123", "text": "NEW ISSUE something"}
        assert not is_teleop_new_issue(msg)


class TestThreadHasLinearConversion:
    def test_detects_reply_from_linear_bot(self):
        dumper = Mock()
        dumper.get_thread_replies.return_value = [
            {"ts": "1.1", "username": "Tele-op Issue", "text": "NEW ISSUE\nfoo"},
            {"ts": "1.2", "username": "Linear", "text": "Created PAN-24"},
        ]
        assert thread_has_linear_conversion(dumper, "C1", "1.1")

    def test_detects_linear_app_link_in_reply(self):
        dumper = Mock()
        dumper.get_thread_replies.return_value = [
            {"ts": "1.1", "username": "Tele-op Issue", "text": "NEW ISSUE\nfoo"},
            {"ts": "1.2", "user": "U1", "text": "done: https://linear.app/pantheon-industries/issue/PAN-24"},
        ]
        assert thread_has_linear_conversion(dumper, "C1", "1.1")

    def test_no_conversion_when_no_matching_reply(self):
        dumper = Mock()
        dumper.get_thread_replies.return_value = [
            {"ts": "1.1", "username": "Tele-op Issue", "text": "NEW ISSUE\nfoo"},
            {"ts": "1.2", "user": "U1", "text": "looking into it"},
        ]
        assert not thread_has_linear_conversion(dumper, "C1", "1.1")

    def test_swallows_errors(self):
        dumper = Mock()
        dumper.get_thread_replies.side_effect = Exception("boom")
        assert not thread_has_linear_conversion(dumper, "C1", "1.1")


class TestFindNewIssueMessages:
    def test_filters_and_attaches_permalinks(self):
        dumper = Mock()
        dumper.get_channel_messages.return_value = [
            {"ts": "1.1", "username": "Tele-op Issue", "text": "NEW ISSUE\nServo chain preflight failed"},
            {"ts": "1.2", "user": "U1", "text": "unrelated chat"},
            {"ts": "1.3", "username": "Tele-op Issue", "text": "status update, not an issue"},
        ]
        dumper.get_permalink.return_value = "https://example.slack.com/archives/C1/p11"
        dumper.get_thread_replies.return_value = []

        found = find_new_issue_messages(dumper, "C1")

        assert len(found) == 1
        assert found[0].ts == "1.1"
        assert found[0].permalink == "https://example.slack.com/archives/C1/p11"
        assert found[0].already_converted is False
        dumper.get_channel_messages.assert_called_once_with("C1", oldest=None, latest=None)

    def test_skips_permalink_lookup_when_disabled(self):
        dumper = Mock()
        dumper.get_channel_messages.return_value = [
            {"ts": "1.1", "username": "Tele-op Issue", "text": "NEW ISSUE\nfoo"},
        ]
        dumper.get_thread_replies.return_value = []

        found = find_new_issue_messages(dumper, "C1", with_permalinks=False)

        assert found[0].permalink is None
        dumper.get_permalink.assert_not_called()

    def test_skips_conversion_check_when_disabled(self):
        dumper = Mock()
        dumper.get_channel_messages.return_value = [
            {"ts": "1.1", "username": "Tele-op Issue", "text": "NEW ISSUE\nfoo"},
        ]

        found = find_new_issue_messages(dumper, "C1", check_converted=False)

        assert found[0].already_converted is None
        dumper.get_thread_replies.assert_not_called()

    def test_flags_already_converted_message(self):
        dumper = Mock()
        dumper.get_channel_messages.return_value = [
            {"ts": "1.1", "username": "Tele-op Issue", "text": "NEW ISSUE\nfoo"},
        ]
        dumper.get_thread_replies.return_value = [
            {"ts": "1.2", "username": "Linear", "text": "Created PAN-24"},
        ]

        found = find_new_issue_messages(dumper, "C1")

        assert found[0].already_converted is True


class TestParseNewIssue:
    def test_splits_title_and_description(self):
        text = "NEW ISSUE\nServo chain preflight failed\nReported by: mike\nReproduce: run a teleop session"
        result = parse_new_issue(text)
        assert result["title"] == "Servo chain preflight failed"
        assert result["description_lines"] == ["Reported by: mike", "Reproduce: run a teleop session"]

    def test_title_only(self):
        result = parse_new_issue("NEW ISSUE\nJust a title")
        assert result["title"] == "Just a title"
        assert result["description_lines"] == []

    def test_no_prefix_falls_back_to_first_line(self):
        result = parse_new_issue("Servo chain preflight failed")
        assert result["title"] == "Servo chain preflight failed"


class TestBuildLinearMentionComment:
    def test_builds_expected_format(self):
        comment = build_linear_mention_comment(
            team="PAN",
            project="Teleop",
            title="Servo chain preflight failed",
            description_lines=[
                "Reported in #teleop",
                "@mike Servo chain preflight failed",
                "Reproduce: run a teleop session",
                "Source: https://pantheon-kmx3690.slack.com/archives/C0BJ2PD955H/p1786480502903879",
            ],
            labels=["bug", "teleop"],
            priority="High",
            status="Triage",
        )

        assert comment.startswith("@Linear create an issue in the PAN team and add it to the Teleop project.")
        assert "Title: Servo chain preflight failed" in comment
        assert "- Reported in #teleop" in comment
        assert "Labels: bug, teleop" in comment
        assert "Priority: High" in comment
        assert "Status: Triage" in comment

    def test_uses_custom_mention(self):
        comment = build_linear_mention_comment(
            team="PAN",
            project="Teleop",
            title="t",
            description_lines=[],
            labels=["bug"],
            priority="High",
            status="Triage",
            linear_mention="<@U0LINEARBOT>",
        )
        assert comment.startswith("<@U0LINEARBOT> create an issue")


class TestResolveChannelId:
    def test_passes_through_channel_id(self):
        dumper = Mock()
        assert resolve_channel_id(dumper, "C0BJ2PD955H") == "C0BJ2PD955H"
        dumper.get_channels.assert_not_called()

    def test_resolves_name_to_id(self):
        dumper = Mock()
        dumper.get_channels.return_value = [{"id": "C0BJ2PD955H", "name": "teleop"}]
        assert resolve_channel_id(dumper, "#teleop") == "C0BJ2PD955H"

    def test_raises_when_not_found(self):
        dumper = Mock()
        dumper.get_channels.return_value = [{"id": "C1", "name": "general"}]
        try:
            resolve_channel_id(dumper, "teleop")
            assert False, "expected ValueError"
        except ValueError:
            pass
