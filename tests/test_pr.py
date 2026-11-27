"""wf pr: pull requests and CI as an agent asks about them."""
import json
from types import SimpleNamespace

import pytest

from workflow import pr as prs
from workflow.pr import PrError

JOB = "https://github.com/acme/app/actions/runs/900/job/{}"


def check(name, conclusion="SUCCESS", status="COMPLETED", job=1, workflow="CI"):
    return {"__typename": "CheckRun", "name": name, "workflowName": workflow, "status": status,
            "conclusion": conclusion, "detailsUrl": JOB.format(job)}


def raw_pr(**over):
    return {
        "number": 12, "title": "PERSONAL-7 Fix the thing", "url": "https://github.com/acme/app/pull/12",
        "state": "OPEN", "isDraft": False, "author": {"login": "sam"}, "baseRefName": "stage",
        "headRefName": "personal-7-Fix-the-thing", "headRefOid": "a" * 40, "mergeable": "MERGEABLE",
        "mergeStateStatus": "CLEAN", "reviewDecision": "", "mergedAt": None, "mergeCommit": None,
        "additions": 10, "deletions": 2, "changedFiles": 3, "updatedAt": "2026-11-27T00:00:00Z",
        "statusCheckRollup": [check("unit")],
        **over,
    }


class Gh:
    """Stands in for `gh`: answers by the first words of the command."""

    def __init__(self, answers):
        self.answers = answers
        self.calls = []

    def __call__(self, args, cwd=None, timeout=60):
        self.calls.append(args)
        for prefix, answer in self.answers.items():
            if args[:len(prefix.split())] == prefix.split():
                if isinstance(answer, Exception):
                    raise answer
                if callable(answer):
                    answer = answer(args)
                return answer if isinstance(answer, str) else json.dumps(answer)
        if args[0] == "api" and args[-1].endswith("/annotations"):
            raise PrError("HTTP 404")  # a job that annotated nothing
        raise AssertionError(f"unexpected gh {' '.join(args)}")


def task(key="PERSONAL-7", **over):
    return SimpleNamespace(**{"key": key, "title": "Fix the thing", "status": "In Review",
                              "description": "", "file_path": "/tasks/personal-7.md", **over})


def test_the_task_key_comes_from_the_branch_or_else_the_title():
    assert prs.task_key("personal-12-fix-it") == "PERSONAL-12"
    assert prs.task_key("sam/pan-1187-arm") == "PAN-1187"
    assert prs.task_key("fix-the-thing", "PAN-3 Fix the thing") == "PAN-3"
    assert prs.task_key("main", "Fix the thing") is None
    assert prs.task_key(None, None) is None


def test_a_summary_names_the_task_and_the_note_block_it_was_cut_from():
    described = task(description="Task: docId abc123, blockId XyZ_9. Keep its status current")
    out = prs.pr_summary("/repo", "12", run=Gh({"pr view": raw_pr()}), lookup=lambda key, path: described)
    assert out["task"] == {
        "key": "PERSONAL-7", "found": True, "title": "Fix the thing", "status": "In Review",
        "file": "/tasks/personal-7.md", "note": {"docId": "abc123", "blockId": "XyZ_9"}}
    assert out["verdict"] == "ready to merge"
    assert out["size"] == {"files": 3, "additions": 10, "deletions": 2}
    text = prs.format_summary(out)
    assert "task: PERSONAL-7 [In Review] Fix the thing" in text
    assert "note abc123, block XyZ_9" in text


def test_a_task_the_tracker_lacks_or_cannot_be_asked_about_is_still_named():
    gh = Gh({"pr view": raw_pr()})
    assert prs.pr_summary("/repo", run=gh, lookup=lambda k, p: None)["task"] == \
        {"key": "PERSONAL-7", "found": False}

    def broken(key, path):
        raise RuntimeError("tracker is down")
    assert prs.pr_summary("/repo", run=gh, lookup=broken)["task"] == {"key": "PERSONAL-7", "found": False}
    no_key = Gh({"pr view": raw_pr(headRefName="fix-typo", title="Fix a typo")})
    assert prs.pr_summary("/repo", run=no_key, lookup=broken)["task"] is None


@pytest.mark.parametrize("over, expected", [
    ({}, "ready to merge"),
    ({"state": "MERGED", "mergeCommit": {"oid": "abcdef1234567890"}}, "merged as abcdef12"),
    ({"state": "CLOSED"}, "closed without merging"),
    ({"mergeable": "CONFLICTING"}, "conflicts with stage"),
    ({"isDraft": True}, "not ready: a draft"),
    ({"reviewDecision": "CHANGES_REQUESTED"}, "not ready: changes requested"),
    ({"reviewDecision": "REVIEW_REQUIRED"}, "not ready: review required"),
    ({"statusCheckRollup": [check("unit", "FAILURE"), check("lint", "FAILURE")]},
     "not ready: 2 checks failing"),
    ({"statusCheckRollup": [check("unit", "", "IN_PROGRESS")]}, "not ready: 1 check still running"),
    ({"isDraft": True, "statusCheckRollup": [check("unit", "FAILURE")]},
     "not ready: a draft, 1 check failing"),
    ({"mergeable": "UNKNOWN"}, "GitHub is still working out whether it merges cleanly"),
])
def test_the_verdict_is_the_first_thing_in_the_way(over, expected):
    out = prs.pr_summary("/repo", run=Gh({"pr view": raw_pr(**over)}), lookup=lambda k, p: None)
    assert out["verdict"] == expected


def test_checks_are_counted_and_the_failing_and_running_ones_named():
    out = prs.summarize_checks([
        check("unit"), check("lint", "FAILURE", job=22), check("e2e", "", "IN_PROGRESS", job=33),
        check("docs", "SKIPPED"), check("old", "CANCELLED", job=44),
        {"__typename": "StatusContext", "context": "ci/legacy", "state": "SUCCESS", "targetUrl": "https://ci/1"},
        {"__typename": "StatusContext", "context": "ci/slow", "state": "PENDING", "targetUrl": "https://ci/2"},
    ])
    assert (out["state"], out["passed"], out["failed"], out["pending"], out["skipped"]) == \
        ("failing", 2, 2, 2, 1)
    assert [(c["name"], c["conclusion"], c["run_id"], c["job_id"]) for c in out["failing"]] == \
        [("lint", "failure", 900, 22), ("old", "cancelled", 900, 44)]
    assert [c["name"] for c in out["pending_checks"]] == ["e2e", "ci/slow"]
    assert prs.summarize_checks([])["state"] == "none"
    assert prs.summarize_checks([check("unit")])["state"] == "passing"


def test_a_task_key_finds_its_pull_request():
    gh = Gh({
        "pr list": [{"number": 9, "title": "Mentions PERSONAL-7 in passing", "headRefName": "personal-3-other"},
                    {"number": 12, "title": "PERSONAL-7 Fix the thing", "headRefName": "personal-7-Fix"}],
        "pr view": raw_pr(),
    })
    prs.pr_summary("/repo", "personal-7", run=gh, lookup=lambda k, p: None)
    assert gh.calls[0][:5] == ["pr", "list", "--state", "all", "--search"]
    assert gh.calls[1][:3] == ["pr", "view", "12"]

    with pytest.raises(PrError, match="No pull request for PERSONAL-8"):
        prs.pr_summary("/repo", "PERSONAL-8", run=Gh({"pr list": []}))


def test_a_number_a_branch_and_the_current_branch_go_to_gh_as_they_are():
    for given, expected in (("12", ["pr", "view", "12", "--json"]), ("my-branch", ["pr", "view", "my-branch", "--json"]),
                            (None, ["pr", "view", "--json"])):
        gh = Gh({"pr view": raw_pr()})
        prs.pr_summary("/repo", given, repo="acme/app", run=gh, lookup=lambda k, p: None)
        assert gh.calls[0][:len(expected)] == expected
        assert gh.calls[0][-2:] == ["--repo", "acme/app"]


def test_failing_checks_come_with_the_step_that_failed_and_the_end_of_the_log():
    gh = Gh({
        "pr view": raw_pr(statusCheckRollup=[check("unit"), check("lint", "FAILURE", job=22)]),
        "run view 900": {"jobs": [{"databaseId": 22, "name": "lint", "status": "completed",
                                   "conclusion": "failure",
                                   "steps": [{"name": "Checkout", "conclusion": "success"},
                                             {"name": "Run actionlint", "conclusion": "failure"}]}]},
        "run view --job": "lint\tRun actionlint\t2026-11-27T19:20:00.1234567Z build.yml:249: use find\n"
                          "lint\tRun actionlint\t2026-11-27T19:20:01.1234567Z ##[error]exit code 1\n",
    })
    out = prs.pr_checks("/repo", "12", run=gh)
    (lint,) = out["failing"]
    assert lint["failed_steps"] == ["Run actionlint"]
    assert lint["log_tail"] == "build.yml:249: use find\n##[error]exit code 1"
    text = prs.format_checks(out)
    assert "✗ lint (CI): failure" in text and "failed at: Run actionlint" in text
    assert "    build.yml:249: use find" in text

    quiet = Gh({"pr view": raw_pr(statusCheckRollup=[check("lint", "FAILURE", job=22)])})
    assert "log_tail" not in prs.pr_checks("/repo", "12", logs=False, run=quiet)["failing"][0]
    assert len(quiet.calls) == 1


def test_a_job_that_failed_in_a_run_still_going_is_read_through_the_api():
    gh = Gh({
        "pr view": raw_pr(statusCheckRollup=[check("lint", "FAILURE", job=22)]),
        "run view 900": {"jobs": []},
        "run view --job": PrError("run 900 is still in progress; logs will be available when it is complete"),
        "api --allow-escape-sequences repos/acme/app/actions/jobs/22/logs":
            "2026-11-27T19:20:00.1Z setting up\n"
            "2026-11-27T19:20:01.1Z \x1b[31mbuild.yml:249: use find\x1b[0m\n"
            "2026-11-27T19:20:02.1Z ##[error]Process completed with exit code 1.\n"
            "2026-11-27T19:20:03.1Z Post job cleanup.\n",
    })
    log = prs.pr_checks("/repo", "12", run=gh)["failing"][0]["log_tail"]
    assert log == "setting up\nbuild.yml:249: use find\n##[error]Process completed with exit code 1."

    neither = Gh({
        "pr view": raw_pr(statusCheckRollup=[check("lint", "FAILURE", job=22)]),
        "run view 900": PrError("no such run"),
        "run view --job": PrError("still in progress"),
        "api": PrError("HTTP 404"),
    })
    assert prs.pr_checks("/repo", "12", run=neither)["failing"][0]["log_tail"] == \
        "(log not available: still in progress)"


def test_a_failing_check_names_the_files_and_lines_it_pointed_at():
    gh = Gh({
        "pr view": raw_pr(statusCheckRollup=[check("lint", "FAILURE", job=22)]),
        "run view 900": {"jobs": []},
        "run view --job": "j\ts\tFound 0 warnings and 1 error.\n",
        "api repos/acme/app/check-runs/22/annotations": [
            {"path": ".github", "start_line": 22, "annotation_level": "failure",
             "message": "Process completed with exit code 1."},
            {"path": "src/chat.ts", "start_line": 1, "annotation_level": "failure",
             "message": "Run autofix to sort these imports!"},
            {"path": ".github", "start_line": 26, "annotation_level": "warning", "message": "Restore cache failed"},
        ],
    })
    out = prs.pr_checks("/repo", "12", run=gh)
    assert out["failing"][0]["annotations"] == [
        {"path": "src/chat.ts", "line": 1, "message": "Run autofix to sort these imports!"}]
    assert "  src/chat.ts:1: Run autofix to sort these imports!" in prs.format_checks(out)


def test_only_the_first_few_failing_jobs_have_their_logs_fetched():
    failing = [check(f"job-{n}", "FAILURE", job=n) for n in range(1, 7)]
    gh = Gh({"pr view": raw_pr(statusCheckRollup=failing), "run view 900": {"jobs": []},
             "run view --job": "j\ts\tboom\n"})
    out = prs.pr_checks("/repo", "12", run=gh)
    assert [("log_tail" in c) for c in out["failing"]] == [True] * 3 + [False] * 3


def test_a_long_log_keeps_its_end():
    gh = Gh({"pr view": raw_pr(statusCheckRollup=[check("lint", "FAILURE", job=22)]),
             "run view 900": {"jobs": []},
             "run view --job": "".join(f"j\ts\tline {n}\n" for n in range(500))})
    log = prs.pr_checks("/repo", "12", tail=60, run=gh)["failing"][0]["log_tail"]
    assert log.startswith("… ") and log.endswith("line 499") and len(log) <= 62


def test_runs_are_listed_narrowed_to_a_branch_workflow_or_commit():
    gh = Gh({"run list": [{"databaseId": 900, "workflowName": "Deploy prod", "displayTitle": "Release",
                           "status": "completed", "conclusion": "success", "event": "push",
                           "headBranch": "main", "headSha": "f" * 40, "createdAt": "2026-11-27T19:13:36Z",
                           "url": "https://github.com/acme/app/actions/runs/900"}]})
    rows = prs.ci_runs("/repo", branch="main", workflow="deploy.yml", commit="abc", limit=3, run=gh)
    args = gh.calls[0]
    for flag, value in (("--branch", "main"), ("--workflow", "deploy.yml"), ("--commit", "abc"), ("--limit", "3")):
        assert args[args.index(flag) + 1] == value
    assert rows[0]["id"] == 900 and rows[0]["conclusion"] == "success"
    assert prs.format_runs(rows) == "✓ 900 Deploy prod · main ffffffff · success · 2026-11-27T19:13:36Z"
    assert prs.format_runs([]) == "No workflow runs."


def test_one_run_shows_its_jobs_and_why_the_failed_ones_failed():
    gh = Gh({
        "run view 900": {
            "databaseId": 900, "workflowName": "Build APK", "displayTitle": "Release", "status": "completed",
            "conclusion": "failure", "event": "push", "headBranch": "main", "headSha": "f" * 40,
            "createdAt": "t", "url": "https://github.com/acme/app/actions/runs/900", "attempt": 2,
            "jobs": [
                {"databaseId": 1, "name": "changes", "status": "completed", "conclusion": "success", "steps": []},
                {"databaseId": 2, "name": "build", "status": "completed", "conclusion": "failure",
                 "url": JOB.format(2), "steps": [{"name": "Build", "conclusion": "failure"}]},
            ]},
        "run view --job": "build\tBuild\tCould not resolve plugin\n",
    })
    out = prs.ci_run(900, "/repo", run=gh)
    assert [j["name"] for j in out["jobs"]] == ["changes", "build"]
    assert out["jobs"][1]["failed_steps"] == ["Build"]
    assert out["jobs"][1]["log_tail"] == "Could not resolve plugin"
    assert "log_tail" not in out["jobs"][0]
    text = prs.format_run(out)
    assert "✗ 900 Build APK: failure (attempt 2)" in text
    assert "failed at: Build" in text and "Could not resolve plugin" in text


def test_a_review_gathers_the_task_the_description_the_discussion_and_the_diff():
    gh = Gh({
        "pr view": raw_pr(
            body="**Problem:** it broke.",
            files=[{"path": "a.py", "additions": 9, "deletions": 1}],
            comments=[{"author": {"login": "kim"}, "createdAt": "2026-11-27T10:00:00Z", "body": "Why not b?"}],
            reviews=[{"author": {"login": "lee"}, "submittedAt": "2026-11-27T09:00:00Z",
                      "state": "CHANGES_REQUESTED", "body": "Needs a test"},
                     {"author": {"login": "bot"}, "submittedAt": "2026-11-27T09:30:00Z",
                      "state": "COMMENTED", "body": ""}]),
        "pr diff": "diff --git a/a.py b/a.py\n+new line\n",
    })
    out = prs.pr_review("/repo", "12", run=gh,
                        lookup=lambda k, p: task(description="Make the thing work."))
    assert out["task"]["description"] == "Make the thing work."
    assert out["body"] == "**Problem:** it broke."
    # In the order they were said; a review with nothing in it is left out.
    assert [(d["kind"], d["author"]) for d in out["discussion"]] == [("review", "lee"), ("comment", "kim")]
    assert out["files"] == [{"path": "a.py", "additions": 9, "deletions": 1}]
    assert out["diff"].startswith("diff --git") and out["diff_truncated"] is False
    text = prs.format_review(out)
    for part in ("## Task PERSONAL-7", "Make the thing work.", "## Description", "- lee (changes requested)",
                 "    Needs a test", "- a.py (+9 -1)", "## Diff\ndiff --git"):
        assert part in text


def test_a_huge_diff_is_cut_and_says_so():
    gh = Gh({"pr view": raw_pr(body="", files=[], comments=[], reviews=[]), "pr diff": "x" * 500})
    out = prs.pr_review("/repo", "12", diff_limit=100, run=gh, lookup=lambda k, p: None)
    assert len(out["diff"]) == 100 and out["diff_truncated"] is True
    assert "## Diff (first 100 characters; the rest is cut)" in prs.format_review(out)


def test_the_list_marks_each_pull_request_and_names_its_task():
    gh = Gh({"pr list": [
        raw_pr(),
        raw_pr(number=13, title="Fix a typo", headRefName="fix-typo", isDraft=True,
               mergeable="CONFLICTING", statusCheckRollup=[check("unit", "FAILURE")]),
    ]})
    rows = prs.pr_list("/repo", mine=True, search="typo", run=gh, lookup=lambda k, p: task())
    args = gh.calls[0]
    assert args[args.index("--author") + 1] == "@me" and args[args.index("--search") + 1] == "typo"
    assert [r["task"] and r["task"]["key"] for r in rows] == ["PERSONAL-7", None]
    text = prs.format_list(rows)
    assert "✓ #12 PERSONAL-7 Fix the thing" in text
    assert "✗ #13 Fix a typo [draft] [conflicts]" in text
    assert "task: PERSONAL-7 [In Review] Fix the thing" in text
    assert prs.format_list([]) == "No pull requests."

    keys_only = prs.pr_list("/repo", tasks=False, run=Gh({"pr list": [raw_pr()]}))
    assert keys_only[0]["task"] == {"key": "PERSONAL-7"}


def test_what_gh_complains_of_is_what_the_caller_is_told(monkeypatch):
    def failing(*args, **kwargs):
        return SimpleNamespace(returncode=1, stdout="", stderr="no pull requests found for branch \"x\"\n")
    monkeypatch.setattr(prs.subprocess, "run", failing)
    with pytest.raises(PrError, match="no pull requests found"):
        prs.gh(["pr", "view"])

    def missing(*args, **kwargs):
        raise FileNotFoundError()
    monkeypatch.setattr(prs.subprocess, "run", missing)
    with pytest.raises(PrError, match="not installed"):
        prs.gh(["pr", "view"])


def test_the_mcp_tools_answer_in_json_or_with_the_error(monkeypatch):
    from workflow import mcp as wf_mcp

    monkeypatch.setattr(prs, "gh", Gh({"pr view": raw_pr()}))
    monkeypatch.setattr(prs, "find_task", lambda key, path: None)

    def call(tool, **kwargs):
        return getattr(tool, "fn", tool)(**kwargs)

    # The tools take workflow.pr's defaults, so patch those the functions hold.
    monkeypatch.setattr(prs.pr_summary, "__defaults__", (None, None, None, prs.gh, None))
    out = json.loads(call(wf_mcp.pr_status, repo_path="/repo", pr="12"))
    assert out["number"] == 12 and out["verdict"] == "ready to merge"

    def refuse(**kwargs):
        raise PrError("no pull requests found")
    monkeypatch.setattr(prs, "pr_checks", refuse)
    assert call(wf_mcp.pr_checks, repo_path="/repo") == "Error: no pull requests found"


def test_agent_runs_may_use_the_readers_without_asking():
    from workflow.deploy.jobs import WF_TOOLS_ALLOWED
    for name in ("pr_status", "pr_list", "pr_checks", "ci_runs", "ci_run", "pr_review_context"):
        assert f"mcp__wf-tools__{name}" in WF_TOOLS_ALLOWED
