"""Install the skills this repo ships (claude/skills/<name>) for Claude Code.

Each skill is symlinked into ~/.claude/skills/<name>, and into the skills
folder of every Claude account a project uses (`claude_config_dir:`, see
claude_accounts), so every Claude session on the machine sees it: a session
started with CLAUDE_CONFIG_DIR=~/.claude-personal reads ~/.claude-personal/
skills and never ~/.claude/skills. Edits in the repo take effect without
reinstalling.
A link already pointing at the repo is left alone; a link pointing elsewhere
is repointed; a real directory of the same name is someone else's skill and
is only replaced with force.
"""

import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Tuple

REPO_SKILLS = Path(__file__).resolve().parent.parent / "claude" / "skills"
USER_SKILLS = Path.home() / ".claude" / "skills"


@dataclass
class Result:
    linked: List[str] = field(default_factory=list)
    current: List[str] = field(default_factory=list)
    skipped: List[str] = field(default_factory=list)


def bundled(source: Path = REPO_SKILLS) -> List[Path]:
    """The repo's skills: directories holding a SKILL.md."""
    if not source.is_dir():
        return []
    return sorted(p for p in source.iterdir() if (p / "SKILL.md").is_file())


def skill_dirs(cfg=None) -> List[Path]:
    """~/.claude/skills, then <dir>/skills for each distinct claude_config_dir a project sets."""
    if cfg is None:
        from workflow.config import load_config

        cfg = load_config()
    from workflow.claude_accounts import CONFIG_KEY

    dirs = [USER_SKILLS]
    for project in (cfg.get("projects") or {}).values():
        raw = (project or {}).get(CONFIG_KEY)
        if raw:
            skills = Path(raw).expanduser() / "skills"
            if skills not in dirs:
                dirs.append(skills)
    return dirs


def install_everywhere(source: Path = REPO_SKILLS, force: bool = False, names: Optional[List[str]] = None,
                       cfg=None) -> List[Tuple[Path, Result]]:
    """install() into every folder skill_dirs() names; [(folder, its Result)]."""
    return [(dest, install(source, dest, force=force, names=names)) for dest in skill_dirs(cfg)]


def install(source: Path = REPO_SKILLS, dest: Path = USER_SKILLS, force: bool = False,
            names: Optional[List[str]] = None) -> Result:
    result = Result()
    dest.mkdir(parents=True, exist_ok=True)
    for skill in bundled(source):
        if names and skill.name not in names:
            continue
        target = dest / skill.name
        if target.is_symlink():
            if target.resolve() == skill.resolve():
                result.current.append(skill.name)
                continue
            target.unlink()
        elif target.exists():
            if not force:
                result.skipped.append(skill.name)
                continue
            shutil.rmtree(target) if target.is_dir() else target.unlink()
        target.symlink_to(skill.resolve(), target_is_directory=True)
        result.linked.append(skill.name)
    return result
