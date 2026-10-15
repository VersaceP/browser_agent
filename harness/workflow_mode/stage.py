"""Prepare an incremental stage using the same workspace CAS and plan contract."""
import copy
import hashlib

from harness.skill_builder.workspace import build_package


def prepare_files(builder, updates, packages):
    pending = []
    seen = set()
    for update in updates:
        path = update['path']
        if path in seen:
            raise ValueError('A file may only appear once in updates')
        seen.add(path)
        target = builder._path(path)
        original = target.read_bytes().decode('utf-8') if target.exists() else None
        actual = hashlib.sha256(original.encode()).hexdigest() if original is not None else None
        if actual != update.get('expected_hash'):
            raise ValueError(f'File version has changed: {path}; read the current hash')
        if ('content' in update) == ('edits' in update):
            raise ValueError('Supply content or edits, not both')
        body = update.get('content', original)
        if 'edits' in update:
            if original is None or not update['edits']:
                raise ValueError('Edits require an existing file and nonempty replacements')
            for edit in update['edits']:
                old = edit['old_text']
                if not old or body.count(old) != 1:
                    raise ValueError('Patch anchor must occur exactly once')
                body = body.replace(old, edit['new_text'], 1)
        pending.append((path, body, actual))
    # A stale/ambiguous edit never writes a prefix of the update list.
    written = [builder._write_file(path, body, previous) for path, body, previous in pending]
    built = [build_package(builder, **package) for package in packages]
    return {'written': written, 'packages': built}


def stage_plan(owner, stage):
    plan = copy.deepcopy(owner.store.load().get('plan') or {'schemaVersion': 1, 'steps': []})
    for index, previous in enumerate(plan['steps']):
        if previous['id'] == stage['id']:
            plan['steps'][index] = stage
            break
    else:
        plan['steps'].append(stage)
    return plan
