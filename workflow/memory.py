from pathlib import Path
from datetime import datetime
import json
from workflow.git_utils import get_repo

ROOT = Path.home() / ".wf/memory"

def _dir(issue):
    d = ROOT / issue.key
    d.mkdir(parents=True, exist_ok=True)
    return d

def load_memory(issue):
    d = _dir(issue)
    return {
        "session": (d / "session.md").read_text() if (d / "session.md").exists() else "",
        "summary": (d / "summary.md").read_text() if (d / "summary.md").exists() else "",
    }

def save_session(issue, text):
    print(f"🔍 Starting session save for {issue.key}...")
    try:
        _dir(issue).mkdir(parents=True, exist_ok=True)
        with open(_dir(issue) / "session.md", "a") as f:
            f.write(f"\n\n--- {datetime.now()} ---\n{text}")
        print(f"✅ Session saved successfully for {issue.key}")
    except Exception as e:
        print(f"❌ Failed to save session: {e}")
        import traceback
        traceback.print_exc()

def save_summary(issue, text):
    (_dir(issue) / "summary.md").write_text(text)

def save_meta(issue, provider):
    repo = get_repo()
    meta = {
        "issue": issue.key,
        "branch": repo.active_branch.name,
        "provider": provider,
        "updated": datetime.now().isoformat(),
    }
    (_dir(issue) / "meta.json").write_text(json.dumps(meta, indent=2))
