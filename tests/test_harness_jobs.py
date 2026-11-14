import asyncio
import pytest
from onq.store import Store,digest
from onq.bundles import build
from onq.harness_jobs import HarnessJobs

@pytest.fixture
def prepared(course_store):
    store=course_store;c=store.courses()[0]
    store.ingest(c,'one','Lecture','https://example.test/one',b'# Content','lesson.md','lectures');build(store,c)
    return store,c,HarnessJobs(store)

def test_repeated_enqueue_is_durable_and_does_not_duplicate(prepared):
    store,c,h=prepared;first=h.enqueue(c,'manual');second=h.enqueue(c,'schedule')
    assert first['id']==second['id']
    assert first['status']=='waiting-configuration'
    assert len(h.list())==1
    assert not store.setting('cloud_verified')
    assert all(digest((store.root/first['path']/f['upload_name']).read_bytes())==f['hash'] for f in first['files'])

def test_revised_release_supersedes_only_unstarted_jobs(prepared):
    store,c,h=prepared;old=h.enqueue(c,'manual')
    store.ingest(c,'one','Lecture','https://example.test/one',b'# Revision','lesson.md','lectures');build(store,c)
    new=h.enqueue(c,'schedule')
    assert new['id']!=old['id']
    assert h.get(old['id'])['status']=='superseded'
    assert h.get(new['id'])['status']=='waiting-configuration'
    assert h.get(old['id'])['files']

def test_destination_change_keeps_jobs_scoped(prepared):
    store,c,h=prepared;old=h.enqueue(c,'manual')
    c['project']='https://chatgpt.com/g/g-p-other/project'
    new=h.enqueue(c,'manual')
    assert new['project']!=old['project']
    assert h.get(old['id'])['status']=='superseded'

def test_modified_bundle_never_becomes_executable_job(prepared):
    store,c,h=prepared;release=store.latest(c['id'])
    import json
    f=json.loads(release['manifest'])['files'][0]
    (store.root/release['path']/f['name']).write_bytes(b'changed')
    with pytest.raises(ValueError,match='hash'):h.enqueue(c,'manual')
    assert h.list()==[]

def test_manual_and_scheduled_sync_enqueue_same_durable_job(prepared):
    store,c,h=prepared
    from onq.service import Service
    service=Service(store)
    async def collected(course):return 0,[]
    service.collector.collect=collected
    asyncio.run(service.run([c['id']],'sync'))
    assert len(h.list())==1
    asyncio.run(service.run([c['id']],'sync'))
    assert len(h.list())==1
    assert h.list()[0]['status']=='waiting-configuration'


def test_tampered_snapshot_and_stale_destination_fail_preflight(prepared):
    store,c,h=prepared;job=h.enqueue(c)
    assert h.validate(job['id'],c)['id']==job['id']
    f=job['files'][0];(store.root/job['path']/f['upload_name']).write_bytes(b'changed')
    with pytest.raises(ValueError,match='hash'):h.validate(job['id'],c)
    c['project']='https://chatgpt.com/g/g-p-elsewhere/project'
    with pytest.raises(ValueError,match='destination'):h.validate(job['id'],c)


def test_concurrent_enqueue_creates_one_snapshot(prepared):
    from concurrent.futures import ThreadPoolExecutor
    store,c,h=prepared
    with ThreadPoolExecutor(max_workers=3) as pool:
        jobs=list(pool.map(lambda _:h.enqueue(c),range(3)))
    assert len({j['id'] for j in jobs})==1
    assert len(h.list())==1


def test_scheduled_trigger_preserved(prepared):
    store,c,h=prepared
    from onq.service import Service
    service=Service(store)
    async def collected(course):return 0,[]
    service.collector.collect=collected
    asyncio.run(service.run([c['id']],'sync',trigger='schedule'))
    assert h.list()[0]['trigger']=='schedule'


def test_reenqueue_repairs_corrupt_snapshot_without_overwriting_it(prepared):
    store,c,h=prepared;old=h.enqueue(c);f=old['files'][0]
    damaged=store.root/old['path']/f['upload_name'];damaged.write_bytes(b'altered')
    repaired=h.enqueue(c)
    assert repaired['id']==old['id'] and repaired['path']!=old['path']
    assert repaired['previous_paths']==[old['path']]
    assert damaged.read_bytes()==b'altered'
    assert h.validate(repaired['id'],c)
    assert len(h.list())==1

def test_queue_stages_compact_sources_without_changing_local_release(prepared):
    from io import BytesIO
    from pypdf import PdfReader, PdfWriter
    import json

    store, c, h = prepared
    for category, width in [('lectures', 100), ('course-info', 200),
                            ('assignments', 300), ('practice-tests', 400), ('solutions', 500)]:
        stream = BytesIO()
        with PdfWriter() as writer:
            writer.add_blank_page(width=width, height=200)
            writer.write(stream)
        store.ingest(c, category, category, 'https://example.test/'+category,
                     stream.getvalue(), category+'.pdf', category)
    release, _ = build(store, c)
    manifest = json.loads(release['manifest'])
    job = h.enqueue(c)
    staged = {f['name']:store.root/job['path']/f['upload_name'] for f in job['files']}
    assert set(staged) == {'master.md','lectures-and-info.pdf','assignments-and-tests.pdf'}
    assert 'Content' in staged['master.md'].read_text(encoding='utf-8')
    assert 'lectures-and-info.pdf' in staged['master.md'].read_text(encoding='utf-8')
    assert [int(p.mediabox.width) for p in PdfReader(staged['lectures-and-info.pdf']).pages] == [200,100]
    assert [int(p.mediabox.width) for p in PdfReader(staged['assignments-and-tests.pdf']).pages] == [300,400,500]
    assert all(digest((store.root/release['path']/f['name']).read_bytes()) == f['hash']
               for f in manifest['files'])

def test_compact_pdf_splits_without_losing_pages():
    from io import BytesIO
    from pypdf import PdfReader, PdfWriter
    from onq.compact_sources import pdf_parts

    volumes = []
    for n in range(4):
        stream = BytesIO()
        with PdfWriter() as writer:
            writer.add_blank_page(width=100+n, height=200)
            writer.write(stream)
        volumes.append((f'lectures-{n}.pdf', stream.getvalue()))
    _, single, _ = pdf_parts('lectures', volumes[:2], 10000)[0]
    result = pdf_parts('lectures', volumes, len(single))
    assert [name for name, _, _ in result] == ['lectures-01.pdf', 'lectures-02.pdf']
    assert [int(page.mediabox.width) for _, data, _ in result
            for page in PdfReader(BytesIO(data)).pages] == [100,101,102,103]
    with pytest.raises(ValueError, match='four Sources'):
        pdf_parts('lectures', volumes*2, len(single))

def test_replacement_plan_requires_exact_project_and_intact_snapshot(prepared):
    store, course, harness = prepared
    job = harness.enqueue(course)
    limits = {'max_files':4,'max_file_bytes':1048576,'available_bytes':1048576}
    assert harness.plan(job['id'],course,job['project'],[],{},limits)['upload'] == job['files']
    with pytest.raises(ValueError,match='exact saved project'):
        harness.plan(job['id'],course,'https://chatgpt.com/g/g-p-other/project',[],{},limits)
    path = store.root/job['path']/job['files'][0]['upload_name']
    path.write_bytes(b'damaged')
    with pytest.raises(ValueError,match='hash'):
        harness.plan(job['id'],course,job['project'],[],{},limits)

def test_interrupted_source_upload_reconciles_bytes_without_reupload_or_ownership(prepared):
    from unittest.mock import AsyncMock, Mock
    from onq.browser import ActionNeeded

    store, course, harness = prepared
    job = harness.enqueue(course)
    file = job['files'][0]
    name = file['upload_name']
    browser = Mock(inspect_sources=AsyncMock(return_value=[]),
                   upload_source=AsyncMock(side_effect=ActionNeeded('timed out')),
                   verify_source=AsyncMock())
    limits = {'max_files':4,'max_file_bytes':100000,'available_bytes':100000}
    with pytest.raises(ActionNeeded,match='timed out'):
        asyncio.run(harness.upload_staged(job['id'],course,name,browser,limits))
    restarted = HarnessJobs(Store(store.state,store.root))
    with pytest.raises(ActionNeeded,match='outcome'):
        asyncio.run(restarted.upload_staged(job['id'],course,name,browser,limits))
    browser.inspect_sources.return_value=[name]
    assert asyncio.run(restarted.upload_staged(job['id'],course,name,browser,limits)) == 'reconciled'
    browser.upload_source.assert_awaited_once()
    browser.verify_source.assert_awaited_once_with(job['project'],name,file['hash'],file['size'])
    assert not store.setting('cloud_verified')
    assert store.latest(course['id'])['cloud_status']=='pending'


def test_source_upload_never_adopts_preexisting_name(prepared):
    from unittest.mock import AsyncMock, Mock
    from onq.browser import ActionNeeded

    store, course, harness = prepared
    job = harness.enqueue(course)
    name = job['files'][0]['upload_name']
    browser = Mock(inspect_sources=AsyncMock(return_value=[name]),upload_source=AsyncMock())
    with pytest.raises(ActionNeeded,match='already exists'):
        asyncio.run(harness.upload_staged(job['id'],course,name,browser,{}))
    browser.upload_source.assert_not_awaited()
    assert store.rows('SELECT * FROM source_uploads') == []

def test_successful_source_upload_records_backup_and_reuses_owned_file(prepared):
    from unittest.mock import AsyncMock, Mock

    store, course, harness = prepared
    job = harness.enqueue(course)
    file = job['files'][0]
    name = file['upload_name']
    browser = Mock(inspect_sources=AsyncMock(side_effect=[[],[name]]),
                   upload_source=AsyncMock(),verify_source=AsyncMock())
    limits = {'max_files':4,'max_file_bytes':100000,'available_bytes':100000}
    assert asyncio.run(harness.upload_staged(job['id'],course,name,browser,limits)) == 'owned'
    assert asyncio.run(harness.upload_staged(job['id'],course,name,browser,limits)) == 'owned'
    row, = store.rows('SELECT * FROM source_uploads')
    assert (row['project'],row['job'],row['status']) == (job['project'],job['id'],'owned')
    assert digest((store.root/row['backup']).read_bytes()) == file['hash']
    browser.upload_source.assert_awaited_once()
    browser.verify_source.assert_awaited_once()

def test_removal_requires_owned_obsolete_file_intact_backup_and_observed_completion(prepared):
    from unittest.mock import AsyncMock, Mock
    from onq.browser import ActionNeeded

    store, course, harness = prepared
    original_job = harness.enqueue(course)
    file = original_job['files'][0]
    name = file['upload_name']
    browser = Mock(inspect_sources=AsyncMock(return_value=[]),
                   upload_source=AsyncMock(),remove_source=AsyncMock())
    limits = {'max_files':4,'max_file_bytes':100000,'available_bytes':100000}
    asyncio.run(harness.upload_staged(original_job['id'],course,name,browser,limits))
    with pytest.raises(ActionNeeded,match='obsolete'):
        asyncio.run(harness.remove_staged(original_job['id'],course,name,browser,limits))
    store.ingest(course,'one','Lecture','https://example.test/one',b'# New content','lesson.md','lectures')
    build(store,course)
    replacement = harness.enqueue(course)
    browser.inspect_sources.return_value=['original.pdf',name]
    backup = store.root/original_job['path']/name
    contents = backup.read_bytes()
    backup.write_bytes(b'bad')
    with pytest.raises(ActionNeeded,match='backup'):
        asyncio.run(harness.remove_staged(replacement['id'],course,name,browser,limits))
    browser.remove_source.assert_not_awaited()
    backup.write_bytes(contents)

    browser.remove_source.side_effect=ActionNeeded('outcome uncertain')
    with pytest.raises(ActionNeeded,match='uncertain'):
        asyncio.run(harness.remove_staged(replacement['id'],course,name,browser,limits))
    assert store.rows('SELECT status FROM source_uploads') == [{'status':'removing'}]
    with pytest.raises(ActionNeeded,match='ownership'):
        asyncio.run(harness.remove_staged(replacement['id'],course,name,browser,limits))
    browser.remove_source.assert_awaited_once()

@pytest.mark.parametrize('interrupted',[False,True])
def test_removal_recovery_restores_backup_after_restart_and_refuses_duplicate(prepared,interrupted):
    from unittest.mock import AsyncMock, Mock
    from onq.browser import ActionNeeded

    store, course, harness = prepared
    original = harness.enqueue(course)
    name = original['files'][0]['upload_name']
    browser = Mock(inspect_sources=AsyncMock(return_value=['original.pdf']),
                   upload_source=AsyncMock(),remove_source=AsyncMock(),verify_source=AsyncMock())
    limits = {'max_files':4,'max_file_bytes':100000,'available_bytes':100000}
    asyncio.run(harness.upload_staged(original['id'],course,name,browser,limits))
    store.ingest(course,'one','Lecture','https://example.test/one',b'# Replacement','lesson.md','lectures')
    build(store,course)
    replacement = harness.enqueue(course)
    browser.inspect_sources.return_value=['original.pdf',name]
    asyncio.run(harness.remove_staged(replacement['id'],course,name,browser,limits))
    restarted = HarnessJobs(Store(store.state,store.root))
    browser.inspect_sources.return_value=['original.pdf','unexpected.pdf']
    with pytest.raises(ActionNeeded,match='inventory'):
        asyncio.run(restarted.restore_staged(replacement['id'],course,name,browser,limits))
    browser.inspect_sources.return_value=['original.pdf']
    if interrupted:
        browser.upload_source.side_effect=ActionNeeded('restore timed out')
        with pytest.raises(ActionNeeded,match='timed out'):
            asyncio.run(restarted.restore_staged(replacement['id'],course,name,browser,limits))
        with pytest.raises(ActionNeeded,match='outcome'):
            asyncio.run(restarted.restore_staged(replacement['id'],course,name,browser,limits))
    else:
        assert asyncio.run(restarted.restore_staged(replacement['id'],course,name,browser,limits)) == 'owned'
    assert browser.upload_source.await_count == 2
    assert browser.upload_source.await_args.args[1] == store.root/original['path']/name
    browser.inspect_sources.return_value=['original.pdf',name]
    expected = 'reconciled' if interrupted else 'owned'
    assert asyncio.run(restarted.restore_staged(replacement['id'],course,name,browser,limits)) == expected
    assert browser.upload_source.await_count == 2
    assert store.latest(course['id'])['cloud_status']=='pending'

@pytest.mark.parametrize('fail',[False,True])
def test_release_executor_replaces_or_restores_verified_sources(prepared,fail):
    from onq.browser import ActionNeeded
    from onq.publisher import UploadNotSubmitted

    store, course, harness = prepared
    original = harness.enqueue(course)
    old = original['files'][0]['upload_name']
    sources = {'unrelated.pdf'}
    events = []

    class Browser:
        async def inspect_sources(self, project):
            return sorted(sources)
        async def verify_source(self, project, name, expected_hash, expected_size):
            assert name in sources
        async def upload_source(self, project, path, expected_hash, expected_size, limits):
            events.append(('upload',path.name))
            if fail and path.name == new:
                raise UploadNotSubmitted('chooser did not open')
            sources.add(path.name)
        async def remove_source(self, project, name, expected_hash, expected_size, before):
            assert set(before)==sources
            events.append(('remove',name))
            sources.remove(name)

    browser = Browser()
    limits = {'max_files':4,'max_file_bytes':100000,'available_bytes':100000}
    new = None
    asyncio.run(harness.upload_staged(original['id'],course,old,browser,limits))
    store.ingest(course,'one','Lecture','https://example.test/one',b'# Replacement','lesson.md','lectures')
    build(store,course)
    replacement = harness.enqueue(course)
    new = replacement['files'][0]['upload_name']
    if fail:
        with pytest.raises(ActionNeeded,match='chooser'):
            asyncio.run(harness.publish_release(replacement['id'],course,browser,limits))
        assert sources == {'unrelated.pdf',old}
        assert events[-3:] == [('remove',old),('upload',new),('upload',old)]
        assert store.latest(course['id'])['cloud_status']=='pending'
    else:
        asyncio.run(harness.publish_release(replacement['id'],course,browser,limits))
        assert sources == {'unrelated.pdf',new}
        assert events[-2:] == [('remove',old),('upload',new)]
        assert store.latest(course['id'])['cloud_status']=='verified'
    assert not store.setting('cloud_verified')

def test_worker_publish_requires_exact_project_approval(prepared,monkeypatch):
    from unittest.mock import AsyncMock
    from onq.service import Service

    store, course, harness = prepared
    service = Service(store)
    limits = {'max_files':4,'max_file_bytes':1024,'available_bytes':1024}
    service.publisher.source_limits = AsyncMock(return_value=limits)
    execute = AsyncMock(return_value=2)
    monkeypatch.setattr(HarnessJobs,'publish_release',execute)
    store.set_setting('publishing_verified_projects',{course['id']:'https://chatgpt.com/g/g-p-other/project'})
    asyncio.run(service.run([course['id']],'publish'))
    execute.assert_not_awaited()
    store.set_setting('publishing_verified_projects',{course['id']:course['project']})
    asyncio.run(service.run([course['id']],'publish'))
    execute.assert_awaited_once()
    assert store.course(course['id'])['cloud_status']=='Verified'
    assert not store.setting('cloud_verified')

def test_automatic_publish_runs_only_after_successful_sync_with_exact_opt_in(prepared):
    from unittest.mock import AsyncMock
    from onq.service import Service

    store, course, _ = prepared
    service = Service(store)
    service.collector.collect = AsyncMock(return_value=(0,[]))
    service.publish_job = AsyncMock(return_value=3)
    store.set_setting('publishing_verified_projects',{course['id']:course['project']})
    asyncio.run(service.run([course['id']],'sync','schedule'))
    service.publish_job.assert_not_awaited()
    store.update_course(course['id'],auto_publish_project=course['project'])
    asyncio.run(service.run([course['id']],'sync','schedule'))
    service.publish_job.assert_awaited_once()
    store.update_course(course['id'],project='https://chatgpt.com/g/g-p-other/project')
    asyncio.run(service.run([course['id']],'sync','schedule'))
    assert service.publish_job.await_count==1

def test_migration_review_verifies_exact_backups_and_never_mutates_sources(prepared):
    import json
    from unittest.mock import AsyncMock, Mock
    from onq.browser import ActionNeeded
    from onq.migration import review_migration

    store, course, harness = prepared
    job = harness.enqueue(course)
    backup_id = 'a'*32
    folder = store.root/'Cloud Source Backups'/course['id']/backup_id
    folder.mkdir(parents=True)
    backup = folder/'01.bin'
    backup.write_bytes(b'legacy bytes')
    manifest = {'project':job['project'],'files':[
        {'file':'01.bin','name':'legacy.pdf','size':12,'sha256':digest(b'legacy bytes')}
    ]}
    (folder/'manifest.json').write_text(json.dumps(manifest),encoding='utf-8')
    page = object()
    browser = Mock(open_sources=AsyncMock(return_value=page),
                   source_names=AsyncMock(return_value=['legacy.pdf']),
                   verify_source_on_page=AsyncMock(),confirm_source_inventory=AsyncMock(),
                   source_limits=AsyncMock(return_value={'max_files':4,'max_file_bytes':100000,'available_bytes':100000}))
    result = asyncio.run(review_migration(store,course,job['id'],backup_id,browser))
    assert result['existing']==['legacy.pdf'] and result['minimum_removals']==0
    browser.verify_source_on_page.assert_awaited_once_with(page,job['project'],'legacy.pdf',digest(b'legacy bytes'),12)
    backup.write_bytes(b'wrong bytes!')
    with pytest.raises(ActionNeeded,match='backup bytes'):
        asyncio.run(review_migration(store,course,job['id'],backup_id,browser))
    assert browser.verify_source_on_page.await_count==1
    assert not store.setting('cloud_verified')


@pytest.mark.parametrize('failure', [None, 'rollback', 'resume', 'readback'])
def test_approved_migration_keeps_original_backup_and_verifies_result(prepared, failure):
    from io import BytesIO
    import json
    from unittest.mock import AsyncMock, Mock
    from pypdf import PdfWriter
    from onq.browser import ActionNeeded
    from onq.migration import execute_migration
    from onq.publisher import UploadNotSubmitted
    from onq.service import Service

    store, course, harness = prepared
    with BytesIO() as pdf, PdfWriter() as writer:
        writer.add_blank_page(width=100, height=100)
        writer.write(pdf)
        store.ingest(course, 'pdf', 'Lecture PDF', 'https://example.test/pdf', pdf.getvalue(),
                     'lecture.pdf', 'lectures')
    build(store, course)
    job = harness.enqueue(course)
    backup_id = 'b' * 32
    folder = store.root / 'Cloud Source Backups' / course['id'] / backup_id
    folder.mkdir(parents=True)
    originals = {'old.pdf': b'original', 'old.md': b'old notes'}
    manifest = {'project': job['project'], 'files': []}
    for i, (name, content) in enumerate(originals.items()):
        filename = f'{i}.bin'
        (folder / filename).write_bytes(content)
        manifest['files'].append(dict(name=name, file=filename, size=len(content), sha256=digest(content)))
    (folder / 'manifest.json').write_text(json.dumps(manifest), encoding='utf-8')
    remote = originals.copy()
    replacement = job['files'][1]['upload_name']

    async def verify(_project, name, sha, size):
        assert len(remote[name]) == size and digest(remote[name]) == sha

    async def verify_on_page(_page, project, name, sha, size):
        await verify(project, name, sha, size)

    async def inventory(_project, expected):
        assert set(remote) == set(expected)

    async def remove(project, name, sha, size, before):
        await inventory(project, before)
        await verify(project, name, sha, size)
        del remote[name]

    failed = False

    async def upload(_project, path, sha, size, _limits):
        nonlocal failed
        assert path.name not in remote
        if failure and not failed and path.name == replacement:
            failed = True
            if failure == 'rollback':
                raise UploadNotSubmitted('fixture rejected before submission')
            if failure == 'resume':
                raise ActionNeeded('fixture interrupted before persistence')
        content = path.read_bytes()
        assert len(content) == size and digest(content) == sha
        remote[path.name] = content
        if failure == 'readback' and path.name == replacement:
            raise ActionNeeded('fixture interrupted during readback')

    browser = Mock(open_sources=AsyncMock(), inspect_sources=AsyncMock(side_effect=lambda _: list(remote)),
                   source_names=AsyncMock(side_effect=lambda *_: list(remote)),
                   verify_source=AsyncMock(side_effect=verify),
                   verify_source_on_page=AsyncMock(side_effect=verify_on_page),
                   confirm_source_inventory=AsyncMock(side_effect=inventory),
                   source_limits=AsyncMock(return_value={'max_files': 4, 'max_file_bytes': 100000,
                                                         'available_bytes': 100000}),
                   remove_source=AsyncMock(side_effect=remove), upload_source=AsyncMock(side_effect=upload))
    with pytest.raises(ActionNeeded, match='exact Source names'):
        asyncio.run(execute_migration(store, course, job['id'], backup_id, job['release'], ['old.pdf'], browser))
    browser.remove_source.assert_not_awaited()
    browser.upload_source.assert_not_awaited()
    operation = execute_migration(store, course, job['id'], backup_id, job['release'], list(originals), browser)
    if failure == 'rollback':
        with pytest.raises(UploadNotSubmitted):
            asyncio.run(operation)
        assert remote == originals
        assert harness.get(job['id'])['status'] != 'verified'
        assert not Service(store).publishing_verified(course)
    else:
        if failure in ('resume', 'readback'):
            with pytest.raises(ActionNeeded, match='interrupted'):
                asyncio.run(operation)
            witnessed = replacement if failure == 'readback' else None
            assert set(remote) == {file['upload_name'] for file in job['files'][:2 if witnessed else 1]}
            operation = execute_migration(store, course, job['id'], backup_id, job['release'],
                                          list(originals), browser, witnessed_submission=witnessed)
        asyncio.run(operation)
        assert browser.remove_source.await_count == len(originals)
        assert set(remote) == {file['upload_name'] for file in job['files']}
        assert harness.get(job['id'])['status'] == 'verified'
        service = Service(store)
        assert service.publishing_verified(course)
        assert not service.automatic_publishing(course)
        other_project = next(c['project'] for c in store.courses() if c['id'] != course['id'])
        assert not service.publishing_verified({**course, 'project': other_project})
    for entry in manifest['files']:
        assert (folder / entry['file']).read_bytes() == originals[entry['name']]


def test_publish_names_keep_unchanged_sources_and_stamp_new_bytes(prepared, monkeypatch):
    import re
    from onq import harness_jobs

    store, course, harness = prepared
    files = [('master.md', b'notes'), ('lectures-and-info.pdf', b'lecture')]
    monkeypatch.setattr(harness_jobs, 'compact_sources', lambda *args: files)
    store.execute('UPDATE releases SET created=? WHERE course=?',
                  ('2026-11-13T14:30:15.123456+00:00', course['id']))
    first = harness.enqueue(course)
    first_names = {file['name']: file['upload_name'] for file in first['files']}
    assert all(re.search(r'2026-11-13_09-30-15\.123456-0500', name)
               for name in first_names.values())
    assert harness.enqueue(course)['files'] == first['files']
    store.execute("UPDATE harness_jobs SET status='verified' WHERE id=?", (first['id'],))

    files = [('master.md', b'revised notes'), ('lectures-and-info.pdf', b'lecture')]
    store.ingest(course, 'one', 'Lecture', 'https://example.test/one',
                 b'# Revision', 'lesson.md', 'lectures')
    build(store, course)
    store.execute('UPDATE releases SET created=? WHERE id=?',
                  ('2026-11-13T15:30:00.000001+00:00', store.latest(course['id'])['id']))
    next_job = harness.enqueue(course)
    next_names = {file['name']: file['upload_name'] for file in next_job['files']}
    assert next_names['lectures-and-info.pdf'] == first_names['lectures-and-info.pdf']
    assert '2026-11-13_10-30-00.000001-0500' in next_names['master.md']

    # Repairing a damaged snapshot preserves the names already in its journal.
    (store.root / next_job['path'] / next_names['master.md']).unlink()
    assert harness.enqueue(course)['files'] == next_job['files']


def test_scheduled_completion_reports_all_courses_and_manual_sync_does_not(prepared):
    from onq.service import Service

    store, course, _ = prepared
    service = Service(store)
    second = store.courses()[1]
    seen = []

    async def collected(current):
        assert service.scheduled_completion is None
        seen.append(current['id'])
        return 0, []

    service.collector.collect = collected
    asyncio.run(service.run([course['id']], 'sync'))
    assert service.scheduled_completion is None
    asyncio.run(service.run([course['id'], second['id']], 'sync', trigger='schedule'))
    completion = service.scheduled_completion
    assert seen == [course['id'], course['id'], second['id']]
    assert completion['id'] and completion['finished']
    assert completion['courses'] == 2


def test_original_filename_records_first_seen_time_and_retains_existing_path(prepared, monkeypatch):
    import re
    from onq import store as storage

    store, course, _ = prepared
    old = store.items(course['id'])[0]
    monkeypatch.setattr(storage, 'now', lambda: '2026-11-13T14:30:15.123456+00:00')
    assert not store.ingest(course, 'one', 'Lecture', 'https://example.test/one',
                            b'# Content', 'lesson.md', 'lectures')
    assert store.items(course['id'])[0]['path'] == old['path']
    assert store.ingest(course, 'one', 'Lecture', 'https://example.test/one',
                        b'# New revision', 'lesson.md', 'lectures')
    item = store.items(course['id'])[0]
    assert re.search(r'2026-11-13_09-30-15\.123456-0500-lesson\.md$', item['path'])
    assert (store.root / old['path']).read_bytes() == b'# Content'
    assert (store.root / item['path']).read_bytes() == b'# New revision'
