from workflow.teleop_linear_sync import parse_search_results_page, build_linear_mention_body


def _search_item_html(ts, when, sender, issue_text, replies=None, checkmark=False):
    reply_html = ""
    if replies:
        reply_html = f'<button data-qa="reply_bar_count" type="button">{replies}</button>'
    reaction_html = ""
    if checkmark:
        reaction_html = (
            '<div data-qa="search-reactions"><img alt=":white_check_mark:" '
            'data-qa="emoji" class="c-emoji"></div>'
        )
    return f'''
    <div class="c-search_message">
      <span class="c-message__sender" data-stringify-type="replace" data-stringify-text="{sender}">
        <span class="p-member_profile_hover_card" role="presentation">
          <button data-qa="message_sender_name">{sender}</button>
        </span>
      </span>
      <a aria-label="{when}" data-stringify-type="replace" data-ts="{ts}" class="c-link c-timestamp c-timestamp--static"
         href="https://example.slack.com/archives/C1/p{ts.replace('.', '')}">
        <span class="c-timestamp__label">{when}</span>
      </a>
      <div class="c-message__message_blocks" data-qa="message-text">
        <span class="c-mrkdwn__highlight">NEW</span> <span class="c-mrkdwn__highlight">ISSUE</span>
        <b data-stringify-type="bold"><i data-stringify-type="italic">Issue</i></b>: {issue_text}
      </div>
      {reaction_html}
      {reply_html}
    </div>
    '''


class TestParseSearchResultsPage:
    def test_extracts_genuine_workflow_post(self):
        html = _search_item_html("1786480502.903879", "Aug 11th at 1:35 PM", "Tele-op Issue",
                                  "Servo chain preflight failed", replies="3 replies", checkmark=True)
        results = parse_search_results_page(html)
        assert len(results) == 1
        r = results[0]
        assert r.ts == "1786480502.903879"
        assert r.title == "Servo chain preflight failed"
        assert r.already_converted is True
        assert r.replies == "3 replies"

    def test_filters_out_non_workflow_sender(self):
        html = _search_item_html("1787017019.242839", "Today at 6:36 PM", "Michael Kris",
                                  "some text that mentions issue")
        results = parse_search_results_page(html)
        assert results == []

    def test_pending_item_has_no_checkmark(self):
        html = _search_item_html("1786483749.118849", "Aug 11th at 2:29 PM", "Tele-op Issue",
                                  "Right leader arm gripper isn't closing", replies="1 reply", checkmark=False)
        results = parse_search_results_page(html)
        assert len(results) == 1
        assert results[0].already_converted is False

    def test_defaults_to_zero_replies_when_no_reply_bar(self):
        html = _search_item_html("1786484890.567599", "Aug 11th at 2:48 PM", "Tele-op Issue",
                                  "Left arm is frozen in air")
        results = parse_search_results_page(html)
        assert results[0].replies == "0 replies"

    def test_multiple_items_in_one_page(self):
        html = (
            _search_item_html("1.1", "t1", "Tele-op Issue", "First issue", checkmark=True)
            + _search_item_html("1.2", "t2", "Tele-op Issue", "Second issue", checkmark=False)
        )
        results = parse_search_results_page(html)
        assert [r.ts for r in results] == ["1.1", "1.2"]
        assert results[0].already_converted is True
        assert results[1].already_converted is False


class TestBuildLinearMentionBody:
    def test_builds_expected_body(self):
        body = build_linear_mention_body(
            team="PAN",
            project="Teleop",
            title="Servo chain preflight failed",
            description_lines=["Reported in #teleop", "Reproduce: run a teleop session"],
            labels=["bug", "teleop"],
            priority="High",
            status="Triage",
        )
        assert body.startswith(" create an issue in the PAN team and add it to the Teleop project.")
        assert "Title: Servo chain preflight failed" in body
        assert "- Reported in #teleop" in body
        assert "Labels: bug, teleop" in body
        assert "Priority: High" in body
        assert "Status: Triage" in body
