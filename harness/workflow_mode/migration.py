"""Explicit one-time migration from the retired local market to author drafts."""
from __future__ import annotations
import json
import os
import shutil
import uuid
from pathlib import Path
from harness.workflow_mode import package as pkg
from harness.workflow_mode.registry import RegistryError, _write_json, _now


def inventory(store):
    current=[]
    for file in sorted(store.root.glob('*/publish.json')):
        row=pkg.read_json(file);digest=row['contentHash']
        source=store.root/'.published'/row['templateId']/digest
        if not source.is_dir() or pkg.content_hash(source)!=digest:
            source=file.parent
        if pkg.content_hash(source)!=digest:
            candidates=list(store.worktree.glob('*/workflow/versions/'+digest))
            source=next((p for p in candidates if pkg.content_hash(p)==digest),None)
        if source is None or pkg.content_hash(source)!=digest:
            raise RegistryError('Cannot recover the exact published source for '+row['slug'])
        current.append({'record':row,'source':str(source),'exportHash':pkg.content_hash(file.parent)})
    bottom=[]
    for file in sorted(store.worktree.glob('*/workflow/draft.json')):
        bottom.append({'taskId':file.parts[-3],'record':pkg.read_json(file),
                       'hash':pkg.content_hash(file.parent/'draft')})
    versions=[str(p) for p in store.root.glob('.published/*/*') if p.is_dir()]
    for raw in versions:
        path=Path(raw)
        if path.is_symlink() or path.parent.is_symlink():raise RegistryError('Legacy version directories must not be symbolic links')
        try:uuid.UUID(path.parent.name)
        except ValueError:raise RegistryError('Invalid historical identity') from None
        if len(path.name)!=64 or any(c not in '0123456789abcdef' for c in path.name):raise RegistryError('Invalid historical hash path')
    return {'current':current,'oldDrafts':bottom,'legacyVersions':versions,
            'legacyVerification':'Historical directories retained as-is; validate content on actual reuse, not declared hash alone'}


def migrate(store,*,apply=False):
    marker=store.root/'.storage-v2.json'
    if marker.is_file():return {'status':'already_migrated',**pkg.read_json(marker)}
    progress=store.root/'.migration-in-progress.json'
    if progress.exists():raise RegistryError('Migration was interrupted; use its saved inventory/backup before retrying: '+str(progress))
    plan=inventory(store)
    if not apply:return plan
    # Stop at an actual lifecycle conflict rather than moving a live author's files.
    running=[]
    for file in store.worktree.glob('.workflow-runs/*.json'):
        row=pkg.read_json(file)
        if row.get('status')=='running':
            try:os.kill(int(row['pid']),0)
            except ProcessLookupError:continue  # Proven dead owner, not a completed business task.
            except (OSError,ValueError,TypeError):raise RegistryError('Cannot establish owner state for '+row['taskId'])
            running.append(row['taskId'])
    if running:raise RegistryError('Resolve running Workflow tasks before migration: '+', '.join(running))
    archive=store.root/'.migration'/(_now().replace(':','-')+'-'+uuid.uuid4().hex[:8])
    with store._lock():
        archive.mkdir(parents=True)
        _write_json(archive/'inventory.json',plan)
        _write_json(progress,{'archive':str(archive),'startedAt':_now()})
        # Copy hash-proven source before retiring any registry pointers.
        for item in plan['current']:
            row=item['record'];backup=archive/'current'/row['slug']
            pkg.copy_managed(Path(item['source']),backup)
            if pkg.content_hash(backup)!=row['contentHash']:raise RegistryError('Migration source changed')
            exported=store.root/row['slug']
            if item['exportHash']!=row['contentHash']:
                pkg.copy_managed(exported,archive/'unpublished-exports'/row['slug'])
        # Preserve every old task draft before clearing its active path.
        for item in plan['oldDrafts']:
            source=store.worktree/item['taskId']/'workflow';dest=archive/'task-drafts'/item['taskId'];dest.mkdir(parents=True)
            if pkg.content_hash(source/'draft')!=item['hash']:raise RegistryError('Old author draft changed during migration')
            if (source/'draft').exists():shutil.move(str(source/'draft'),str(dest/'draft'))
            shutil.move(str(source/'draft.json'),str(dest/'draft.json'))
        for item in plan['current']:
            row=item['record'];directory=store._directory(row['slug']);backup=archive/'current'/row['slug']
            if pkg.content_hash(directory)!=row['contentHash']:
                for name in {n.split('/')[0] for n in pkg.managed_files(directory)}:
                    p=directory/name
                    if p.is_dir():shutil.rmtree(p)
                    else:p.unlink()
                pkg.copy_managed(backup,directory)
            if pkg.content_hash(directory)!=row['contentHash']:
                raise RegistryError('Export restoration did not produce the exact source; recovery copies remain at '+str(archive))
            meta=archive/'market-metadata'/row['slug'];meta.mkdir(parents=True)
            for name in ('publish.json','final.workshop.json'):
                if (directory/name).exists():shutil.move(str(directory/name),str(meta/name))
            record={'taskId':row.get('sourceDraft') or uuid.uuid4().hex,'templateId':row['templateId'],
                    'slug':row['slug'],'displayName':row.get('displayName') or pkg.load_metadata(directory)['name'],
                    'kind':'migrated','status':'saved','createdAt':_now(),
                    'request':'Continue the migrated local author draft.', 'baseHash':row['contentHash'],
                    'savedHash':row['contentHash'],'remote':None,
                    'legacyPublication':row}
            store.write_draft(record)
        # All old versions remain usable for exact-version task recovery.
        by_id={i['record']['templateId']:i['record']['slug'] for i in plan['current']}
        for raw in plan['legacyVersions']:
            source=Path(raw);identity,digest=source.parent.name,source.name
            try:uuid.UUID(identity)
            except ValueError:raise RegistryError('Invalid legacy history identity') from None
            if len(digest)!=64 or any(c not in '0123456789abcdef' for c in digest):raise RegistryError('Invalid historical hash path')
            target=store.root/'.history'/identity/digest
            if target.exists():raise RegistryError('History destination already exists; inspect migration state')
            target.parent.mkdir(parents=True,exist_ok=True)
            # Preserve cold/unavailable historical files without opening them.
            # Future exact-version reuse still verifies the content hash.
            source.rename(target)
        events=store.root/'.registry-events.jsonl'
        if events.is_file():
            for line in events.read_text(encoding='utf-8').splitlines():
                try:event=json.loads(line)
                except ValueError:continue
                identity=event.get('templateId');slug=by_id.get(identity)
                if not slug:continue
                old,new=event.get('previousHash'),event.get('contentHash')
                store.operation({'slug':slug},'legacy-'+event.get('event','record'),{
                    'originalAt':event.get('at'),'parentHash':old,'contentHash':new,'changes':None,
                    'changesNote':'Original publication event retained; historical file differences not rederived',
                    'legacyEvent':event})
        for name in ('.published','.current-drafts','.unpublished','.registry-events.jsonl'):
            source=store.root/name
            if source.exists():shutil.move(str(source),str(archive/name))
        result={'schemaVersion':2,'marketplace':'WebCross Workshop','archive':str(archive),
                'drafts':len(plan['current']),'clearedOldDrafts':len(plan['oldDrafts']),
                'retainedLegacyVersions':len(plan['legacyVersions']),'legacyVersionsRevalidated':False,
                'completedAt':_now()}
    # Checkpoints take their own author-store lock.
    for item in plan['current']:store.checkpoint(item['record']['slug'],reason='migration',summary='Retired local market; retained author draft')
    _write_json(marker,result);progress.unlink()
    return {'status':'migrated',**result}
