"""Task-scoped file permissions. Only terminal decisions grant external access.

Kept alongside local_fs: tools recheck this policy even when called without
model dispatch. Approval records live outside model-writable task outputs.
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import unquote, urlparse

# Interactive consent has no deadline. Task cancellation remains interruptible.

_STATES = {}
_LOCKS = {}
_BARRIERS = {}


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
    barrier = _barrier(value)
    barrier['pending'] += 1
    barrier['event'].clear()
    try:
        return await _authorize_path(value, raw, mode, base)
    finally:
        barrier['pending'] -= 1
        if not barrier['pending']:
            barrier['event'].set()


async def _authorize_path(value, raw, mode='read', base=None):
    from harness.runtime.hitl_input import read_terminal_input
    p = canonical(value, raw, base)
    if protected(value, p):
        return {'status': 'failed', 'code': 'protected_path_denied', 'tool_was_executed': False}
    if allowed(value, p, mode):
        return None
    if inside(p, task_root(value)):
        return {'status': 'failed', 'code': 'task_control_write_denied', 'tool_was_executed': False}
    # Never silently grant a file's parent. Directory operations grant the
    # exact displayed directory; single-file calls grant only that file.
    key = (asyncio.get_running_loop(), str(task_root(value)))
    async with _LOCKS.setdefault(key, asyncio.Lock()):
        if allowed(value, p, mode):
            return None
        denied = (str(p), mode)
        if denied in state(value)['denied']:
            return {'status': 'failed', 'code': 'external_path_access_denied', 'tool_was_executed': False}
        prompt = (f'\n[本地文件授权] 任务 {task_root(value).name}\n'
                  f'权限：{mode.upper()}；范围：{p}\n'
                  '目录授权仅对当前任务、当前权限生效，并包含该目录下的后代；不包含父目录或兄弟目录。符号链接解析到授权范围外时不继承授权。\n'
                  '批准请输入 yes，拒绝请输入 no。文件内容可能进入模型上下文。\n'
                  '当前任务已暂停新的模型轮次和工具调用；不限时等待，确认后原 worker 继续。')
        _log(value, 'local_path.authorization.requested', {'path': str(p), 'mode': mode})
        while True:
            answer = await read_terminal_input(prompt)
            if answer is None or answer.strip().lower() in {'y', 'yes', 'n', 'no'}:
                break
            prompt = '未识别授权选择。请输入 yes 批准或 no 拒绝；任务继续等待。'
        if answer is None:
            return {'status': 'needs_human', 'code': 'external_path_confirmation_required',
                    'path': str(p), 'mode': mode, 'tool_was_executed': False}
        if answer.strip().lower() not in {'y', 'yes'}:
            state(value)['denied'].add(denied)
            _log(value, 'local_path.authorization.denied', {'path': str(p), 'mode': mode})
            return {'status': 'failed', 'code': 'external_path_access_denied', 'tool_was_executed': False}
        # Re-resolve after terminal wait: symlink changes must not inherit approval.
        if canonical(value, raw, base) != p or protected(value, p):
            return {'status': 'failed', 'code': 'path_changed_during_confirmation', 'tool_was_executed': False}
        # Grant exactly the displayed scope, never a broader existing parent.
        # A list/search/mkdir call on a directory grants its descendants too.
        state(value)[mode + 'Roots'].append(str(p))
        _persist(value)
        _log(value, 'local_path.authorization.granted', {'path': str(p), 'mode': mode})
        return None


def requests(call):
    name = call.get('name', '')
    args = call.get('input') or {}
    if name == 'local_fs_read':
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
                    if any(m == mode and (inside(Path(p), root) or inside(root, Path(p)))
                           for p, m in state(agent)['denied']):
                        return {'status': 'failed', 'code': 'external_path_access_denied',
                                'tool_was_executed': False}
                    if not allowed(agent, root, mode):
                        _log(agent, 'local_path.access_intent.preflight', {
                            'phaseId': contract.get('phase_id'), 'path': str(root),
                            'mode': mode, 'reason': scope['reason'],
                        })
                    error = await authorize_path(agent, scope['path'], mode)
                    if error:
                        return error
        for raw, mode, base in path_requests:
            error = await authorize_path(agent, raw, mode, base)
            if error:
                return error
    except (OSError, ValueError) as exc:
        return {'status': 'failed', 'code': 'local_path_error', 'error': str(exc), 'tool_was_executed': False}
    return None
