"""What an agent run changed, as one record for NotesGraph to show.

    recorder = ChangeRecorder(cwd)
    recorder.tool_use(part)       # each `tool_use` part, as the run goes
    recorder.tool_result(part)    # each `tool_result` part
    base = git_base(cwd)          # before the run starts
    ...
    changes = recorder.changes(git_changes(cwd, base))   # None when nothing changed

The record (camelCase, as the server takes it):

    {"notes": {"created": [{"docId", "title"}],
               "edited":  [{"docId", "title"?}],
               "linked":  [{"parentDocId", "childDocId"}]},
     "files": [{"path", "before", "after"}],
     "git":   {"base", "head", "branch"?, "commits": [{"sha", "subject"}],
               "stat": [{"path", "added", "removed"}], "patch", "truncated"}}

Only tool calls whose result is not an error count. Git is best effort:
any failure just leaves `git` out.
"""

import json
import os
import subprocess
from pathlib import Path
from typing import Optional

TEXT_CAP = 8000
MAX_FILES = 50
MAX_NOTES = 100
PATCH_CAP = 60000
JSON_CAP = 200_000
GIT_TIMEOUT = 20
MAX_UNTRACKED = 200
CUT = "\n… (cut)"

FILE_TOOLS = {"Edit", "MultiEdit", "Write", "NotebookEdit"}
NOTE_EDIT_TOOLS = {"update_document", "update_document_meta", "update_block", "update_task"}


def _cap(text, limit: int = TEXT_CAP):
    if text is None:
        return None
    text = str(text)
    return text if len(text) <= limit else text[:limit] + CUT


def _title(value) -> Optional[str]:
    if not isinstance(value, str):
        return None
    title = " ".join(value.split())
    return title or None


def _result_text(content) -> str:
    if isinstance(content, list):
        content = "\n".join(
            part.get("text", "") for part in content if isinstance(part, dict)
        )
    return str(content or "")


def _result_json(content) -> dict:
    """The JSON object a NotesGraph tool answered with, or {}."""
    text = _result_text(content).strip()
    for candidate in (text, text[text.find("{"):text.rfind("}") + 1] if "{" in text else ""):
        if not candidate:
            continue
        try:
            value = json.loads(candidate)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(value, dict):
            return value
    return {}


def _note_tool(name: str) -> Optional[str]:
    """`create_document` for `mcp__notesgraph__create_document` (or bare)."""
    if name.startswith("mcp__"):
        parts = name.split("__")
        if len(parts) >= 3 and "notesgraph" in parts[1].lower():
            return parts[-1]
        return None
    return name


class ChangeRecorder:
    """Collects the notes and files a run changed, from its tool calls."""

    def __init__(self, cwd: Optional[str] = None):
        self.cwd = cwd
        self.pending: dict = {}
        self.created: dict = {}    # docId -> entry, in order
        self.edited: dict = {}
        self.linked: list = []
        self.files: list = []

    # --- feeding ----------------------------------------------------------

    def tool_use(self, part: dict) -> None:
        tool_id = part.get("id")
        if tool_id:
            self.pending[tool_id] = (part.get("name") or "", part.get("input") or {})

    def tool_result(self, part: dict) -> None:
        call = self.pending.pop(part.get("tool_use_id"), None)
        if call is None or part.get("is_error"):
            return
        name, args = call
        if not isinstance(args, dict):
            args = {}
        try:
            self._record(name, args, part.get("content"))
        except Exception:
            # A tool call we can't read is not worth failing the run over.
            pass

    def _record(self, name: str, args: dict, content) -> None:
        if name in FILE_TOOLS:
            self._file(name, args)
            return
        tool = _note_tool(name)
        if tool == "create_document":
            doc_id = _result_json(content).get("docId")
            if isinstance(doc_id, str) and doc_id and doc_id not in self.created:
                self.created[doc_id] = {"docId": doc_id, "title": _title(args.get("title")) or ""}
        elif tool in NOTE_EDIT_TOOLS:
            doc_id = args.get("docId") or _result_json(content).get("docId")
            if not isinstance(doc_id, str) or not doc_id or doc_id in self.created:
                return
            entry = self.edited.setdefault(doc_id, {"docId": doc_id})
            title = _title(args.get("title"))
            if title:
                entry["title"] = title
        elif tool == "link_document":
            result = _result_json(content)
            parent = result.get("parentDocId") or args.get("parentDocId")
            child = result.get("childDocId") or args.get("childDocId")
            if isinstance(parent, str) and isinstance(child, str) and parent and child:
                link = {"parentDocId": parent, "childDocId": child}
                if link not in self.linked:
                    self.linked.append(link)

    def _file(self, name: str, args: dict) -> None:
        path = self._path(args.get("notebook_path" if name == "NotebookEdit" else "file_path"))
        if not path:
            return
        if name == "Edit":
            self.files.append({"path": path, "before": args.get("old_string"),
                               "after": args.get("new_string")})
        elif name == "MultiEdit":
            for edit in args.get("edits") or []:
                if isinstance(edit, dict):
                    self.files.append({"path": path, "before": edit.get("old_string"),
                                       "after": edit.get("new_string")})
        elif name == "Write":
            self.files.append({"path": path, "before": None, "after": args.get("content")})
        elif name == "NotebookEdit":
            self.files.append({"path": path, "before": None, "after": args.get("new_source")})

    def _path(self, value) -> Optional[str]:
        if not isinstance(value, str) or not value:
            return None
        if not self.cwd:
            return value
        try:
            base = Path(self.cwd).resolve()
            target = Path(value if os.path.isabs(value) else os.path.join(self.cwd, value)).resolve()
            return str(target.relative_to(base))
        except (ValueError, OSError):
            return value

    # --- the record -------------------------------------------------------

    def changes(self, git: Optional[dict] = None) -> Optional[dict]:
        """The capped record, or None when the run changed nothing."""
        created = list(self.created.values())[:MAX_NOTES]
        edited = [e for e in self.edited.values() if e["docId"] not in self.created][:MAX_NOTES]
        linked = self.linked[:MAX_NOTES]
        files = [{"path": f["path"], "before": _cap(f["before"]), "after": _cap(f["after"])}
                 for f in self.files[:MAX_FILES]]
        if not (created or edited or linked or files or git):
            return None
        record: dict = {"notes": {"created": created, "edited": edited, "linked": linked},
                        "files": files}
        if git:
            git = dict(git)
            if len(git.get("patch") or "") > PATCH_CAP:
                git["patch"] = git["patch"][:PATCH_CAP] + CUT
                git["truncated"] = True
            record["git"] = git
        return fit(record)


def fit(record: dict, limit: int = JSON_CAP) -> dict:
    """Shrink a record until its JSON is under `limit` bytes."""
    def size() -> int:
        return len(json.dumps(record, ensure_ascii=False).encode("utf-8"))

    if size() > limit and record.get("git"):
        record["git"]["patch"] = ""
        record["git"]["truncated"] = True
    if size() > limit:
        record["files"] = []
    if size() > limit and record.get("git"):
        record["git"]["stat"] = []
        record["git"]["truncated"] = True
    return record


# --- git --------------------------------------------------------------------

def _git(cwd: str, *args: str, ok=(0,)) -> Optional[str]:
    try:
        proc = subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True,
                              timeout=GIT_TIMEOUT, errors="replace")
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    return proc.stdout if proc.returncode in ok else None


def git_base(cwd: Optional[str]) -> Optional[dict]:
    """Where the repo at `cwd` stands before a run: {"top", "base", "branch"?}."""
    if not cwd:
        return None
    try:
        top = _git(cwd, "rev-parse", "--show-toplevel")
        base = _git(cwd, "rev-parse", "HEAD")
        if not top or not base:
            return None
        found = {"top": top.strip(), "base": base.strip()}
        branch = (_git(cwd, "rev-parse", "--abbrev-ref", "HEAD") or "").strip()
        if branch and branch != "HEAD":
            found["branch"] = branch
        return found
    except Exception:
        return None


def git_changes(cwd: Optional[str], start: Optional[dict]) -> Optional[dict]:
    """What changed in the repo since `start` (from git_base); None if nothing."""
    if not cwd or not start:
        return None
    try:
        return _git_changes(start)
    except Exception:
        return None


def _git_changes(start: dict) -> Optional[dict]:
    top, base = start["top"], start["base"]
    head = (_git(top, "rev-parse", "HEAD") or "").strip()
    if not head:
        return None
    log = _git(top, "log", "--reverse", "--format=%H%x00%s", f"{base}..HEAD")
    commits = []
    for line in (log or "").splitlines():
        sha, _, subject = line.partition("\x00")
        if sha:
            commits.append({"sha": sha[:12], "subject": subject})

    stat = []
    for line in (_git(top, "diff", "--numstat", base) or "").splitlines():
        added, removed, path = (line.split("\t", 2) + ["", ""])[:3]
        if path:
            stat.append({"path": path, "added": int(added) if added.isdigit() else 0,
                         "removed": int(removed) if removed.isdigit() else 0})

    patch = _git(top, "diff", base) or ""
    truncated = False
    untracked = [p for p in (_git(top, "ls-files", "--others", "--exclude-standard") or "")
                 .splitlines() if p]
    if len(untracked) > MAX_UNTRACKED:
        untracked, truncated = untracked[:MAX_UNTRACKED], True
    for path in untracked:
        diff = _git(top, "diff", "--no-index", "/dev/null", path, ok=(0, 1)) or ""
        added = sum(1 for line in diff.splitlines()
                    if line.startswith("+") and not line.startswith("+++"))
        stat.append({"path": path, "added": added, "removed": 0})
        if len(patch) <= PATCH_CAP:
            patch += diff
        else:
            truncated = True

    if not commits and not patch.strip():
        return None
    if len(patch) > PATCH_CAP:
        patch, truncated = patch[:PATCH_CAP] + CUT, True
    git = {"base": base, "head": head}
    if start.get("branch"):
        git["branch"] = start["branch"]
    git.update({"commits": commits, "stat": stat, "patch": patch, "truncated": truncated})
    return git
