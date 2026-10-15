"""Small deterministic editing tools; all paths use the existing Builder boundary."""
import fnmatch
import hashlib
import json
import re


def patch_file(builder, *, path, expected_hash, edits):
    target = builder._path(path)
    original = target.read_bytes().decode('utf-8')
    if hashlib.sha256(original.encode()).hexdigest() != expected_hash:
        raise ValueError("File changed; read the current file before applying a patch")
    if not edits:
        raise ValueError("Provide at least one exact text replacement")
    result = original
    for edit in edits:
        old, new = edit['old_text'], edit['new_text']
        if not old or result.count(old) != 1:
            raise ValueError("Patch anchor must occur exactly once; include more surrounding text")
        result = result.replace(old, new, 1)
    # All replacements succeed before the atomic compare-and-swap write.
    value = builder._write_file(path, result, expected_hash)
    return {**value, 'editsApplied': len(edits), 'bytesBefore': len(original.encode()),
            'bytesAfter': len(result.encode())}


def search_files(builder, *, pattern, path_glob='*', limit=30):
    expression = re.compile(pattern)
    limit = max(1, min(limit, 100))
    matches, skipped = [], []
    for item in builder._list_files()['files']:
        if not fnmatch.fnmatchcase(item['path'], path_glob):
            continue
        try:
            body = builder._path(item['path']).read_bytes().decode('utf-8')
        except UnicodeDecodeError:
            skipped.append(item['path'])
            continue
        for number, line in enumerate(body.splitlines(), 1):
            found = expression.search(line)
            if found:
                if len(matches) == limit:
                    return {'matches': matches, 'hasMore': True, 'nonTextFiles': skipped}
                start = max(0, found.start() - 160)
                matches.append({'path': item['path'], 'sha256': item['sha256'], 'line': number,
                                'text': line[start:start+800], 'lineTruncated': len(line) > 800})
    return {'matches': matches, 'hasMore': False, 'nonTextFiles': skipped}


def build_package(builder, *, path, name, entry, files, expected_hash=None):
    from harness.skill_runtime.plan import relative_path
    if not isinstance(files, dict) or not files or not isinstance(entry, str) or entry not in files:
        raise ValueError("The entry must name a source file in the package")
    sources, inputs = {}, []
    for logical, work_path in files.items():
        if not isinstance(work_path, str):
            raise ValueError("Each source module must refer to a workspace file path")
        relative_path(logical)
        body = builder._path(work_path).read_bytes().decode('utf-8')
        sources[logical] = body
        inputs.append({'path': work_path, 'module': logical, 'sha256': hashlib.sha256(body.encode()).hexdigest()})
    document = {'schemaVersion': 1, 'name': name, 'entry': entry, 'files': sources}
    saved = builder._write_file(path, json.dumps(document, ensure_ascii=False, indent=2) + '\n', expected_hash)
    return {**saved, 'entry': entry, 'sources': inputs,
            'validation': 'Package assembled from exact source bytes; WebCross owns compilation and real execution.'}
