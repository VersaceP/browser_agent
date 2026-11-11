"""Workflow run authority. Historical evidence does not restore retired tools."""


def require_observation(mode='observation'):
    if mode != 'observation':
        raise PermissionError('Workflow runs delegate observation only. Use supported package recovery; '
                              'otherwise report remaining work. Package changes require /workflow-edit.')


def retired_completion_parent(manifest):
    startup = (manifest or {}).get('startup_args') or {}
    if isinstance(startup, dict) and startup.get('workflowHandoffRef'):
        return str(startup.get('parentWorkflowTask') or '<parent-task>')
    return None


def completion_resume_error(manifest):
    parent = retired_completion_parent(manifest)
    if parent:
        return ('This historical Browser completion cannot resume: Workflow runs now allow observation only. '
                f'Its receipts remain available. Resume the parent with /resume {parent}; '
                'use /workflow-edit for package recovery changes. A separately requested Browser task '
                'must start as a new task with explicit remaining scope.')
    return None
