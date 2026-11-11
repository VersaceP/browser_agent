"""Local author workspaces and immutable history. WebCross owns the marketplace.

Active draft: workflows/<slug>/draft.json plus managed package files.
History: workflows/<slug>/history/versions/<hash>/ and changes.jsonl.
Task snapshots remain task evidence, not a second executable catalog.
"""
from __future__ import annotations
import difflib
import json
import os
import shutil
import tempfile
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from harness.workflow_mode import package as pkg

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_WORKFLOWS_ROOT = PROJECT_ROOT / 'workflows'
DRAFT_FILE = 'draft.json'
VERSIONS_DIR = 'workflow/versions'

class RegistryError(ValueError):
    pass

class RegistryNotFound(RegistryError):
    pass

def _now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')

def _write_json(path, value):
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name('.' + path.name + '.' + uuid.uuid4().hex + '.tmp')
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    os.replace(temp, path)

class Registry:
    """Draft storage only; there is no local publish/list-market operation."""
    def __init__(self, worktree_dir, workflows_root=None):
        self.worktree = Path(worktree_dir).expanduser().resolve()
        self.root = Path(workflows_root or DEFAULT_WORKFLOWS_ROOT).expanduser().resolve()

    @classmethod
    def for_runtime(cls, runtime):
        return cls(runtime.harness.worktree_dir, os.environ.get('HARNESS_WORKFLOWS_ROOT'))

    def _directory(self, slug):
        if not isinstance(slug, str) or not pkg.SLUG_RE.fullmatch(slug):
            raise RegistryError('Invalid local draft slug')
        target = self.root / slug
        if target.is_symlink():
            raise RegistryError('Draft directory must not be a symbolic link')
        return target

    def list_drafts(self, *, include_deleted=False, with_hashes=True):
        rows = []
        for path in sorted(self.root.glob('*/draft.json')):
            if path.parent.name.startswith('.'):
                continue
            if path.is_symlink() or path.parent.is_symlink():
                raise RegistryError('Draft metadata must not be a symbolic link')
            record = pkg.read_json(path)
            if record.get('slug') != path.parent.name:
                raise RegistryError('Draft metadata differs from its directory identity')
            if record.get('status') == 'deleted' and not include_deleted:
                continue
            record['path'] = str(path.parent)
            if (path.parent/'history').is_dir():record['historyPath']=str(path.parent/'history')
            if with_hashes:record['currentHash'] = pkg.content_hash(path.parent)
            rows.append(record)
        return rows

    def read_draft(self, task_id):
        rows = [r for r in self.list_drafts(include_deleted=True,with_hashes=False) if r.get('taskId') == task_id]
        if len(rows) > 1:
            raise RegistryError('Several drafts claim this author task')
        return rows[0] if rows else None

    def draft_path(self, task_id):
        row = self.read_draft(task_id)
        if row:
            return Path(row['path'])
        if not isinstance(task_id, str) or not task_id or Path(task_id).name != task_id or task_id in {'.','..'}:
            raise RegistryError('Invalid author task ID')
        return self._directory('workflow-' + task_id[:8])

    def write_draft(self, record):
        value = {k:v for k,v in record.items() if k not in {'path','currentHash','problem','historyPath'}}
        directory = self._directory(value['slug'])
        if (directory/DRAFT_FILE).is_symlink():raise RegistryError('Draft metadata must not be a symbolic link')
        previous = pkg.read_json(directory / DRAFT_FILE) if (directory / DRAFT_FILE).is_file() else None
        if previous and any(value.get(k) != previous.get(k) for k in ('templateId','taskId')):
            raise RegistryError('Draft identity/author ownership cannot change implicitly')
        value.update(schemaVersion=2, updatedAt=_now())
        _write_json(directory / DRAFT_FILE, value)
        return {**value, 'path':str(directory)}

    def resolve_draft(self, reference):
        text = str(reference or '').strip().removeprefix('@')
        if not text:
            raise RegistryError('Select a local draft by name, slug, ID or author task')
        rows = self.list_drafts(with_hashes=False)
        for match in (lambda r:r.get('taskId') == text,
                      lambda r:text in {r.get('slug'),r.get('displayName'),r.get('templateId'),(r.get('remote') or {}).get('workshopId')},
                      lambda r:str(r.get('taskId','')).startswith(text)):
            found = [r for r in rows if match(r)]
            if len(found) == 1:
                found[0]['currentHash']=pkg.content_hash(Path(found[0]['path']))
                return found[0]
            if found:raise RegistryError('Several local drafts match; use an exact ID: ' + ', '.join(r['taskId'] for r in found))
        raise RegistryNotFound(f"No local draft matches {text!r}. Use /workflow-drafts.")

    def resolve_edit_draft(self, reference):
        try:return self.resolve_draft(reference)
        except RegistryNotFound:return None

    def select_draft(self, task_id):
        row = self.read_draft(task_id)
        if not row or row.get('status') == 'deleted':
            raise RegistryError('This old author workspace is archived/deleted. Select a current draft with /workflow-drafts and /workflow-edit.')
        return row

    def edit_draft(self, task_id, reference, *, request):
        return self.resolve_draft(reference)

    @contextmanager
    def _lock(self):
        self.root.mkdir(parents=True,exist_ok=True)
        path = self.root / '.draft.lock'; deadline=time.monotonic()+10
        while True:
            try:fd=os.open(path,os.O_CREAT|os.O_EXCL|os.O_WRONLY);break
            except FileExistsError:
                if time.monotonic() >= deadline:raise RegistryError('Another draft operation holds the lock')
                time.sleep(.05)
        try:yield
        finally:os.close(fd);path.unlink(missing_ok=True)

    def snapshot_directory(self, source, task_dir):
        source=Path(source);digest=pkg.content_hash(source)
        draft = next((r for r in self.list_drafts(with_hashes=False) if Path(r['path']) == source),None)
        if draft:
            contract=pkg.load_contract(source)
            if contract['templateId']!=draft['templateId'] or contract['slug']!=draft['slug']:
                raise RegistryError('Executable contract differs from the local draft identity')
        target=Path(task_dir)/VERSIONS_DIR/digest
        self._copy_version(source,target,digest)
        if draft:self.checkpoint(draft['taskId'],reason='validation')
        return digest,target

    @staticmethod
    def _copy_version(source,target,digest):
        target=Path(target)
        if Path(source).is_symlink():raise RegistryError('Version source must not be a symbolic link')
        if any(p.is_symlink() for p in (target,target.parent,target.parent.parent)):
            raise RegistryError('Version paths must not be symbolic links')
        if target.exists():
            if pkg.content_hash(target)!=digest:raise RegistryError('Immutable version differs from its hash')
            return
        target.parent.mkdir(parents=True,exist_ok=True)
        staging=Path(tempfile.mkdtemp(prefix='.version-',dir=target.parent))
        try:
            pkg.copy_managed(source,staging)
            if pkg.content_hash(staging)!=digest:raise RegistryError('Source changed while copying a version')
            staging.rename(target)
        finally:shutil.rmtree(staging,ignore_errors=True)

    def checkpoint(self, reference, *, reason='save', summary=None):
        with self._lock():
            row=self.resolve_draft(reference);source=Path(row['path']);digest=pkg.content_hash(source)
            history=source/'history';versions=history/'versions';head=history/'head.json'
            if history.is_symlink() or versions.is_symlink():raise RegistryError('History must not be a symbolic link')
            before=pkg.read_json(head).get('contentHash') if head.is_file() else None
            if before is not None and (not isinstance(before,str) or len(before)!=64 or any(c not in '0123456789abcdef' for c in before)):
                raise RegistryError('Invalid history head identity')
            self._copy_version(source,versions/digest,digest)
            if before != digest:
                if before and (not (versions/before).is_dir() or pkg.content_hash(versions/before)!=before):
                    raise RegistryError('The previous history version is missing or corrupt; cannot invent a source diff')
                left={i['path']:i['sha256'] for i in pkg.file_inventory(versions/before)} if before else {}
                right={i['path']:i['sha256'] for i in pkg.file_inventory(versions/digest)}
                changes=[{'path':p,'before':left.get(p),'after':right.get(p)} for p in sorted(left.keys()|right.keys()) if left.get(p)!=right.get(p)]
                patch=[]
                for item in changes:
                    name=item['path']
                    try:
                        a=(versions/before/name).read_text(encoding='utf-8') if before and (versions/before/name).is_file() else ''
                        b=(versions/digest/name).read_text(encoding='utf-8') if (versions/digest/name).is_file() else ''
                    except UnicodeError:
                        patch.append('Binary file changed: '+name+'\n');continue
                    patch.extend(difflib.unified_diff(a.splitlines(True),b.splitlines(True),fromfile='before/'+name,tofile='after/'+name))
                (history/(digest+'.diff')).write_text(''.join(patch),encoding='utf-8')
                self.operation(row,reason,{'parentHash':before,'contentHash':digest,'changes':changes,'summary':summary})
                _write_json(head,{'contentHash':digest,'updatedAt':_now()})
            return digest

    def operation(self, row, kind, details):
        directory=self._directory(row['slug'])/'history';directory.mkdir(parents=True,exist_ok=True)
        path=directory/'changes.jsonl'
        if directory.is_symlink() or path.is_symlink():raise RegistryError('History paths must not be symbolic links')
        with path.open('a',encoding='utf-8') as f:f.write(json.dumps({'at':_now(),'kind':kind,**details},ensure_ascii=False)+'\n')

    def snapshot(self,row,task_dir):
        source=Path(row.get('packagePath') or row['path']);digest=row['contentHash']
        if pkg.content_hash(source)!=digest:raise RegistryError('Selected snapshot changed before execution')
        target=Path(task_dir)/VERSIONS_DIR/digest;self._copy_version(source,target,digest);return target

    def restore_snapshot(self,info,task_dir):
        digest=info.get('contentHash');identity=info.get('templateId')
        if not isinstance(digest,str) or len(digest)!=64 or any(c not in '0123456789abcdef' for c in digest):
            raise RegistryError('Invalid historical version hash')
        candidates=list(self.root.glob('*/history/versions/'+digest))+list(self.root.glob('.history/*/'+digest))+list(self.root.glob('.history/remote/*/'+digest))
        remote_id,remote_hash=info.get('workshopId'),info.get('manifestHash')
        if remote_id and remote_hash:
            try:remote_id=str(uuid.UUID(remote_id))
            except ValueError:raise RegistryError('Invalid recorded Workshop identity') from None
            if not isinstance(remote_hash,str) or len(remote_hash)!=64 or any(c not in '0123456789abcdef' for c in remote_hash):
                raise RegistryError('Invalid recorded manifest hash')
            candidates.insert(0,self.root/'.history'/'remote'/remote_id/remote_hash)
        for source in candidates:
            if (self.root/'.history').is_symlink() or any(p.is_symlink() for p in (source,source.parent,source.parent.parent)):
                continue
            if source.is_dir() and pkg.content_hash(source)==digest and pkg.load_contract(source).get('templateId')==identity:
                return self.snapshot({'path':str(source),'contentHash':digest},task_dir)
        raise RegistryError('The exact recorded version is unavailable; current Workshop source cannot replace historical evidence')

    def rename(self, reference, display_name):
        if not isinstance(display_name,str) or not display_name.strip() or len(display_name)>200:raise RegistryError('Name must contain 1..200 characters')
        with self._lock():
            row=self.resolve_draft(reference);source=Path(row['path']);meta=pkg.load_metadata(source);previous=meta['name']
            _write_json(source/'workflow.json',{**meta,'name':display_name})
            self.write_draft({**row,'displayName':display_name,'status':'development'})
        self.checkpoint(row['taskId'],reason='rename',summary=previous+' -> '+display_name)
        return {'slug':row['slug'],'previous':previous,'displayName':display_name}

    def delete(self, reference):
        row=self.resolve_draft(reference);self.checkpoint(row['taskId'],reason='delete-snapshot')
        with self._lock():
            source=Path(row['path'])
            for name in sorted({p.split('/')[0] for p in pkg.managed_files(source)}|{'final.workshop.json'}):
                p=source/name
                if p.is_dir():shutil.rmtree(p)
                else:p.unlink(missing_ok=True)
            self.write_draft({**row,'status':'deleted'})
            self.operation(row,'delete',{'contentHash':row['currentHash'],'remote':row.get('remote'),'remoteRemoved':False})
        return {'slug':row['slug'],'keptUnmanaged':sorted(p.name for p in source.iterdir()),'remoteRemoved':False}

    def create_draft(self, task_id: str, *, request: str, slug: str | None = None,
                     display_name: str | None = None) -> dict[str, Any]:
        slug = slug or "workflow-" + task_id[:8]
        directory = self._directory(slug)
        if self.read_draft(task_id):
            raise RegistryError("This task already owns a draft")
        if directory.exists() and any(directory.iterdir()):
            raise RegistryError("This task already has a draft")
        slug = slug or "workflow-" + task_id[:8]
        contract = pkg.new_contract(slug)
        contract["preflight"] = {"entry": "src/preflight.js"}
        directory.mkdir(parents=True, exist_ok=True)
        _write_json(directory / "contract.json", contract)
        from harness.workflow_mode import coverage
        _write_json(directory / coverage.FILE, coverage.starter(request))
        _write_json(directory / "workflow.json", {
            "schemaVersion": 1, "name": display_name or slug, "description": "",
            "entry": "src/main.js", "timeoutMs": 600000,
            "availability": {"mode": "all-hosts"}})
        (directory / "src").mkdir(exist_ok=True)
        from harness.workflow_mode.preflight import STARTER
        (directory / "src" / "preflight.js").write_text(STARTER, encoding="utf-8")
        sdk = Path(__file__).resolve().parents[1] / "workflow" / "sdk" / "package_protocol.js"
        (directory / "src" / "workflow-protocol.js").write_text(sdk.read_text(encoding="utf-8"), encoding="utf-8")
        descriptor = {"id": "prepare", "kind": "operation", "visibility": "public", "default": True,
                      "purpose": "Collect pure input check facts; replace with the agreed business stages",
                      "requires": [], "produces": ["checks"], "effects": []}
        contract["stageProtocol"] = {"version": 1, "entry": "src/stages.js"}
        contract["stages"] = [descriptor]
        _write_json(directory / "contract.json", contract)
        (directory / "src" / "stages.js").write_text(
            "import {defineStages} from './workflow-protocol.js';\n"
            "import preflight from './preflight.js';\n"
            "const definition = " + json.dumps(descriptor) + ";\n"
            "const stages = defineStages([{...definition, run: async (api, input) => ({checks: await preflight(input, {files: []})})}]);\n"
            "export const describeStages = stages.describeStages;\n"
            "export const runStages = stages.runStages;\n", encoding="utf-8")
        (directory / "src" / "main.js").write_text(
            "import {runStages} from './stages.js';\n"
            "import {buildReport} from './workflow-protocol.js';\n"
            "export default async function main(platform, input = {}) {\n"
            "  const execution = await runStages(platform, input);\n"
            "  return buildReport({outcome: 'needs-decision', ...execution, unresolved: [{reason: 'not-implemented'}]});\n"
            "}\n", encoding="utf-8")
        (directory / "README.md").write_text(
            "# " + (display_name or slug) + "\n\n"
            "## Run contract\n\n"
            "Describe the goal, complete input, output facts, stages and resume dependencies. "
            "Keep this section short enough to prepare a new run without reading implementation history.\n\n"
            "## Verification boundaries\n\n"
            "Keep browser observation, pure parsing, delivery planning and side-effect execution "
            "independently testable where useful. Observations retain target identity, provenance and "
            "coverage; pure parsers consume saved observations without browser/file effects. Delivery "
            "plans preserve exact source/target mapping. Expose only useful independent stages; keep "
            "a deterministic composite for ordinary one-call execution. Saved-fixture verification "
            "does not prove downloads: changes affecting delivery need actual download/write/readback "
            "verification. Record what the final version has and has not verified.\n\n"
            "## Concurrency and recovery\n\n"
            "Identify independent inputs, prerequisites, shared pages/sessions and output paths. "
            "Keep dependent same-page work sequential. Expose independently callable deterministic "
            "units when useful; keep a normal composite entry. Use bounded pools for independent IO, "
            "retain each item identity/result and unknown effects, and settle started work. Document "
            "serial/parallel validation evidence and unverified boundaries; concurrency is not required. "
            "Runs cannot delegate Browser business completion. Expose useful inspection/recovery entries "
            "that preserve original target identities and complete checkpoints. Separate capture, inspect "
            "existing delivery, transfer missing items and final text writes. Never truncate final output "
            "as a reservation. Distinguish not-started, confirmed terminal and unknown effects; retry only "
            "with current evidence and an idempotent or explicitly authorized operation. If recovery is "
            "unsupported, return exact remaining work for a later author task.\n\n"
            "## Scope, controls and scenarios\n\n"
            "Review the intended reuse scope from the user's goal before implementation. Ask about "
            "ambiguous business scenarios, not every control. Maintain coverage.json with included, "
            "excluded and undecided areas, controls and representative scenarios, including optional "
            "branches unused by the first example. Record prerequisites, dependencies, options and "
            "their retrieval/refresh conditions, interaction/readback and owned evidence. Distinguish "
            "unknown, discovered, documented, implemented and verified; discovery alone is not support. "
            "Put runtime rules and observation logic in src/; coverage.json and this README are "
            "authoring records and are not embedded in final.workshop.json.\n\n"
            "## Requirements and preflight\n\n"
            "Record verified requirements with their source and applicable context. Put stable input "
            "structure in params.schema.json and pure deterministic checks in src/preflight.js. "
            "validate_input runs the declared checker locally with authorized file metadata; aggregate "
            "all independent issues. Missing or stale observations mean unknown. Dynamic choices must "
            "be re-observed when their page/category/account dependencies change.\n\n"
            "## Execution and decisions\n\n"
            "Run ordinary actions and readbacks continuously inside the package. Return evidence, "
            "remaining issues and checkpoints at actual decision or diagnostic boundaries. Describe "
            "pre-authorized handling and what needs user input; never infer substitutes or silently "
            "skip requested content.\n\n"
            "## Verified scope and limitations\n\n"
            "Record versions and evidence separately from historical debugging details.\n",
            encoding="utf-8")
        return self.write_draft({"schemaVersion": 1, "taskId": task_id, "kind": "create", "slug": slug,
            "templateId": contract["templateId"], "displayName": display_name or slug,
            "baseHash": None, "status": "development", "createdAt": _now(), "request": request,
            "savedHash": None})

