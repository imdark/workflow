import io
import json
import subprocess
import sys

from workflow.deploy.fetch_guard import decision, failures_by_site, main, site


def fetch(id_, url):
    return {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": id_, "name": "WebFetch", "input": {"url": url, "prompt": "Price?"}}]}}


def result(id_, error, text="timeout of 60000ms exceeded"):
    return {"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": id_, "is_error": error, "content": text}]}}


def transcript(tmp_path, entries):
    path = tmp_path / "session.jsonl"
    path.write_text("\n".join(json.dumps(e) for e in entries) + "\nnot json\n")
    return str(path)


def ask(path, url, tool="WebFetch"):
    return {"hook_event_name": "PreToolUse", "tool_name": tool,
            "tool_input": {"url": url}, "transcript_path": path}


def test_site_drops_www_and_case():
    assert site("https://WWW.HP.com/us-en/shop/pdp/x") == "hp.com"
    assert site("not a url") == ""


def test_only_failed_fetches_count_per_site():
    lines = [json.dumps(e) for e in (
        fetch("a", "https://www.hp.com/1"), result("a", True),
        fetch("b", "https://hp.com/2"), result("b", True),
        fetch("c", "https://bestbuy.com/1"), result("c", False, "ok"),
        result("unknown", True),
    )]
    assert failures_by_site(lines) == {"hp.com": 2}


def test_third_fetch_to_a_failing_site_is_refused(tmp_path):
    path = transcript(tmp_path, [fetch("a", "https://www.hp.com/1"), result("a", True),
                                 fetch("b", "https://www.hp.com/2"), result("b", True)])
    out = decision(ask(path, "https://www.hp.com/3"))
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "hp.com" in out["hookSpecificOutput"]["permissionDecisionReason"]
    # Other sites, and other tools, go ahead.
    assert decision(ask(path, "https://www.bestbuy.com/x")) is None
    assert decision(ask(path, "https://www.hp.com/3", tool="Read")) is None


def test_one_failure_is_not_enough(tmp_path):
    path = transcript(tmp_path, [fetch("a", "https://hp.com/1"), result("a", True)])
    assert decision(ask(path, "https://hp.com/2")) is None


def test_never_fails(tmp_path):
    out = io.StringIO()
    assert main(io.StringIO("{not json"), out) == 0 and out.getvalue() == ""
    assert decision(ask(str(tmp_path / "missing.jsonl"), "https://hp.com/")) is None


def test_runs_as_a_module(tmp_path):
    path = transcript(tmp_path, [fetch("a", "https://hp.com/1"), result("a", True),
                                 fetch("b", "https://hp.com/2"), result("b", True)])
    proc = subprocess.run([sys.executable, "-m", "workflow.deploy.fetch_guard"],
                          input=json.dumps(ask(path, "https://hp.com/3")),
                          capture_output=True, text=True)
    assert json.loads(proc.stdout)["hookSpecificOutput"]["permissionDecision"] == "deny"
