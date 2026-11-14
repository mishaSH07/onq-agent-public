"""Conservative browser publisher: fail closed on an unrecognized project UI."""
import asyncio
import json
import re
from contextlib import asynccontextmanager
from decimal import Decimal
from hashlib import file_digest
from mimetypes import guess_type
from pathlib import Path
from shutil import copyfile
from tempfile import TemporaryDirectory
from uuid import uuid4
from urllib.parse import urlparse
from playwright.async_api import Error as PlaywrightError, TimeoutError as PlaywrightTimeout
from .browser import ActionNeeded, SharedBrowserBlocker, SignInNeeded
from .store import digest, now

def browser_verification_title(title):
    return any(s in title.lower() for s in ('just a moment','verify you are human','security verification'))

def project_url(url):
    if not re.fullmatch(r'https://chatgpt\.com/g/g-p-[a-zA-Z0-9-]+(?:/project)?/?',url):
        raise ValueError('Use the actual https://chatgpt.com/g/g-p-…/project URL.')
    return url.rstrip('/') if url.rstrip('/').endswith('/project') else url.rstrip('/')+'/project'

def sources_url(project):
    return project_url(project) + '?tab=sources'


STORAGE_URL = 'https://chatgpt.com/settings/storage'
STORAGE_LABEL = re.compile(
    r'^([0-9]+(?:\.[0-9]+)?) (B|KB|MB|GB|TB) of ([0-9]+(?:\.[0-9]+)?) (B|KB|MB|GB|TB) used$'
)


def storage_headroom(label):
    match = STORAGE_LABEL.fullmatch(label)
    if not match:
        raise ActionNeeded('Account storage usage could not be confirmed.')
    used, used_unit, capacity, capacity_unit = match.groups()
    units = {'B': 0, 'KB': 1, 'MB': 2, 'GB': 3, 'TB': 4}
    # The UI may round values and use different byte units. Reserve the
    # full last displayed digit on each side, with the smaller capacity unit.
    used_bound = (Decimal(used) + Decimal(1).scaleb(Decimal(used).as_tuple().exponent)) * 1024**units[used_unit]
    capacity_bound = (Decimal(capacity) - Decimal(1).scaleb(Decimal(capacity).as_tuple().exponent)) * 1000**units[capacity_unit]
    return max(0, int(capacity_bound - used_bound))


def plan_replacement(remote, owned, files, limits):
    """Plan from an exact project inventory and prior independently verified ownership."""
    max_files, max_file_bytes, available_bytes = (
        limits.get(key) for key in ('max_files', 'max_file_bytes', 'available_bytes')
    )
    if any(type(value) is not int or value < 0
           for value in (max_files, max_file_bytes, available_bytes)):
        raise ActionNeeded('Confirm project and account capacity before replacing Sources.')
    by_name = {source['name']: source for source in remote}
    ids = {source['id'] for source in remote}
    if len(ids) != len(remote) or len(by_name) != len(remote) or not all(ids):
        raise ActionNeeded('Source identities are ambiguous; refresh the project inventory.')
    wanted = {file['upload_name']: file for file in files}
    if not wanted or len(wanted) != len(files):
        raise ActionNeeded('The prepared release has missing or duplicate upload names.')

    remove, preserve = [], []
    for source in remote:
        prior = owned.get(source['id'])
        desired = wanted.get(source['name'])
        if prior is not None and (prior['name'] != source['name'] or not prior['hash']):
            raise ActionNeeded('Source ownership no longer matches the current inventory.')
        if desired is not None:
            if prior is None or prior['hash'] != desired['hash']:
                raise ActionNeeded('Cannot confirm ownership of an existing upload name.')
            preserve.append(source)
        elif prior is not None:
            remove.append(source)
        else:
            preserve.append(source)

    upload = [file for file in files if file['upload_name'] not in by_name]
    if len(preserve) + len(upload) > min(max_files, 4):
        raise ActionNeeded('The resulting project would exceed its file count limit.')
    if any(type(file['size']) is not int or not 0 < file['size'] <= max_file_bytes for file in files):
        raise ActionNeeded('A prepared Source exceeds the confirmed file size limit.')
    if sum(file['size'] for file in upload) > available_bytes:
        raise ActionNeeded('Available account storage is insufficient for the new Sources.')
    return {'remove': remove, 'upload': upload, 'preserve': preserve}


class UploadNotSubmitted(ActionNeeded):
    """The browser failed before any file bytes were handed to the upload control."""


class SourceReadbackTimeout(ActionNeeded):
    """The browser did not finish a readback within its bounded wait."""


class UncertainSourceOperation(ActionNeeded):
    """A temporary failure may have interrupted a remote write or its verification."""


class TemporaryBrowserFailure(ActionNeeded):
    """A navigation timed out before the destination could be read."""


RETRY_DELAYS = (1, 2)
TEMPORARY_CHROME_ERRORS = re.compile(
    r'net::ERR_(?:TIMED_OUT|CONNECTION_(?:RESET|CLOSED|TIMED_OUT|ABORTED)|'
    r'INTERNET_DISCONNECTED|NETWORK_CHANGED|NAME_NOT_RESOLVED)\b'
)


def temporary_failure(exc):
    return (isinstance(exc, (TemporaryBrowserFailure, SourceReadbackTimeout))
            or isinstance(exc, PlaywrightError) and bool(TEMPORARY_CHROME_ERRORS.search(str(exc))))


async def retry_read(operation, *args):
    """Retry only reads with evidence of a temporary failure."""
    for delay in (*RETRY_DELAYS, None):
        try:
            return await operation(*args)
        except Exception as exc:
            if not temporary_failure(exc):
                raise
            if delay is None:
                raise ActionNeeded('ChatGPT did not respond after retries. Check your connection and try again.') from exc
        await asyncio.sleep(delay)


class Publisher:
    def __init__(self,store,browsers):
        self.store=store
        self.browsers=browsers
        self.storage_page=None

    async def require_chatgpt_access(self, page):
        if browser_verification_title(await page.title()):
            raise SharedBrowserBlocker('Complete browser verification yourself in dedicated Chrome, then retry.')
        login_redirect = urlparse(page.url).hostname == 'auth.openai.com'
        if login_redirect or await page.get_by_role('button', name='Log in', exact=True).count():
            raise SignInNeeded('Sign in to ChatGPT in the dedicated browser, then retry.')

    async def source_limits(self, max_file_bytes):
        ctx = await self.browsers.context('chatgpt')
        page = self.storage_page
        retrying = page is not None and not page.is_closed()
        if not retrying:
            page = await ctx.new_page()
        keep_open = False
        try:
            if retrying:
                await self.require_chatgpt_access(page)
            if not retrying or page.url != STORAGE_URL:
                await self.navigate(page, STORAGE_URL, wait_until='domcontentloaded', timeout=30000)
            await self.require_chatgpt_access(page)
            indicator = page.get_by_role('progressbar', name=STORAGE_LABEL)
            await indicator.wait_for(state='visible', timeout=10000)
            if page.url != STORAGE_URL or await indicator.count() != 1:
                raise ActionNeeded('The exact account storage view could not be confirmed.')
            label = await indicator.get_attribute('aria-label')
            if label is None:
                snapshot = await indicator.aria_snapshot()
                match = re.match(r'^- progressbar "([^"]+)"', snapshot)
                label = match.group(1) if match else ''
            return {'max_files': 4, 'max_file_bytes': max_file_bytes,
                    'available_bytes': storage_headroom(label)}
        except PlaywrightTimeout as exc:
            try:
                await self.require_chatgpt_access(page)
            except (ActionNeeded, SignInNeeded):
                keep_open = True
                raise
            raise ActionNeeded('Account storage usage could not be confirmed in dedicated Chrome.') from exc
        except (ActionNeeded, SignInNeeded):
            try:
                await self.require_chatgpt_access(page)
            except (ActionNeeded, SignInNeeded):
                keep_open = True
            raise
        finally:
            self.storage_page = page if keep_open else None
            if not keep_open:
                await page.close()

    async def navigate(self, page, url, **options):
        try:
            await page.goto(url, **options)
        except PlaywrightTimeout as exc:
            await self.require_chatgpt_access(page)
            raise TemporaryBrowserFailure('ChatGPT did not finish loading.') from exc

    async def open_sources(self, project):
        """Open the inline Sources view for inspection without changing any Sources."""
        destination = sources_url(project)
        ctx = await self.browsers.context('chatgpt')
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        await self.navigate(page, destination, wait_until='domcontentloaded', timeout=30000)
        await self.require_chatgpt_access(page)
        if page.url != destination:
            raise ActionNeeded('The browser did not reach the exact saved Sources destination.')
        marker = page.get_by_role('button', name='Add sources', exact=True)
        try:
            await marker.wait_for(state='visible', timeout=10000)
        except PlaywrightTimeout as exc:
            await self.require_chatgpt_access(page)
            raise ActionNeeded('The inline ChatGPT Sources view could not be confirmed.') from exc
        if page.url != destination or await marker.count() != 1:
            raise ActionNeeded('The exact Sources destination or its controls could not be confirmed.')
        return page

    async def inspect_sources(self, project):
        """Read visible file rows without treating them as ownership or a quota signal."""
        page = await self.open_sources(project)
        return await self.source_names(page, project)

    async def inspect_initial_sources(self, project):
        """Allow the Sources list time to hydrate before relying on an empty inventory."""
        page = await self.open_sources(project)
        for _ in range(10):
            names = await self.source_names(page, project)
            if names:
                return names
            await asyncio.sleep(0.5)
        return await self.source_names(page, project)

    async def source_names(self, page, project, *, include_hidden=False):
        prefix = 'Actions for '
        options = {'name': re.compile(r'^Actions for .+'), 'include_hidden': include_hidden}
        controls = page.get_by_role('button', **options)
        rows = page.get_by_role('listitem', include_hidden=include_hidden).filter(has=controls)
        labels = await rows.get_by_role('button', **options).evaluate_all(
            "(buttons) => buttons.map(button => button.getAttribute('aria-label'))"
        )
        if page.url != sources_url(project):
            raise ActionNeeded('The exact Sources destination changed during inspection.')
        if any(not label or not label.startswith(prefix) or not label[len(prefix):].strip()
               for label in labels) or len(set(labels)) != len(labels):
            raise ActionNeeded('Visible Source names are ambiguous.')
        return [label[len(prefix):] for label in labels]

    async def confirm_source_inventory(self, project, expected, transient=()):
        """Wait on the reopened page for the exact expected Sources to settle."""
        page = await self.open_sources(project)
        expected = set(expected)
        allowed = expected | set(transient)
        for attempt in range(15):
            observed = set(await self.source_names(page, project))
            if observed == expected:
                return
            if observed - allowed:
                break
            if attempt < 14:
                await asyncio.sleep(1)
        await self.require_chatgpt_access(page)
        raise ActionNeeded('The Sources list did not settle to the exact expected inventory.')

    async def source_action(self, page, name):
        label = 'Actions for ' + name
        buttons = page.get_by_role('button', name=label, exact=True)
        row = page.get_by_role('listitem').filter(has=buttons)
        action = row.get_by_role('button', name=label, exact=True)
        if await action.count() == 0:
            try:
                # Sources controls can render before the file list hydrates.
                await action.first.wait_for(state='visible', timeout=10000)
            except PlaywrightTimeout as exc:
                await self.require_chatgpt_access(page)
                raise ActionNeeded('The exact Source row could not be identified.') from exc
        if await row.count() != 1 or await action.count() != 1:
            raise ActionNeeded('The exact Source row could not be identified.')
        return action

    async def verify_source(self, project, name, expected_hash, expected_size):
        """Compare the exact Source's downloaded bytes without adopting ownership."""
        page = await self.open_sources(project)
        await self.verify_source_on_page(page, project, name, expected_hash, expected_size)
        return page

    async def verify_source_on_page(self, page, project, name, expected_hash, expected_size):
        async with self.download_source(page, project, name) as path:
            with path.open('rb') as stream:
                if path.stat().st_size != expected_size or file_digest(stream, 'sha256').hexdigest() != expected_hash:
                    raise ActionNeeded('The downloaded Source does not match the expected file.')

    @asynccontextmanager
    async def download_source(self, page, project, name):
        destination = sources_url(project)
        action = await self.source_action(page, name)
        download = None
        step = 'opening the exact Source menu'
        try:
            if page.url != destination:
                raise ActionNeeded('The exact Sources destination changed before readback.')
            menu = page.get_by_role('menu', name='Actions for ' + name, exact=True)
            if not await menu.count():
                await action.click(timeout=10000)
            control = menu.get_by_role('menuitem', name='Download', exact=True)
            step = 'waiting for the Download menu item'
            await control.wait_for(state='visible', timeout=10000)
            if page.url != destination or await menu.count() != 1 or await control.count() != 1:
                raise ActionNeeded('The exact Source download control could not be confirmed.')
            step = 'waiting for Chrome to start the download'
            async with page.expect_download(timeout=20000) as event:
                await control.click(timeout=10000)
            download = await event.value
            step = 'reading the downloaded bytes'
            path = await asyncio.wait_for(download.path(), timeout=60)
            if page.url != destination:
                raise ActionNeeded('The exact Sources destination changed during readback.')
            if download.suggested_filename != name:
                raise ActionNeeded('The downloaded Source does not match the expected file.')
            yield path
        except (PlaywrightTimeout, TimeoutError) as exc:
            await self.require_chatgpt_access(page)
            raise SourceReadbackTimeout(f'The exact Source download timed out while {step}.') from exc
        except OSError as exc:
            raise ActionNeeded(f'The exact Source download could not be verified while {step}.') from exc
        finally:
            if download is not None:
                await download.delete()

    async def backup_sources(self, project, names, directory):
        """Preserve an exact project inventory locally without adopting ownership."""
        expected = set(names)
        if not expected or len(expected) != len(names):
            raise ActionNeeded('Choose the full list of unique Source names to back up.')
        page = await self.open_sources(project)
        if set(await self.source_names(page, project)) != expected:
            raise ActionNeeded('The live Sources inventory differs from the requested backup.')
        directory.mkdir(parents=True, exist_ok=True)
        destination = directory / uuid4().hex
        files = []
        with TemporaryDirectory(dir=directory) as temporary:
            stage = Path(temporary) / 'snapshot'
            stage.mkdir()
            for index, name in enumerate(sorted(expected), 1):
                local = stage / f'{index:02d}.bin'
                async with self.download_source(page, project, name) as path:
                    copyfile(path, local)
                    with path.open('rb') as stream, local.open('rb') as copy:
                        sha = file_digest(stream, 'sha256').hexdigest()
                        if file_digest(copy, 'sha256').hexdigest() != sha:
                            raise ActionNeeded('The local Source backup did not match the download.')
                    files.append({'name': name, 'file': local.name, 'sha256': sha, 'size': local.stat().st_size})
            await self.confirm_source_inventory(project, expected)
            (stage / 'manifest.json').write_text(
                json.dumps({'project': project_url(project), 'created': now(), 'files': files}, indent=2),
                encoding='utf-8'
            )
            stage.rename(destination)
        return destination, files

    async def remove_source(self, project, name, expected_hash, expected_size, before):
        """Remove a byte-verified Source and confirm the exact remaining inventory."""
        page = await self.verify_source(project, name, expected_hash, expected_size)
        if name not in before or set(await self.source_names(page, project)) != set(before):
            raise ActionNeeded('Sources changed before removal; refresh the replacement plan.')
        action = await self.source_action(page, name)
        menu = page.get_by_role('menu', name='Actions for ' + name, exact=True)
        delete = menu.get_by_role('menuitem', name='Delete', exact=True)
        try:
            if not await menu.count():
                await action.click(timeout=10000)
            await delete.wait_for(state='visible', timeout=10000)
            if page.url != sources_url(project) or await menu.count() != 1 or await delete.count() != 1:
                raise ActionNeeded('The exact Source delete control could not be confirmed.')
            await delete.click(timeout=10000)
            await action.wait_for(state='detached', timeout=15000)
        except PlaywrightTimeout as exc:
            await self.require_chatgpt_access(page)
            raise UncertainSourceOperation('The removal outcome is uncertain. Reconcile Sources before retrying.') from exc
        await self.confirm_source_inventory(project, set(before) - {name}, transient={name})

    async def upload_source(self, project, path, expected_hash, expected_size, limits, *, expected_before=None):
        """Submit one absent file and verify its bytes after reopening Sources."""
        if type(expected_size) is not int or expected_size <= 0 or path.stat().st_size != expected_size:
            raise ActionNeeded('The staged upload size does not match.')
        data = path.read_bytes()
        if len(data) != expected_size or digest(data) != expected_hash:
            raise ActionNeeded('The staged upload bytes do not match.')
        name = path.name
        destination = sources_url(project)
        try:
            page = await self.open_sources(project)
            before = await self.source_names(page, project)
        except (ActionNeeded, SignInNeeded, PlaywrightError) as exc:
            raise UploadNotSubmitted(str(exc)) from exc
        if expected_before is not None and set(before) != expected_before:
            raise UploadNotSubmitted('The Sources inventory changed before upload. Review the project and retry.')
        if name in before:
            raise ActionNeeded('This Source name already exists; reconcile before retrying.')
        plan_replacement([{'id': existing, 'name': existing} for existing in before], {},
                         [{'upload_name': name, 'hash': expected_hash, 'size': expected_size}], limits)

        add = page.get_by_role('button', name='Add sources', exact=True)
        dialog = page.get_by_role('dialog', name='Add sources', exact=True)
        upload = dialog.get_by_role('button', name='Upload', exact=True)
        submitted = False
        try:
            if page.url != destination or await add.count() != 1:
                raise ActionNeeded('The exact project upload control could not be confirmed.')
            await add.click(timeout=10000)
            await upload.wait_for(state='visible', timeout=10000)
            if page.url != destination or await dialog.count() != 1 or await upload.count() != 1:
                raise ActionNeeded('The exact project upload dialog could not be confirmed.')
            async with page.expect_file_chooser(timeout=10000) as event:
                await upload.click(timeout=10000)
            chooser = await event.value
            if page.url != destination:
                raise ActionNeeded('The exact Sources destination changed before upload.')
            if expected_before is not None and set(await self.source_names(page, project, include_hidden=True)) != expected_before:
                raise UploadNotSubmitted('The Sources inventory changed before upload. Review the project and retry.')
            submitted = True
            # Large rollback files use the local file chooser path to avoid
            # the browser driver's in-memory upload limit.
            payload = path if expected_size > 8 * 1024**2 else {
                'name': name, 'mimeType': guess_type(name)[0] or 'application/octet-stream', 'buffer': data,
            }
            await chooser.set_files(payload, timeout=30000)
            await page.get_by_role('button', name='Actions for ' + name, exact=True).wait_for(
                state='visible', timeout=45000
            )
        except UploadNotSubmitted:
            raise
        except (ActionNeeded, PlaywrightError) as exc:
            if isinstance(exc, PlaywrightTimeout):
                try:
                    await self.require_chatgpt_access(page)
                except (SharedBrowserBlocker, SignInNeeded) as blocked:
                    if submitted:
                        raise
                    raise UploadNotSubmitted(str(blocked)) from blocked
            if not submitted:
                raise UploadNotSubmitted('The Source upload stopped before file submission.') from exc
            if isinstance(exc, PlaywrightTimeout) or temporary_failure(exc):
                raise UncertainSourceOperation('The upload outcome is uncertain. Reconcile Sources before retrying.') from exc
            raise
        # An optimistic row can appear before the server finishes the upload.
        # Read back in the same tab before any navigation can interrupt it.
        try:
            await self.verify_source_on_page(page, project, name, expected_hash, expected_size)
        except SourceReadbackTimeout:
            if page.url != destination:
                raise
            if expected_size > 8 * 1024**2:
                # Leave the upload tab intact while the document becomes downloadable.
                try:
                    await self.verify_source_on_page(page, project, name, expected_hash, expected_size)
                except SourceReadbackTimeout:
                    if page.url != destination:
                        raise
            # The persisted inventory and exact bytes must still verify after reopening.
        await self.confirm_source_inventory(project, {*before, name})
        await self.verify_source(project, name, expected_hash, expected_size)

    async def open(self,project):
        ctx=await self.browsers.context('chatgpt')
        page=ctx.pages[0] if ctx.pages else await ctx.new_page()
        await page.goto(project_url(project),wait_until='domcontentloaded')
        if 'auth' in page.url or await page.get_by_role('button',name='Log in',exact=True).count():
            raise SignInNeeded('Sign in to ChatGPT in the dedicated browser, then retry publishing.')
        # Only project-source controls are eligible; never a conversation attachment button.
        control=page.get_by_role('button',name=re.compile(r'^(Add files|Add sources|Sources|Project files)(?:\s*\d+)?$',re.I))
        # Permit ordinary browser verification/loading to finish without refreshes,
        # challenge interaction, or changing browser security settings.
        for _ in range(60):
            if await control.count(): break
            if not browser_verification_title(await page.title()): break
            await asyncio.sleep(.5)
        if browser_verification_title(await page.title()):
            raise ActionNeeded('ChatGPT is showing its browser-verification page (Just a moment). Complete any requested verification yourself in this app-owned Chrome window, leave it open, then retry. No files were uploaded.')
        if await control.count()!=1:
            raise ActionNeeded('Project source controls were not recognized. Open the project and complete the publishing verification first.')
        await control.click()
        await page.get_by_role('dialog').wait_for(timeout=10000)
        return page

    async def upload(self,page,path,name):
        dialog=page.get_by_role('dialog')
        if await dialog.get_by_text(name,exact=True).count():
            return # A previously interrupted upload is reconciled by its content-derived name.
        inp=dialog.locator('input[type=file]')
        if await inp.count()!=1:
            button=dialog.get_by_role('button',name=re.compile(r'^(Upload files|Add files|Upload from computer)$',re.I))
            if await button.count()!=1: raise ActionNeeded('No unambiguous project file upload control. No files were removed.')
            async with page.expect_file_chooser() as fc:
                await button.click()
            await (await fc.value).set_files(str(path))
        else: await inp.set_input_files(str(path))
        for _ in range(90):
            text=await dialog.inner_text()
            if re.search(r'limit reached|too many files|upload failed|file too large|unable to upload',text,re.I):
                raise ActionNeeded('ChatGPT refused an upload or reached its capacity. Existing project files are intact.')
            row=dialog.get_by_text(name,exact=True)
            pending=dialog.get_by_role('progressbar')
            if await row.count()==1 and not await pending.count() and not re.search(r'uploading|processing',text,re.I):
                return
            await asyncio.sleep(1)
        raise ActionNeeded('Upload was not confirmed before timeout. Retry will reconcile the filename.')

    async def remove_owned(self,page,project,name):
        owned=self.store.rows("SELECT * FROM remote_files WHERE project=? AND name=? AND status='verified'",(project,name))
        if not owned: raise ActionNeeded('Refusing to remove an untracked project file.')
        dialog=page.get_by_role('dialog')
        label=dialog.get_by_text(name,exact=True)
        if await label.count()==0: return
        if await label.count()!=1: raise ActionNeeded('Duplicate remote filenames need review; no deletion attempted.')
        # Require a row containing just this filename and an explicitly labeled remove button.
        row=dialog.get_by_role('listitem').filter(has=label)
        if await row.count()!=1:
            raise ActionNeeded('Cannot identify the exact managed-file row. New uploads retained; old files need review.')
        remove=row.get_by_role('button',name=re.compile(r'^(Remove|Delete)( file)?$',re.I))
        if await remove.count()!=1: raise ActionNeeded('Managed-file removal control was not recognized.')
        await remove.click()
        confirmation=page.get_by_role('alertdialog')
        if await confirmation.count():
            if name not in await confirmation.inner_text(): raise ActionNeeded('Deletion confirmation did not name the expected managed file.')
            await confirmation.get_by_role('button',name=re.compile(r'^(Remove|Delete)$',re.I)).click()
        await label.wait_for(state='detached',timeout=15000)

    async def publish(self,c,release,test=False):
        if not c.get('project'): raise ActionNeeded('Choose this course’s ChatGPT project first.')
        if not test and not self.store.setting('cloud_verified'):
            raise ActionNeeded('Cloud publishing needs its first upload/replacement verification. Use Verify cloud sync.')
        project=project_url(c['project'])
        page=await self.open(project)
        manifest=json.loads(release['manifest'])
        wanted=[]
        # Upload all new files before removing any old managed file. If capacity is unknown,
        # upload refusal is the safe preflight: no previous files are deleted to make room.
        for f in manifest['files']:
            path=self.store.root/release['path']/f['name']
            if digest(path.read_bytes())!=f['hash']: raise ActionNeeded('Bundle changed after generation; rebuild before publishing.')
            name=f'onq-{c["id"][:6]}-{f["hash"][:12]}-{f["name"]}'
            if test: name='ONQ-VERIFICATION-'+name
            upload_dir=self.store.state/'uploads'; upload_dir.mkdir(exist_ok=True)
            target=upload_dir/name
            if not target.exists(): target.write_bytes(path.read_bytes())
            await self.upload(page,target,name)
            self.store.execute('INSERT OR REPLACE INTO remote_files VALUES (?,?,?,?,?)',(project,name,f['hash'],release['id'],'verified'))
            wanted.append(name)
        # Reopen the source dialog to ensure rows survive a page reload.
        page=await self.open(project)
        for name in wanted:
            if await page.get_by_role('dialog').get_by_text(name,exact=True).count()!=1:
                raise ActionNeeded('Uploaded project files could not be confirmed after reopening.')
        prefix='ONQ-VERIFICATION-' if test else f'onq-{c["id"][:6]}-'
        old=self.store.rows("SELECT * FROM remote_files WHERE project=? AND status='verified'",(project,))
        for f in old:
            if f['name'].startswith(prefix) and f['name'] not in wanted:
                await self.remove_owned(page,project,f['name'])
                self.store.execute("UPDATE remote_files SET status='replaced' WHERE project=? AND name=?",(project,f['name']))
        if not test:
            self.store.execute("UPDATE releases SET cloud_status='verified' WHERE id=?",(release['id'],))
        return wanted

    async def verify(self,c):
        # Two tiny, clearly marked generations test upload + replacement; no course files touched.
        import uuid
        for n in (1,2):
            folder=self.store.root/'_verification'/str(n)
            folder.mkdir(parents=True,exist_ok=True)
            p=folder/'sync-test.md'
            p.write_text(f'# OnQ Agent verification\n\nDisposable sync test, generation {n}.\n',encoding='utf-8')
            release={'id':'test-'+uuid.uuid4().hex,'path':str(folder.relative_to(self.store.root)),
              'manifest':json.dumps({'files':[{'name':p.name,'hash':digest(p.read_bytes()),'size':p.stat().st_size}]})}
            names=await self.publish(c,release,test=True)
        page=await self.open(c['project'])
        for name in names:
            await self.remove_owned(page,project_url(c['project']),name)
            self.store.execute("UPDATE remote_files SET status='test-cleaned' WHERE project=? AND name=?",(project_url(c['project']),name))
        self.store.set_setting('cloud_verified',True)
