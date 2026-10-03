"""Execute recipes against targets, and turn the results into status.

Two entry points:

  `run_steps`  executes a list of steps and reports what happened
  `check`      runs a recipe's health probes and records a DeviceStatus

Both take an already-built runner so tests can hand in a fake and so a
caller can reuse one multiplexed connection across several operations.
"""

from dataclasses import dataclass, field
from typing import Optional

from workflow.deploy.recipes import Recipe, Step, load_recipe
from workflow.deploy.registry import Check, DeviceStatus


@dataclass
class StepResult:
    step: Step
    ok: bool
    output: str = ""
    duration_ms: int = 0
    skipped: bool = False


@dataclass
class RunReport:
    device_id: str
    results: list = field(default_factory=list)
    version: Optional[str] = None

    @property
    def ok(self) -> bool:
        return all(r.ok or r.step.optional or r.skipped for r in self.results)

    @property
    def failed(self) -> list:
        return [r for r in self.results if not r.ok and not r.skipped]


def run_steps(runner, steps, context: dict, stop_on_failure: bool = True,
              on_step=None) -> list:
    """Run steps in order. Returns a StepResult per step.

    A required step that fails stops the run and the remainder come back
    marked `skipped`, so a report always accounts for every step rather
    than trailing off.
    """
    results = []
    halted = False

    for step in steps:
        if halted:
            results.append(StepResult(step=step, ok=False, skipped=True,
                                      output="skipped after an earlier failure"))
            continue

        if on_step:
            on_step(step)
        result = runner.run(step.render(context), timeout=step.timeout)
        results.append(StepResult(
            step=step, ok=result.ok, output=result.output,
            duration_ms=result.duration_ms,
        ))
        if not result.ok and not step.optional and stop_on_failure:
            halted = True

    return results


def deploy(device, recipe: Optional[Recipe] = None, runner=None,
           on_step=None, cfg=None) -> RunReport:
    """Bring a device to its wanted revision by running the recipe's steps."""
    from workflow.deploy.ssh import runner_for

    recipe = recipe or load_recipe(device.recipe, cfg)
    runner = runner or runner_for(device)
    context = recipe.context(device)

    report = RunReport(device_id=device.id)
    report.results = run_steps(runner, recipe.steps, context, on_step=on_step)
    if report.ok:
        report.version = _read_version(runner, recipe, context)
    return report


def restart(device, recipe: Optional[Recipe] = None, runner=None,
            on_step=None, cfg=None) -> RunReport:
    """Run a recipe's restart steps. Callers are expected to confirm first."""
    from workflow.deploy.ssh import runner_for

    recipe = recipe or load_recipe(device.recipe, cfg)
    runner = runner or runner_for(device)
    report = RunReport(device_id=device.id)
    report.results = run_steps(runner, recipe.restart, recipe.context(device),
                               on_step=on_step)
    return report


def check(device, recipe: Optional[Recipe] = None, runner=None, cfg=None) -> DeviceStatus:
    """Run health probes and roll them into a DeviceStatus.

    An unreachable device yields no checks at all, which `from_checks`
    reads as offline -- distinct from a device that answered and failed a
    probe, which is degraded.
    """
    from workflow.deploy.ssh import runner_for

    recipe = recipe or load_recipe(device.recipe, cfg)
    runner = runner or runner_for(device)

    reachable = runner.reachable()
    if not reachable.ok:
        return DeviceStatus.from_checks([])

    context = recipe.context(device)
    checks = []
    for result in run_steps(runner, recipe.health, context, stop_on_failure=False):
        if result.skipped:
            continue
        checks.append(Check(
            name=result.step.name,
            # An optional probe that fails is reported, not counted against
            # the device: a sim box has no rig to be unhealthy about.
            ok=result.ok or result.step.optional,
            detail=_summarize(result.output) if not result.ok else "",
            duration_ms=result.duration_ms,
        ))

    return DeviceStatus.from_checks(checks, version=_read_version(runner, recipe, context))


def _read_version(runner, recipe: Recipe, context: dict) -> Optional[str]:
    if not recipe.version:
        return None
    result = runner.run(recipe.version.render(context), timeout=recipe.version.timeout)
    return result.first_line() if result.ok else None


def _summarize(output: str, limit: int = 200) -> str:
    """One readable line from a command's output, for a status detail."""
    text = " ".join((output or "").split())
    return text[:limit] + ("…" if len(text) > limit else "")
