import pytest
from onq.bundles import build
from onq.handoff import Handoff

@pytest.fixture
def setup(course_store):
    store=course_store; course=store.courses()[0]
    store.ingest(course,'one','Lecture','https://example.test',b'# Original content','lesson.md','lectures')
    build(store,course)
    return store,course,Handoff(store)

def test_prepare_preserves_originals_and_does_not_claim_upload(setup):
    store,c,h=setup
    before={v['path']:(store.root/v['path']).read_bytes() for v in store.rows('SELECT path FROM versions')}
    ticket=h.prepare(c)
    assert ticket['files'] and all(f['changed'] for f in ticket['files'])
    assert all((store.root/ticket['path']/f['upload_name']).read_bytes() for f in ticket['files'])
    assert h.summary(c)['pending']>0 and not store.setting('cloud_verified')
    assert all((store.root/p).read_bytes()==data for p,data in before.items())

def test_confirmation_tracks_only_selected_snapshot_files(setup):
    store,c,h=setup; ticket=h.prepare(c)
    chosen=ticket['files'][0]['name']
    h.confirm(c,ticket['id'],[chosen])
    assert h.summary(c)['pending']==len(ticket['files'])-1
    assert not store.setting('cloud_verified')
    with pytest.raises(ValueError):h.confirm(c,ticket['id'],['not-in-this-ticket.pdf'])

def test_updated_bundle_becomes_pending_and_old_ticket_cannot_hide_it(setup):
    store,c,h=setup; old=h.prepare(c)
    h.confirm(c,old['id'],[f['name'] for f in old['files']])
    assert h.summary(c)['pending']==0
    store.ingest(c,'one','Lecture','https://example.test',b'# Revised content','lesson.md','lectures')
    build(store,c)
    h.confirm(c,old['id'],[f['name'] for f in old['files']])
    assert h.summary(c)['pending']>0

def test_project_mapping_changes_do_not_reuse_other_destination_receipts(setup):
    store,c,h=setup; t=h.prepare(c)
    h.confirm(c,t['id'],[f['name'] for f in t['files']])
    c['project']='https://chatgpt.com/g/g-p-other/project'
    assert h.summary(c)['pending']==h.summary(c)['total']
    with pytest.raises(ValueError):h.confirm(c,t['id'],[t['files'][0]['name']])

def test_manual_sync_never_calls_automatic_publisher(setup):
    import asyncio
    from onq.service import Service
    store,c,h=setup; service=Service(store)
    async def collected(course):return 0,[]
    async def forbidden(*args,**kwargs):raise AssertionError('Automatic publisher must not run')
    service.collector.collect=collected; service.publisher.publish=forbidden
    asyncio.run(service.run([c['id']],'sync'))
    assert store.rows('SELECT status FROM runs')[-1]['status']=='success'
    assert 'manual upload' in store.course(c['id'])['cloud_status']
    assert not store.setting('cloud_verified')

def test_modified_prepared_copy_cannot_be_confirmed(setup):
    store,c,h=setup; t=h.prepare(c); f=t['files'][0]
    (store.root/t['path']/f['upload_name']).write_bytes(b'edited outside app')
    with pytest.raises(ValueError,match='changed'):h.confirm(c,t['id'],[f['name']])
    assert h.summary(c)['pending']==h.summary(c)['total']

def test_obsolete_bundle_requires_explicit_manual_removal_record(setup):
    store,c,h=setup
    store.ingest(c,'extra','Extra source','url',b'old solution','old.md','solutions');build(store,c)
    old=h.prepare(c);h.confirm(c,old['id'],[f['name'] for f in old['files']])
    store.execute("UPDATE items SET status='unavailable' WHERE source_key='extra'");build(store,c)
    description=h.describe(c)
    assert [f['name'] for f in description['removals']]==['solutions-01.md']
    ticket=h.prepare(c)
    h.confirm_removals(c,ticket['id'],['solutions-01.md'])
    assert h.summary(c)['removal_pending']==0
    assert not store.setting('cloud_verified')

def test_reset_confirmation_and_reprepare_reuses_valid_snapshot(setup):
    store,c,h=setup;ticket=h.prepare(c)
    assert h.prepare(c)['id']==ticket['id']
    h.confirm(c,ticket['id'],[f['name'] for f in ticket['files']])
    h.reset(c,[f['name'] for f in ticket['files']])
    assert h.summary(c)['pending']==h.summary(c)['total']
    assert h.prepare(c)['id']==ticket['id']


def test_reset_preserves_inventory_for_later_obsolete_files(setup):
    store,c,h=setup
    store.ingest(c,'extra','Solution','url',b'old solution','old.md','solutions');build(store,c)
    t=h.prepare(c);h.confirm(c,t['id'],[f['name'] for f in t['files']])
    h.reset(c,[f['name'] for f in t['files']])
    store.execute("UPDATE items SET status='unavailable' WHERE source_key='extra'");build(store,c)
    assert [f['name'] for f in h.describe(c)['removals']]==['solutions-01.md']


def test_removal_preflight_rejects_stale_release(setup):
    store,c,h=setup; t=h.prepare(c)
    store.ingest(c,'one','Lecture','url',b'revised','lesson.md','lectures');build(store,c)
    with pytest.raises(ValueError,match='fresh removal checklist'):h.removal_check(c,t['id'])
