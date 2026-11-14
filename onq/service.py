import asyncio
from uuid import uuid4
from datetime import datetime,timezone
from .store import now
from .browser import Browsers, SignInNeeded, ActionNeeded, SharedBrowserBlocker
from .collector import Collector
from .publisher import Publisher, project_url, retry_read
from .scheduling import next_run
from .bundles import build
from .handoff import Handoff
from .harness_jobs import HarnessJobs

class Service:
    def __init__(self,store):
        self.store=store; self.browsers=Browsers(store); self.collector=Collector(store,self.browsers)
        self.publisher=Publisher(store,self.browsers); self.lock=asyncio.Lock(); self.task=None; self.scheduler=None; self.active=None
        self.resume_batch = None
        self.scheduled_completion = None

    def publishing_verified(self, course):
        if not course.get('project'):
            return False
        project = project_url(course['project'])
        if course.get('verified_publish_project') == project:
            return True
        if course.get('project_requires_verification'):
            return False
        approvals = self.store.setting('publishing_verified_projects') or {}
        if isinstance(approvals, dict) and approvals.get(course['id']) == project:
            return True
        return bool(self.store.rows(
            "SELECT 1 FROM harness_jobs j JOIN settings s ON s.key='source_migration_' || j.id "
            "WHERE j.course=? AND j.project=? AND j.status='verified' "
            "AND json_extract(s.value,'$.phase')='verified' "
            "AND json_extract(s.value,'$.project')=j.project "
            "AND json_extract(s.value,'$.release')=j.release LIMIT 1",
            (course['id'], project),
        ))

    def automatic_publishing(self, course):
        return (self.publishing_verified(course)
                and course.get('auto_publish_project') == project_url(course['project']))

    async def publish_job(self, course, harness, job, *, resume=False):
        if not self.publishing_verified(course):
            raise ActionNeeded('Use Verify and publish to approve this exact course and project.')
        if not job:
            raise ActionNeeded('Collect or import materials before publishing.')
        self.active = {'course': course['name'], 'stage': 'Checking project storage'}
        try:
            limits = await retry_read(self.publisher.source_limits, min(self.store.setting('max_pdf_mb'), 40) * 1024**2)
        except SignInNeeded:
            raise
        except ActionNeeded as exc:
            # The exact storage view is a prerequisite shared by all projects.
            raise SharedBrowserBlocker(str(exc)) from exc
        self.active = {'course': course['name'], 'stage': 'Replacing and verifying Sources'}
        count = await harness.publish_release(job['id'], course, self.publisher, limits, resume=resume)
        self.store.update_course(course['id'], cloud_status='Verified')
        return count

    def start_verify_and_publish(self, course, project, jid):
        self.check_idle()
        if not course.get('project') or project != project_url(course['project']):
            raise ActionNeeded('The saved destination changed. Review it and confirm again.')
        if self.publishing_verified(course):
            raise ActionNeeded('This course and project are already verified. Use Publish now for a new release.')
        harness = HarnessJobs(self.store)
        try:
            job = harness.get(jid)
        except ValueError:
            job = None
        latest = self.store.latest(course['id'])
        if (not job or not latest or job['course'] != course['id'] or job['project'] != project
                or job['release'] != latest['id'] or job.get('format') != 'compact-v1'):
            raise ActionNeeded('Sync the course, or use Queue desktop agent to stage its current bundle, then confirm again.')
        self.task = asyncio.create_task(self.run_verify_and_publish(course, harness, jid))

    async def run_verify_and_publish(self, course, harness, jid):
        async with self.lock:
            cid = course['id']
            rid = self.store.start_run(cid, 'verify-and-publish')
            self.active = {'course': course['name'], 'stage': 'Verifying and publishing the staged bundle'}
            try:
                try:
                    await asyncio.to_thread(harness.validate, jid, course)
                except (OSError, ValueError, KeyError, TypeError) as exc:
                    raise ActionNeeded('The staged bundle could not be verified. Use Queue desktop agent to prepare it again.') from exc
                count = await harness.publish_release(jid, course, self.publisher, first=True)
            except asyncio.CancelledError:
                self.store.finish_run(rid, 'interrupted', 'Worker stopped. Retry Verify and publish to check the upload journal.')
                raise
            except (ActionNeeded, SignInNeeded) as exc:
                self.store.update_course(cid, cloud_status='Sign-in needed' if isinstance(exc, SignInNeeded) else 'Action needed')
                self.store.finish_run(rid, 'action-needed', str(exc)[:500])
            except Exception as exc:
                self.store.update_course(cid, cloud_status='Error')
                self.store.finish_run(rid, 'error', f'{type(exc).__name__}: {str(exc)[:500]}')
            else:
                self.store.finish_run(rid, 'success', f'Verified the project and all {count} Sources. Publish after sync is available.')
            finally:
                self.active = None

    def resume_candidates(self, jobs):
        """Select exact current releases; keep older snapshots for diagnostics only."""
        for course in self.store.courses():
            if not course['enabled'] or not self.publishing_verified(course):
                continue
            release = self.store.latest(course['id'])
            if not release:
                continue
            staged = [j for j in jobs if j['course'] == course['id']]
            job = next((j for j in staged if j['project'] == project_url(course['project'])
                        and j['release'] == release['id']), None)
            yield course, job, bool(staged)

    def start_resume_publishing(self):
        self.check_idle()
        if not any(not job or job['status'] != 'verified'
                   for _, job, _ in self.resume_candidates(HarnessJobs(self.store).list())):
            raise ActionNeeded('No eligible courses have a pending release.')
        self.start(mode='resume-publishing')

    def check_idle(self):
        if self.task and not self.task.done():
            raise ActionNeeded('A run is already in progress. Please wait for it to finish.')
        if self.lock.locked():
            raise ActionNeeded('The browser is busy; try again shortly.')

    def start(self,cids=None,mode='sync',trigger='manual'):
        self.check_idle()
        self.task=asyncio.create_task(self.run(cids,mode,trigger))

    async def run_resume_publishing(self):
        harness = HarnessJobs(self.store)
        candidates = list(self.resume_candidates(harness.list()))
        results = [
            {'course': c['id'], 'name': c['name'], **(
                {'status': 'skipped', 'message': 'Current release was already verified.'}
                if job and job['status'] == 'verified' else
                {'status': 'pending', 'message': 'Waiting to publish.'}
            )}
            for c, job, _ in candidates
        ]
        self.resume_batch = {'running': True, 'blocker': None, 'results': results}
        try:
            for (course, job, has_staged), result in zip(candidates, results):
                cid = course['id']
                if result['status'] == 'skipped':
                    continue
                rid = self.store.start_run(cid, 'publish')
                result.update(status='running', message='Checking the staged release.')
                self.active = {'course': course['name'], 'stage': 'Checking the staged release'}
                try:
                    if not job:
                        if has_staged:
                            raise ActionNeeded('Stale staged release: no snapshot matches the current release and saved project. Stage the current release first.')
                        raise ActionNeeded('Missing staged release. Stage the current release first.')
                    saved = self.store.course(cid)
                    if not saved or not saved['enabled'] or not self.publishing_verified(saved):
                        raise ActionNeeded('Course selection or project approval changed. Review the saved course settings.')
                    try:
                        staged = await asyncio.to_thread(harness.validate, job['id'], saved)
                        if staged.get('format') != 'compact-v1':
                            raise ValueError('Expected a compact snapshot.')
                    except FileNotFoundError as exc:
                        raise ActionNeeded('Staged bundle files are missing. Stage the current release again.') from exc
                    except (ValueError, OSError, KeyError, TypeError) as exc:
                        raise ActionNeeded('Staged bundle is invalid or no longer current. Stage the current release again.') from exc
                    count = await self.publish_job(saved, harness, staged, resume=True)
                except asyncio.CancelledError:
                    message = 'Worker stopped. Resume publishing again to review the unfinished release.'
                    result.update(status='pending', message=message)
                    self.store.finish_run(rid, 'interrupted', message)
                    raise
                except (SignInNeeded, SharedBrowserBlocker) as exc:
                    message = str(exc)
                    result.update(status='action-needed', message=message)
                    self.resume_batch['blocker'] = message
                    self.store.update_course(cid, cloud_status='Sign-in needed' if isinstance(exc, SignInNeeded) else 'Action needed')
                    self.store.finish_run(rid, 'action-needed', message)
                    break
                except Exception as exc:
                    message = str(exc)[:500] if isinstance(exc, ActionNeeded) else f'Publishing failed: {type(exc).__name__}. See Activity for details.'
                    result.update(status='action-needed', message=message)
                    self.store.update_course(cid, cloud_status='Action needed')
                    self.store.finish_run(rid, 'action-needed', str(exc)[:500])
                else:
                    message = f'Verified the current release and {count} visible Sources.'
                    result.update(status='verified', message=message)
                    self.store.finish_run(rid, 'success', message)
        finally:
            for result in results:
                if result['status'] == 'pending' and self.resume_batch['blocker']:
                    result['message'] = 'Batch paused. Resolve the action above, then resume publishing.'
            self.resume_batch['running'] = False
            self.active = None

    async def run(self,cids,mode,trigger='manual'):
        async with self.lock:
            if mode == 'resume-publishing':
                await self.run_resume_publishing()
                return
            courses=[c for c in self.store.courses() if (c['id'] in cids if cids is not None else c['enabled'])]
            if mode=='discover':
                rid=self.store.start_run(None,'discover'); self.active={'stage':'Discovering courses'}
                try:
                    count=await self.collector.discover(); self.store.finish_run(rid,'success',f'Checked {count} enrollments.')
                except Exception as e: self.store.finish_run(rid,'action-needed',str(e)[:500])
                finally: self.active=None
                return
            for c in courses:
                rid=self.store.start_run(c['id'],mode); self.active={'course':c['name'],'stage':mode}
                changed=0; problems=[]; local_ok=False; interrupted=False
                try:
                    if mode=='verify':
                        await self.publisher.verify(c)
                        self.store.finish_run(rid,'success','Cloud upload and replacement verified; test files removed.')
                        continue
                    if mode=='publish':
                        if not self.publishing_verified(c):
                            raise ActionNeeded('Use Verify and publish to approve this exact course and project.')
                        harness = HarnessJobs(self.store)
                        job = await asyncio.to_thread(harness.enqueue, c, trigger)
                        if job and job['status'] == 'verified':
                            self.store.update_course(c['id'], cloud_status='Verified')
                            self.store.finish_run(rid, 'success', 'Current release was already verified.')
                            continue
                        count = await self.publish_job(c, harness, job)
                        self.store.finish_run(rid,'success',f'Verified release in project with {count} visible Sources.')
                        continue
                    if mode=='sync':
                        self.store.update_course(c['id'],local_status='Collecting')
                        changed,problems=await self.collector.collect(c)
                    if mode in ('sync','build'):
                        self.active={'course':c['name'],'stage':'Building bundles'}
                        release,_=await asyncio.to_thread(build,self.store,c)
                        local_ok=True
                        self.store.update_course(c['id'],last_check=now(),new_count=changed,
                            local_status='Updated with coverage notes' if problems else 'Up to date',cloud_status='Pending upload')
                    else: release=self.store.latest(c['id'])
                    if not release: raise ActionNeeded('Collect or import materials before publishing.')
                    if mode=='sync':
                        harness = HarnessJobs(self.store)
                        job = await asyncio.to_thread(harness.enqueue, c, trigger)
                        if job and self.automatic_publishing(c):
                            count = await self.publish_job(c, harness, job) if job['status'] != 'verified' else None
                            if count is None:
                                self.store.update_course(c['id'], cloud_status='Verified')
                            message = ('Current release was already verified.' if count is None else
                                       f'Published and verified {count} visible Sources.')
                            self.store.finish_run(rid,'partial' if problems else 'success',message,changed)
                            continue
                    ready=Handoff(self.store).summary(c)
                    label=f'{ready["pending"]} ready for manual upload' if ready['pending'] else 'Uploaded — confirmed by you'
                    if ready['removal_pending']:label+=f'; {ready["removal_pending"]} obsolete files to review'
                    self.store.update_course(c['id'],cloud_status=label)
                    self.store.finish_run(rid,'partial' if problems else 'success',f'{changed} new or revised originals.'+(' Coverage notes: '+', '.join(problems) if problems else ''),changed)
                except asyncio.CancelledError:
                    interrupted=True
                    self.store.finish_run(rid,'interrupted','Worker stopped; the interrupted sync is due again on startup.',changed)
                    if mode=='sync': self.store.update_course(c['id'],next_run=now())
                    raise
                except (SignInNeeded,ActionNeeded) as e:
                    status='Sign-in needed' if isinstance(e,SignInNeeded) else 'Action needed'
                    field='cloud_status' if local_ok or mode in ('publish','verify') else 'local_status'
                    self.store.update_course(c['id'],**{field:status})
                    self.store.finish_run(rid,'action-needed',str(e),changed)
                except Exception as e:
                    field='cloud_status' if local_ok or mode in ('publish','verify') else 'local_status'
                    self.store.update_course(c['id'],**{field:'Error'})
                    self.store.finish_run(rid,'error',f'{type(e).__name__}: {str(e)[:500]}',changed)
                finally:
                    current=self.store.course(c['id'])
                    schedule=current.get('schedule') or self.store.setting('schedule')
                    if mode=='sync' and not interrupted: self.store.update_course(c['id'],next_run=next_run(schedule))
                    self.active=None

            if mode == 'sync' and trigger == 'schedule' and courses:
                self.scheduled_completion = {
                    'id': uuid4().hex, 'finished': now(), 'courses': len(courses),
                }

    async def loop(self):
        for c in self.store.courses():
            if not c.get('next_run') or not self.store.setting('startup_catchup'):
                self.store.update_course(c['id'],next_run=next_run(c.get('schedule') or self.store.setting('schedule')))
        while True:
            try:
                if not self.store.setting('paused') and not self.lock.locked() and not (self.task and not self.task.done()):
                    current=datetime.now(timezone.utc)
                    due=[c['id'] for c in self.store.courses() if c['enabled'] and c.get('next_run') and datetime.fromisoformat(c['next_run'])<=current]
                    if due: self.start(due,trigger='schedule')
            except Exception as e:
                rid=self.store.start_run(None,'scheduler'); self.store.finish_run(rid,'error',str(e)[:500])
            await asyncio.sleep(20)

    async def close(self):
        if self.scheduler: self.scheduler.cancel()
        if self.task and not self.task.done():
            self.task.cancel()
            try: await self.task
            except asyncio.CancelledError: pass
        await self.browsers.close()
