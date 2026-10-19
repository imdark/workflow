"""A short map of a repo's top-level names, for an agent to read at start.

Adapted from Ponytail's hooks/ponytail-map.js (MIT, DietrichGebert/ponytail):
`git ls-files` plus one regex per language, no model and no dependencies,
cut to a character budget. Shared folders (utils, lib, components...) come
first, since they hold what a new change should reuse rather than rebuild.
A task run gets it as SessionStart context (see jobs.claude_code_argv), so
"is it already in the codebase?" doesn't cost the agent a search.
"""

import re
import subprocess
from pathlib import Path

MAP_BUDGET_CHARS = 2000
# Bigger files are generated or vendored; reading them is slow and their
# names are noise.
MAX_FILE_BYTES = 200_000

_PY = re.compile(r"^(?:async\s+)?(?:def|class)\s+([A-Za-z]\w*)", re.M)
_JS = re.compile(
    r"^export\s+(?:default\s+)?(?:async\s+)?"
    r"(?:function\*?|class|const|let|var|interface|type|enum)\s+([A-Za-z_$][\w$]*)", re.M)
_GO = re.compile(r"^(?:func|type)\s+(?:\([^)]*\)\s*)?([A-Z]\w*)", re.M)
_RS = re.compile(r"^pub\s+(?:async\s+)?(?:fn|struct|enum|trait|type)\s+(\w+)", re.M)
_CPP = re.compile(r"^(?:class|struct)\s+([A-Za-z_]\w*)\s*[:{]", re.M)
PATTERNS = {
    ".py": _PY,
    ".js": _JS, ".jsx": _JS, ".ts": _JS, ".tsx": _JS, ".mjs": _JS,
    ".go": _GO,
    ".rs": _RS,
    ".h": _CPP, ".hpp": _CPP, ".cpp": _CPP, ".cc": _CPP,
}
SHARED_DIRS = {"utils", "util", "lib", "libs", "common", "shared", "helpers",
               "components", "hooks", "core"}
SKIP_PARTS = {"test", "tests", "__tests__", "spec", "fixtures", "migrations",
              "vendor", "node_modules", "dist", "build", "generated"}


def _tracked_files(root: Path) -> list:
    out = subprocess.run(["git", "-C", str(root), "ls-files"],
                         capture_output=True, text=True)
    return out.stdout.splitlines() if out.returncode == 0 else []


def _wanted(path: str) -> bool:
    parts = Path(path).parts
    name = parts[-1]
    if any(p in SKIP_PARTS for p in parts[:-1]) or name.startswith("test_"):
        return False
    return not re.search(r"\.(test|spec|d)\.[jt]sx?$", name)


def _shared_first(path: str) -> tuple:
    return (not any(p in SHARED_DIRS for p in Path(path).parts[:-1]), path.count("/"), path)


def build_map(root: Path, budget: int = MAP_BUDGET_CHARS) -> str:
    """One line per source file, `path: name, name`, within `budget` chars.

    "" outside a git repo or when nothing matches; never raises.
    """
    root = Path(root)
    files = sorted((f for f in _tracked_files(root)
                    if Path(f).suffix in PATTERNS and _wanted(f)), key=_shared_first)
    lines, used = [], 0
    for rel in files:
        path = root / rel
        try:
            if path.stat().st_size > MAX_FILE_BYTES:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        names = list(dict.fromkeys(PATTERNS[path.suffix].findall(text)))
        if not names:
            continue
        line = f"{rel}: {', '.join(names)}"
        if used + len(line) + 1 > budget:
            # One file with many names mustn't crowd out the rest.
            continue
        lines.append(line)
        used += len(line) + 1
    return "\n".join(lines)


MAP_PROMPT = """\
A map of this repo's top-level names (shared folders first, cut at {budget}
characters; tests and generated code left out). Before writing something
new, check whether it is already here.

{map}"""


def map_context(root: Path, budget: int = MAP_BUDGET_CHARS) -> str:
    """The map with a line saying what it is; "" when there is no map."""
    found = build_map(root, budget)
    return MAP_PROMPT.format(budget=budget, map=found) if found else ""
