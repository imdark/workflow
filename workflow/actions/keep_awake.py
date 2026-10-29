from pathlib import Path
from typing import Any, Dict

from .base import Action, ActionType

# The file in an agent job's directory that asks its run to keep the Mac
# awake; workflow/deploy/jobs.py reads it when the job starts.
MARKER = "keep-awake"


class KeepAwakeAction(Action):
    """Keep this Mac awake while an agent run goes on.

    For a NotesGraph `prestart` automation: its input carries the agent job
    (`job`), so the action needs no config. It marks that job, and when the
    job runs on this device, its run holds off sleep (`caffeinate -w` on the
    agent's process) until the agent exits. `job` may also be given as a job
    id. Deploy the automation to the device the agent runs on: the mark is
    made where the automation runs.
    """

    def __init__(self, action_id: str):
        super().__init__(action_id, ActionType.KEEP_AWAKE)

    def execute(self, context: Dict[str, Any]) -> Dict[str, Any]:
        from workflow.deploy.jobs import JOB_ROOT

        job = context.get('job')
        job_id = job.get('id') if isinstance(job, dict) else job
        if not job_id or not isinstance(job_id, str) or '/' in job_id or job_id.startswith('.'):
            return {'success': False,
                    'error': 'no agent job to keep awake for (run it from a prestart hook, or give job: <id>)'}
        job_dir = Path(JOB_ROOT) / job_id
        job_dir.mkdir(parents=True, exist_ok=True)
        (job_dir / MARKER).touch()
        print(f"This Mac stays awake while job {job_id} runs here", flush=True)
        return {'success': True, 'job': job_id}

    def validate_config(self, config: Dict[str, Any]) -> bool:
        return True
