"""Tests for the Linear task backend.

Everything here runs against fakes -- LinearClient is swapped out for the
backend tests, and requests.Session is swapped out for the client tests --
so no test touches the network or ~/.wf.
"""

import pytest

from workflow.backends import linear as linear_mod
from workflow.backends.linear import (
    LinearBackend,
    LinearClient,
    LinearError,
    LinearTask,
    _looks_like_uuid,
)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class FakeClient:
    """Stands in for LinearClient: records every call and replays canned
    responses, either in order or from a handler keyed off the document."""

    def __init__(self, responses=None, handler=None):
        self.responses = list(responses or [])
        self.handler = handler
        self.calls = []

    def query(self, document, variables=None):
        self.calls.append((document, variables or {}))
        if self.handler is not None:
            return self.handler(document, variables or {})
        if not self.responses:
            raise AssertionError(f"unexpected Linear query: {document.strip()[:120]}")
        return self.responses.pop(0)

    def mutate(self, document, variables, root):
        result = self.query(document, variables)[root]
        if not result.get("success", True):
            raise LinearError(f"Linear mutation '{root}' did not succeed")
        return result

    def last_variables(self):
        return self.calls[-1][1]


TEAM = {
    "id": "team-uuid",
    "key": "ENG",
    "name": "Engineering",
    "states": {"nodes": [
        {"id": "s-backlog", "name": "Backlog", "type": "backlog", "position": 0},
        {"id": "s-todo", "name": "Todo", "type": "unstarted", "position": 1},
        {"id": "s-prog", "name": "In Progress", "type": "started", "position": 2},
        {"id": "s-review", "name": "In Review", "type": "started", "position": 3},
        {"id": "s-done", "name": "Done", "type": "completed", "position": 4},
    ]},
}

ISSUE_NODE = {
    "id": "issue-uuid",
    "identifier": "ENG-123",
    "title": "Fix the servo preflight",
    "description": "It fails on cold start",
    "url": "https://linear.app/acme/issue/ENG-123/fix-the-servo-preflight",
    "priority": 2,
    "priorityLabel": "High",
    "createdAt": "2026-09-01T10:00:00.000Z",
    "updatedAt": "2026-09-05T11:30:00.000Z",
    "dueDate": None,
    "branchName": "eng-123-fix-the-servo-preflight",
    "state": {"id": "s-prog", "name": "In Progress", "type": "started"},
    "assignee": {"name": "mkris", "displayName": "Michael Kris", "email": "m@example.com"},
    "creator": {"name": "someone", "displayName": "Some One"},
    "team": {"id": "team-uuid", "key": "ENG", "name": "Engineering"},
    "project": {"id": "proj-uuid", "name": "Teleop"},
    "labels": {"nodes": [{"id": "l-bug", "name": "Bug"}, {"id": "l-teleop", "name": "teleop"}]},
}


def make_backend(monkeypatch, client, linear_cfg=None, cfg=None, teams=(TEAM,)):
    """Build a LinearBackend wired to `client`, with team metadata already
    cached so tests only need to stub the calls they care about."""
    resolved = {"api_key": "lin_api_test", "team": "ENG"}
    resolved.update(linear_cfg or {})
    monkeypatch.setattr(linear_mod, "get_linear_config", lambda: resolved)
    monkeypatch.setattr(linear_mod, "LinearClient", lambda *a, **k: client)

    backend = LinearBackend(cfg if cfg is not None else {})
    if teams is not None:
        backend._teams_cache = list(teams)
        backend._teams_cache_time = float("inf")
    return backend


# ---------------------------------------------------------------------------
# LinearTask: mapping a GraphQL node onto the CLI's task interface
# ---------------------------------------------------------------------------

class TestLinearTask:
    def test_maps_core_fields(self):
        task = LinearTask(ISSUE_NODE)
        assert task.key == "ENG-123"
        assert task.id == "issue-uuid"
        assert task.title == "Fix the servo preflight"
        assert task.status == "In Progress"
        assert task.priority == "High"
        assert task.assignee == "Michael Kris"
        assert task.reporter == "Some One"
        assert task.labels == ["Bug", "teleop"]
        assert task.project == "Teleop"
        assert task.team == "ENG"
        assert task.created_date == "2026-09-01"
        assert task.updated_date == "2026-09-05"

    def test_issue_type_comes_from_a_type_ish_label(self):
        # Linear has no issue types, so a Bug label stands in for one.
        assert LinearTask(ISSUE_NODE).issue_type == "Bug"

    def test_issue_type_defaults_to_task_without_a_type_label(self):
        node = dict(ISSUE_NODE, labels={"nodes": [{"id": "l", "name": "teleop"}]})
        assert LinearTask(node).issue_type == "Task"

    def test_priority_falls_back_to_the_numeric_scale(self):
        node = dict(ISSUE_NODE, priorityLabel=None, priority=1)
        assert LinearTask(node).priority == "Urgent"

    def test_handles_unassigned_and_project_less_issues(self):
        node = dict(ISSUE_NODE, assignee=None, creator=None, project=None, labels={"nodes": []})
        task = LinearTask(node)
        assert task.assignee is None
        assert task.reporter is None
        assert task.project is None
        assert task.labels == []

    def test_to_issue_carries_the_web_url(self):
        issue = LinearTask(ISSUE_NODE).to_issue()
        assert issue.key == "ENG-123"
        assert issue.url == ISSUE_NODE["url"]
        assert issue.updated == "2026-09-05T11:30:00.000Z"


# ---------------------------------------------------------------------------
# LinearClient: transport and error handling
# ---------------------------------------------------------------------------

class FakeResponse:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


class FakeSession:
    def __init__(self, response):
        self.response = response
        self.headers = {}
        self.posted = None

    def post(self, url, json=None, timeout=None):
        self.posted = {"url": url, "json": json, "timeout": timeout}
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def make_client(response):
    client = LinearClient("lin_api_test")
    client._session = FakeSession(response)
    return client


class TestLinearClient:
    def test_requires_an_api_key(self):
        # get_backend() relies on KeyError to print setup guidance.
        with pytest.raises(KeyError):
            LinearClient("")

    def test_sends_the_key_without_a_bearer_prefix(self):
        # A personal API key goes in Authorization verbatim; "Bearer <key>"
        # is rejected by Linear.
        client = LinearClient("lin_api_test")
        assert client._session.headers["Authorization"] == "lin_api_test"

    def test_returns_the_data_payload(self):
        client = make_client(FakeResponse(payload={"data": {"viewer": {"id": "u1"}}}))
        assert client.query("query { viewer { id } }") == {"viewer": {"id": "u1"}}
        assert client._session.posted["json"]["query"].strip().startswith("query")

    def test_graphql_errors_become_linear_errors(self):
        client = make_client(FakeResponse(payload={"errors": [{"message": "Field 'nope' doesn't exist"}]}))
        with pytest.raises(LinearError, match="Field 'nope' doesn't exist"):
            client.query("query { nope }")

    def test_auth_failure_explains_how_to_fix_it(self):
        client = make_client(FakeResponse(status_code=401, payload={"errors": [{"message": "nope"}]}))
        with pytest.raises(LinearError, match="linear.app/<team-name>/settings/account/security/api-keys"):
            client.query("query { viewer { id } }")

    def test_non_json_response_is_reported_with_its_body(self):
        client = make_client(FakeResponse(status_code=502, payload=None, text="<html>bad gateway</html>"))
        with pytest.raises(LinearError, match="non-JSON"):
            client.query("query { viewer { id } }")

    def test_transport_failure_is_wrapped(self):
        import requests
        client = make_client(requests.ConnectionError("no route to host"))
        with pytest.raises(LinearError, match="could not reach the Linear API"):
            client.query("query { viewer { id } }")

    def test_unsuccessful_mutation_is_an_error(self):
        client = make_client(FakeResponse(payload={"data": {"issueUpdate": {"success": False}}}))
        with pytest.raises(LinearError, match="issueUpdate"):
            client.mutate("mutation { issueUpdate { success } }", {}, "issueUpdate")


# ---------------------------------------------------------------------------
# Workflow state resolution -- Linear has no named transitions
# ---------------------------------------------------------------------------

class TestStateResolution:
    def test_matches_state_names_exactly(self, monkeypatch):
        backend = make_backend(monkeypatch, FakeClient())
        assert backend._resolve_state("in_progress")["id"] == "s-prog"
        assert backend._resolve_state("review")["id"] == "s-review"
        assert backend._resolve_state("done")["id"] == "s-done"

    def test_matches_state_names_by_substring(self, monkeypatch):
        team = dict(TEAM, states={"nodes": [
            {"id": "s-x", "name": "Code Review (PR open)", "type": "started", "position": 1},
            {"id": "s-y", "name": "Shipped to prod", "type": "completed", "position": 2},
        ]})
        backend = make_backend(monkeypatch, FakeClient(), teams=(team,))
        assert backend._resolve_state("review")["id"] == "s-x"
        assert backend._resolve_state("done")["id"] == "s-y"

    def test_config_override_wins_over_the_heuristics(self, monkeypatch):
        backend = make_backend(
            monkeypatch, FakeClient(),
            linear_cfg={"states": {"review": "Todo"}},
        )
        assert backend._resolve_state("review")["id"] == "s-todo"

    def test_unknown_configured_state_falls_back_and_warns(self, monkeypatch, capsys):
        backend = make_backend(
            monkeypatch, FakeClient(),
            linear_cfg={"states": {"done": "Shipped To Prod"}},
        )
        assert backend._resolve_state("done")["id"] == "s-done"
        assert "not found" in capsys.readouterr().out

    def test_falls_back_to_the_state_type_when_no_name_matches(self, monkeypatch):
        # A workspace with entirely custom state names still has the coarse
        # started/completed types to fall back on.
        team = dict(TEAM, states={"nodes": [
            {"id": "s-a", "name": "Icebox", "type": "backlog", "position": 0},
            {"id": "s-b", "name": "Cooking", "type": "started", "position": 1},
            {"id": "s-c", "name": "Landed", "type": "completed", "position": 2},
        ]})
        backend = make_backend(monkeypatch, FakeClient(), teams=(team,))
        assert backend._resolve_state("in_progress")["id"] == "s-b"
        assert backend._resolve_state("done")["id"] == "s-c"
        # No review-ish state at all: land on in-progress rather than
        # moving the issue backwards.
        assert backend._resolve_state("review")["id"] == "s-b"

    def test_error_lists_the_available_states(self, monkeypatch):
        team = dict(TEAM, states={"nodes": [
            {"id": "s-a", "name": "Icebox", "type": "backlog", "position": 0},
        ]})
        backend = make_backend(monkeypatch, FakeClient(), teams=(team,))
        with pytest.raises(LinearError) as exc:
            backend._resolve_state("done")
        assert "Icebox" in str(exc.value)
        assert "linear.states.done" in str(exc.value)


class TestTeamResolution:
    def test_unknown_team_key_lists_the_real_ones(self, monkeypatch):
        backend = make_backend(monkeypatch, FakeClient())
        with pytest.raises(LinearError, match="Available teams: ENG"):
            backend._team("NOPE")

    def test_single_team_workspace_needs_no_default(self, monkeypatch):
        backend = make_backend(monkeypatch, FakeClient(), linear_cfg={"team": None})
        assert backend._team()["key"] == "ENG"

    def test_multi_team_workspace_without_a_default_is_an_error(self, monkeypatch):
        other = dict(TEAM, id="t2", key="OPS", name="Operations")
        backend = make_backend(monkeypatch, FakeClient(), linear_cfg={"team": None}, teams=(TEAM, other))
        with pytest.raises(LinearError, match="linear.team"):
            backend._team()


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------

class TestReads:
    def test_get_returns_an_issue_with_its_url(self, monkeypatch):
        client = FakeClient([{"issue": ISSUE_NODE}])
        backend = make_backend(monkeypatch, client)
        issue = backend.get("ENG-123")
        assert issue.key == "ENG-123"
        assert issue.url == ISSUE_NODE["url"]
        # Linear's issue(id:) takes the human identifier directly.
        assert client.last_variables() == {"id": "ENG-123"}

    def test_missing_issue_raises_a_not_found_error(self, monkeypatch):
        backend = make_backend(monkeypatch, FakeClient([{"issue": None}]))
        with pytest.raises(Exception, match="does not exist"):
            backend.get("ENG-999")

    @pytest.mark.parametrize("text,valid", [
        ("ENG-123", True), ("eng-123", True), ("A1-9", True),
        ("fix the servo preflight", False), ("ENG-", False), ("-123", False),
    ])
    def test_ticket_key_detection(self, monkeypatch, text, valid):
        backend = make_backend(monkeypatch, FakeClient())
        assert backend._is_valid_ticket_key(text) is valid

    def test_get_or_create_creates_from_free_text(self, monkeypatch):
        created = dict(ISSUE_NODE, identifier="ENG-500", title="fix the servo")
        client = FakeClient(handler=lambda doc, v: (
            {"viewer": {"id": "u1", "name": "me", "displayName": "Me", "email": "m@e.com"}}
            if "viewer {" in doc and "assignedIssues" not in doc
            else {"team": {"activeCycle": None}} if "activeCycle" in doc
            else {"issueCreate": {"success": True, "issue": created}}
        ))
        backend = make_backend(monkeypatch, client)
        issue = backend.get_or_create("fix the servo")
        assert issue.key == "ENG-500"

    def test_assigned_tasks_are_cached_and_reused(self, monkeypatch):
        client = FakeClient([{"viewer": {"assignedIssues": {"nodes": [ISSUE_NODE]}}}])
        backend = make_backend(monkeypatch, client)
        first = backend.get_assigned_tasks()
        second = backend.get_assigned_tasks()
        assert [i.key for i in first] == ["ENG-123"]
        assert second is first
        assert len(client.calls) == 1  # second call served from cache

    def test_assigned_task_nodes_are_kept_for_uuid_lookups(self, monkeypatch):
        client = FakeClient([{"viewer": {"assignedIssues": {"nodes": [ISSUE_NODE]}}}])
        backend = make_backend(monkeypatch, client)
        backend.get_assigned_tasks()
        # Resolving the UUID must not cost another round trip.
        assert backend._issue_uuid("ENG-123") == "issue-uuid"
        assert len(client.calls) == 1

    def test_list_tasks_filters_by_team_key(self, monkeypatch):
        client = FakeClient([{"issues": {"nodes": [ISSUE_NODE]}}])
        backend = make_backend(monkeypatch, client)
        tasks = backend.list_tasks(project_name="ENG")
        assert [t.key for t in tasks] == ["ENG-123"]
        assert client.last_variables()["filter"] == {"team": {"key": {"eq": "ENG"}}}

    def test_list_tasks_treats_a_non_team_name_as_a_linear_project(self, monkeypatch):
        client = FakeClient([{"issues": {"nodes": []}}])
        backend = make_backend(monkeypatch, client)
        backend.list_tasks(project_name="Teleop")
        assert client.last_variables()["filter"] == {"project": {"name": {"eqIgnoreCase": "Teleop"}}}

    def test_list_tasks_maps_friendly_status_names(self, monkeypatch):
        client = FakeClient([{"issues": {"nodes": []}}])
        backend = make_backend(monkeypatch, client)
        backend.list_tasks(status="in review")
        assert client.last_variables()["filter"]["state"] == {"name": {"eqIgnoreCase": "In Review"}}

    def test_unfiltered_list_defaults_to_my_issues(self, monkeypatch):
        client = FakeClient([{"issues": {"nodes": []}}])
        backend = make_backend(monkeypatch, client)
        backend.list_tasks()
        assert client.last_variables()["filter"] == {"assignee": {"isMe": {"eq": True}}}

    def test_is_done_uses_the_state_type(self, monkeypatch):
        done = dict(ISSUE_NODE, state={"id": "s-done", "name": "Landed", "type": "completed"})
        backend = make_backend(monkeypatch, FakeClient([{"issue": done}]))
        assert backend.is_done(LinearTask(done).to_issue()) is True

    def test_is_done_is_false_for_a_started_issue(self, monkeypatch):
        backend = make_backend(monkeypatch, FakeClient([{"issue": ISSUE_NODE}]))
        assert backend.is_done(LinearTask(ISSUE_NODE).to_issue()) is False

    def test_is_in_review_matches_review_state_names(self, monkeypatch):
        node = dict(ISSUE_NODE, state={"id": "s-review", "name": "In Review", "type": "started"})
        backend = make_backend(monkeypatch, FakeClient([{"issue": node}]))
        assert backend.is_in_review(LinearTask(node).to_issue()) is True


class TestIssueUrl:
    def test_prefers_a_url_already_on_a_cached_issue(self, monkeypatch):
        client = FakeClient([{"viewer": {"assignedIssues": {"nodes": [ISSUE_NODE]}}}])
        backend = make_backend(monkeypatch, client)
        backend.get_assigned_tasks()
        assert backend.issue_url("ENG-123") == ISSUE_NODE["url"]
        assert len(client.calls) == 1

    def test_builds_a_url_from_the_workspace_slug_without_a_round_trip(self, monkeypatch):
        client = FakeClient()
        backend = make_backend(monkeypatch, client, linear_cfg={"workspace": "acme"})
        assert backend.issue_url("ENG-7") == "https://linear.app/acme/issue/ENG-7"
        assert client.calls == []

    def test_returns_none_rather_than_raising_when_it_cannot_resolve(self, monkeypatch):
        backend = make_backend(monkeypatch, FakeClient([{"issue": None}]))
        assert backend.issue_url("ENG-7") is None


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------

class TestTransitions:
    def _transition_client(self):
        return FakeClient(handler=lambda doc, v: {
            "issueUpdate": {"success": True, "issue": dict(ISSUE_NODE)}
        } if "issueUpdate" in doc else {"issue": ISSUE_NODE})

    def test_move_to_review_sets_the_review_state(self, monkeypatch):
        client = self._transition_client()
        backend = make_backend(monkeypatch, client)
        backend._assigned_nodes["ENG-123"] = ISSUE_NODE  # skip the UUID lookup
        backend.move_to_review(LinearTask(ISSUE_NODE).to_issue())
        assert client.last_variables() == {"id": "issue-uuid", "input": {"stateId": "s-review"}}

    def test_move_to_done_sets_the_completed_state(self, monkeypatch):
        client = self._transition_client()
        backend = make_backend(monkeypatch, client)
        backend._assigned_nodes["ENG-123"] = ISSUE_NODE
        backend.move_to_done(LinearTask(ISSUE_NODE).to_issue())
        assert client.last_variables()["input"] == {"stateId": "s-done"}

    def test_move_to_in_progress_uses_the_key_prefix_as_the_team(self, monkeypatch):
        ops = dict(TEAM, id="t2", key="OPS", states={"nodes": [
            {"id": "ops-prog", "name": "Doing", "type": "started", "position": 1},
        ]})
        client = FakeClient(handler=lambda doc, v: {
            "issueUpdate": {"success": True, "issue": dict(ISSUE_NODE, identifier="OPS-1")}
        })
        backend = make_backend(monkeypatch, client, teams=(TEAM, ops))
        backend._assigned_nodes["OPS-1"] = dict(ISSUE_NODE, identifier="OPS-1", id="ops-issue")
        backend.move_to_in_progress(LinearTask({"identifier": "OPS-1", "id": "ops-issue"}).to_issue())
        assert client.last_variables()["input"] == {"stateId": "ops-prog"}


class TestCreateIssue:
    def _create_client(self, created=None, cycle=None):
        created = created or ISSUE_NODE
        return FakeClient(handler=lambda doc, v: (
            {"viewer": {"id": "user-uuid", "name": "me", "displayName": "Me", "email": "m@e.com"}}
            if "viewer {" in doc and "assignedIssues" not in doc
            else {"team": {"activeCycle": cycle}} if "activeCycle" in doc
            else {"team": {"labels": {"nodes": [{"id": "l-bug", "name": "Bug"}]}},
                  "issueLabels": {"nodes": []}} if "issueLabels" in doc
            else {"issueCreate": {"success": True, "issue": created}}
        ))

    def test_creates_assigned_to_the_current_user(self, monkeypatch):
        client = self._create_client()
        backend = make_backend(monkeypatch, client)
        issue = backend.create_issue("Fix the servo preflight", "It fails on cold start")
        assert issue.key == "ENG-123"
        sent = client.last_variables()["input"]
        assert sent["teamId"] == "team-uuid"
        assert sent["title"] == "Fix the servo preflight"
        assert sent["assigneeId"] == "user-uuid"

    def test_issue_type_becomes_a_matching_label(self, monkeypatch):
        client = self._create_client()
        backend = make_backend(monkeypatch, client)
        backend.create_issue("Broken", "", issue_type="Bug")
        assert client.last_variables()["input"]["labelIds"] == ["l-bug"]

    def test_unknown_issue_type_is_skipped_not_created(self, monkeypatch, capsys):
        client = self._create_client()
        backend = make_backend(monkeypatch, client)
        backend.create_issue("Broken", "", issue_type="Widget")
        assert "labelIds" not in client.last_variables()["input"]
        assert "Skipping unknown Linear label" in capsys.readouterr().out

    def test_adds_the_issue_to_the_active_cycle(self, monkeypatch):
        client = self._create_client(cycle={"id": "cycle-uuid", "number": 12, "name": None,
                                            "startsAt": "2026-09-01", "endsAt": "2026-09-14"})
        backend = make_backend(monkeypatch, client)
        backend.create_issue("Fix it", "")
        assert client.last_variables()["input"]["cycleId"] == "cycle-uuid"

    def test_respects_auto_assign_sprint_false(self, monkeypatch):
        client = self._create_client(cycle={"id": "cycle-uuid", "number": 12, "name": None,
                                            "startsAt": None, "endsAt": None})
        backend = make_backend(monkeypatch, client, cfg={"auto_assign_sprint": False})
        backend.create_issue("Fix it", "")
        assert "cycleId" not in client.last_variables()["input"]

    def test_applies_configured_field_defaults(self, monkeypatch):
        # Same contract as the Jira backend: custom_fields entries with a
        # default_value are applied when the issue is created.
        client = self._create_client()
        backend = make_backend(monkeypatch, client, cfg={
            "custom_fields": {"priority": {"field_id": "priority", "default_value": "High"}},
        })
        backend.create_issue("Fix it", "")
        assert client.last_variables()["input"]["priority"] == 2

    def test_field_defaults_scoped_to_another_team_are_skipped(self, monkeypatch):
        client = self._create_client()
        backend = make_backend(monkeypatch, client, cfg={
            "custom_fields": {"priority": {"field_id": "priority",
                                           "default_value": "High",
                                           "project_key": "OPS"}},
        })
        backend.create_issue("Fix it", "")
        assert "priority" not in client.last_variables()["input"]

    def test_default_labels_and_issue_type_labels_are_unioned(self, monkeypatch):
        def labels(doc, v):
            if "issueLabels" in doc:
                return {"team": {"labels": {"nodes": [
                            {"id": "l-bug", "name": "Bug"},
                            {"id": "l-teleop", "name": "teleop"}]}},
                        "issueLabels": {"nodes": []}}
            return None

        created = dict(ISSUE_NODE)
        client = FakeClient(handler=lambda doc, v: (
            labels(doc, v) or (
                {"viewer": {"id": "user-uuid", "name": "me", "displayName": "Me", "email": "m@e.com"}}
                if "viewer {" in doc and "assignedIssues" not in doc
                else {"team": {"activeCycle": None}} if "activeCycle" in doc
                else {"issueCreate": {"success": True, "issue": created}}
            )
        ))
        backend = make_backend(monkeypatch, client, cfg={
            "custom_fields": {"labels": {"field_id": "labelIds", "default_value": "teleop"}},
        })
        backend.create_issue("Fix it", "", issue_type="Bug")
        assert sorted(client.last_variables()["input"]["labelIds"]) == ["l-bug", "l-teleop"]

    def test_returns_none_on_failure(self, monkeypatch, capsys):
        def boom(doc, v):
            raise LinearError("team not found")
        backend = make_backend(monkeypatch, FakeClient(handler=boom))
        assert backend.create_issue("Fix it", "") is None
        assert "Failed to create Linear issue" in capsys.readouterr().out


class TestCustomFields:
    def _update_client(self, extra=None):
        handler = extra or (lambda doc, v: None)
        def dispatch(doc, v):
            result = handler(doc, v)
            if result is not None:
                return result
            return {"issueUpdate": {"success": True, "issue": dict(ISSUE_NODE)}}
        return FakeClient(handler=dispatch)

    def test_maps_a_priority_name_to_linears_numeric_scale(self, monkeypatch):
        client = self._update_client()
        backend = make_backend(monkeypatch, client)
        backend._assigned_nodes["ENG-123"] = ISSUE_NODE
        backend._apply_custom_fields("ENG-123", {"priority": "Urgent"})
        assert client.last_variables()["input"] == {"priority": 1}

    def test_rejects_an_unknown_priority(self, monkeypatch, capsys):
        client = self._update_client()
        backend = make_backend(monkeypatch, client)
        backend._apply_custom_fields("ENG-123", {"priority": "Spicy"})
        assert "Unknown Linear priority" in capsys.readouterr().out
        assert client.calls == []  # nothing sent

    def test_maps_comma_separated_labels_to_ids(self, monkeypatch):
        def labels(doc, v):
            if "issueLabels" in doc:
                return {"team": {"labels": {"nodes": [{"id": "l-bug", "name": "Bug"}]}},
                        "issueLabels": {"nodes": [{"id": "l-teleop", "name": "teleop"}]}}
            return None
        client = self._update_client(labels)
        backend = make_backend(monkeypatch, client)
        backend._assigned_nodes["ENG-123"] = ISSUE_NODE
        backend._apply_custom_fields("ENG-123", {"labels": "Bug, teleop"})
        assert client.last_variables()["input"]["labelIds"] == ["l-bug", "l-teleop"]

    def test_unsupported_field_says_what_linear_has(self, monkeypatch, capsys):
        client = self._update_client()
        backend = make_backend(monkeypatch, client)
        backend._apply_custom_fields("ENG-123", {"pod_name": "Teleop"})
        out = capsys.readouterr().out
        assert "no field 'pod_name'" in out
        assert "priority, labels, project, estimate, assignee" in out
        assert client.calls == []

    def test_estimate_must_be_numeric(self, monkeypatch, capsys):
        client = self._update_client()
        backend = make_backend(monkeypatch, client)
        backend._apply_custom_fields("ENG-123", {"estimate": "big"})
        assert "must be a number" in capsys.readouterr().out
        assert client.calls == []

    def test_state_field_resolves_to_a_workflow_state(self, monkeypatch):
        client = self._update_client()
        backend = make_backend(monkeypatch, client)
        backend._assigned_nodes["ENG-123"] = ISSUE_NODE
        backend._apply_custom_fields("ENG-123", {"status": "Todo"})
        assert client.last_variables()["input"] == {"stateId": "s-todo"}

    def test_unknown_state_lists_the_real_ones(self, monkeypatch, capsys):
        client = self._update_client()
        backend = make_backend(monkeypatch, client)
        backend._apply_custom_fields("ENG-123", {"state": "Frozen"})
        out = capsys.readouterr().out
        assert "No Linear state named 'Frozen'" in out
        assert "In Review" in out
        assert client.calls == []

    def test_assignee_me_resolves_to_the_viewer(self, monkeypatch):
        def viewer(doc, v):
            if "viewer {" in doc and "assignedIssues" not in doc:
                return {"viewer": {"id": "user-uuid", "name": "me", "displayName": "Me", "email": "m@e.com"}}
            return None
        client = self._update_client(viewer)
        backend = make_backend(monkeypatch, client)
        backend._assigned_nodes["ENG-123"] = ISSUE_NODE
        backend._apply_custom_fields("ENG-123", {"assignee": "me"})
        assert client.last_variables()["input"] == {"assigneeId": "user-uuid"}


# ---------------------------------------------------------------------------
# Cycles (Linear's sprints)
# ---------------------------------------------------------------------------

class TestActiveCycle:
    def test_maps_a_cycle_onto_the_sprint_shape(self, monkeypatch):
        cycle = {"id": "cycle-uuid", "number": 12, "name": "Hardening",
                 "startsAt": "2026-09-01", "endsAt": "2026-09-14"}
        backend = make_backend(monkeypatch, FakeClient([{"team": {"activeCycle": cycle}}]))
        sprint = backend.get_active_sprint("ENG")
        # The `active_sprint` variables read exactly these three keys.
        assert sprint["id"] == "cycle-uuid"
        assert sprint["name"] == "Hardening"
        assert sprint["state"] == "active"

    def test_unnamed_cycles_get_linears_own_display_name(self, monkeypatch):
        cycle = {"id": "c", "number": 12, "name": None, "startsAt": None, "endsAt": None}
        backend = make_backend(monkeypatch, FakeClient([{"team": {"activeCycle": cycle}}]))
        assert backend.get_active_sprint("ENG")["name"] == "Cycle 12"

    def test_no_active_cycle_returns_none_and_is_cached(self, monkeypatch):
        client = FakeClient([{"team": {"activeCycle": None}}])
        backend = make_backend(monkeypatch, client)
        assert backend.get_active_sprint("ENG") is None
        assert backend.get_active_sprint("ENG") is None
        assert len(client.calls) == 1

    def test_unresolvable_team_warns_instead_of_raising(self, monkeypatch, capsys):
        backend = make_backend(monkeypatch, FakeClient())
        assert backend.get_active_sprint("NOPE") is None
        assert "Could not resolve Linear team" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Comments -- exposed under 1-based ordinals so `wf task comment` works
# ---------------------------------------------------------------------------

COMMENTS = [
    {"id": "c-uuid-1", "body": "first", "createdAt": "2026-09-01T00:00:00.000Z",
     "updatedAt": "2026-09-01T00:00:00.000Z", "user": {"name": "a", "displayName": "Ada"}},
    {"id": "c-uuid-2", "body": "second", "createdAt": "2026-09-02T00:00:00.000Z",
     "updatedAt": "2026-09-02T00:00:00.000Z", "user": None},
]


class TestComments:
    def test_comments_are_numbered_from_one_with_uuids_kept(self, monkeypatch):
        backend = make_backend(monkeypatch, FakeClient([{"issue": {"comments": {"nodes": COMMENTS}}}]))
        comments = backend.get_comments("ENG-123")
        assert [c["id"] for c in comments] == [1, 2]
        assert [c["linear_id"] for c in comments] == ["c-uuid-1", "c-uuid-2"]
        assert comments[0]["author"] == "Ada"
        assert comments[1]["author"] == "unknown"
        assert comments[0]["text"] == "first"

    def test_missing_issue_yields_no_comments(self, monkeypatch, capsys):
        backend = make_backend(monkeypatch, FakeClient([{"issue": None}]))
        assert backend.get_comments("ENG-999") == []
        assert "Failed to get comments" in capsys.readouterr().out

    def test_edit_resolves_the_ordinal_to_linears_uuid(self, monkeypatch):
        client = FakeClient(handler=lambda doc, v: (
            {"issue": {"comments": {"nodes": COMMENTS}}} if "comments" in doc and "mutation" not in doc
            else {"commentUpdate": {"success": True}}
        ))
        backend = make_backend(monkeypatch, client)
        assert backend.update_comment("ENG-123", 2, "revised") is True
        assert client.last_variables() == {"id": "c-uuid-2", "input": {"body": "revised"}}

    def test_delete_resolves_the_ordinal_to_linears_uuid(self, monkeypatch):
        client = FakeClient(handler=lambda doc, v: (
            {"issue": {"comments": {"nodes": COMMENTS}}} if "comments" in doc and "mutation" not in doc
            else {"commentDelete": {"success": True}}
        ))
        backend = make_backend(monkeypatch, client)
        assert backend.delete_comment("ENG-123", 1) is True
        assert client.last_variables() == {"id": "c-uuid-1"}

    def test_out_of_range_ordinal_fails_cleanly(self, monkeypatch, capsys):
        backend = make_backend(monkeypatch, FakeClient([{"issue": {"comments": {"nodes": COMMENTS}}}]))
        assert backend.delete_comment("ENG-123", 9) is False
        assert "No comment #9" in capsys.readouterr().out

    def test_a_raw_uuid_is_accepted_too(self, monkeypatch):
        client = FakeClient(handler=lambda doc, v: {"commentDelete": {"success": True}})
        backend = make_backend(monkeypatch, client)
        assert backend.delete_comment("ENG-123", "c1234567-89ab-cdef-0123-456789abcdef") is True
        # No lookup query was needed to resolve it.
        assert len(client.calls) == 1

    def test_add_comment_posts_against_the_issue_uuid(self, monkeypatch):
        posted = {"id": "c-uuid-3", "body": "note", "createdAt": "2026-09-03T00:00:00.000Z",
                  "updatedAt": "2026-09-03T00:00:00.000Z", "user": {"name": "a", "displayName": "Ada"}}
        calls = []

        def handler(doc, v):
            calls.append(doc)
            if "commentCreate" in doc:
                return {"commentCreate": {"success": True, "comment": posted}}
            return {"issue": {"comments": {"nodes": COMMENTS + [posted]}}}

        client = FakeClient(handler=handler)
        backend = make_backend(monkeypatch, client)
        backend._assigned_nodes["ENG-123"] = ISSUE_NODE
        result = backend.add_comment("ENG-123", "note", author="ignored")
        assert result["linear_id"] == "c-uuid-3"
        assert result["id"] == 3
        create_call = next(d for d in calls if "commentCreate" in d)
        assert create_call


class TestWorkflowIntrospection:
    def test_every_state_can_reach_every_other(self, monkeypatch):
        # Linear places no restrictions on state-to-state moves.
        backend = make_backend(monkeypatch, FakeClient())
        workflow = backend.get_workflow("ENG")
        assert "In Review" in workflow["In Progress"]
        assert "In Progress" not in workflow["In Progress"]

    def test_available_transitions_exclude_the_current_state(self, monkeypatch):
        backend = make_backend(monkeypatch, FakeClient([{"issue": ISSUE_NODE}]))
        transitions = backend.get_available_transitions("ENG-123")
        assert "In Progress" not in transitions
        assert "Done" in transitions

    def test_priority_field_options_cover_linears_scale(self, monkeypatch):
        backend = make_backend(monkeypatch, FakeClient())
        values = [o["value"] for o in backend.get_field_options("priority")]
        assert values == ["None", "Urgent", "High", "Medium", "Low"]

    def test_state_field_options_come_from_the_team(self, monkeypatch):
        backend = make_backend(monkeypatch, FakeClient())
        values = [o["value"] for o in backend.get_field_options("state")]
        assert values == ["Backlog", "Todo", "In Progress", "In Review", "Done"]

    def test_find_field_by_name_is_case_insensitive(self, monkeypatch):
        backend = make_backend(monkeypatch, FakeClient())
        assert backend.find_field_by_name("Priority")["id"] == "priority"
        assert backend.find_field_by_name("nope") is None


@pytest.mark.parametrize("value,expected", [
    ("c1234567-89ab-cdef-0123-456789abcdef", True),
    ("ENG-123", False),
    ("2", False),
    ("", False),
])
def test_looks_like_uuid(value, expected):
    assert _looks_like_uuid(value) is expected
