import asyncio
from unittest.mock import AsyncMock

import pytest

from onq.browser import ActionNeeded
from onq.harness_jobs import HarnessJobs
from onq.service import Service
from test_resume_publishing import setup, stage


@pytest.fixture
def app_client(first_publication, monkeypatch):
    from fastapi.testclient import TestClient
    from onq.handoff import Handoff

    store, _, service, _, _, _ = first_publication
    store.set_setting('paused', True)
    monkeypatch.setenv('ONQ_STATE', str(store.state))
    from onq import app as application

    monkeypatch.setattr(application, 'store', store)
    monkeypatch.setattr(application, 'service', service)
    monkeypatch.setattr(application, 'handoff', Handoff(store))
    with TestClient(application.app) as client:
        yield client


@pytest.fixture
def first_publication(setup):
    store, (course, *_), service, browser = setup
    approvals = store.setting('publishing_verified_projects')
    del approvals[course['id']]
    store.set_setting('publishing_verified_projects', approvals)
    project = course['project']
    browser.files[project] = {}
    job = stage(store, course)
    name = job['files'][0]['upload_name']
    browser.inspect_initial_sources = browser.inspect_sources
    return store, course, service, browser, job, name


async def publish(service, course, job):
    service.start_verify_and_publish(course, job['project'], job['id'])
    await service.task


def test_first_publication_approves_exact_project_and_leaves_opt_in_off(first_publication):
    store, course, service, browser, job, name = first_publication
    async def exercise():
        service.start_verify_and_publish(course, job['project'], job['id'])
        with pytest.raises(ActionNeeded, match='already in progress'):
            service.start_verify_and_publish(course, job['project'], job['id'])
        await service.task
        with pytest.raises(ActionNeeded, match='already verified'):
            service.start_verify_and_publish(store.course(course['id']), job['project'], job['id'])

    asyncio.run(exercise())
    saved = store.course(course['id'])
    assert service.publishing_verified(saved)
    assert not service.automatic_publishing(saved)
    assert HarnessJobs(store).get(job['id'])['status'] == 'verified'
    assert browser.writes == [('upload', job['project'], name)]
    assert ('bytes', job['project'], name) in browser.reads
    assert store.rows('SELECT status FROM source_uploads') == [{'status': 'owned'}]


@pytest.mark.parametrize('problem', ['nonempty', 'wrong destination', 'storage', 'wrong bytes'])
def test_first_publication_stops_without_approval(first_publication, problem):
    store, course, service, browser, job, name = first_publication
    if problem == 'nonempty':
        browser.files[job['project']][name] = b'unrelated even with matching name'
    elif problem == 'wrong destination':
        browser.block = ActionNeeded('The browser did not reach the exact saved Sources destination.')
    elif problem == 'storage':
        browser.source_limits = AsyncMock(return_value={
            'max_files': 4, 'max_file_bytes': 100000, 'available_bytes': 0,
        })
    else:
        browser.verify_source = AsyncMock(side_effect=ActionNeeded('The downloaded Source does not match.'))

    asyncio.run(publish(service, course, job))
    assert not service.publishing_verified(store.course(course['id']))
    assert HarnessJobs(store).get(job['id'])['status'] != 'verified'
    assert len(browser.writes) == (1 if problem == 'wrong bytes' else 0)
    run = store.rows('SELECT status,message FROM runs')[0]
    assert run['status'] == 'action-needed'
    if problem == 'nonempty':
        assert 'Manual upload' in run['message']


@pytest.mark.parametrize('visible', [True, False])
def test_interrupted_first_publication_reconciles_without_resubmitting(first_publication, visible):
    store, course, service, browser, job, name = first_publication
    upload = browser.upload_source

    async def interrupt(*args, **kwargs):
        await upload(*args, **kwargs)
        if not visible:
            del browser.files[job['project']][name]
        raise asyncio.CancelledError

    async def exercise():
        browser.upload_source = interrupt
        with pytest.raises(asyncio.CancelledError):
            await publish(service, course, job)
        assert not service.publishing_verified(store.course(course['id']))
        browser.upload_source = upload
        restarted = Service(store)
        restarted.publisher = browser
        await publish(restarted, course, job)
        assert restarted.publishing_verified(store.course(course['id'])) == visible

    asyncio.run(exercise())
    assert browser.writes == [('upload', job['project'], name)]
    assert store.rows('SELECT status FROM source_uploads') == [
        {'status': 'reconciled' if visible else 'pending'},
    ]


def test_verify_requires_current_snapshot_and_confirmed_destination(first_publication):
    store, course, service, browser, job, _ = first_publication
    with pytest.raises(ActionNeeded, match='destination'):
        service.start_verify_and_publish(course, 'https://chatgpt.com/g/g-p-other/project', job['id'])
    stage(store, course, b'# New revision', queue=False)
    with pytest.raises(ActionNeeded, match='Queue desktop agent'):
        service.start_verify_and_publish(course, job['project'], job['id'])
    assert not browser.writes


def test_changing_verified_link_resets_approval_and_automatic_publishing(first_publication, app_client):
    store, course, service, browser, job, _ = first_publication
    asyncio.run(publish(service, course, job))
    headers = {'X-OnQ-Client': 'dashboard'}
    route = f"/api/courses/{course['id']}"
    other = next(c for c in store.courses() if c['id'] != course['id'])
    assert app_client.patch(route + '/chatgpt/automatic', headers=headers, json={'enabled': True}).is_success
    assert service.automatic_publishing(store.course(course['id']))
    for project in ('https://chatgpt.com/g/g-p-new-project/project', job['project']):
        assert app_client.patch(route, headers=headers, json={'project': project}).is_success
        saved = store.course(course['id'])
        assert not service.publishing_verified(saved)
        assert not service.automatic_publishing(saved)
        row = next(c for c in app_client.get('/api/state').json()['courses'] if c['id'] == course['id'])
        assert row['publishing']['verified_release'] is None
        assert app_client.patch(route + '/chatgpt/automatic', headers=headers, json={'enabled': True}).status_code == 409
    assert service.publishing_verified(other)
    assert len(browser.writes) == 1


def test_dashboard_first_publication(first_publication, app_client):
    from pathlib import Path
    from playwright.sync_api import expect, sync_playwright
    from onq.browser import chrome_executable

    try:
        chrome_executable()
    except ActionNeeded:
        pytest.skip('Installed Chrome is required for dashboard verification.')
    if not (Path(__file__).resolve().parents[1]/'web/dist/index.html').exists():
        pytest.skip('Build the dashboard first with npm --prefix web run build.')

    store, course, _, browser, job, name = first_publication
    unstaged = next(c for c in store.courses() if c['id'] != course['id'])
    store.update_course(unstaged['id'], project='https://chatgpt.com/g/g-p-unstaged/project')
    base = 'http://testserver'
    api_path = f"/api/courses/{course['id']}/chatgpt/verify-and-publish"
    assert app_client.post(api_path, json={'project': job['project'], 'job': job['id']}).status_code == 403

    with sync_playwright() as pw:
        chrome = pw.chromium.launch(channel='chrome', chromium_sandbox=True)
        try:
            page = chrome.new_page(viewport={'width': 1400, 'height': 950}, service_workers='block')

            def route_fixture(route):
                request = route.request
                if not request.url.startswith(base + '/'):
                    route.abort()
                    return
                response = app_client.request(request.method, request.url, headers=request.headers,
                                              content=request.post_data_buffer)
                route.fulfill(status=response.status_code, headers=dict(response.headers), body=response.content)

            page.route('**/*', route_fixture)
            page.goto(base)
            page.get_by_role('navigation').get_by_role('button', name='Ready for ChatGPT').click()
            row = page.locator('.list-row').filter(has_text=job['project'])
            unstaged_row = page.locator('.list-row').filter(has_text='https://chatgpt.com/g/g-p-unstaged/project')
            verify = row.get_by_role('button', name='Verify and publish', exact=True)
            automatic = row.get_by_role('checkbox', name='Publish after sync')
            expect(unstaged_row.get_by_text('Sync this course to collect materials and stage its bundle.')).to_be_visible()
            expect(unstaged_row.get_by_role('button', name='Verify and publish')).to_be_disabled()
            expect(automatic).to_be_disabled()
            verify.click()
            review = page.get_by_role('dialog', name='Confirm first publication')
            expect(review.get_by_text(course['name'], exact=True)).to_be_visible()
            expect(review.get_by_text(job['project'], exact=True)).to_be_visible()
            review.get_by_role('button', name='Cancel').click()
            assert not browser.writes

            browser.files[job['project']]['unrelated.pdf'] = b'preserve'
            verify.click()
            review.get_by_role('button', name='Confirm and publish').click()
            expect(row.get_by_text('Use Ready for ChatGPT → Manual upload', exact=False)).to_be_visible(timeout=10000)
            expect(automatic).to_be_disabled()
            assert not browser.writes

            del browser.files[job['project']]['unrelated.pdf']
            verify.click()
            review.get_by_role('button', name='Confirm and publish').click()
            expect(automatic).to_be_enabled(timeout=10000)
            expect(automatic).not_to_be_checked()
            expect(verify).to_have_count(0)
            automatic.click()
            expect(automatic).to_be_checked()
            assert browser.writes == [('upload', job['project'], name)]

            nav = page.get_by_role('navigation')
            nav.get_by_role('button', name='Courses', exact=True).click()
            course_row = page.locator('.list-row').filter(has=page.get_by_text(course['name'], exact=True))
            collect = course_row.get_by_role('checkbox', name='Collect this course')
            collect.click()
            expect(collect).not_to_be_checked()
            nav.get_by_role('button', name='File Library', exact=True).click()
            expect(page.get_by_text(course['name'], exact=True)).to_have_count(0)
            assert store.items(course['id'])
            nav.get_by_role('button', name='Courses', exact=True).click()
            collect.click()
            expect(collect).to_be_checked()
            nav.get_by_role('button', name='File Library', exact=True).click()
            expect(page.get_by_text(course['name'], exact=True)).to_be_visible()
        finally:
            chrome.close()
