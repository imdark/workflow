from typing import Dict, Any
from .base import Action, ActionType


class CloudDeployAction(Action):
    """Run a project's cloud deploy (see workflow.deploy.cloud).

    Config: `project` (optional: the current project's, or the only one
    with a cloud deploy). Brings the deploy checkout to its branch without
    discarding anything, runs the deploy command with its output on this
    process's stdout, then asks the verify URL, if any.
    """

    def __init__(self, action_id: str):
        super().__init__(action_id, ActionType.CLOUD_DEPLOY)

    def execute(self, context: Dict[str, Any]) -> Dict[str, Any]:
        from workflow.deploy import cloud

        try:
            project, target = cloud.find_target(context.get('project') or None)
        except ValueError as e:
            return {'success': False, 'error': str(e)}
        problem = cloud.sync_checkout(target)
        if problem:
            return {'success': False, 'error': problem, 'project': project}
        commit = cloud._git(target.repo, 'rev-parse', 'HEAD').stdout.strip()
        subject = cloud._git(target.repo, 'log', '-1', '--format=%s').stdout.strip()
        print(f"Deploying {commit[:8]} {subject} from {target.repo}", flush=True)
        code = cloud.run_deploy(target)
        result = {'success': code == 0, 'project': project, 'commit': commit,
                  'subject': subject, 'return_code': code}
        if code != 0:
            result['error'] = f"`{target.command}` exited {code}"
            return result
        if target.verify_url:
            ok, detail = cloud.verify(target.verify_url)
            result['verified'] = ok
            result['verify'] = f"{target.verify_url} answered {detail}"
        return result

    def validate_config(self, config: Dict[str, Any]) -> bool:
        return True
