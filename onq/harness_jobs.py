"""Durable desktop-publishing outbox. Queuing is never proof of a cloud upload."""
import asyncio
import json
import uuid
from pathlib import Path
from tempfile import TemporaryDirectory
from .browser import ActionNeeded, SharedBrowserBlocker, SignInNeeded
from .compact_sources import compact_sources
from . import publisher
from .publisher import UncertainSourceOperation, UploadNotSubmitted, plan_replacement, project_url, retry_read
from .store import digest, filename_stamp, now, safe

INITIAL_SOURCES_CHANGED = (
    'The saved project already contains Sources outside this verification attempt. '
    'Use Ready for ChatGPT → Manual upload, or save an empty project link and stage the bundle again.'
)


class UnresolvedSourceOperation(ActionNeeded):
    """The journaled effect is not yet visible in Sources."""


class HarnessJobs:
    def __init__(self,store):self.store=store

    @staticmethod
    def decode(row):
        return {**json.loads(row['data']),**{k:row[k] for k in ('id','course','project','release','status','trigger','created','updated','error')}}

    def list(self):
        return [self.decode(row) for row in self.store.rows('SELECT * FROM harness_jobs ORDER BY created DESC')]

    def get(self,jid):
        rows=self.store.rows('SELECT * FROM harness_jobs WHERE id=?',(jid,))
        if not rows:raise ValueError('Harness job not found')
        return self.decode(rows[0])

    def validate(self,jid,c):
        job=self.get(jid);release=self.store.latest(c['id'])
        if job['course']!=c['id'] or job['project']!=project_url(c['project']) or not release or job['release']!=release['id']:
            raise ValueError('Job destination or current release changed; queue a fresh job')
        folder=(self.store.root/job['path']).resolve()
        if not folder.is_relative_to(self.store.root):raise ValueError('Job is outside the library')
        if not isinstance(job['files'], list) or not job['files']:
            raise ValueError('Prepared job has no staged files')
        names = [f['upload_name'] for f in job['files']]
        if len(set(names)) != len(names):
            raise ValueError('Prepared job has duplicate upload names')
        for f in job['files']:
            path = (folder / f['upload_name']).resolve()
            if (Path(f['upload_name']).name != f['upload_name'] or path.parent != folder
                    or path.stat().st_size != f['size'] or digest(path.read_bytes()) != f['hash']):
                raise ValueError('Prepared job hash mismatch; no desktop actions allowed')
        return job

    def plan(self, jid, course, project, remote, owned, limits):
        job = self.validate(jid, course)
        if project != job['project'] or job.get('format') != 'compact-v1':
            raise ValueError('Replacement requires the exact saved project and a compact snapshot.')
        return plan_replacement(remote, owned, job['files'], limits)

    async def upload_staged(self, jid, course, name, browser, limits, expected_before=None):
        """Journal one upload; only a fully observed submission can establish ownership."""
        job = self.validate(jid, course)
        files = [file for file in job['files'] if file['upload_name'] == name]
        if job.get('format') != 'compact-v1' or len(files) != 1:
            raise ActionNeeded('The upload must name one Source in the current compact snapshot.')
        file = files[0]
        project = job['project']
        key = (project, name)
        rows = self.store.rows('SELECT * FROM source_uploads WHERE project=? AND name=?', key)
        names = await retry_read(browser.inspect_sources, project)
        if expected_before is not None and set(names) != expected_before:
            raise ActionNeeded(INITIAL_SOURCES_CHANGED)
        reviewed = bool(rows and rows[0]['status'] == 'absent-reviewed')
        if rows and not reviewed:
            return await self.reconcile_upload(course, project, file, rows[0], names, browser)
        if name in names:
            raise ActionNeeded('This Source name already exists without an app upload record.')
        plan_replacement([{'id': existing, 'name': existing} for existing in names], {}, files, limits)
        backup = Path(job['path']) / name
        with self.store.connect() as db:
            if reviewed:
                inserted = db.execute(
                    "UPDATE source_uploads SET status='pending',updated=? "
                    "WHERE project=? AND name=? AND course=? AND job=? AND hash=? AND size=? "
                    "AND status='absent-reviewed'",
                    (now(), *key, course['id'], jid, file['hash'], file['size'])
                ).rowcount
            else:
                inserted = db.execute(
                    'INSERT OR IGNORE INTO source_uploads VALUES (?,?,?,?,?,?,?,?,?)',
                    (*key, course['id'], jid, file['hash'], file['size'], str(backup), 'pending', now())
                ).rowcount
        if not inserted:
            raise ActionNeeded('Another attempt already recorded this upload; reconcile before retrying.')
        try:
            options = {} if expected_before is None else {'expected_before': expected_before}
            await browser.upload_source(project, self.store.root / backup, file['hash'], file['size'], limits, **options)
        except UploadNotSubmitted as exc:
            if reviewed:
                self.store.execute(
                    "UPDATE source_uploads SET status='absent-reviewed',updated=? "
                    "WHERE project=? AND name=? AND job=? AND status='pending'", (now(), *key, jid)
                )
            else:
                self.store.execute("DELETE FROM source_uploads WHERE project=? AND name=? AND job=? AND status='pending'",
                                   (*key, jid))
            if isinstance(exc.__cause__, (SharedBrowserBlocker, SignInNeeded)):
                raise exc.__cause__
            raise
        with self.store.connect() as db:
            updated = db.execute(
                "UPDATE source_uploads SET status='owned',updated=? "
                "WHERE project=? AND name=? AND status='pending'", (now(), *key)
            ).rowcount
        if not updated:
            raise ActionNeeded('The upload record changed during submission; review Sources.')
        return 'owned'

    async def reconcile_upload(self, course, project, file, prior, names, browser):
        name = file['upload_name']
        if (prior['course'], prior['hash'], prior['size']) != (course['id'], file['hash'], file['size']):
            raise ActionNeeded('The Source upload record conflicts with the staged bytes.')
        if name not in names:
            raise UnresolvedSourceOperation('The previous upload outcome is unresolved; review Sources before retrying.')
        await retry_read(browser.verify_source, project, name, file['hash'], file['size'])
        if prior['status'] == 'pending':
            self.set_rollback_state((project, name, course['id']), 'pending', 'reconciled')
            return 'reconciled'
        return prior['status']

    async def remove_staged(self, jid, course, name, browser, limits, also_remove=()):
        """Journal removal only when an owned obsolete Source has recoverable bytes."""
        job = self.validate(jid, course)
        desired = {file['upload_name'] for file in job['files']}
        if job.get('format') != 'compact-v1' or name in desired:
            raise ActionNeeded('Only an obsolete Source may be removed for this compact snapshot.')
        project = job['project']
        owned = {row['name']: row for row in self.store.rows(
            "SELECT * FROM source_uploads WHERE project=? AND course=? AND status='owned'",
            (project, course['id'])
        )}
        prior = owned.get(name)
        if prior is None:
            raise ActionNeeded('App ownership is absent or a previous removal needs reconciliation.')
        self.verified_backup(prior, name)
        names = await retry_read(browser.inspect_sources, project)
        if name not in names:
            raise ActionNeeded('The owned Source was not found; reconcile before removal.')
        planned = {name, *also_remove}
        if not planned <= owned.keys() or planned & desired:
            raise ActionNeeded('The planned removals do not match verified obsolete Sources.')
        relevant = {existing: record for existing, record in owned.items()
                    if existing in planned or existing in desired}
        plan = plan_replacement([{'id': existing, 'name': existing} for existing in names],
                                relevant, job['files'], limits)
        if sum(file['size'] for file in plan['upload']) + prior['size'] > limits['available_bytes']:
            raise ActionNeeded('Available storage cannot cover both replacement and rollback.')
        if prior['size'] > limits['max_file_bytes']:
            raise ActionNeeded('The rollback backup exceeds the confirmed file size limit.')
        key = (project, name, course['id'])
        with self.store.connect() as db:
            updated = db.execute(
                "UPDATE source_uploads SET status='removing',updated=? "
                "WHERE project=? AND name=? AND course=? AND status='owned'", (now(), *key)
            ).rowcount
            if not updated:
                raise ActionNeeded('The Source ownership record changed before removal.')
            db.execute('INSERT OR REPLACE INTO source_removals VALUES (?,?,?,?)',
                       (project, name, jid, json.dumps(names)))
        await browser.remove_source(project, name, prior['hash'], prior['size'], names)
        with self.store.connect() as db:
            updated = db.execute(
                "UPDATE source_uploads SET status='removed',updated=? "
                "WHERE project=? AND name=? AND course=? AND status='removing'", (now(), *key)
            ).rowcount
        if not updated:
            raise ActionNeeded('The removal record changed; review Sources before continuing.')

    def verified_backup(self, prior, name):
        backup = (self.store.root / prior['backup']).resolve()
        if (not backup.is_relative_to(self.store.root) or backup.name != name
                or not backup.is_file() or backup.stat().st_size != prior['size']
                or digest(backup.read_bytes()) != prior['hash']):
            raise ActionNeeded('The rollback backup is missing or does not match.')
        return backup

    async def restore_staged(self, jid, course, name, browser, limits):
        """Restore a journaled removal only against its exact prior inventory."""
        job = self.get(jid)
        saved = self.store.course(course['id'])
        if (saved is None or job['course'] != saved['id']
                or job['project'] != project_url(saved['project'])):
            raise ActionNeeded('The rollback destination no longer matches the saved project.')
        project = job['project']
        key = (project, name, saved['id'])
        rows = self.store.rows(
            'SELECT u.*,r.baseline FROM source_uploads u JOIN source_removals r '
            'ON u.project=r.project AND u.name=r.name '
            'WHERE u.project=? AND u.name=? AND u.course=? AND r.replacement_job=?', (*key, jid)
        )
        if not rows:
            raise ActionNeeded('No matching rollback record exists.')
        prior = rows[0]
        status = prior['status']
        backup = self.verified_backup(prior, name)
        baseline = set(json.loads(prior['baseline']))
        current = set(await browser.inspect_sources(project))
        if current == baseline:
            await browser.verify_source(project, name, prior['hash'], prior['size'])
            result = 'owned' if status == 'owned' else 'reconciled'
            self.set_rollback_state(key, status, result)
            return result
        if current != baseline - {name}:
            raise ActionNeeded('The current Sources inventory differs from the removal baseline.')
        if status not in ('removing', 'removed'):
            raise ActionNeeded('The previous restore outcome is unresolved; review Sources.')
        pending = self.store.rows(
            "SELECT name FROM source_uploads WHERE project=? AND status='pending'", (project,)
        )
        if pending:
            raise ActionNeeded('A replacement upload is unresolved; reconcile it before rollback.')
        plan_replacement([{'id': existing, 'name': existing} for existing in current], {},
                         [{'upload_name': name, 'hash': prior['hash'], 'size': prior['size']}], limits)
        self.set_rollback_state(key, status, 'restoring')
        await browser.upload_source(project, backup, prior['hash'], prior['size'], limits)
        self.set_rollback_state(key, 'restoring', 'owned')
        return 'owned'

    def set_rollback_state(self, key, previous, status):
        with self.store.connect() as db:
            updated = db.execute(
                'UPDATE source_uploads SET status=?,updated=? '
                'WHERE project=? AND name=? AND course=? AND status=?',
                (status, now(), *key, previous)
            ).rowcount
        if not updated:
            raise ActionNeeded('The rollback record changed; review Sources before continuing.')

    async def reconcile_release(self, job, course, browser):
        """Resolve a journaled write by readback only; absence never authorizes a reupload."""
        project, jid = job['project'], job['id']
        records = self.store.rows(
            "SELECT u.*,r.replacement_job,r.baseline FROM source_uploads u LEFT JOIN source_removals r "
            "ON u.project=r.project AND u.name=r.name "
            "WHERE u.project=? AND u.status IN ('pending','removing','restoring')", (project,)
        )
        desired = {f['upload_name']: f for f in job['files']}
        reconciled = set()
        for row in records:
            name, status = row['name'], row['status']
            key = (project, name, course['id'])
            if row['course'] != course['id']:
                raise ActionNeeded('Review the unfinished Source operation for this project before publishing.')
            if status == 'pending':
                if row['job'] != jid or name not in desired:
                    raise ActionNeeded('Review the unfinished Source upload for this project before publishing.')
                names = await retry_read(browser.inspect_sources, project)
                await self.reconcile_upload(course, project, desired[name], row, names, browser)
                reconciled.add(name)
                continue
            if row['replacement_job'] != jid:
                raise ActionNeeded('Review the unfinished Source removal or rollback before publishing.')
            self.verified_backup(row, name)
            baseline = set(json.loads(row['baseline']))
            current = set(await retry_read(browser.inspect_sources, project))
            if name not in baseline:
                raise ActionNeeded('The removal journal is invalid; review Sources before publishing.')
            if status == 'removing' and current == baseline - {name}:
                self.set_rollback_state(key, 'removing', 'removed')
            elif status == 'restoring' and current == baseline:
                await retry_read(browser.verify_source, project, name, row['hash'], row['size'])
                self.set_rollback_state(key, 'restoring', 'reconciled')
            else:
                # Neither a still-visible removal target nor an absent restore is
                # proof that the original request failed. Leave the journal intact.
                raise UnresolvedSourceOperation('The previous removal or rollback outcome is unresolved; review Sources before retrying.')
            reconciled.add(name)
        return reconciled

    async def release_write(self, operation, args, job, course, browser):
        # A chooser failure before submission clears the pending journal entry.
        # Only a temporary cause permits another attempt; all other uncertain
        # operations go through the exact inventory and byte checks above.
        for delay in (*publisher.RETRY_DELAYS, None):
            try:
                return await operation(*args)
            except UploadNotSubmitted as exc:
                if delay is None or not publisher.temporary_failure(exc.__cause__):
                    raise
            except Exception as exc:
                if not (isinstance(exc, UncertainSourceOperation) or publisher.temporary_failure(exc)):
                    raise
                # No journal entry means the write was never reached. Surface the
                # failure rather than assuming success and advancing the plan.
                name = args[2]
                for read_delay in (*publisher.RETRY_DELAYS, None):
                    try:
                        if name not in await self.reconcile_release(job, course, browser):
                            raise exc
                        return
                    except UnresolvedSourceOperation:
                        if read_delay is None:
                            raise
                        await asyncio.sleep(read_delay)
            await asyncio.sleep(delay)

    async def publish_release(self, jid, course, browser, limits=None, *, resume=False, first=False):
        """Execute one planned release with the worker's browser lock held by the caller."""
        course = self.store.course(course['id'])
        if course is None:
            raise ActionNeeded('The course is no longer configured.')
        job = self.validate(jid, course)
        project = job['project']
        if job.get('format') != 'compact-v1':
            raise ActionNeeded('Replacement requires a compact snapshot.')
        recovering_first = first and job.get('initial_publication')
        if resume or recovering_first:
            try:
                await self.reconcile_release(job, course, browser)
            except ActionNeeded as exc:
                if not first or isinstance(exc, SharedBrowserBlocker):
                    raise
                raise ActionNeeded(f'{exc} Retry Verify and publish when the upload is visible, or use Manual upload.') from exc
        records = self.store.rows('SELECT * FROM source_uploads WHERE project=?', (project,))
        if any(row['status'] in ('pending', 'removing', 'restoring') for row in records):
            if first:
                raise ActionNeeded(INITIAL_SOURCES_CHANGED)
            raise ActionNeeded('Reconcile the previous project operation before publishing.')
        desired = {file['upload_name'] for file in job['files']}
        # Byte-reconciled uploads may satisfy this exact release, but never
        # grant permission to delete a Source during a later replacement.
        owned = {row['name']: row for row in records if row['course'] == course['id']
                 and (row['status'] == 'owned' or (
                     (resume or recovering_first) and row['status'] == 'reconciled' and row['job'] == jid and row['name'] in desired
                 ))}
        inspect = browser.inspect_sources
        if first:
            # Recovery may read files journaled by this exact job. First publication
            # never adopts unrelated files or runs the replacement/removal path.
            owned = {name: record for name, record in owned.items()
                     if recovering_first and record['job'] == jid and name in desired}
            inspect = browser.inspect_initial_sources
        before = await retry_read(inspect, project)
        if first and (set(before) != owned.keys() or len(before) != len(owned)):
            raise ActionNeeded(INITIAL_SOURCES_CHANGED)
        if first:
            limits = await retry_read(browser.source_limits, min(self.store.setting('max_pdf_mb'), 40) * 1024**2)
        plan = self.plan(jid, course, project, [{'id': name, 'name': name} for name in before],
                         owned, limits)
        removals = [source['name'] for source in plan['remove']]
        for name in removals:
            prior = owned[name]
            self.verified_backup(prior, name)
            if prior['size'] > limits['max_file_bytes']:
                raise ActionNeeded('A rollback backup exceeds the confirmed file size limit.')
            await retry_read(browser.verify_source, project, name, prior['hash'], prior['size'])
        upload_bytes = sum(file['size'] for file in plan['upload'])
        rollback_bytes = sum(owned[name]['size'] for name in removals)
        if upload_bytes + rollback_bytes > limits['available_bytes']:
            raise ActionNeeded('Available storage cannot cover both replacement and rollback.')
        for file in job['files']:
            if file['upload_name'] in before:
                await retry_read(browser.verify_source, project, file['upload_name'], file['hash'], file['size'])
        if first and not recovering_first:
            self.store.execute("UPDATE harness_jobs SET data=json_set(data,'$.initial_publication',1) WHERE id=?", (jid,))
        removed = []
        try:
            for i, name in enumerate(removals):
                args = (jid, course, name, browser, limits, removals[i+1:])
                if resume:
                    await self.release_write(self.remove_staged, args, job, course, browser)
                else:
                    await self.remove_staged(*args)
                removed.append(name)
            expected_before = set(before)
            for file in plan['upload']:
                args = (jid, course, file['upload_name'], browser, limits)
                if first:
                    args += (expected_before.copy(),)
                if resume or first:
                    await self.release_write(self.upload_staged, args, job, course, browser)
                else:
                    await self.upload_staged(*args)
                expected_before.add(file['upload_name'])
        except (ActionNeeded, SignInNeeded) as exc:
            for name in reversed(removed):
                try:
                    await self.restore_staged(jid, course, name, browser, limits)
                except (SharedBrowserBlocker, SignInNeeded):
                    raise
                except ActionNeeded as recovery:
                    kind = type(exc) if isinstance(exc, (SharedBrowserBlocker, SignInNeeded)) else ActionNeeded
                    raise kind(f'{exc} Rollback needs review: {recovery}') from exc
            raise
        expected = (set(before) - set(removals)) | {file['upload_name'] for file in job['files']}
        if set(await retry_read(browser.inspect_sources, project)) != expected:
            raise ActionNeeded('The final project inventory does not match the release plan.')
        for file in job['files']:
            await retry_read(browser.verify_source, project, file['upload_name'], file['hash'], file['size'])
        if first and set(await retry_read(browser.inspect_sources, project)) != expected:
            raise ActionNeeded('The final project inventory changed during byte verification.')
        current = self.store.course(course['id'])
        if current is None:
            raise ActionNeeded('The course changed before final verification.')
        self.validate(jid, current)
        with self.store.connect() as db:
            db.execute("UPDATE releases SET cloud_status='verified' WHERE id=?", (job['release'],))
            db.execute("UPDATE harness_jobs SET status='verified',updated=?,error='' WHERE id=?", (now(), jid))
            if first:
                db.execute("UPDATE harness_jobs SET data=json_remove(data,'$.initial_publication') WHERE id=?", (jid,))
                current.update(verified_publish_project=project, auto_publish_project=None, cloud_status='Verified')
                current.pop('project_requires_verification', None)
                db.execute('UPDATE courses SET data=? WHERE id=?', (json.dumps(current), current['id']))
        return len(expected)

    def snapshot(self, c, release, previous_files=()):
        # Preserve existing names for identical bytes, including legacy names.
        prior_names = {(f['name'], f['hash'], f['size']): f['upload_name']
                       for f in previous_files if f.get('upload_name')}
        stamp = filename_stamp(release['created'])
        prefix = f'onqa-{safe(c["name"])[:40]}'
        source_root=(self.store.root/release['path']).resolve()
        if not source_root.is_relative_to(self.store.root):raise ValueError('Bundle is outside the library')
        max_bytes = int(self.store.setting('max_pdf_mb')) * 1024 * 1024
        sources = compact_sources(source_root, json.loads(release['manifest']), max_bytes)
        prepared = []
        relative = Path('Harness Queue') / c['id'] / uuid.uuid4().hex
        folder = self.store.root / relative
        folder.parent.mkdir(parents=True, exist_ok=True)
        with TemporaryDirectory(dir=folder.parent) as temporary:
            staging = Path(temporary) / 'snapshot'
            staging.mkdir()
            for name, data in sources:
                sha = digest(data)
                upload_name = prior_names.get((name, sha, len(data)), f'{prefix}-{stamp}-{name}')
                if Path(upload_name).name != upload_name:
                    raise ValueError('Invalid staged upload name')
                (staging / upload_name).write_bytes(data)
                prepared.append({'name': name, 'hash': sha, 'size': len(data), 'upload_name': upload_name})
            staging.rename(folder)
        payload = {'course_name': c['name'], 'path': str(relative), 'files': prepared, 'format': 'compact-v1'}
        return payload

    def enqueue(self,c,trigger='manual'):
        if trigger not in ('manual','schedule'):raise ValueError('Unknown sync trigger')
        if not c.get('project'):return None
        project=project_url(c['project']);release=self.store.latest(c['id'])
        if not release:return None
        # Keep remote verification independent from manual upload receipts. On its first
        # connection the real adapter must reconcile the whole set before marking current.
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute("UPDATE harness_jobs SET status='superseded',updated=? WHERE course=? AND status IN ('waiting-configuration','queued','needs-auth') AND (project<>? OR release<>?)",(now(),c['id'],project,release['id']))
            row=db.execute('SELECT * FROM harness_jobs WHERE course=? AND project=? AND release=?',(c['id'],project,release['id'])).fetchone()
            if row:
                try:
                    job = self.validate(row['id'], c)
                    if job.get('format') != 'compact-v1':
                        raise ValueError('Prepare a compact snapshot of the existing release.')
                except (ValueError,OSError):
                    previous = json.loads(row['data'])
                    payload = self.snapshot(c, release, previous.get('files', ()))
                    if previous.get('initial_publication'):
                        payload['initial_publication'] = 1
                    payload['previous_paths']=previous.get('previous_paths',[])+[previous['path']]
                    db.execute("UPDATE harness_jobs SET data=?,status='waiting-configuration',updated=?,error=? WHERE id=?",
                      (json.dumps(payload),now(),'Prepared compact snapshot; no cloud upload attempted.',row['id']))
                    row=db.execute('SELECT * FROM harness_jobs WHERE id=?',(row['id'],)).fetchone()
                if row['status']=='superseded':
                    db.execute("UPDATE harness_jobs SET status='waiting-configuration',updated=? WHERE id=?",(now(),row['id']))
                    row=db.execute('SELECT * FROM harness_jobs WHERE id=?',(row['id'],)).fetchone()
                return self.decode(dict(row))
            prior_rows = db.execute(
                "SELECT data FROM harness_jobs WHERE course=? AND project=? "
                "ORDER BY (status='verified') ASC, updated ASC, created ASC",
                (c['id'], project),
            )
            # A removed Source can return in a later release. Give it a new
            # name so the removal journal cannot be mistaken for a pending upload.
            removed = {row['name'] for row in db.execute(
                "SELECT name FROM source_uploads WHERE project=? AND status='removed'", (project,)
            )}
            # Prefer the latest verified inventory when several jobs used the same bytes.
            previous_files = []
            for prior in prior_rows:
                data = json.loads(prior['data'])
                if data.get('format') == 'compact-v1':
                    previous_files.extend(file for file in data['files'] if file['upload_name'] not in removed)
            jid = uuid.uuid4().hex
            payload = self.snapshot(c, release, previous_files)
            created=now()
            db.execute('INSERT INTO harness_jobs VALUES (?,?,?,?,?,?,?,?,?,?)',
              (jid,c['id'],project,release['id'],'waiting-configuration',trigger,created,created,json.dumps(payload),
               'Select and configure a desktop harness. No cloud upload has been attempted.'))
            row=db.execute('SELECT * FROM harness_jobs WHERE id=?',(jid,)).fetchone()
            return self.decode(dict(row))
