"""Linear task backend, an alternative to workflow.backends.jira.

Linear has no REST API -- everything goes through a single GraphQL endpoint
authenticated with a personal API key (`Authorization: <key>`, with no
`Bearer` prefix; that prefix is only for OAuth access tokens).

Three places where Linear's model doesn't line up with Jira's, and what
this backend does about each:

- **No issue types.** Jira's Task/Bug/Story/Epic become *labels* here. On
  create, an `issue_type` that matches an existing team label is attached;
  one that doesn't is skipped rather than created, so a typo can't litter
  the workspace with new labels.
- **No named transitions.** Jira asks the server "which transitions are
  available?"; Linear just has workflow states per team, each with a
  coarse `type` (triage/backlog/unstarted/started/completed/canceled). So
  "move to review" is resolved to a concrete state by name heuristics,
  overridable per-workspace via `linear.states` in the config.
- **UUIDs everywhere.** Mutations take the issue's UUID, not its `ENG-123`
  identifier, and comment IDs are UUIDs too. Identifiers are resolved to
  UUIDs on the way in, and comments are exposed to the CLI under 1-based
  ordinals (matching the Markdown backend) so `wf task comment edit KEY 2`
  keeps working.
"""

import re
import time
from typing import Any, Dict, List, Optional

import requests
import typer

from workflow.backends.base import Issue, TaskBackend
from workflow.config import get_linear_config
from workflow.variables import VariableResolver

LINEAR_API_URL = "https://api.linear.app/graphql"

# Linear costs a query roughly as the product of its nested page sizes,
# so asking for 100 teams x 100 states at once is rejected as "Query too
# complex". These sizes stay well inside the limit; _teams() pages.
TEAMS_PAGE_SIZE = 25
STATES_PAGE_SIZE = 50

# Linear's numeric priority scale, and the names people actually type.
PRIORITY_LABELS = {0: "None", 1: "Urgent", 2: "High", 3: "Medium", 4: "Low"}
PRIORITY_VALUES = {
    "none": 0, "no priority": 0,
    "urgent": 1, "critical": 1, "blocker": 1, "highest": 1,
    "high": 2,
    "medium": 3, "normal": 3, "default": 3,
    "low": 4, "lowest": 4, "minor": 4,
}

# Labels that stand in for Jira issue types.
TYPE_LABEL_NAMES = {"bug", "feature", "story", "epic", "task", "improvement", "chore", "spike"}

# Name heuristics for the three transitions the workflow CLI drives, tried
# in order before falling back to the state's coarse `type`.
IN_PROGRESS_STATE_NAMES = ("in progress", "started", "doing", "wip", "in dev")
REVIEW_STATE_NAMES = ("in review", "code review", "peer review", "pr review", "reviewing", "review")
DONE_STATE_NAMES = ("done", "completed", "complete", "closed", "shipped", "merged")

DONE_STATE_TYPES = ("completed",)

# Shared selection set, spliced into queries with %-formatting: GraphQL is
# full of braces, so f-strings and .format() would need every one escaped.
ISSUE_FIELDS = """
  id
  identifier
  title
  description
  url
  priority
  priorityLabel
  createdAt
  updatedAt
  dueDate
  branchName
  state { id name type }
  assignee { name displayName email }
  creator { name displayName }
  team { id key name }
  project { id name }
  labels { nodes { id name } }
"""


class LinearError(Exception):
    """Any failure talking to Linear: transport, GraphQL errors, or a
    mutation that came back with success=false."""


class LinearTask:
    """Task-like view of a Linear issue, matching the attribute surface the
    CLI expects from JiraBackend.list_tasks()'s JiraTask and from
    MarkdownTask, so `wf task list` renders all three the same way."""

    def __init__(self, node: Dict[str, Any]):
        self._node = node
        self.key = node.get("identifier") or node.get("id")
        self.id = node.get("id")
        self.title = node.get("title") or ""
        self.description = node.get("description") or ""
        self.url = node.get("url")
        self.status = (node.get("state") or {}).get("name") or "Unknown"
        self.state_type = (node.get("state") or {}).get("type") or ""
        self.labels = [l["name"] for l in (node.get("labels") or {}).get("nodes", [])]
        self.priority = node.get("priorityLabel") or PRIORITY_LABELS.get(node.get("priority") or 0, "None")

        assignee = node.get("assignee") or {}
        self.assignee = assignee.get("displayName") or assignee.get("name")
        creator = node.get("creator") or {}
        self.reporter = creator.get("displayName") or creator.get("name")

        project = node.get("project") or {}
        self.project = project.get("name")
        team = node.get("team") or {}
        self.team = team.get("key")

        # Linear has no issue types; a type-ish label stands in for one.
        self.issue_type = next(
            (l.title() for l in self.labels if l.lower() in TYPE_LABEL_NAMES), "Task"
        )

        # Jira surfaces these; Linear has no equivalent, so keep them empty
        # rather than absent so callers don't need hasattr() guards.
        self.components: List[str] = []
        self.fix_versions: List[str] = []

        self.due_date = node.get("dueDate")
        self.created_date = (node.get("createdAt") or "")[:10]
        self.updated_date = (node.get("updatedAt") or "")[:10]
        self.file_path = None  # Not applicable for Linear tasks

    def to_issue(self) -> Issue:
        return Issue(self.key, self.title, self.description, self._node.get("updatedAt"), url=self.url)


class LinearClient:
    """Minimal GraphQL transport for Linear's API."""

    def __init__(self, api_key: str, url: str = LINEAR_API_URL, timeout: int = 30):
        if not api_key:
            raise KeyError("linear.api_key")
        self._session = requests.Session()
        # A personal API key goes in Authorization verbatim -- prefixing it
        # with "Bearer" (correct for OAuth tokens) yields a 400.
        self._session.headers.update({
            "Authorization": api_key,
            "Content-Type": "application/json",
        })
        self.url = url
        self.timeout = timeout

    def query(self, document: str, variables: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        try:
            response = self._session.post(
                self.url,
                json={"query": document, "variables": variables or {}},
                timeout=self.timeout,
            )
        except requests.RequestException as e:
            raise LinearError(f"could not reach the Linear API: {e}") from e

        if response.status_code in (401, 403):
            raise LinearError(
                "Linear rejected the API key (HTTP "
                f"{response.status_code}). Create a new one at "
                "https://linear.app/<team-name>/settings/account/security/api-keys and run 'wf config linear-token'."
            )

        try:
            payload = response.json()
        except ValueError:
            raise LinearError(f"Linear returned a non-JSON response (HTTP {response.status_code}): {response.text[:200]}")

        if payload.get("errors"):
            messages = "; ".join(e.get("message", str(e)) for e in payload["errors"])
            raise LinearError(f"Linear API error: {messages}")

        data = payload.get("data")
        if data is None:
            raise LinearError(f"Linear returned no data (HTTP {response.status_code})")
        return data

    def mutate(self, document: str, variables: Dict[str, Any], root: str) -> Dict[str, Any]:
        """Run a mutation and unwrap its payload, treating success=false as
        an error so callers never act on a silent no-op."""
        result = self.query(document, variables)[root]
        if not result.get("success", True):
            raise LinearError(f"Linear mutation '{root}' did not succeed")
        return result


class LinearBackend(TaskBackend):
    def __init__(self, cfg):
        linear_cfg = get_linear_config()
        api_key = linear_cfg.get("api_key")
        if not api_key:
            # get_backend() catches KeyError to print setup guidance, the
            # same contract JiraBackend has for missing config.
            raise KeyError("linear.api_key (set it with 'wf config linear-token')")

        self.client = LinearClient(api_key)
        self._cfg = cfg
        # `cfg` is the *effective* config, so a project's own linear block
        # (team, workspace, states) overrides the global one -- while the
        # API key stays with the global, Keychain-backed copy.
        self._linear_cfg = {**linear_cfg, **(cfg.get("linear") or {})}
        self._linear_cfg["api_key"] = api_key

        self._assigned_issues_cache = None
        self._cache_time = None
        self._cache_ttl = 30  # Cache for 30 seconds
        self._assigned_nodes: Dict[str, Dict[str, Any]] = {}

        self._teams_cache = None
        self._teams_cache_time = None
        self._teams_cache_ttl = 3600  # Cache teams/states for 1 hour
        self._fields_cache = None
        self._fields_cache_time = None
        self._fields_cache_ttl = 3600

        self._active_cycle_cache: Dict[str, Any] = {}
        self._active_cycle_cache_time: Dict[str, float] = {}
        self._active_cycle_cache_ttl = 300  # Cache active cycle for 5 minutes

        self._viewer_cache = None
        self._variable_resolver = VariableResolver(self._cfg)

    def _get_config(self):
        """Get configuration - helper method"""
        return self._cfg

    # ------------------------------------------------------------------
    # Workspace metadata (teams, states, viewer)
    # ------------------------------------------------------------------

    def _default_team_key(self) -> Optional[str]:
        key = self._linear_cfg.get("team")
        return key.upper() if key else None

    def _viewer(self) -> Dict[str, Any]:
        if self._viewer_cache is None:
            self._viewer_cache = self.client.query(
                "query { viewer { id name displayName email } }"
            )["viewer"]
        return self._viewer_cache

    def _teams(self) -> List[Dict[str, Any]]:
        """All teams with their workflow states, cached for an hour."""
        now = time.time()
        if (self._teams_cache is not None and self._teams_cache_time
                and now - self._teams_cache_time < self._teams_cache_ttl):
            return self._teams_cache

        teams: List[Dict[str, Any]] = []
        cursor = None
        while True:
            data = self.client.query("""
                query($first: Int!, $after: String, $states: Int!) {
                  teams(first: $first, after: $after) {
                    nodes {
                      id
                      key
                      name
                      states(first: $states) {
                        nodes { id name type position }
                        pageInfo { hasNextPage endCursor }
                      }
                    }
                    pageInfo { hasNextPage endCursor }
                  }
                }
            """, {"first": TEAMS_PAGE_SIZE, "after": cursor, "states": STATES_PAGE_SIZE})
            page = data["teams"]
            teams.extend(page["nodes"])
            if not page["pageInfo"]["hasNextPage"]:
                break
            cursor = page["pageInfo"]["endCursor"]

        for team in teams:
            self._fill_states(team)

        self._teams_cache = teams
        self._teams_cache_time = now
        return self._teams_cache

    def _fill_states(self, team: Dict[str, Any]) -> None:
        """Pull the rest of a team's states when they didn't fit one page,
        then drop the pageInfo so callers see the same shape as before."""
        page_info = team["states"].get("pageInfo") or {}
        while page_info.get("hasNextPage"):
            data = self.client.query("""
                query($id: String!, $first: Int!, $after: String) {
                  team(id: $id) {
                    states(first: $first, after: $after) {
                      nodes { id name type position }
                      pageInfo { hasNextPage endCursor }
                    }
                  }
                }
            """, {"id": team["id"], "first": STATES_PAGE_SIZE, "after": page_info.get("endCursor")})
            page = data["team"]["states"]
            team["states"]["nodes"].extend(page["nodes"])
            page_info = page["pageInfo"]
        team["states"].pop("pageInfo", None)

    def _team(self, team_key: Optional[str] = None) -> Dict[str, Any]:
        """Resolve a team by key (or the configured default). Raises rather
        than guessing when the workspace has more than one team."""
        team_key = (team_key or self._default_team_key() or "").upper()
        teams = self._teams()

        if team_key:
            for team in teams:
                if team["key"].upper() == team_key:
                    return team
            available = ", ".join(t["key"] for t in teams)
            raise LinearError(f"No Linear team with key '{team_key}'. Available teams: {available}")

        if len(teams) == 1:
            return teams[0]
        if not teams:
            raise LinearError("Your Linear account has no teams")

        available = ", ".join(t["key"] for t in teams)
        raise LinearError(
            f"No default Linear team configured and this workspace has several ({available}). "
            "Set one with: wf config set linear.team <KEY>"
        )

    def _states(self, team_key: Optional[str] = None) -> List[Dict[str, Any]]:
        states = self._team(team_key)["states"]["nodes"]
        return sorted(states, key=lambda s: s.get("position") or 0)

    def _resolve_state(self, target: str, team_key: Optional[str] = None) -> Dict[str, Any]:
        """Pick the workflow state for one of 'in_progress' / 'review' /
        'done'. Config wins, then name heuristics, then the state type."""
        states = self._states(team_key)

        configured = (self._linear_cfg.get("states") or {}).get(target)
        if configured:
            match = self._find_state_by_name(states, [configured.lower()])
            if match:
                return match
            typer.echo(f"⚠️  Configured Linear state '{configured}' for '{target}' not found; falling back to auto-detection")

        candidates = {
            "in_progress": IN_PROGRESS_STATE_NAMES,
            "review": REVIEW_STATE_NAMES,
            "done": DONE_STATE_NAMES,
        }[target]
        match = self._find_state_by_name(states, candidates)
        if match:
            return match

        # Fall back to the coarse state type. "review" has no type of its
        # own in Linear -- it's just another started state -- so it lands on
        # in_progress, which at least never moves the issue backwards.
        fallback_types = {"in_progress": ("started",), "review": ("started",), "done": DONE_STATE_TYPES}[target]
        for state in states:
            if state.get("type") in fallback_types:
                return state

        available = ", ".join(s["name"] for s in states)
        raise LinearError(
            f"No Linear workflow state found for '{target}'. Available states: {available}. "
            f"Set one explicitly with: wf config set linear.states.{target} '<State Name>'"
        )

    @staticmethod
    def _find_state_by_name(states, names) -> Optional[Dict[str, Any]]:
        lowered = [(s, (s.get("name") or "").lower()) for s in states]
        for name in names:
            for state, state_name in lowered:
                if state_name == name:
                    return state
        for name in names:
            for state, state_name in lowered:
                if name in state_name:
                    return state
        return None

    def _labels(self, team_key: Optional[str] = None) -> List[Dict[str, Any]]:
        """Labels usable on this team's issues: the team's own labels plus
        the workspace-level ones (which have no team)."""
        team = self._team(team_key)
        data = self.client.query("""
            query($teamId: String!) {
              team(id: $teamId) { labels(first: 250) { nodes { id name } } }
              issueLabels(first: 250, filter: { team: { null: true } }) { nodes { id name } }
            }
        """, {"teamId": team["id"]})
        labels = list(data["team"]["labels"]["nodes"])
        labels.extend(data["issueLabels"]["nodes"])
        return labels

    def _label_ids(self, names: List[str], team_key: Optional[str] = None) -> List[str]:
        """Map label names to IDs, warning about (but not creating) misses."""
        if not names:
            return []
        available = self._labels(team_key)
        by_name = {l["name"].lower(): l["id"] for l in available}
        ids, missing = [], []
        for name in names:
            label_id = by_name.get(name.strip().lower())
            if label_id:
                ids.append(label_id)
            else:
                missing.append(name)
        if missing:
            typer.echo(f"⚠️  Skipping unknown Linear label(s): {', '.join(missing)}")
        return ids

    # ------------------------------------------------------------------
    # Issue reads
    # ------------------------------------------------------------------

    def _fetch_issue(self, key: str) -> Dict[str, Any]:
        """Fetch one issue node. Linear's `issue(id:)` accepts either the
        human identifier (ENG-123) or the UUID."""
        data = self.client.query(
            "query($id: String!) { issue(id: $id) { %s } }" % ISSUE_FIELDS,
            {"id": key},
        )
        node = data.get("issue")
        if not node:
            raise Exception(f"Issue '{key}' does not exist or you do not have permission to see it.")
        return node

    def _issue_uuid(self, key: str) -> str:
        """Resolve an identifier to the UUID that mutations require."""
        if _looks_like_uuid(key):
            return key
        node = self._assigned_nodes.get(key.upper())
        if node:
            return node["id"]
        return self._fetch_issue(key)["id"]

    def get(self, key):
        try:
            node = self._fetch_issue(key)
        except LinearError as e:
            raise Exception(f"Failed to get issue '{key}': {e}")
        return LinearTask(node).to_issue()

    def get_issue(self, key):
        """Alias kept for callers that use the Jira-era name."""
        return self.get(key)

    def _is_valid_ticket_key(self, text):
        """Check if text follows Linear's identifier format (TEAM-123)"""
        return bool(re.match(r'^[A-Z][A-Z0-9]*-\d+$', text.strip().upper()))

    def get_or_create(self, q):
        """Get existing issue or create new one if not found or if input is free text"""
        if not self._is_valid_ticket_key(q):
            typer.echo(f"🎯 Creating new ticket from text: '{q}'")
            return self._create_issue_from_text(q)

        try:
            return self.get(q)
        except Exception as e:
            error_msg = str(e)
            if "does not exist" in error_msg or "not found" in error_msg.lower():
                typer.echo(f"❌ Ticket '{q}' not found. Creating new ticket...")
                return self._create_issue_from_text(q, use_as_key=True)
            typer.echo(f"❌ Error accessing issue '{q}': {error_msg}")
            return None

    def _create_issue_from_text(self, text, use_as_key=False):
        """Create a new issue from free text automatically"""
        text = text.strip().strip('"\'')

        if use_as_key and self._is_valid_ticket_key(text):
            # Linear assigns identifiers itself, so a requested key can only
            # be honoured as far as its team prefix.
            summary = text
            team_key = text.split('-')[0].upper()
            typer.echo("ℹ️  Linear assigns issue numbers itself - the new ticket will get the next free number")
        else:
            summary = text
            team_key = self._default_team_key()

        description = f"Auto-created ticket from: '{text}'"
        typer.echo(f"📝 Creating ticket '{summary}' in team {team_key or '(default)'}...")

        new_issue = self.create_issue(summary, description, "Task", team_key)
        if new_issue:
            typer.echo(f"✅ Successfully created ticket {new_issue.key}")
            return new_issue
        typer.echo(f"❌ Failed to create ticket '{summary}'")
        return None

    def _get_cached_assigned_tasks(self):
        """Get assigned tasks with caching"""
        now = time.time()
        if (self._assigned_issues_cache and self._cache_time
                and now - self._cache_time < self._cache_ttl):
            return self._assigned_issues_cache

        data = self.client.query("""
            query {
              viewer {
                assignedIssues(
                  first: 100
                  orderBy: updatedAt
                  filter: { state: { type: { nin: ["completed", "canceled"] } } }
                ) { nodes { %s } }
              }
            }
        """ % ISSUE_FIELDS)
        nodes = data["viewer"]["assignedIssues"]["nodes"]

        # Keep the raw nodes so select_interactively() can show statuses and
        # _issue_uuid() can skip a round trip -- Jira needs a second search
        # for the same information.
        self._assigned_nodes = {n["identifier"]: n for n in nodes}
        self._assigned_issues_cache = [LinearTask(n).to_issue() for n in nodes]
        self._cache_time = now
        return self._assigned_issues_cache

    def get_assigned_tasks(self):
        """Get assigned tasks (used by tab completion)"""
        return self._get_cached_assigned_tasks()

    def list_tasks(self, project_name: Optional[str] = None, status: Optional[str] = None):
        """List tasks from Linear with optional filtering.

        `project_name` accepts either a team key (ENG) or a Linear project
        name, since the CLI's --project flag is shared with Jira where the
        two concepts are one."""
        issue_filter: Dict[str, Any] = {}

        if project_name:
            team_keys = {t["key"].upper() for t in self._teams()}
            if project_name.upper() in team_keys:
                issue_filter["team"] = {"key": {"eq": project_name.upper()}}
            else:
                issue_filter["project"] = {"name": {"eqIgnoreCase": project_name}}

        if status:
            status_mapping = {
                "to do": "Todo", "todo": "Todo", "open": "Todo",
                "in progress": "In Progress",
                "in review": "In Review", "review": "In Review",
                "done": "Done",
            }
            normalized_status = status_mapping.get(status.lower(), status)
            issue_filter["state"] = {"name": {"eqIgnoreCase": normalized_status}}
        elif not project_name:
            # Same default as the Jira backend: my open work.
            issue_filter["assignee"] = {"isMe": {"eq": True}}

        data = self.client.query("""
            query($filter: IssueFilter) {
              issues(first: 100, orderBy: updatedAt, filter: $filter) { nodes { %s } }
            }
        """ % ISSUE_FIELDS, {"filter": issue_filter})

        return [LinearTask(n) for n in data["issues"]["nodes"]]

    def get_projects(self) -> List[str]:
        """Linear project names, for the CLI's project pickers."""
        data = self.client.query("query { projects(first: 250) { nodes { id name } } }")
        return [p["name"] for p in data["projects"]["nodes"]]

    def select_interactively(self, include_others=False):
        from prompt_toolkit import prompt
        from prompt_toolkit.completion import FuzzyWordCompleter

        self._get_cached_assigned_tasks()
        mapping = {
            f"{n['identifier']} [{(n.get('state') or {}).get('name', '?')}] {n['title']}": n
            for n in self._assigned_nodes.values()
        }
        choice = prompt("Task: ", completer=FuzzyWordCompleter(list(mapping.keys())))

        if choice in mapping:
            return LinearTask(mapping[choice]).to_issue()

        typer.echo(f"🎯 '{choice}' not found in existing tasks. Creating new ticket...")
        return self._create_issue_from_text(choice)

    def issue_url(self, key) -> Optional[str]:
        """Web URL for an issue, without a round trip when possible."""
        key = getattr(key, "key", key)
        node = self._assigned_nodes.get(str(key).upper())
        if node and node.get("url"):
            return node["url"]

        workspace = self._linear_cfg.get("workspace")
        if workspace:
            return f"https://linear.app/{workspace}/issue/{key}"
        try:
            return self._fetch_issue(str(key)).get("url")
        except Exception:
            return None

    # ------------------------------------------------------------------
    # Cycles (Linear's answer to sprints)
    # ------------------------------------------------------------------

    def get_active_sprint(self, project_key=None):
        """Return the team's active cycle in the same {id, name, state}
        shape the Jira backend returns a sprint in, so the `active_sprint`
        variables resolve identically on either backend."""
        try:
            team = self._team(project_key)
        except LinearError as e:
            typer.echo(f"⚠️  Could not resolve Linear team for cycle lookup: {e}")
            return None

        now = time.time()
        cached_at = self._active_cycle_cache_time.get(team["id"])
        if cached_at and now - cached_at < self._active_cycle_cache_ttl:
            return self._active_cycle_cache.get(team["id"])

        try:
            data = self.client.query("""
                query($teamId: String!) {
                  team(id: $teamId) {
                    activeCycle { id number name startsAt endsAt }
                  }
                }
            """, {"teamId": team["id"]})
        except LinearError as e:
            typer.echo(f"⚠️  Could not fetch the active Linear cycle: {e}")
            return None

        cycle = (data.get("team") or {}).get("activeCycle")
        result = None
        if cycle:
            result = {
                "id": cycle["id"],
                # Cycles are often unnamed; Linear shows "Cycle 12" for those.
                "name": cycle.get("name") or f"Cycle {cycle.get('number')}",
                "state": "active",
                "number": cycle.get("number"),
                "startDate": cycle.get("startsAt"),
                "endDate": cycle.get("endsAt"),
            }

        self._active_cycle_cache[team["id"]] = result
        self._active_cycle_cache_time[team["id"]] = now
        return result

    # ------------------------------------------------------------------
    # Issue writes
    # ------------------------------------------------------------------

    def create_issue(self, summary, description, issue_type="Task", project_key=None):
        """Create a Linear issue, assigned to the current user and (unless
        disabled) added to the team's active cycle."""
        try:
            team = self._team(project_key)

            issue_input: Dict[str, Any] = {
                "teamId": team["id"],
                "title": summary,
                "description": description or "",
            }

            try:
                issue_input["assigneeId"] = self._viewer()["id"]
            except LinearError as e:
                typer.echo(f"⚠️  Could not auto-assign ticket: {e}")

            # Configured custom_fields defaults, the same way the Jira
            # backend applies them at create time.
            defaults = self._default_fields_for_team(team["key"])
            if defaults:
                typer.echo(f"🎯 Applying default field values: {', '.join(f'{k}={v}' for k, v in defaults.items())}")
                issue_input.update(self._build_field_update(defaults, team["key"]))

            # Jira issue types have no Linear equivalent; attach a matching
            # existing label if there is one.
            if issue_type and issue_type.lower() != "task":
                label_ids = self._label_ids([issue_type], team["key"])
                # Union with any labels a default already contributed rather
                # than clobbering them.
                for label_id in label_ids:
                    issue_input.setdefault("labelIds", [])
                    if label_id not in issue_input["labelIds"]:
                        issue_input["labelIds"].append(label_id)

            if self._cfg.get("auto_assign_sprint", True):
                active_cycle = self.get_active_sprint(team["key"])
                if active_cycle:
                    issue_input["cycleId"] = active_cycle["id"]
                else:
                    typer.echo("⚠️  Could not find an active cycle to assign the ticket to")

            result = self.client.mutate("""
                mutation($input: IssueCreateInput!) {
                  issueCreate(input: $input) { success issue { %s } }
                }
            """ % ISSUE_FIELDS, {"input": issue_input}, "issueCreate")

            task = LinearTask(result["issue"])
            if issue_input.get("assigneeId"):
                typer.echo(f"👤 Assigned ticket {task.key} to current user")
            if issue_input.get("cycleId"):
                typer.echo(f"🏃 Added ticket {task.key} to active cycle")
            return task.to_issue()

        except Exception as e:
            print(f"❌ Failed to create Linear issue: {e}")
            return None

    def _update_issue(self, key: str, update: Dict[str, Any]) -> Dict[str, Any]:
        result = self.client.mutate("""
            mutation($id: String!, $input: IssueUpdateInput!) {
              issueUpdate(id: $id, input: $input) { success issue { %s } }
            }
        """ % ISSUE_FIELDS, {"id": self._issue_uuid(key), "input": update}, "issueUpdate")
        node = result["issue"]
        self._assigned_nodes[node["identifier"]] = node
        return node

    def _transition(self, key, target: str):
        """Move an issue to the state matching one of the logical targets
        'in_progress' / 'review' / 'done'."""
        team_key = key.split("-")[0].upper() if "-" in str(key) else None
        state = self._resolve_state(target, team_key)
        self._update_issue(key, {"stateId": state["id"]})
        return state

    def move_to_in_progress(self, issue, custom_fields=None):
        """Move issue to the in-progress state with optional custom fields"""
        self._transition(issue.key, "in_progress")
        if custom_fields:
            self._apply_custom_fields(issue.key, custom_fields)

    def move_to_review(self, issue):
        """Move an issue to the review state"""
        self._transition(issue.key, "review")

    def move_to_done(self, issue):
        """Move an issue to the done state"""
        self._transition(issue.key, "done")

    def _build_field_update(self, custom_fields, team_key=None) -> Dict[str, Any]:
        """Translate configured field names onto Linear's built-in issue
        attributes, as an IssueUpdateInput/IssueCreateInput fragment.

        Linear has no custom fields, so the names people configure under
        `custom_fields` are mapped onto the attributes it does have; anything
        else is reported and skipped rather than failing the whole write.
        """
        update: Dict[str, Any] = {}

        for name, value in custom_fields.items():
            field = name.strip().lower()

            if field in ("priority", "priority_label"):
                priority = PRIORITY_VALUES.get(str(value).strip().lower())
                if priority is None:
                    typer.echo(f"⚠️  Unknown Linear priority '{value}' (expected: {', '.join(PRIORITY_LABELS.values())})")
                    continue
                update["priority"] = priority

            elif field in ("label", "labels"):
                names = value if isinstance(value, list) else [v for v in str(value).split(",") if v.strip()]
                label_ids = self._label_ids(names, team_key)
                if label_ids:
                    update["labelIds"] = label_ids

            elif field in ("project", "pod"):
                project_id = self._find_project_id(str(value))
                if not project_id:
                    typer.echo(f"⚠️  No Linear project named '{value}'")
                    continue
                update["projectId"] = project_id

            elif field in ("estimate", "points", "story_points"):
                try:
                    update["estimate"] = int(value)
                except (TypeError, ValueError):
                    typer.echo(f"⚠️  Linear estimate must be a number, got '{value}'")

            elif field in ("assignee", "owner"):
                user_id = self._find_user_id(str(value))
                if not user_id:
                    typer.echo(f"⚠️  No Linear user matching '{value}'")
                    continue
                update["assigneeId"] = user_id

            elif field in ("due", "due_date", "duedate"):
                update["dueDate"] = str(value)

            elif field in ("state", "status"):
                match = self._find_state_by_name(self._states(team_key), [str(value).lower()])
                if not match:
                    available = ", ".join(st["name"] for st in self._states(team_key))
                    typer.echo(f"⚠️  No Linear state named '{value}' (available: {available})")
                    continue
                update["stateId"] = match["id"]

            else:
                typer.echo(
                    f"⚠️  Linear has no field '{name}' (supported: priority, labels, project, estimate, assignee, due_date)"
                )

        return update

    def _apply_custom_fields(self, issue_key, custom_fields):
        """Set Linear issue attributes on an existing issue."""
        team_key = issue_key.split("-")[0].upper() if "-" in str(issue_key) else None
        update = self._build_field_update(custom_fields, team_key)
        if not update:
            return

        try:
            self._update_issue(issue_key, update)
            for name, value in custom_fields.items():
                typer.echo(f"✅ Set {name} = {value} for {issue_key}")
        except Exception as e:
            typer.echo(f"❌ Error applying fields: {e}")

    def _default_fields_for_team(self, team_key) -> Dict[str, Any]:
        """Configured `custom_fields` defaults that apply to this team.

        Mirrors JiraBackend._get_default_fields_for_project: a field's
        `project_key` scopes it to one project/team, `default_value` may
        contain `{variables}`, and `value_mapping` translates the
        user-facing value. Values stay as names here -- _build_field_update
        turns them into Linear IDs.
        """
        from workflow.config import get_field_value_mapping

        defaults: Dict[str, Any] = {}
        for field_name, field_config in (self._get_config().get("custom_fields") or {}).items():
            default_value = field_config.get("default_value")
            if not default_value:
                continue

            scope = field_config.get("project_key", "")
            if scope and team_key and scope.upper() != str(team_key).upper():
                continue

            resolved = self._variable_resolver.resolve_variables(
                default_value, {"project_key": team_key}
            )
            defaults[field_name] = get_field_value_mapping(field_name, resolved)

        return defaults

    def _find_project_id(self, name: str) -> Optional[str]:
        data = self.client.query("query { projects(first: 250) { nodes { id name } } }")
        for project in data["projects"]["nodes"]:
            if project["name"].lower() == name.strip().lower():
                return project["id"]
        return None

    def _find_user_id(self, name: str) -> Optional[str]:
        if name.strip().lower() in ("me", "self", "currentuser", "current_user"):
            return self._viewer()["id"]
        data = self.client.query("query { users(first: 250) { nodes { id name displayName email } } }")
        needle = name.strip().lower()
        for user in data["users"]["nodes"]:
            candidates = {(user.get(f) or "").lower() for f in ("name", "displayName", "email")}
            if needle in candidates:
                return user["id"]
        return None

    # ------------------------------------------------------------------
    # Status checks
    # ------------------------------------------------------------------

    def is_done(self, issue):
        """Check if an issue is in a done/completed state"""
        try:
            state = (self._fetch_issue(issue.key).get("state") or {})
            if state.get("type") in DONE_STATE_TYPES:
                return True
            return (state.get("name") or "").lower() in DONE_STATE_NAMES
        except Exception as e:
            print(f"❌ Failed to check status for {issue.key}: {e}")
            return False

    def is_in_review(self, issue):
        """Check if an issue is in a review state"""
        try:
            name = ((self._fetch_issue(issue.key).get("state") or {}).get("name") or "").lower()
            return "review" in name
        except Exception as e:
            print(f"❌ Failed to check status for {issue.key}: {e}")
            return False

    def get_workflow(self, project_name: Optional[str] = None) -> Dict[str, List[str]]:
        """Linear allows any state-to-state move, so every state can reach
        every other one."""
        names = [s["name"] for s in self._states(project_name)]
        return {name: [other for other in names if other != name] for name in names}

    def get_available_transitions(self, key: str) -> List[str]:
        """Available target states for an issue"""
        team_key = key.split("-")[0].upper() if "-" in str(key) else None
        try:
            current = (self._fetch_issue(key).get("state") or {}).get("name")
        except Exception:
            current = None
        return [s["name"] for s in self._states(team_key) if s["name"] != current]

    # ------------------------------------------------------------------
    # Fields (Linear has none; expose its built-ins under the same API)
    # ------------------------------------------------------------------

    def _get_fields(self) -> Dict[str, Dict[str, Any]]:
        """Linear's built-in issue attributes, keyed the way JiraBackend
        keys Jira's field catalogue (lowercased name -> metadata)."""
        now = time.time()
        if (self._fields_cache is not None and self._fields_cache_time
                and now - self._fields_cache_time < self._fields_cache_ttl):
            return self._fields_cache

        fields = {
            "priority": {"id": "priority", "name": "priority", "schema": {"type": "option"}},
            "labels": {"id": "labelIds", "name": "labels", "schema": {"type": "array"}},
            "project": {"id": "projectId", "name": "project", "schema": {"type": "option"}},
            "estimate": {"id": "estimate", "name": "estimate", "schema": {"type": "number"}},
            "assignee": {"id": "assigneeId", "name": "assignee", "schema": {"type": "user"}},
            "due_date": {"id": "dueDate", "name": "due_date", "schema": {"type": "date"}},
            "state": {"id": "stateId", "name": "state", "schema": {"type": "option"}},
        }
        self._fields_cache = fields
        self._fields_cache_time = now
        return fields

    def find_field_by_name(self, field_name) -> Optional[Dict[str, Any]]:
        return self._get_fields().get(str(field_name).strip().lower())

    def search_fields(self, query: str = "") -> Dict[str, Dict[str, Any]]:
        needle = (query or "").lower()
        return {
            info["name"]: info
            for name, info in self._get_fields().items()
            if needle in name
        }

    def get_field_metadata(self, field_id: str) -> Optional[Dict[str, Any]]:
        for info in self._get_fields().values():
            if info["id"] == field_id or info["name"] == field_id:
                return info
        return None

    def get_field_options(self, field_id: str) -> List[Dict[str, Any]]:
        """Allowed values for the fields that have a fixed set."""
        field = (field_id or "").lower()
        if field in ("priority", "priority_label"):
            return [{"id": str(v), "value": label} for v, label in sorted(PRIORITY_LABELS.items())]
        if field in ("labels", "labelids", "label"):
            return [{"id": l["id"], "value": l["name"]} for l in self._labels()]
        if field in ("state", "stateid", "status"):
            return [{"id": s["id"], "value": s["name"]} for s in self._states()]
        if field in ("project", "projectid"):
            data = self.client.query("query { projects(first: 250) { nodes { id name } } }")
            return [{"id": p["id"], "value": p["name"]} for p in data["projects"]["nodes"]]
        return []

    def _process_field_value(self, value, field_type, field_schema, field_id=None):
        """Linear's GraphQL input types are already typed, so values pass
        through unchanged; _apply_custom_fields does the real conversion."""
        return value

    # ------------------------------------------------------------------
    # Comments
    # ------------------------------------------------------------------

    def get_comments(self, key: str) -> List[Dict[str, Any]]:
        """Get all comments for an issue.

        `id` is a 1-based ordinal rather than Linear's UUID, so that
        `wf task comment edit/delete KEY <n>` -- which takes an int --
        works the same as it does on the other backends. The UUID is kept
        alongside it as `linear_id`.
        """
        try:
            data = self.client.query("""
                query($id: String!) {
                  issue(id: $id) {
                    comments(first: 100) {
                      nodes { id body createdAt updatedAt user { name displayName } }
                    }
                  }
                }
            """, {"id": key})
            issue = data.get("issue")
            if not issue:
                print(f"❌ Failed to get comments for {key}: issue not found")
                return []

            result = []
            for i, comment in enumerate(issue["comments"]["nodes"], 1):
                user = comment.get("user") or {}
                result.append({
                    'id': i,
                    'linear_id': comment['id'],
                    'author': user.get('displayName') or user.get('name') or 'unknown',
                    'text': comment.get('body') or '',
                    'created': comment.get('createdAt'),
                    'updated': comment.get('updatedAt'),
                })
            return result
        except Exception as e:
            print(f"❌ Failed to get comments for {key}: {e}")
            return []

    def _resolve_comment_id(self, key: str, comment_id) -> Optional[str]:
        """Turn an ordinal (or a raw UUID) into Linear's comment UUID."""
        if _looks_like_uuid(str(comment_id)):
            return str(comment_id)
        comments = self.get_comments(key)
        for comment in comments:
            if str(comment['id']) == str(comment_id):
                return comment['linear_id']
        return None

    def add_comment(self, key: str, text: str, author: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """Add a comment to an issue.

        `author` is accepted for API compatibility but ignored: Linear
        attributes the comment to the API key's own user.
        """
        try:
            result = self.client.mutate("""
                mutation($input: CommentCreateInput!) {
                  commentCreate(input: $input) {
                    success
                    comment { id body createdAt updatedAt user { name displayName } }
                  }
                }
            """, {"input": {"issueId": self._issue_uuid(key), "body": text}}, "commentCreate")

            comment = result["comment"]
            user = comment.get("user") or {}
            return {
                'id': len(self.get_comments(key)),
                'linear_id': comment['id'],
                'author': user.get('displayName') or user.get('name') or 'unknown',
                'text': comment.get('body') or '',
                'created': comment.get('createdAt'),
                'updated': comment.get('updatedAt'),
            }
        except Exception as e:
            print(f"❌ Failed to add comment to {key}: {e}")
            return None

    def update_comment(self, key: str, comment_id: int, text: str) -> bool:
        """Update a comment on an issue"""
        try:
            linear_id = self._resolve_comment_id(key, comment_id)
            if not linear_id:
                print(f"❌ No comment #{comment_id} on {key}")
                return False
            self.client.mutate("""
                mutation($id: String!, $input: CommentUpdateInput!) {
                  commentUpdate(id: $id, input: $input) { success }
                }
            """, {"id": linear_id, "input": {"body": text}}, "commentUpdate")
            return True
        except Exception as e:
            print(f"❌ Failed to update comment #{comment_id} on {key}: {e}")
            return False

    def delete_comment(self, key: str, comment_id: int) -> bool:
        """Delete a comment from an issue"""
        try:
            linear_id = self._resolve_comment_id(key, comment_id)
            if not linear_id:
                print(f"❌ No comment #{comment_id} on {key}")
                return False
            self.client.mutate(
                "mutation($id: String!) { commentDelete(id: $id) { success } }",
                {"id": linear_id}, "commentDelete",
            )
            return True
        except Exception as e:
            print(f"❌ Failed to delete comment #{comment_id} from {key}: {e}")
            return False


def _looks_like_uuid(value: str) -> bool:
    return bool(re.fullmatch(r'[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}', value.strip()))
