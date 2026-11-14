"""Exercise the real Chrome adapter with all network requests intercepted."""
import asyncio
import json
from hashlib import sha256
from types import SimpleNamespace

import pytest
from playwright.async_api import async_playwright

from onq.browser import ActionNeeded, chrome_executable
from onq.bundles import build
from onq.harness_jobs import HarnessJobs
from onq.publisher import Publisher, UploadNotSubmitted, sources_url


SOURCES_HTML = """
<button onclick="document.querySelector('dialog').showModal()">Add sources</button>
<ul id="sources"></ul>
<dialog aria-label="Add sources">
  <button onclick="chooseFile()">Upload</button><input type="file" hidden>
</dialog>
<div id="actions"></div>
<script>
const files = JSON.parse(localStorage.getItem('files') || '{"unrelated.pdf":"b3JpZ2luYWw="}');
function save(kind, name) {
  localStorage.setItem('files', JSON.stringify(files));
  const events = JSON.parse(localStorage.getItem('events') || '[]');
  events.push([kind, name]);
  localStorage.setItem('events', JSON.stringify(events));
  render();
}
function render() {
  document.querySelector('#sources').replaceChildren();
  for (const name of Object.keys(files)) {
    const row = document.createElement('li');
    const button = document.createElement('button');
    button.textContent = 'Actions';
    button.setAttribute('aria-label', 'Actions for ' + name);
    button.onclick = () => showActions(name);
    row.append(button);
    document.querySelector('#sources').append(row);
  }
}
function showActions(name) {
  const menu = document.createElement('div');
  menu.setAttribute('role','menu');
  menu.setAttribute('aria-label','Actions for ' + name);
  const download = document.createElement('a');
  download.setAttribute('role','menuitem');
  download.textContent = 'Download';
  download.download = name;
  download.href = 'data:application/octet-stream;base64,' + files[name];
  download.onclick = () => menu.remove();
  const remove = document.createElement('button');
  remove.setAttribute('role','menuitem');
  remove.textContent = 'Delete';
  remove.onclick = () => { delete files[name]; menu.remove(); save('remove',name); };
  menu.append(download,remove);
  document.querySelector('#actions').replaceChildren(menu);
}
function chooseFile() {
  if (localStorage.getItem('fail-once')) {
    localStorage.removeItem('fail-once');
    return;
  }
  document.querySelector('input').click();
}
document.querySelector('input').onchange = async event => {
  const file = event.target.files[0];
  const bytes = new Uint8Array(await file.arrayBuffer());
  files[file.name] = btoa(String.fromCharCode(...bytes));
  document.querySelector('dialog').close();
  save('upload',file.name);
};
render();
</script>
"""


@pytest.mark.parametrize('fail_before_submission', [False, True])
def test_compact_release_through_chrome(tmp_path, course_store, fail_before_submission):
    try:
        chrome_executable()
    except ActionNeeded:
        pytest.skip('Installed Google Chrome is required for this integration test.')
    store = course_store
    course = store.courses()[0]
    project = course['project']
    harness = HarnessJobs(store)
    limits = {'max_files': 4, 'max_file_bytes': 100000, 'available_bytes': 100000}

    def stage(body):
        store.ingest(course,'one','Lecture','https://example.test/one',body,'lesson.md','lectures')
        build(store, course)
        return harness.enqueue(course)

    async def exercise():
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(channel='chrome', chromium_sandbox=True)
            try:
                context = await browser.new_context(accept_downloads=True, service_workers='block')
                page = await context.new_page()

                async def fixture(route):
                    if route.request.url == sources_url(project):
                        await route.fulfill(content_type='text/html', body=SOURCES_HTML)
                    else:
                        await route.abort()

                async def dedicated(name):
                    assert name == 'chatgpt'
                    return SimpleNamespace(pages=[page])

                await context.route('**/*', fixture)
                publisher = Publisher(store, SimpleNamespace(context=dedicated))
                first = stage(b'# Original compact bundle')
                old = first['files'][0]['upload_name']
                assert await harness.publish_release(first['id'],course,publisher,limits) == 2
                second = stage(b'# Updated compact bundle')
                new = second['files'][0]['upload_name']
                if fail_before_submission:
                    await page.evaluate("localStorage.setItem('fail-once','true')")
                    with pytest.raises(UploadNotSubmitted):
                        await harness.publish_release(second['id'],course,publisher,limits)
                    expected = old
                    assert store.latest(course['id'])['cloud_status'] == 'pending'
                else:
                    assert await harness.publish_release(second['id'],course,publisher,limits) == 2
                    expected = new
                    assert store.latest(course['id'])['cloud_status'] == 'verified'
                assert set(await publisher.inspect_sources(project)) == {'unrelated.pdf', expected}
                assert await page.evaluate("JSON.parse(localStorage.getItem('events'))") == [
                    ['upload',old], ['remove',old], ['upload',expected]
                ]
                directory, files = await publisher.backup_sources(
                    project, ['unrelated.pdf', expected], tmp_path / 'cloud-backups'
                )
                manifest = json.loads((directory / 'manifest.json').read_text(encoding='utf-8'))
                assert manifest['project'] == project and manifest['files'] == files
                for file in files:
                    data = (directory / file['file']).read_bytes()
                    assert len(data) == file['size'] and sha256(data).hexdigest() == file['sha256']
                    if file['name'] == 'unrelated.pdf':
                        assert data == b'original'
                assert set(await publisher.inspect_sources(project)) == {'unrelated.pdf', expected}
                assert not store.setting('cloud_verified')
            finally:
                await browser.close()

    asyncio.run(exercise())


def test_source_readback_waits_for_rows_after_sources_control_is_ready():
    try:
        chrome_executable()
    except ActionNeeded:
        pytest.skip('Installed Google Chrome is required for this integration test.')
    project = 'https://chatgpt.com/g/g-p-fixture/project'
    fixture = SOURCES_HTML.replace('render();\n</script>', 'setTimeout(render, 700);\n</script>')

    async def exercise():
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(channel='chrome', chromium_sandbox=True)
            try:
                context = await browser.new_context(accept_downloads=True, service_workers='block')
                page = await context.new_page()

                async def route_fixture(route):
                    if route.request.url == sources_url(project):
                        await route.fulfill(content_type='text/html', body=fixture)
                    else:
                        await route.abort()

                await context.route('**/*', route_fixture)

                async def dedicated(name):
                    return SimpleNamespace(pages=[page])

                publisher = Publisher(None, SimpleNamespace(context=dedicated))
                await publisher.verify_source(project, 'unrelated.pdf', sha256(b'original').hexdigest(), 8)
            finally:
                await browser.close()

    asyncio.run(exercise())


@pytest.mark.parametrize('scenario', ['empty', 'delayed files', 'redirect', 'bad download'])
def test_first_publication_through_chrome(course_store, scenario):
    try:
        chrome_executable()
    except ActionNeeded:
        pytest.skip('Installed Google Chrome is required for this integration test.')

    store = course_store
    course = store.courses()[0]
    project = course['project']
    harness = HarnessJobs(store)
    store.ingest(course, 'fixture', 'Lecture', 'https://example.test/lesson', b'# Course fixture', 'lesson.md')
    build(store, course)
    job = harness.enqueue(course)
    name = job['files'][0]['upload_name']
    html = SOURCES_HTML
    if scenario != 'delayed files':
        html = html.replace('{"unrelated.pdf":"b3JpZ2luYWw="}', '{}')
    else:
        html = html.replace('render();\n</script>', 'setTimeout(render, 700);\n</script>')
    if scenario == 'bad download':
        html = html.replace(" + files[name];", " + 'd3Jvbmc=';")

    async def exercise():
        async with async_playwright() as pw:
            chrome = await pw.chromium.launch(channel='chrome', chromium_sandbox=True)
            try:
                context = await chrome.new_context(accept_downloads=True, service_workers='block')
                page = await context.new_page()
                destination = sources_url(project)
                other = sources_url('https://chatgpt.com/g/g-p-other/project')

                async def route_fixture(route):
                    if route.request.url == destination and scenario == 'redirect':
                        await route.fulfill(status=302, headers={'location': other})
                    elif route.request.url in (destination, other):
                        await route.fulfill(content_type='text/html', body=html)
                    elif route.request.url == 'https://chatgpt.com/settings/storage':
                        await route.fulfill(content_type='text/html', body=(
                            '<div role="progressbar" aria-label="0.1 GB of 20.0 GB used">Storage</div>'
                        ))
                    else:
                        await route.abort()

                async def dedicated(service):
                    assert service == 'chatgpt'
                    return context

                await context.route('**/*', route_fixture)
                publisher = Publisher(store, SimpleNamespace(context=dedicated))
                if scenario == 'empty':
                    assert await harness.publish_release(job['id'], course, publisher, first=True) == 1
                    assert store.course(course['id'])['verified_publish_project'] == project
                else:
                    message = {'delayed files': 'already contains', 'redirect': 'exact saved',
                               'bad download': 'does not match'}[scenario]
                    with pytest.raises(ActionNeeded, match=message):
                        await harness.publish_release(job['id'], course, publisher, first=True)
                    assert not store.course(course['id']).get('verified_publish_project')
                events = await page.evaluate("JSON.parse(localStorage.getItem('events') || '[]')")
                assert events == ([['upload', name]] if scenario in ('empty', 'bad download') else [])
            finally:
                await chrome.close()

    asyncio.run(exercise())
