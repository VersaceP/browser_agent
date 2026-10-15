"""Task-scoped file permissions. Only terminal decisions grant external access.

Kept alongside local_fs: tools recheck this policy even when called without
model dispatch. Approval records live outside model-writable task outputs.
"""
from __future__ import annotations

import asyncio
import json
import os
import uuid
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import unquote, urlparse

# Interactive consent has no deadline. Task cancellation remains interruptible.

_STATES = {}
_LOCKS = {}
_BARRIERS = {}

LOCAL_ACCESS_GUIDE = """Plan local access from the user's task before requesting files.
Use local_fs_authorize(paths, mode) to request explicit files or directories without
reading them; for multiple assets under a task material/delivery directory, request
that directory once. Choose the scope from the goal, not from a hardcoded site rule.
A batch gets one terminal decision per permission mode. Only the human can choose
the displayed directory alternative; yes grants only the displayed requested scopes.
Directory grants cover descendants for this task and mode, survive resume, and are
reused by its workers and host-owned READ trials. READ does not grant WRITE, and
source-task references do not grant a new task access. Do not retry denied scopes
through ancestors, siblings or another tool. Protected paths and symlinks resolving
outside the approved scope remain restricted. For uploads, supply the actual
canonical file paths in Workflow input; a directory grant is not an upload manifest.
"""

LOCAL_AUTHORIZATION_DESCRIPTION = (
    "Request task-scoped READ or WRITE permission for explicit local files or directories, "
    "without reading or changing their contents. Batch paths share one terminal confirmation; "
    "a directory grant covers descendants for the same permission. Request the task's material "
    "or delivery directory once when multiple assets are needed. Only the human may approve "
    "the displayed scope or directory alternative. Existing grants are reused, including after resume."
)
LOCAL_AUTHORIZATION_SCHEMA = {
    'type': 'object', 'properties': {
        'paths': {'type': 'array', 'minItems': 1,
                  'items': {'type': 'string', 'minLength': 1}},
        'mode': {'type': 'string', 'enum': ['read', 'write'], 'default': 'read'},
    }, 'required': ['paths'], 'additionalProperties': False,
}


def _barrier(value):
    key = (asyncio.get_running_loop(), str(task_root(value)))
    if key not in _BARRIERS:
        event = asyncio.Event()
        event.set()
        _BARRIERS[key] = {'event': event, 'pending': 0}
    return _BARRIERS[key]


async def wait_for_local_authorization(value):
    """Cooperative task barrier; never blocks the event loop or terminal IO.

    Already dispatched RPCs/LLM requests may finish; no new model turn/tool
    starts past this checkpoint until the operator has answered all prompts.
    """
    # Non-session library dispatchers have no task barrier. Path-bearing calls
    # still require task identity in authorize_path and fail closed without it.
    if getattr(owner(value).logger, 'task_dir', None) is not None:
        await _barrier(value)['event'].wait()
_SENSITIVE = {'.git', '.ssh', '.gnupg', '.env', 'credentials', 'node_modules', '.local-file-permissions'}


def owner(value):
    return value if hasattr(value, 'logger') else SimpleNamespace(logger=value)


def task_root(value):
    return Path(owner(value).logger.task_dir).resolve()


def state(value):
    root = task_root(value)
    if str(root) not in _STATES:
        record = root.parent / '.local-file-permissions' / (root.name + '.json')
        try:
            data = json.loads(record.read_text())
        except (OSError, ValueError):
            data = {}
        _STATES[str(root)] = {'readRoots': data.get('readRoots', []),
                              'writeRoots': data.get('writeRoots', []), 'denied': set(), 'protected': set()}
    s = _STATES[str(root)]
    cfg = getattr(getattr(owner(value), 'runtime', None), 'harness', None)
    s['protected'].update(str(Path(p).expanduser().resolve())
                          for p in getattr(cfg, 'protected_local_roots', []) or [])
    return s


def inherit_read_permissions(grantor, grantee):
    """Host-only transfer to a host-created owned trial; never a model tool.

    Copy exact approved READ scopes. WRITE, denials and parent scopes never
    transfer, and protected-root checks still apply at every later read.
    """
    destination = state(grantee)
    for raw in state(grantor)["readRoots"]:
        path = Path(raw)
        if (not protected(grantor, path) and not protected(grantee, path)
                and not _denied_overlap(grantee, path, 'read')
                and raw not in destination["readRoots"]):
            destination["readRoots"].append(raw)
    _persist(grantee)
    _log(grantee, 'local_path.authorization.inherited', {
        'fromTaskId': task_root(grantor).name, 'mode': 'read', 'readRoots': destination['readRoots']})


def inside(path, root):
    return path == root or root in path.parents


def canonical(value, raw, base=None):
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError('path must be a non-empty string')
    p = Path(raw).expanduser()
    if '..' in p.parts:
        raise ValueError('path escapes the current task worktree')
    if not p.is_absolute():
        if base == 'desktop':
            p = Path.home() / 'Desktop' / p
        elif p.parts and p.parts[0].lower() == 'desktop':
            p = Path.home() / 'Desktop' / Path(*p.parts[1:])
        else:
            p = task_root(value) / p
    return p.resolve(strict=False)


def protected(value, p):
    s = state(value)
    if any(part.lower() in _SENSITIVE or part.lower().startswith('.env.') for part in p.parts):
        return True
    if any(inside(p, Path(root)) for root in s['protected']):
        return True
    # Task data is a deliberate exception to the built-in development install
    # root; explicit protected roots above always take precedence.
    task = task_root(value)
    install = Path(__file__).resolve().parents[2]
    if inside(p, install) and not inside(p, task):
        return True
    if task.parent.name == 'worktree' and inside(p, task.parent.parent) and not inside(p, task):
        return True
    return False


def allowed(value, p, mode):
    if protected(value, p):
        return False
    task = task_root(value)
    if inside(p, task):
        if mode == 'read':
            return True
        rel = p.relative_to(task)
        return bool(rel.parts and rel.parts[0] in {'observations', 'deliverables', 'scratchpad'})
    return any(inside(p, Path(root)) for root in state(value)[mode + 'Roots'])


def authorized_root_for(value, path, *, mode='read'):
    roots = [task_root(value)] + [Path(r) for r in state(value)[mode + 'Roots']]
    matches = [r for r in roots if inside(path, r)]
    return max(matches, key=lambda r: len(r.parts), default=None)


def resolve_authorized_path(value, raw, *, mode='read', base=None):
    p = canonical(value, raw, base)
    if protected(value, p):
        raise ValueError('protected_path_denied: application or sensitive path')
    if not allowed(value, p, mode):
        raise ValueError('external_path_confirmation_required: path is not authorized for ' + mode)
    return p


def _log(value, event, payload):
    write = getattr(owner(value).logger, 'write', None)
    if callable(write):
        write(event, payload)


def _persist(value):
    task = task_root(value)
    folder = task.parent / '.local-file-permissions'
    folder.mkdir(mode=0o700, exist_ok=True)
    record = folder / (task.name + '.json')
    temporary = record.with_suffix('.tmp')
    data = {k: state(value)[k] for k in ('readRoots', 'writeRoots')}
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(data, stream)
    os.replace(temporary, record)


async def authorize_path(value, raw, mode='read', base=None):
    return await authorize_paths(value, [raw], mode, base)


async def authorize_paths(value, paths, mode='read', base=None):
    """One decision for an explicit batch; never infer a grant from file inputs."""
    if not isinstance(paths, (list, tuple)) or not paths:
        raise ValueError('paths must be a non-empty array')
    return await _authorize_requests(value, [(raw, mode, base) for raw in paths])


async def authorize_local_paths(value, paths, mode='read'):
    """Shared public result for Browser, Workflow, Skill and Builder tools."""
    try:
        error = await authorize_paths(value, paths, mode)
        if error:
            return error
        resolved = list(dict.fromkeys(str(canonical(value, raw)) for raw in paths))
        roots = []
        task = task_root(value)
        for raw in resolved:
            path = Path(raw)
            root = authorized_root_for(value, path, mode=mode)
            if mode == 'write' and inside(path, task):
                # Built-in WRITE access covers output subtrees, not task control.
                root = task / path.relative_to(task).parts[0]
            if str(root) not in roots:
                roots.append(str(root))
        return {'status': 'authorized', 'paths': resolved, 'mode': mode,
                'scopes': [{'path': root, 'mode': mode} for root in roots]}
    except (OSError, ValueError) as exc:
        return {'status': 'failed', 'code': 'local_path_error', 'error': str(exc),
                'tool_was_executed': False}


def _denied_overlap(value, path, mode):
    return any(m == mode and (inside(path, Path(p)) or inside(Path(p), path))
               for p, m in state(value)['denied'])


def _request_error(value, entries):
    for raw, mode, base, path in entries:
        if canonical(value, raw, base) != path:
            return 'path_changed_during_confirmation'
        if protected(value, path):
            return 'protected_path_denied'
        if _denied_overlap(value, path, mode):
            return 'external_path_access_denied'
        if inside(path, task_root(value)) and not allowed(value, path, mode):
            return 'task_control_write_denied'
    return None


def _directory_option(value, paths, mode):
    # This is a displayed alternative, not a semantic selection or a grant.
    # Derive only the nearest existing common directory of the literal paths.
    directory = Path(os.path.commonpath([p if p.is_dir() else p.parent for p in paths]))
    if (directory in paths or not directory.is_dir() or protected(value, directory)
            or _denied_overlap(value, directory, mode)
            or (inside(directory, task_root(value)) and not allowed(value, directory, mode))):
        return None
    return directory


async def _authorize_requests(value, requests):
    entries = []
    for raw, mode, base in requests:
        if mode not in ('read', 'write'):
            raise ValueError('mode must be read or write')
        entries.append((raw, mode, base, canonical(value, raw, base)))
    if not entries:
        return None
    barrier = _barrier(value)
    barrier['pending'] += 1
    barrier['event'].clear()
    try:
        key = (asyncio.get_running_loop(), str(task_root(value)))
        async with _LOCKS.setdefault(key, asyncio.Lock()):
            error = _request_error(value, entries)
            if error:
                return {'status': 'failed', 'code': error, 'tool_was_executed': False}
            # Keep READ and WRITE decisions distinct, even for a mixed copy batch.
            for mode in dict.fromkeys(item[1] for item in entries):
                paths = list(dict.fromkeys(p for _, m, _, p in entries
                                         if m == mode and not allowed(value, p, mode)))
                # Only remove scopes already covered by an explicitly supplied
                # ancestor; do not synthesize a parent grant.
                paths = [p for p in paths if not any(p != root and inside(p, root)
                                                    for root in paths)]
                if not paths:
                    continue
                error = await _confirm_paths(value, paths, mode, entries)
                if error:
                    return error
            error = _request_error(value, entries)
            if error:
                return {'status': 'failed', 'code': error, 'tool_was_executed': False}
            return None
    finally:
        barrier['pending'] -= 1
        if not barrier['pending']:
            barrier['event'].set()


async def _confirm_paths(value, paths, mode, entries):
    from harness.runtime.hitl_input import read_terminal_input
    directory = _directory_option(value, paths, mode)
    authorization_id = uuid.uuid4().hex
    scope_text = '\n'.join('  ' + json.dumps(str(p), ensure_ascii=False) for p in paths)
    choices = 'yes = approve all listed scopes once; no = deny.'
    if directory is not None:
        choices += ('\ndirectory = approve this directory and its descendants instead: '
                    + json.dumps(str(directory), ensure_ascii=False))
    prompt = (f'\n[Local file authorization] Task {task_root(value).name}\n'
              f'Permission: {mode.upper()}; requested scopes ({len(paths)}):\n{scope_text}\n'
              'Directory authorization applies only to this task and permission, including descendants. Parents and sibling directories are excluded. Symbolic links outside the authorized scope do not inherit permission.\n'
              f'{choices}\nFile contents may enter the model context.\n'
              'New model turns and tool calls are paused without a time limit; the original worker continues after approval.')
    original_prompt = prompt
    for p in paths:
        _log(value, 'local_path.authorization.requested', {
            'path': str(p), 'mode': mode, 'authorizationId': authorization_id,
            'directoryOption': str(directory) if directory is not None else None})
    valid = {'y', 'yes', 'n', 'no'} | ({'directory'} if directory is not None else set())
    while True:
        answer = await read_terminal_input(prompt)
        if answer is None or answer.strip().lower() in valid:
            break
        prompt = original_prompt + '\nUnrecognized response. Choose one of the displayed options; the task remains paused.'
    if answer is None:
        return {'status': 'needs_human', 'code': 'external_path_confirmation_required',
                'path': str(paths[0]), 'paths': [str(p) for p in paths],
                'mode': mode, 'tool_was_executed': False}
    selection = answer.strip().lower()
    if selection in {'n', 'no'}:
        for p in paths:
            state(value)['denied'].add((str(p), mode))
            _log(value, 'local_path.authorization.denied', {
                'path': str(p), 'mode': mode, 'authorizationId': authorization_id})
        return {'status': 'failed', 'code': 'external_path_access_denied', 'tool_was_executed': False}
    approved = [directory] if selection == 'directory' else paths
    # Revalidate every original path after the wait, including symlink targets.
    checked = entries + [(str(p), mode, None, p) for p in approved]
    error = _request_error(value, checked)
    if selection == 'directory' and not directory.is_dir():
        error = 'path_changed_during_confirmation'
    if error:
        return {'status': 'failed', 'code': error, 'tool_was_executed': False}
    roots = state(value)[mode + 'Roots']
    roots.extend(str(p) for p in approved if str(p) not in roots)
    _persist(value)
    for p in approved:
        _log(value, 'local_path.authorization.granted', {
            'path': str(p), 'mode': mode, 'authorizationId': authorization_id,
            'selection': 'directory' if selection == 'directory' else 'requested'})
    return None


def requests(call):
    name = call.get('name', '')
    args = call.get('input') or {}
    if name == 'local_fs_authorize':
        for path in args.get('paths', []):
            yield path, args.get('mode', 'read'), None
    elif name == 'local_fs_read':
        yield args.get('path'), 'read', None
    elif name == 'local_fs_search':
        yield args.get('path') or '.', 'read', None
    elif name == 'local_fs_batch':
        for op in args.get('operations', []):
            if op.get('op') == 'copy':
                yield op.get('source'), 'read', None
            yield (op.get('path'),
                   'read' if op.get('op') in {'stat', 'list'} else 'write',
                   op.get('base'))
    # Browser path-bearing calls, including nested workflow steps. Browser-side
    # sandboxing remains authoritative; this gate does not widen its roots.
    else:
        def walk(node):
            if isinstance(node, dict):
                for k, v in node.items():
                    if k == 'options' and isinstance(v, dict) and isinstance(v.get('path'), str):
                        yield v['path'], 'write', None
                        yield from walk({key: item for key, item in v.items() if key != 'path'})
                    elif k in {'savePath', 'outputDir', 'downloadPath'} and isinstance(v, str) and v:
                        yield v, 'write', None
                    elif k in {'filePaths', 'files', 'paths'} and isinstance(v, list):
                        for item in v:
                            if isinstance(item, str):
                                if not Path(item).expanduser().is_absolute():
                                    raise ValueError('browser local file paths must be absolute')
                                yield item, 'read', None
                    elif isinstance(v, str) and v.startswith('file://'):
                        yield unquote(urlparse(v).path), 'read', None
                    elif isinstance(v, (dict, list)):
                        yield from walk(v)
            elif isinstance(node, list):
                for item in node:
                    yield from walk(item)
        if name in {'browser_call', 'execute_browser_workflow', 'execute_saved_browser_workflow', 'navigate_verified'} or '.' in name:
            yield from walk(args)


def normalize_local_access_intent(value):
    """Validate declared phase scopes, not infer or grant permissions."""
    if not isinstance(value, list) or len(value) > 32:
        raise ValueError('local_access_intent must be an array of at most 32 scopes')
    normalized = []
    for item in value:
        if not isinstance(item, dict) or set(item) != {'path', 'modes', 'reason'}:
            raise ValueError('local_access_intent entries require only path, modes and reason')
        path, modes, reason = item['path'], item['modes'], item['reason']
        if (not isinstance(path, str) or not path.strip()
                or not Path(path).is_absolute() or '..' in Path(path).parts):
            raise ValueError('local_access_intent.path must be an absolute literal path without ..')
        if (not isinstance(modes, list) or not modes
                or any(mode not in ('read', 'write') for mode in modes)):
            raise ValueError('local_access_intent.modes must contain read and/or write')
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError('local_access_intent.reason must explain the task need')
        normalized.append({'path': path, 'modes': list(dict.fromkeys(modes)), 'reason': reason.strip()})
    return normalized


async def authorize_tool_call(agent, call):
    await wait_for_local_authorization(agent)
    try:
        path_requests = list(requests(call))
        contract = getattr(agent, 'worker_contract', None) or {}
        intent = contract.get('local_access_intent')
        approvals = []
        if path_requests and intent is not None:
            scopes = normalize_local_access_intent(intent)
            # Preflight only on a call involving a declared scope. Do not ask
            # for external permission merely to read internal observations.
            # Select the narrowest matching declaration per actual request;
            # one output call must not preflight unrelated inputs or ancestors.
            selected = []
            for raw, mode, base in path_requests:
                requested = canonical(agent, raw, base)
                matches = [scope for scope in scopes
                           if mode in scope['modes'] and inside(
                               requested, canonical(agent, scope['path']))]
                if matches:
                    scope = max(matches, key=lambda item: len(
                        canonical(agent, item['path']).parts))
                    request_scope = (scope, mode)
                    if request_scope not in selected:
                        selected.append(request_scope)
            if selected:
                for scope, mode in selected:
                    root = canonical(agent, scope['path'])
                    # A declaration is not a request for every listed mode.
                    # Ask for READ only when this call actually reads, and
                    # WRITE only when it writes. Keep child denials intact.
                    if _denied_overlap(agent, root, mode):
                        return {'status': 'failed', 'code': 'external_path_access_denied',
                                'tool_was_executed': False}
                    if not allowed(agent, root, mode):
                        _log(agent, 'local_path.access_intent.preflight', {
                            'phaseId': contract.get('phase_id'), 'path': str(root),
                            'mode': mode, 'reason': scope['reason'],
                        })
                    approvals.append((scope['path'], mode, None))
        return await _authorize_requests(agent, approvals + path_requests)
    except (OSError, ValueError) as exc:
        return {'status': 'failed', 'code': 'local_path_error', 'error': str(exc), 'tool_was_executed': False}
