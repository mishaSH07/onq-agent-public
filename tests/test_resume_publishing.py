import asyncio
from unittest.mock import AsyncMock

import pytest

from onq.browser import ActionNeeded, SignInNeeded
from onq.bundles import build
from onq.harness_jobs import HarnessJobs
from onq.publisher import project_url
from onq.service import Service
from onq.store import digest


LIMITS = {'max_files': 4, 'max_file_bytes': 100000, 'available_bytes': 100000}


class FixtureSources:
    def __init__(self):
        self.files = {}
        self.writes = []
        self.reads = []
        self.block = None
        self.uncertain = None
        self.uncertain_remove = False
        self.storage_calls = 0

    async def source_limits(self, max_file_bytes):
        self.storage_calls += 1
        await asyncio.sleep(0)
        return LIMITS

    async def inspect_sources(self, project):
        self.reads.append(('inventory', project))
        if self.block:
            raise self.block
        return sorted(self.files.setdefault(project, {'unrelated.pdf': b'leave intact'}))

    async def verify_source(self, project, name, sha, size):
        self.reads.append(('bytes', project, name))
        data = self.files[project][name]
        assert digest(data) == sha and len(data) == size

    async def upload_source(self, project, path, sha, size, limits, *, expected_before=None):
        if expected_before is not None:
            assert set(self.files[project]) == expected_before
        self.writes.append(('upload', project, path.name))
        assert path.name not in self.files[project]
        if self.uncertain != 'absent':
            self.files[project][path.name] = path.read_bytes()
        if self.uncertain:
            from onq.publisher import UncertainSourceOperation
            raise UncertainSourceOperation('Network failed after file submission.')

    async def remove_source(self, project, name, sha, size, before):
        self.writes.append(('remove', project, name))
        assert set(before) == set(self.files[project])
        await self.verify_source(project, name, sha, size)
        del self.files[project][name]
        if self.uncertain_remove:
            from onq.publisher import UncertainSourceOperation
            raise UncertainSourceOperation('Network failed after removal.')


def stage(store, course, revision=b'# Fixture', queue=True):
    store.ingest(course, 'item', 'Lecture', 'https://example.test/item', revision, 'fixture.md', 'lectures')
    build(store, course)
    return HarnessJobs(store).enqueue(course) if queue else None


@pytest.fixture
def setup(course_store):
    store = course_store
    for i in range(2):
        store.save_course({**store.courses()[0], 'id': f'fixture-{i}', 'name': f'Fixture {i}'})
    courses = store.courses()
    for i, course in enumerate(courses):
        store.update_course(course['id'], enabled=True, project=f'https://chatgpt.com/g/g-p-fixture-{i}/project')
    courses = [store.course(c['id']) for c in courses]
    store.set_setting('publishing_verified_projects', {c['id']: project_url(c['project']) for c in courses})
    service = Service(store)
    browser = FixtureSources()
    service.publisher = browser
    service.collector.collect = AsyncMock(side_effect=AssertionError('resume must not collect'))
    return store, courses, service, browser


def results(service):
    return {r['course']: r for r in service.resume_batch['results']}


def test_resume_mixed_batch_uses_only_current_intact_snapshots(setup):
    store, (verified, invalid, stale, missing, fresh, newest), service, browser = setup
    done = stage(store, verified)
    asyncio.run(HarnessJobs(store).publish_release(done['id'], verified, browser, LIMITS))
    browser.writes.clear()
    damaged = stage(store, invalid)
    (store.root/damaged['path']/damaged['files'][0]['upload_name']).write_bytes(b'wrong bytes')
    stage(store, stale)
    stage(store, stale, b'# Unstaged revision', queue=False)
    stage(store, missing, queue=False)
    good = stage(store, fresh)
    stage(store, newest)
    latest = stage(store, newest, b'# Current revision')
    old_jobs = HarnessJobs(store).list()

    asyncio.run(service.run(None, 'resume-publishing'))

    outcome = results(service)
    assert outcome[verified['id']]['status'] == 'skipped'
    for course, detail in [(invalid, 'invalid'), (stale, 'stale'), (missing, 'missing')]:
        assert outcome[course['id']]['status'] == 'action-needed'
        assert detail in outcome[course['id']]['message'].lower()
    assert outcome[fresh['id']]['status'] == outcome[newest['id']]['status'] == 'verified'
    assert browser.writes == [
        ('upload', job['project'], job['files'][0]['upload_name']) for job in (good, latest)
    ]
    assert {j['id']: j['path'] for j in old_jobs} == {j['id']: j['path'] for j in HarnessJobs(store).list()}
    service.collector.collect.assert_not_awaited()


@pytest.mark.parametrize('blocker', ['sign-in', 'verification', 'profile'])
def test_shared_blocker_stops_batch_and_next_click_resumes(setup, blocker):
    from onq.browser import SharedBrowserBlocker

    store, (first, second, *rest), service, browser = setup
    for course in rest:
        store.update_course(course['id'], enabled=False)
    stage(store, first)
    stage(store, second)
    browser.block = SignInNeeded('Sign in to ChatGPT in dedicated Chrome.') if blocker == 'sign-in' else SharedBrowserBlocker(
        'Complete browser verification in dedicated Chrome.' if blocker == 'verification' else 'Close the dedicated Chrome window.'
    )

    async def exercise():
        service.start_resume_publishing()
        with pytest.raises(ActionNeeded, match='already in progress'):
            service.start_resume_publishing()
        with pytest.raises(ActionNeeded, match='already in progress'):
            service.start([first['id']], 'publish')
        with pytest.raises(ActionNeeded, match='already in progress'):
            service.start([second['id']], trigger='schedule')
        await service.task
        assert results(service)[first['id']]['status'] == 'action-needed'
        assert str(browser.block) == results(service)[first['id']]['message']
        assert results(service)[second['id']]['status'] == 'pending'
        assert not browser.writes and browser.storage_calls == 1
        assert all(store.latest(c['id'])['cloud_status'] == 'pending' for c in (first, second))
        browser.block = None
        service.start_resume_publishing()
        await service.task
        assert [r['status'] for r in results(service).values()] == ['verified', 'verified']
        assert len(browser.writes) == 2
        with pytest.raises(ActionNeeded, match='No eligible'):
            service.start_resume_publishing()

    asyncio.run(exercise())


@pytest.mark.parametrize('operation', ['upload-present', 'upload-absent', 'removal'])
def test_uncertain_mutation_reconciles_before_any_further_write(setup, monkeypatch, operation):
    from onq import publisher

    store, (course, *_), service, browser = setup
    old = stage(store, course)
    if operation == 'removal':
        asyncio.run(HarnessJobs(store).publish_release(old['id'], course, browser, LIMITS))
        browser.writes.clear()
        job = stage(store, course, b'# Replacement')
        browser.uncertain_remove = True
    else:
        job = old
        browser.uncertain = operation.removeprefix('upload-')
    name = job['files'][0]['upload_name']
    monkeypatch.setattr(publisher, 'RETRY_DELAYS', (0, 0))

    asyncio.run(service.run(None, 'resume-publishing'))

    assert browser.writes.count(('upload', job['project'], name)) == 1
    outcome = results(service)[course['id']]
    if operation == 'upload-absent':
        assert outcome['status'] == 'action-needed'
        assert HarnessJobs(store).get(job['id'])['status'] != 'verified'
        assert store.rows('SELECT status FROM source_uploads') == [{'status': 'pending'}]
        browser.uncertain = None
        asyncio.run(service.run(None, 'resume-publishing'))
        assert len(browser.writes) == 1
        assert results(service)[course['id']]['status'] == 'action-needed'
    else:
        assert outcome['status'] == 'verified'
        assert ('bytes', job['project'], name) in browser.reads
        assert set(browser.files[job['project']]) == {'unrelated.pdf', name}
        expected = 'removed' if operation == 'removal' else 'reconciled'
        assert expected in [r['status'] for r in store.rows('SELECT status FROM source_uploads')]
        if operation == 'removal':
            assert browser.writes.count(('remove', job['project'], old['files'][0]['upload_name'])) == 1
def test_temporary_reads_back_off_without_repeating_writes(setup, monkeypatch):
    from playwright.async_api import Error as ChromeError
    from onq import publisher

    store, (course, *_), service, browser = setup
    job = stage(store, course)
    name = job['files'][0]['upload_name']
    inspect = browser.inspect_sources
    attempts = []

    async def flaky_read(project):
        attempts.append(project)
        if len(attempts) <= 2:
            raise ChromeError('Page.goto: net::ERR_CONNECTION_RESET')
        return await inspect(project)

    browser.inspect_sources = flaky_read
    sleep = asyncio.sleep
    delays = []

    async def record_delay(seconds):
        delays.append(seconds)
        await sleep(0)

    monkeypatch.setattr(publisher.asyncio, 'sleep', record_delay)
    asyncio.run(service.run(None, 'resume-publishing'))

    assert delays == [0, 1, 2]  # Initial fixture storage yield, then two retries.
    assert results(service)[course['id']]['status'] == 'verified'
    assert browser.writes == [('upload', job['project'], name)]


def test_dashboard_resume_publishing_uses_isolated_fixtures(setup, monkeypatch):
    """Drive the built React dashboard and real API; abort every external request."""
    import json
    from pathlib import Path
    from threading import Event

    from fastapi.testclient import TestClient
    from playwright.sync_api import expect, sync_playwright

    from onq.browser import chrome_executable
    from onq.handoff import Handoff

    try:
        chrome_executable()
    except ActionNeeded:
        pytest.skip('Installed Chrome is required for dashboard verification.')
    if not (Path(__file__).resolve().parents[1]/'web/dist/index.html').exists():
        pytest.skip('Build the dashboard first with npm --prefix web run build.')

    store, (first, second, third, *rest), service, browser = setup
    for course in rest:
        store.update_course(course['id'], enabled=False)
    done = stage(store, first)
    asyncio.run(HarnessJobs(store).publish_release(done['id'], first, browser, LIMITS))
    browser.writes.clear()
    stage(store, second)
    stage(store, third)
    store.set_setting('paused', True)

    monkeypatch.setenv('ONQ_STATE', str(store.state))
    from onq import app as application
    monkeypatch.setattr(application, 'store', store)
    monkeypatch.setattr(application, 'service', service)
    monkeypatch.setattr(application, 'handoff', Handoff(store))

    release = Event()
    upload = browser.upload_source
    async def upload_after_pause(*args):
        while not release.is_set():
            await asyncio.sleep(0.05)
        await upload(*args)
    browser.upload_source = upload_after_pause
    inspect = browser.inspect_sources
    async def inspect_with_blocker(project):
        if project == third['project'] and not resumed.is_set():
            raise SignInNeeded('Sign in to ChatGPT in dedicated Chrome, then retry.')
        return await inspect(project)
    resumed = Event()
    browser.inspect_sources = inspect_with_blocker

    base = 'http://testserver'
    with TestClient(application.app) as client, sync_playwright() as pw:
        chrome = pw.chromium.launch(channel='chrome', chromium_sandbox=True)
        try:
            page = chrome.new_page(viewport={'width': 1400, 'height': 950}, service_workers='block')
            def route_fixture(route):
                request = route.request
                if not request.url.startswith(base + '/'):
                    route.abort()
                    return
                response = client.request(request.method, request.url, headers=request.headers,
                                          content=request.post_data_buffer)
                route.fulfill(status=response.status_code, headers=dict(response.headers), body=response.content)
            page.route('**/*', route_fixture)
            page.goto(base)
            page.get_by_role('navigation').get_by_role('button', name='Ready for ChatGPT').click()
            resume = page.get_by_role('button', name='Resume publishing', exact=True)
            expect(resume).to_be_enabled()
            assert client.post('/api/chatgpt/resume-publishing').status_code == 403
            resume.click()
            batch = page.get_by_role('region', name='Resume publishing results')
            expect(batch.get_by_text('Running', exact=True)).to_be_visible()
            expect(batch.get_by_text(second['name'], exact=True)).to_be_visible()
            expect(resume).to_be_disabled()
            release.set()

            expect(batch.get_by_role('heading', name='Publishing paused')).to_be_visible(timeout=10000)
            expect(batch.get_by_text('Action needed', exact=True)).to_be_visible()
            expect(batch.get_by_text('Sign in to ChatGPT in dedicated Chrome, then retry.', exact=True)).to_have_count(2)
            expect(batch.get_by_text('Verified', exact=True)).to_be_visible()
            expect(batch.get_by_text('Skipped', exact=True)).to_be_visible()
            expect(resume).to_be_enabled()
            resumed.set()
            resume.click()
            expect(batch.get_by_role('heading', name='Resume publishing results')).to_be_visible(timeout=10000)
            expect(batch.get_by_text('Skipped', exact=True)).to_have_count(2)
            expect(batch.get_by_text('Verified', exact=True)).to_be_visible()
            expect(resume).to_be_disabled()
            assert [project for _, project, _ in browser.writes] == [second['project'], third['project']]
        finally:
            release.set()
            chrome.close()
