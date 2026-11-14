import asyncio
import json
import os
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlparse
from fastapi import FastAPI,HTTPException,Request
from fastapi.responses import FileResponse,JSONResponse
from fastapi.staticfiles import StaticFiles
from .store import Store,CATEGORIES,validate_library
from .service import Service
from .scheduling import validate_schedule,next_run
from .publisher import project_url, sources_url
from .browser import ActionNeeded, SignInNeeded
from .version import PROTOCOL_VERSION,APP_VERSION
from .handoff import Handoff
from .migration import review_migration
from .harness_jobs import HarnessJobs
from .browser import chrome_executable
import subprocess

store=Store()
service=Service(store)
handoff=Handoff(store)

@asynccontextmanager
async def lifespan(app):
    service.scheduler=asyncio.create_task(service.loop())
    yield
    await service.close()

app=FastAPI(lifespan=lifespan,docs_url=None,redoc_url=None)

@app.get('/api/health')
def health(): return {'application':'onq-library','protocol':PROTOCOL_VERSION,'version':APP_VERSION}

@app.get('/api/scheduled-completion')
async def scheduled_completion():
    return {'completion': service.scheduled_completion}


@app.middleware('http')
async def local_only(request:Request,call_next):
    host=request.headers.get('host','').split(':')[0]
    if host not in ('127.0.0.1','localhost','testserver'):
        return JSONResponse({'detail':'Local access only'},status_code=403)
    if request.method not in ('GET','HEAD','OPTIONS'):
        origin=request.headers.get('origin')
        if (origin and urlparse(origin).netloc!=request.headers.get('host')) or request.headers.get('x-onq-client')!='dashboard':
            return JSONResponse({'detail':'Open this action from the local dashboard'},status_code=403)
    response=await call_next(request)
    response.headers['X-Content-Type-Options']='nosniff'
    response.headers['X-Frame-Options']='DENY'
    response.headers['Referrer-Policy']='no-referrer'
    return response

@app.exception_handler(ValueError)
async def bad_input(request,exc): return JSONResponse({'detail':str(exc)},status_code=400)

@app.exception_handler(ActionNeeded)
async def action_needed(request,exc): return JSONResponse({'detail':str(exc)},status_code=409)

def course(cid):
    c=store.course(cid)
    if not c: raise HTTPException(404,'Course not found')
    return c

def idle():
    if service.lock.locked() or (service.task and not service.task.done()): raise ActionNeeded('Wait for the current run to finish before changing settings.')

def publishing_state(c, jobs):
    project = project_url(c['project']) if c.get('project') else None
    release = store.latest(c['id'])
    local = release['id'] if release else None
    matching = [job for job in jobs if job['course'] == c['id'] and job['project'] == project]
    eligible = service.publishing_verified(c)
    verified = max((job for job in matching if eligible and job['status'] == 'verified'),
                   key=lambda job: job['updated'], default=None)
    staged = next((job for job in matching if job['release'] == local and job.get('format') == 'compact-v1'), None)
    return {'eligible': eligible, 'automatic': service.automatic_publishing(c),
            'local_release': local,
            'destination': project, 'staged_job': staged['id'] if staged else None,
            'verified_release': verified['release'] if verified else None,
            'last_verified': verified['updated'] if verified else None,
            'staged_count': len(staged['files']) if staged and isinstance(staged.get('files'), list) else None,
            'pending_release': bool(local and (not staged or staged['status'] != 'verified'))}


@app.get('/api/state')
def state():
    jobs = HarnessJobs(store).list()
    return {'courses':[{**c,'handoff':handoff.summary(c),'publishing':publishing_state(c,jobs)}
                       for c in store.courses()],'settings':{k:store.setting(k) for k in ('library','schedule','paused','startup_catchup','cloud_verified','max_pdf_mb','max_pdf_pages')},
      'harness_jobs':jobs,'resume_batch':service.resume_batch,
      'active':service.active,'busy':service.lock.locked() or bool(service.task and not service.task.done()),
      'runs':store.rows('SELECT * FROM runs ORDER BY started DESC LIMIT 40'),'categories':CATEGORIES,
      'totals':{'versions':store.rows('SELECT COUNT(*) AS n FROM versions')[0]['n'],
        'files':store.rows('SELECT COUNT(*) AS n FROM items WHERE current_hash IS NOT NULL')[0]['n']},
      'connections':{k:bool(v.pages) for k,v in service.browsers.contexts.items()}}

@app.post('/api/chatgpt/resume-publishing')
async def resume_publishing():
    service.start_resume_publishing()
    return {'queued': True}


@app.post('/api/run')
async def run(request:Request):
    data=await request.json(); mode=data.get('mode','sync'); cids=data.get('courses')
    if mode not in ('sync','build','publish','discover','verify'): raise ValueError('Invalid action')
    if mode=='verify':
        raise ActionNeeded('The legacy ChatGPT publishing check is disabled.')
    if mode=='publish' and (not cids or len(cids)!=1 or not service.publishing_verified(course(cids[0]))):
        raise ActionNeeded('ChatGPT publishing is disabled until you verify the saved course project.')
    if cids is not None:
        for cid in cids: course(cid)
    service.start(cids,mode)
    return {'queued':True}

@app.get('/api/harness/jobs')
def harness_jobs():return HarnessJobs(store).list()

@app.post('/api/courses/{cid}/harness/queue')
async def queue_harness(cid):
    idle()
    async with service.lock:
        job = await asyncio.to_thread(HarnessJobs(store).enqueue, course(cid), 'manual')
    if not job:raise ActionNeeded('Set this course project destination and build bundles before queuing an agent job.')
    return job

@app.post('/api/courses/{cid}/chatgpt/verify-and-publish')
async def verify_and_publish(cid, request: Request):
    data = await request.json()
    if not isinstance(data, dict) or any(not isinstance(data.get(key), str) for key in ('project', 'job')):
        raise ValueError('Confirm the exact project destination and staged bundle.')
    service.start_verify_and_publish(course(cid), data['project'], data['job'])
    return {'queued': True}


@app.patch('/api/courses/{cid}/chatgpt/automatic')
async def set_automatic_chatgpt(cid, request: Request):
    data = await request.json()
    idle()
    c = course(cid)
    enabled = data.get('enabled') if isinstance(data, dict) else None
    if type(enabled) is not bool:
        raise ValueError('Choose whether to enable automatic publishing.')
    if enabled and not service.publishing_verified(c):
        raise ActionNeeded('Use Verify and publish for this exact project before enabling automatic publishing.')
    store.update_course(cid, auto_publish_project=project_url(c['project']) if enabled else None)
    return {'enabled': enabled}


@app.post('/api/courses/{cid}/chatgpt/login')
async def login_course_chatgpt(cid):
    idle()
    destination = sources_url(course(cid)['project'])
    async with service.lock:
        await service.browsers.login('chatgpt', destination)
    return {'opened': True}


@app.post('/api/courses/{cid}/chatgpt/migration-review')
async def review_course_migration(cid, request: Request):
    data = await request.json()
    if not isinstance(data, dict) or any(not isinstance(data.get(key), str) for key in ('job', 'backup')):
        raise ValueError('Provide the staged job and Source backup identifiers.')
    idle()
    async with service.lock:
        try:
            return await review_migration(store, course(cid), data['job'], data['backup'], service.publisher)
        except SignInNeeded as exc:
            raise ActionNeeded(str(exc)) from exc


@app.post('/api/courses/{cid}/chatgpt/backup')
async def backup_course_sources(cid, request: Request):
    data = await request.json()
    names = data.get('names') if isinstance(data, dict) else None
    if not isinstance(names, list) or not names or any(not isinstance(n, str) or not n for n in names):
        raise ValueError('Provide the full exact list of existing Source names.')
    idle()
    c = course(cid)
    async with service.lock:
        try:
            directory, files = await service.publisher.backup_sources(
                c['project'], names, store.root / 'Cloud Source Backups' / c['id']
            )
        except SignInNeeded as exc:
            raise ActionNeeded(str(exc)) from exc
    return {'directory': str(directory), 'files': files}


@app.post('/api/courses/{cid}/chatgpt/check')
async def check_course_chatgpt(cid):
    idle()
    project = course(cid)['project']
    async with service.lock:
        try:
            names = await service.publisher.inspect_sources(project)
        except SignInNeeded as exc:
            raise ActionNeeded(str(exc)) from exc
    return {'sources_open': True, 'visible_files': names}


@app.post('/api/login/{name}')
async def login(name):
    if name not in ('onq','chatgpt'): raise HTTPException(404)
    idle()
    async with service.lock:
        await service.browsers.login(name)
    return {'opened':True}

@app.get('/api/connections/{name}/inspection')
async def inspect_connection(name):
    idle()
    if name not in ('onq','chatgpt'): raise HTTPException(404)
    ctx=service.browsers.contexts.get(name)
    if not ctx or not ctx.pages: return {'open':False}
    page=ctx.pages[0]
    return {'open':True,'url':page.url.split('?')[0], 'title':await page.title(),
      'buttons':await page.get_by_role('button').all_text_contents(),
      'dialogs':await page.get_by_role('dialog').all_text_contents(),
      'controls':await page.locator('button,[role=tab],a').evaluate_all('''elements => elements.map(e => ({
        tag:e.tagName,role:e.getAttribute('role'),text:(e.innerText||'').slice(0,160),
        label:e.getAttribute('aria-label'),title:e.getAttribute('title'),href:e.getAttribute('href'),
        testid:e.getAttribute('data-testid')}))'''),
      'main_text':[text[:6000] for text in await page.locator('main').all_text_contents()]}

def reset_project_verification(course, previous_project):
    old = project_url(previous_project) if previous_project else None
    new = project_url(course['project']) if course['project'] else None
    if old != new:
        course.pop('verified_publish_project', None)
        course.update(project_requires_verification=True, auto_publish_project=None, cloud_status='Not verified')


@app.patch('/api/courses/{cid}')
async def edit_course(cid,request:Request):
    data=await request.json(); idle(); c=course(cid)
    previous_project = c['project']
    for k in ('enabled','semester','name','project','org_id','schedule'):
        if k in data:
            if k=='project' and data[k]: project_url(data[k])
            if k=='org_id' and data[k] and not str(data[k]).isdigit(): raise ValueError('onQ course ID must be numeric')
            if k=='schedule' and data[k]: validate_schedule(data[k])
            if k in ('name','semester') and not str(data[k]).strip(): raise ValueError(f'{k} is required')
            c[k]=data[k]
    reset_project_verification(c, previous_project)
    c['next_run']=next_run(c.get('schedule') or store.setting('schedule'))
    store.save_course(c)
    return c

@app.get('/api/courses/{cid}/library')
def library(cid):
    course(cid)
    return {'items':store.items(cid),'releases':store.rows('SELECT * FROM releases WHERE course=? ORDER BY created DESC',(cid,))}

@app.get('/api/items/{iid}/versions')
def versions(iid): return store.rows('SELECT * FROM versions WHERE item=? ORDER BY created DESC',(iid,))

@app.get('/api/courses/{cid}/handoff')
def describe_handoff(cid):return handoff.describe(course(cid))

@app.post('/api/courses/{cid}/handoff/prepare')
def prepare_handoff(cid):
    idle()
    return handoff.prepare(course(cid))

@app.post('/api/courses/{cid}/handoff/{tid}/open-folder')
def open_handoff_folder(cid,tid):
    ticket=handoff.ticket(course(cid),tid)
    path=(store.root/ticket['path']).resolve()
    if not path.is_relative_to(store.root) or not path.is_dir():raise ValueError('Prepared folder is missing.')
    if os.name=='nt':os.startfile(path)
    return {'path':str(path)}

@app.post('/api/courses/{cid}/open-project')
def open_course_project(cid):
    url=project_url(course(cid)['project'])
    # Ordinary user-driven Chrome. No dedicated automation session or browser control.
    subprocess.Popen([str(chrome_executable()),'--new-window',url],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    return {'url':url}

@app.post('/api/courses/{cid}/handoff/{tid}/confirm')
async def confirm_handoff(cid,tid,request:Request):
    idle()
    return handoff.confirm(course(cid),tid,(await request.json()).get('files'))

@app.post('/api/courses/{cid}/handoff/reset')
async def reset_handoff(cid,request:Request):
    idle()
    return handoff.reset(course(cid),(await request.json()).get('files'))

@app.post('/api/courses/{cid}/handoff/{tid}/review-removals')
def review_handoff_removals(cid,tid):
    idle()
    return handoff.removal_check(course(cid),tid)

@app.post('/api/courses/{cid}/handoff/{tid}/confirm-removals')
async def confirm_handoff_removals(cid,tid,request:Request):
    idle()
    return handoff.confirm_removals(course(cid),tid,(await request.json()).get('files'))

@app.patch('/api/items/{iid}')
async def category(iid,request:Request):
    idle(); value=(await request.json()).get('category')
    if value not in CATEGORIES: raise ValueError('Invalid category')
    store.execute('UPDATE items SET category=? WHERE id=?',(value,iid))
    return {'saved':True}

@app.get('/api/file')
def file(path:str):
    target=(store.root/path).resolve()
    if not target.is_relative_to(store.root) or not target.is_file(): raise HTTPException(404)
    # Force downloads for HTML, SVG and code; never execute archived content in dashboard origin.
    return FileResponse(target,filename=target.name,content_disposition_type='attachment')

@app.post('/api/open-library')
def open_library():
    if os.name=='nt': os.startfile(store.root)
    return {'path':str(store.root)}

@app.post('/api/shutdown')
async def shutdown():
    idle()
    if not hasattr(app.state,'server'): raise ActionNeeded('Close the worker from Windows and reopen it.')
    app.state.server.should_exit=True
    return {'stopping':True}

@app.patch('/api/settings')
async def settings(request:Request):
    data=await request.json()
    if set(data)-{'paused'}: idle()
    if 'library' in data:
        p=validate_library(data['library'])
        if str(p)!=str(store.root) and store.rows('SELECT id FROM versions LIMIT 1'):
            raise ValueError('This library contains originals. Copy the complete library to the new computer and use the transfer guide; moving it live is disabled.')
        p.mkdir(parents=True,exist_ok=True); store.set_setting('library',str(p))
    if 'schedule' in data:
        validate_schedule(data['schedule']); store.set_setting('schedule',data['schedule'])
        for c in store.courses():
            if not c.get('schedule'): store.update_course(c['id'],next_run=next_run(data['schedule']))
    for key in ('max_pdf_mb','max_pdf_pages'):
        if key in data:
            n=int(data[key])
            if n<1 or n>1000: raise ValueError('Bundle limits must be 1–1000')
            store.set_setting(key,n)
    for key in ('paused','startup_catchup'):
        if key in data: store.set_setting(key,bool(data[key]))
    return {'saved':True}

@app.get('/api/export')
def export():
    return JSONResponse({'format':'onq-settings-v1','courses':[{k:c.get(k) for k in ('id','org_id','name','semester','enabled','project','schedule')} for c in store.courses()],
        'settings':{k:store.setting(k) for k in ('schedule','max_pdf_mb','max_pdf_pages','startup_catchup')}},
        headers={'Content-Disposition':'attachment; filename="onq-settings.json"'})

@app.post('/api/import-settings')
async def import_settings(request:Request):
    data=await request.json(); idle()
    if data.get('format')!='onq-settings-v1': raise ValueError('Not an OnQ Agent settings export')
    settings=data.get('settings',{})
    validate_schedule(settings['schedule'])
    clean=[]
    for entry in data.get('courses',[]):
        if not all(isinstance(entry.get(k),str) for k in ('id','org_id','name','semester','project')): raise ValueError('Invalid course settings')
        if not __import__('re').fullmatch('[a-f0-9]{32}',entry['id']): raise ValueError('Invalid course identity')
        if entry['project']: project_url(entry['project'])
        if entry.get('schedule'): validate_schedule(entry['schedule'])
        c=store.course(entry['id']) or {'local_status':'Not checked','cloud_status':'Not verified','last_check':None,'new_count':0}
        previous_project = c.get('project')
        c.update({k:entry.get(k) for k in ('id','org_id','name','semester','enabled','project','schedule')})
        reset_project_verification(c, previous_project)
        c['next_run']=next_run(c.get('schedule') or settings['schedule']); clean.append(c)
    for c in clean: store.save_course(c)
    for k in ('schedule','max_pdf_mb','max_pdf_pages','startup_catchup'):
        if k in settings: store.set_setting(k,settings[k])
    return {'saved':True}

@app.post('/api/courses/{cid}/import-folder')
async def import_folder(cid,request:Request):
    data=await request.json(); idle(); c=course(cid)
    folder=Path(data['path']).expanduser().resolve()
    if not folder.is_dir() or folder==Path(folder.anchor): raise ValueError('Choose a specific source folder')
    if store.root.is_relative_to(folder) or folder.is_relative_to(store.root): raise ValueError('Choose a source outside the managed library')
    if '.chatgpt-projects' in folder.parts: raise ValueError('Project mirror sources are not import targets')
    async with service.lock:
        rid=store.start_run(cid,'import'); count=0
        try:
            for p in folder.rglob('*'):
                if not p.is_file() or p.is_symlink() or p.suffix.lower() not in {'.pdf','.md','.txt','.ipynb','.py','.c','.h','.java','.pptx','.docx','.mp4','.mp3','.html','.zip'}: continue
                rel=p.relative_to(folder).as_posix()
                count+=int(store.ingest(c,'import:'+str(folder)+'/'+rel,p.name,p.as_uri(),p.read_bytes(),p.name))
            store.finish_run(rid,'success',f'Imported {count} new or revised files. Originals were copied, not moved.',count)
        except Exception as e:
            store.finish_run(rid,'error',str(e),count); raise
    service.start([cid],'build')
    return {'imported':count}

dist=Path(__file__).resolve().parents[1]/'web'/'dist'
if dist.exists(): app.mount('/',StaticFiles(directory=dist,html=True),name='dashboard')
else:
    @app.get('/')
    def waiting(): return {'status':'Building dashboard','api':'/api/state'}
