"""Preflight and scoped execution for migrating existing user-managed Sources."""
import json
import re
from hashlib import file_digest
from pathlib import Path
from shutil import copyfile
from tempfile import TemporaryDirectory

from .browser import ActionNeeded
from .harness_jobs import HarnessJobs
from .publisher import UploadNotSubmitted
from .store import now


async def review_migration(store, course, job_id, backup_id, browser):
    review, *_ = await _review(store, course, job_id, backup_id, browser)
    return review


async def _review(store, course, job_id, backup_id, browser, uploaded=()):
    if not re.fullmatch(r'[a-f0-9]{32}', backup_id):
        raise ValueError('Select a valid Source backup identifier.')
    job = HarnessJobs(store).validate(job_id, course)
    if job.get('format') != 'compact-v1':
        raise ActionNeeded('Stage a compact publishing snapshot before migration review.')
    project = job['project']
    root = store.root.resolve()
    folder = (root / 'Cloud Source Backups' / course['id'] / backup_id).resolve()
    if not folder.is_relative_to(root):
        raise ActionNeeded('The Source backup is outside the archive.')
    try:
        manifest = json.loads((folder / 'manifest.json').read_text(encoding='utf-8'))
        entries = manifest['files']
        existing = sorted(entry['name'] for entry in entries)
        if manifest['project'] != project or not existing or len(existing) != len(set(existing)):
            raise ActionNeeded('The backup manifest does not match the exact project inventory.')
        for entry in entries:
            path = (folder / entry['file']).resolve()
            if path.parent != folder or path.stat().st_size != entry['size']:
                raise ActionNeeded('The saved Source backup bytes do not match the manifest.')
            with path.open('rb') as stream:
                if file_digest(stream, 'sha256').hexdigest() != entry['sha256']:
                    raise ActionNeeded('The saved Source backup bytes do not match the manifest.')
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise ActionNeeded('The saved Source backup could not be verified.') from exc

    page = await browser.open_sources(project)
    expected = sorted(uploaded) if uploaded else existing
    if sorted(await browser.source_names(page, project)) != expected:
        raise ActionNeeded('The live Sources differ from the backup. Take a new backup before planning.')
    if uploaded:
        for file in job['files']:
            if file['upload_name'] in uploaded:
                await browser.verify_source_on_page(page, project, file['upload_name'], file['hash'], file['size'])
    else:
        for entry in entries:
            await browser.verify_source_on_page(page, project, entry['name'], entry['sha256'], entry['size'])
    await browser.confirm_source_inventory(project, expected)
    limits = await browser.source_limits(min(store.setting('max_pdf_mb'), 40) * 1024**2)
    replacements = job['files']
    if any(file['upload_name'] in existing for file in replacements):
        raise ActionNeeded('A staged upload name already exists in the project. Review ownership first.')
    if (len(replacements) > limits['max_files']
            or any(file['size'] > limits['max_file_bytes'] for file in replacements)):
        raise ActionNeeded('The staged replacement does not fit the current project limits.')
    incoming_bytes = sum(file['size'] for file in replacements)
    backup_bytes = sum(entry['size'] for entry in entries)
    if incoming_bytes + backup_bytes > limits['available_bytes']:
        raise ActionNeeded('Available account storage cannot cover replacement and backup restoration.')
    return {
        'project': project, 'release': job['release'], 'backup': backup_id,
        'existing': existing, 'replacement': replacements,
        'minimum_removals': max(0, len(existing) + len(replacements) - limits['max_files']),
        'incoming_bytes': incoming_bytes, 'backup_bytes': backup_bytes,
    }, folder, entries, limits


async def execute_migration(store, course, job_id, backup_id, release, approved_names, browser,
                            witnessed_submission=None):
    """Replace only the explicitly authorized, byte-verified baseline under the worker lock."""
    journal = 'source_migration_' + job_id
    previous = store.setting(journal)
    submitted = previous is not None and witnessed_submission == previous.get('name') and bool(witnessed_submission)
    if witnessed_submission and not submitted:
        raise ActionNeeded('The witnessed submission must match the exact interrupted upload.')
    if previous and (previous['phase'] != 'uploading' or previous['remaining'] or not previous['uploaded']
                     or (not submitted and previous.get('retried') == previous['name'])
                     or (previous['backup'], previous['release'], previous['approved'])
                     != (backup_id, release, sorted(approved_names))):
        raise ActionNeeded('This migration was already attempted. Review its journal before any retry.')
    visible = list(previous['uploaded']) if previous else []
    if submitted:
        visible.append(witnessed_submission)
    review, folder, entries, limits = await _review(store, course, job_id, backup_id, browser, visible)
    if release != review['release'] or sorted(approved_names) != review['existing']:
        raise ActionNeeded('The current release and exact Source names must match the migration approval.')
    project = review['project']
    records = store.rows('SELECT * FROM source_uploads WHERE project=?', (project,))
    if not previous and records:
        raise ActionNeeded('Review existing production ownership records before migrating this project.')
    # The local compact limit applies to new bundles. The provider documents a
    # separate 512 MB document limit, which applies to restoring original PDFs.
    rollback_limits = {**limits, 'max_file_bytes': 512_000_000}
    state = previous or {'project': project, 'backup': backup_id, 'release': release,
             'approved': sorted(approved_names), 'remaining': sorted(approved_names),
             'uploaded': []}
    harness = HarnessJobs(store)

    def checkpoint(phase, name=None):
        store.set_setting(journal, {**state, 'phase': phase, 'name': name})

    with TemporaryDirectory(dir=folder.parent) as temporary:
        originals = {}
        for entry in entries:
            name = entry['name']
            if (not name or Path(name).name != name or name in ('.', '..')
                    or not 0 < entry['size'] <= rollback_limits['max_file_bytes']):
                raise ActionNeeded('A backup cannot be safely staged for restoration.')
            target = Path(temporary) / name
            copyfile(folder / entry['file'], target)
            with target.open('rb') as stream:
                if target.stat().st_size != entry['size'] or file_digest(stream, 'sha256').hexdigest() != entry['sha256']:
                    raise ActionNeeded('A staged rollback copy does not match the archived Source.')
            originals[name] = (target, entry['sha256'], entry['size'])

        if previous:
            files = review['replacement']
            completed = state['uploaded']
            pending = state['name']
            if (state['project'] != project or
                    [file['upload_name'] for file in files[:len(completed) + 1]] != [*completed, pending]):
                raise ActionNeeded('The interrupted upload does not match the approved release order.')
            expected = {
                file['upload_name']: (file['hash'], file['size'],
                                      'owned' if file['upload_name'] in completed else 'pending')
                for file in files[:len(completed) + 1]
            }
            actual = {row['name']: (row['hash'], row['size'], row['status']) for row in records
                      if row['course'] == course['id'] and row['job'] == job_id}
            if len(actual) != len(records) or actual != expected:
                raise ActionNeeded('The previous upload records differ from the reviewed live inventory.')
            status = 'owned' if submitted else 'absent-reviewed'
            if submitted:
                state['uploaded'].append(pending)
            else:
                state['retried'] = pending
            with store.connect() as db:
                db.execute("UPDATE source_uploads SET status=?,updated=? "
                           "WHERE project=? AND name=? AND status='pending'", (status, now(), project, pending))
                db.execute('UPDATE settings SET value=? WHERE key=?',
                           (json.dumps({**state, 'phase': 'uploaded' if submitted else 'uploading'}), journal))

        for name in sorted(state['remaining'], key=lambda name: originals[name][2]):
            _, sha, size = originals[name]
            checkpoint('removing', name)
            await browser.remove_source(project, name, sha, size, state['remaining'])
            state['remaining'].remove(name)
            checkpoint('removed', name)

        try:
            for file in review['replacement'][len(state['uploaded']):]:
                name = file['upload_name']
                await browser.confirm_source_inventory(project, state['uploaded'])
                checkpoint('uploading', name)
                if await harness.upload_staged(job_id, course, name, browser, limits) != 'owned':
                    raise ActionNeeded('A replacement upload lacks verified ownership.')
                state['uploaded'].append(name)
                checkpoint('uploaded', name)
        except UploadNotSubmitted:
            await browser.confirm_source_inventory(project, state['uploaded'])
            for name in reversed(state['uploaded'].copy()):
                file = next(file for file in review['replacement'] if file['upload_name'] == name)
                checkpoint('rollback-removing', name)
                await browser.remove_source(project, name, file['hash'], file['size'], state['uploaded'])
                store.execute("UPDATE source_uploads SET status='removed',updated=? WHERE project=? AND name=?",
                              (now(), project, name))
                state['uploaded'].remove(name)
            for name, (path, sha, size) in originals.items():
                checkpoint('rollback-restoring', name)
                await browser.upload_source(project, path, sha, size, rollback_limits)
                state['remaining'].append(name)
            await browser.confirm_source_inventory(project, approved_names)
            for name, (_, sha, size) in originals.items():
                await browser.verify_source(project, name, sha, size)
            checkpoint('rolled-back')
            raise

        await browser.confirm_source_inventory(project, state['uploaded'])
        count = await harness.publish_release(job_id, course, browser, limits)
        store.update_course(course['id'], cloud_status='Verified')
        checkpoint('verified')
        return count
