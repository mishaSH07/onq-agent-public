import asyncio
import io
import json
from datetime import datetime,timezone
from pathlib import Path
import pytest
from pypdf import PdfWriter,PdfReader
from onq.store import Store,validate_library,safe
from onq.bundles import build,source_text
from onq.scheduling import next_run
from onq.collector import topic_nodes,semester
from onq.publisher import project_url,Publisher

@pytest.fixture
def store(course_store): return course_store

def pdf(pages=1):
    w=PdfWriter()
    for _ in range(pages): w.add_blank_page(width=612,height=792)
    b=io.BytesIO(); w.write(b); return b.getvalue()

def test_immutable_revisions_and_duplicate_names(store):
    c=store.courses()[0]
    assert store.ingest(c,'one','Lecture','https://example.test/1',b'old','same.md')
    assert not store.ingest(c,'one','Lecture','https://example.test/1',b'old','same.md')
    assert store.ingest(c,'one','Lecture','https://example.test/1',b'new','same.md')
    assert store.ingest(c,'two','Different','https://example.test/2',b'other','same.md')
    versions=store.rows('SELECT * FROM versions')
    assert len(versions)==3
    assert { (store.root/v['path']).read_bytes() for v in versions}=={b'old',b'new',b'other'}
    assert len(set(v['path'] for v in versions))==3

def test_bundle_idempotency_splitting_mapping_and_history(store):
    c=store.courses()[0]; store.set_setting('max_pdf_pages',2)
    store.ingest(c,'content:one','Lecture','https://example.test/1',pdf(3),'lecture.pdf','lectures')
    store.ingest(c,'content:duplicate','Same PDF','https://example.test/2',pdf(3),'duplicate.pdf','lectures')
    r,changed=build(store,c); assert changed
    m=json.loads(r['manifest']); path=store.root/r['path']
    assert len(PdfReader(path/'lectures-01.pdf').pages)==2
    assert len(PdfReader(path/'lectures-02.pdf').pages)==1
    assert sum('page' in x for x in m['mapping'])==3
    assert sum('same_content_as' in x for x in m['mapping'])==1
    assert build(store,c)[1] is False
    store.ingest(c,'content:one','Lecture','https://example.test/1',pdf(4),'lecture.pdf','lectures')
    r2,_=build(store,c)
    assert r2['id']!=r['id']
    old=store.rows('SELECT * FROM releases WHERE id=?',(r['id'],))[0]
    assert (store.root/old['path']/'manifest.json').is_file()

def test_failed_bundle_does_not_replace_last_good_release(store):
    c=store.courses()[0]
    store.ingest(c,'one','Lecture','https://example.test/1',pdf(),'lecture.pdf','lectures')
    original,_=build(store,c)
    store.ingest(c,'bad','Broken','https://example.test/2',b'%PDF not a real pdf','bad.pdf','lectures')
    with pytest.raises(ValueError): build(store,c)
    assert store.latest(c['id'])['id']==original['id']
    assert (store.root/original['path']/'lectures-01.pdf').exists()

def test_missing_only_after_complete_listing(store):
    c=store.courses()[0]
    store.ingest(c,'content:1','Old','url',b'keep','old.md')
    store.ingest(c,'updates:1','Updates','url',b'keep too','up.md')
    store.mark_missing(c['id'],set(),'content:')
    rows=store.items(c['id'])
    assert next(x for x in rows if x['source_key']=='content:1')['status']=='unavailable'
    assert all((store.root/x['path']).exists() for x in rows)
    assert next(x for x in rows if x['source_key']=='updates:1')['status']=='available'

def test_notebooks_preserve_code_without_execution(tmp_path):
    p=tmp_path/'test.ipynb'
    p.write_text(json.dumps({'cells':[{'cell_type':'code','source':['raise RuntimeError("DO NOT RUN")'],'outputs':[{'text':['kept output']}]}]}))
    text=source_text(p)
    assert 'DO NOT RUN' in text and 'kept output' in text

def test_hash_tampering_blocks_release(store):
    c=store.courses()[0]; store.ingest(c,'one','A','url',b'old','a.md')
    (store.root/store.items(c['id'])[0]['path']).write_bytes(b'tampered')
    with pytest.raises(ValueError,match='unexpectedly'): build(store,c)

def test_dst_and_schedule_catchup():
    s={'kind':'daily','time':'19:00','timezone':'America/Toronto'}
    before=datetime(2026,3,7,23,0,tzinfo=timezone.utc)
    assert datetime.fromisoformat(next_run(s,before))==datetime(2026,3,8,0,0,tzinfo=timezone.utc)
    after=datetime(2026,3,8,1,0,tzinfo=timezone.utc)
    assert datetime.fromisoformat(next_run(s,after))==datetime(2026,3,8,23,0,tzinfo=timezone.utc)
    assert next_run({'kind':'manual'},after) is None
    assert datetime.fromisoformat(next_run({'kind':'interval','hours':6},after))==datetime(2026,3,8,7,0,tzinfo=timezone.utc)

def test_weekly_and_fallback_once():
    s={'kind':'weekly','weekday':6,'time':'01:30','timezone':'America/Toronto'}
    first=datetime.fromisoformat(next_run(s,datetime(2026,11,1,4,0,tzinfo=timezone.utc)))
    assert first==datetime(2026,11,1,5,30,tzinfo=timezone.utc)
    second=datetime.fromisoformat(next_run(s,first))
    assert second.date().isoformat()=='2026-11-08'

def test_nested_content_and_semesters():
    toc={'Modules':[{'Topics':[{'Id':1,'TopicType':1}], 'Modules':[{'Topics':[{'Id':2,'TopicType':3}]}]}]}
    assert {x['Id'] for x in topic_nodes(toc)}=={1,2}
    assert list(topic_nodes({'Modules':[{'Topics':[{'TopicId':3,'Url':'/file.pdf'}]}]}))[0]['TopicId']==3
    assert semester('Example Course 3 F26')=='Fall 2026'
    assert semester('Example Course 3 W27')=='Winter 2027'
    assert semester('Example Course 3 Summer 2027')=='Summer 2027'

def test_project_target_and_protected_paths():
    with pytest.raises(ValueError): project_url('https://evil.example/g/g-p-123/project')
    assert project_url('https://chatgpt.com/g/g-p-123').endswith('/project')
    with pytest.raises(ValueError): validate_library(Path.home()/'.codex'/'.chatgpt-projects'/'x')
    assert '..' not in safe('../../escape')

def test_cloud_gate_cannot_claim_success(store):
    from onq.browser import ActionNeeded
    p=Publisher(store,None)
    with pytest.raises(ActionNeeded,match='verification'):
        asyncio.run(p.publish(store.courses()[0],{}))

def test_interrupted_run_recovers(store):
    rid=store.start_run(store.courses()[0]['id'],'sync')
    reopened=Store(store.state)
    assert reopened.rows('SELECT status FROM runs WHERE id=?',(rid,))[0]['status']=='interrupted'

def test_long_original_filename_keeps_pdf_extension(store):
    c=store.courses()[0]
    store.ingest(c,'long','Long lecture','url',pdf(),'x'*180+'.pdf','lectures')
    assert Path(store.items(c['id'])[0]['path']).suffix=='.pdf'
    release,_=build(store,c)
    assert (store.root/release['path']/'lectures-01.pdf').exists()

def test_topic_reconciliation_does_not_remove_zip_children(store):
    c=store.courses()[0]
    store.ingest(c,'content:1','Archive','url',b'zip','a.zip')
    store.ingest(c,'content:1:zip:code.py','Code','url',b'print(1)','code.py')
    store.mark_missing(c['id'],{'content:1'},'content:')
    assert all(x['status']=='available' for x in store.items(c['id']))

def test_link_archive_members_follow_parent_availability(store):
    c=store.courses()[0]
    parent='link:https://example.test/course/files.zip'
    store.ingest(c,parent,'Archive','url',b'archive','files.zip')
    store.ingest(c,parent+':zip:lesson.py','Lesson','url',b'print(1)','lesson.py')
    store.mark_missing(c['id'],{parent},'link:')
    assert all(x['status']=='available' for x in store.items(c['id']))
    store.mark_missing(c['id'],set(),'link:')
    assert all(x['status']=='unavailable' for x in store.items(c['id']))
    assert all((store.root/x['path']).is_file() for x in store.items(c['id']))

def test_revised_archive_removed_member_preserved_but_not_bundled(store):
    c=store.courses()[0]; parent='content:42'
    store.ingest(c,parent+':zip:a.py','a.py','url',b'print("current")','a.py','code')
    store.ingest(c,parent+':zip:b.py','b.py','url',b'print("obsolete marker")','b.py','code')
    build(store,c)
    store.mark_missing(c['id'],{parent+':zip:a.py'},parent+':zip:')
    release,_=build(store,c)
    body=(store.root/release['path']/'code-01.md').read_text()
    assert 'current' in body and 'obsolete marker' not in body
    removed=next(i for i in store.items(c['id']) if i['title']=='b.py')
    assert removed['status']=='unavailable' and (store.root/removed['path']).is_file()

def test_cancelled_sync_remains_due_for_startup_recovery(store):
    from onq.service import Service
    async def scenario():
        service=Service(store); c=store.courses()[0]; entered=asyncio.Event()
        async def waiting_collect(course):
            entered.set(); await asyncio.Event().wait()
        service.collector.collect=waiting_collect
        task=asyncio.create_task(service.run([c['id']],'sync'))
        await entered.wait(); task.cancel()
        with pytest.raises(asyncio.CancelledError): await task
        assert store.rows('SELECT status FROM runs')[-1]['status']=='interrupted'
        assert datetime.fromisoformat(store.course(c['id'])['next_run'])<=datetime.now(timezone.utc)
    asyncio.run(scenario())
