"""Deployment recipes: what `wf deploy` actually runs on a target.

A recipe is four ordered lists of shell steps:

  steps    bring the target to the wanted revision (the deploy itself)
  health   probes that report state without changing anything
  version  a single step whose output is the deployed revision
  restart  steps that bounce the running system

`restart` is deliberately separate and empty in the built-in presets. A
deploy to a robot that is mid-session must never take it down as a side
effect; restarting is its own command, and it confirms first.

Presets live here; config overrides and adds:

    deploy:
      recipes:
        pantheon:
          steps:
            - name: install
              run: cd {repo} && ./install.sh
              timeout: 1800
          restart:
            - name: relaunch
              run: cd {repo} && ./clean_relaunch.sh

A recipe named in config is merged over the preset of the same name, list
by list, so overriding `restart` does not cost you the preset's `health`.
"""

from dataclasses import dataclass, field
from typing import Optional

DEFAULT_STEP_TIMEOUT = 600


@dataclass
class Step:
    name: str
    run: str
    timeout: int = DEFAULT_STEP_TIMEOUT
    # A failing step stops the recipe unless it is optional; optional steps
    # report and carry on, which is what you want for a probe that only some
    # targets can answer.
    optional: bool = False

    @classmethod
    def from_config(cls, raw) -> "Step":
        if isinstance(raw, str):
            return cls(name=raw.split()[0][:40], run=raw)
        if not isinstance(raw, dict) or not raw.get("run"):
            raise ValueError(f"recipe step needs a 'run' command: {raw!r}")
        return cls(
            name=str(raw.get("name") or raw["run"].split()[0])[:40],
            run=str(raw["run"]),
            timeout=int(raw.get("timeout", DEFAULT_STEP_TIMEOUT)),
            optional=bool(raw.get("optional", False)),
        )

    def render(self, context: dict) -> str:
        """Substitute {repo}, {ref}, … into the command.

        Unknown placeholders are left alone rather than raising: a recipe is
        allowed to contain shell braces that are not ours.
        """
        rendered = self.run
        for key, value in context.items():
            if value is not None:
                rendered = rendered.replace("{" + key + "}", str(value))
        return rendered


@dataclass
class Recipe:
    name: str
    steps: list = field(default_factory=list)
    health: list = field(default_factory=list)
    version: Optional[Step] = None
    restart: list = field(default_factory=list)

    def context(self, device) -> dict:
        """Placeholders available to every step of this recipe."""
        return {
            "id": device.id,
            "repo": device.repo or device.path or "",
            "path": device.path or device.repo or "",
            "ref": device.target_ref,
            "branch": device.branch,
            "pin": device.pin or "",
            "channel": device.channel,
            "host": device.host,
            "user": device.user,
            "port": device.port,
        }


# ── built-in presets ─────────────────────────────────────────────────────────

# A git checkout brought to a ref, with nothing assumed about how it is run.
GENERIC = {
    "steps": [
        {"name": "fetch", "run": "git -C {repo} fetch --all --tags --prune", "timeout": 600},
        {"name": "checkout", "run": "git -C {repo} checkout {ref} && git -C {repo} pull --ff-only || true"},
    ],
    "health": [
        {"name": "reachable", "run": "true", "timeout": 20},
        {"name": "repo", "run": "git -C {repo} rev-parse --is-inside-work-tree", "optional": True},
    ],
    "version": {"name": "version", "run": "git -C {repo} rev-parse --short HEAD", "timeout": 60},
    "restart": [],
}

# robots_realtime, per its own README: install.sh is idempotent and
# rr-debug status is the canonical rig health check. Nothing here restarts a
# rig -- clean_relaunch.sh is available as a `restart` step you opt into.
PANTHEON = {
    "steps": [
        {"name": "fetch", "run": "git -C {repo} fetch --all --tags --prune", "timeout": 900},
        {"name": "checkout", "run": "git -C {repo} checkout {ref}"},
        {"name": "submodules", "run": "git -C {repo} submodule update --init --recursive", "timeout": 1800},
        {"name": "install", "run": "cd {repo} && ./install.sh", "timeout": 3600},
    ],
    "health": [
        {"name": "reachable", "run": "true", "timeout": 20},
        {"name": "checkout", "run": "git -C {repo} rev-parse --is-inside-work-tree"},
        # The rig probe is optional because a host with no hardware attached
        # (a sim box, a build machine) is healthy without it.
        {"name": "rig", "run": "cd {repo} && .venv/bin/rr-debug status", "timeout": 120, "optional": True},
    ],
    "version": {"name": "version", "run": "git -C {repo} rev-parse --short HEAD", "timeout": 60},
    "restart": [
        {"name": "relaunch", "run": "cd {repo} && ./clean_relaunch.sh", "timeout": 600},
    ],
}

PRESETS = {"generic": GENERIC, "pantheon": PANTHEON}


def _merge(preset: dict, override: dict) -> dict:
    """Override wins list by list, so a partial override keeps the rest."""
    merged = dict(preset)
    for key, value in (override or {}).items():
        if value is not None:
            merged[key] = value
    return merged


def load_recipe(name: str, cfg=None) -> Recipe:
    """Build a recipe by name from presets plus config overrides."""
    if cfg is None:
        from workflow.config import load_effective_config
        cfg = load_effective_config()

    configured = ((cfg.get("deploy") or {}).get("recipes") or {}).get(name)
    preset = PRESETS.get(name)
    if preset is None and configured is None:
        raise ValueError(
            f"Unknown recipe '{name}'. Built in: {', '.join(sorted(PRESETS))}. "
            f"Define your own under deploy.recipes.{name}."
        )

    raw = _merge(preset or {}, configured or {})
    version = raw.get("version")
    return Recipe(
        name=name,
        steps=[Step.from_config(s) for s in raw.get("steps") or []],
        health=[Step.from_config(s) for s in raw.get("health") or []],
        version=Step.from_config(version) if version else None,
        restart=[Step.from_config(s) for s in raw.get("restart") or []],
    )


def available_recipes(cfg=None) -> list:
    if cfg is None:
        from workflow.config import load_effective_config
        cfg = load_effective_config()
    configured = ((cfg.get("deploy") or {}).get("recipes") or {}).keys()
    return sorted(set(PRESETS) | set(configured))
