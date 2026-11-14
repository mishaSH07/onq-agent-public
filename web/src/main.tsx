import React,{useEffect,useState} from 'react';
import {createRoot} from 'react-dom/client';
import {BookOpen,LayoutDashboard,Clock3,History,Settings,ArrowUpRight,Play,Pause,RefreshCw,FolderOpen,Cloud,HardDrive,Check,AlertCircle,ChevronRight,X,Download,Search,Plus,ShieldCheck,LogIn,FileText} from 'lucide-react';
import './style.css';
import {api} from './api';
import {HandoffPanel} from './HandoffPanel';

type Schedule={kind:string,time:string,weekday:number,hours:number,timezone:string};
type Publishing={destination?:string|null,staged_job?:string|null,eligible:boolean,automatic:boolean,local_release:string|null,verified_release:string|null,last_verified:string|null,staged_count:number|null,pending_release:boolean};
type Course={publishing?:Publishing,handoff?:{pending:number,total:number,reported:number,removal_pending:number},id:string,name:string,semester:string,enabled:boolean,org_id:string,project:string,schedule:Schedule|null,next_run:string|null,last_check:string|null,local_status:string,cloud_status:string,new_count:number};
type Run={id:string,course:string,stage:string,status:string,message:string,started:string,changed:number};
type HarnessJob={id:string,course_name:string,status:string,trigger:string,created:string,error:string};
const batchLabels={pending:'Pending',running:'Running',verified:'Verified',skipped:'Skipped','action-needed':'Action needed'};
type ResumeBatch={running:boolean,blocker:string|null,results:{course:string,name:string,status:keyof typeof batchLabels,message:string}[]};
type State={resume_batch?:ResumeBatch|null,harness_jobs?:HarnessJob[],totals?:{versions:number,files:number},courses:Course[],settings:{library:string,schedule:Schedule,paused:boolean,cloud_verified:boolean,max_pdf_mb:number,max_pdf_pages:number,startup_catchup:boolean},busy:boolean,active:{course?:string,stage:string}|null,runs:Run[],categories:string[]};
type Item={id:string,title:string,url:string,category:string,status:string,note:string,path:string|null,current_hash:string};
type Release={id:string,path:string,created:string,manifest:string,cloud_status:string};
type Version={id:string,path:string,created:string,hash:string,size:number};
const days=['Monday','Tuesday','Wednesday','Thursday','Friday','Saturday','Sunday'];
const date=(v:string|null)=>v?new Date(v).toLocaleString(undefined,{month:'short',day:'numeric',hour:'numeric',minute:'2-digit'}):'Not yet';
const fileLink=(p:string)=>'/api/file?path='+encodeURIComponent(p);
function ScheduleFields({value,onChange}:{value:Schedule,onChange:(v:Schedule)=>void}){
 return <div className="schedule-fields"><label>Frequency<select value={value.kind} onChange={e=>onChange({...value,kind:e.target.value})}><option value="daily">Every day</option><option value="weekly">Every week</option><option value="interval">Every few hours</option><option value="manual">Manual only</option></select></label>
 {value.kind==='weekly'&&<label>Day<select value={value.weekday} onChange={e=>onChange({...value,weekday:+e.target.value})}>{days.map((d,i)=><option key={d} value={i}>{d}</option>)}</select></label>}
 {['daily','weekly'].includes(value.kind)&&<label>Time<input type="time" value={value.time} onChange={e=>onChange({...value,time:e.target.value})}/></label>}
 {value.kind==='interval'&&<label>Hours between runs<input type="number" min="1" max="8760" value={value.hours} onChange={e=>onChange({...value,hours:+e.target.value})}/></label>}
 <label>Time zone<input value={value.timezone} onChange={e=>onChange({...value,timezone:e.target.value})}/></label></div>
}
function Badge({value}:{value:string}){return <span className={'badge '+(/up to date|success|available|verified/i.test(value)?'good':/error|needed|partial|not verified|external|unavailable/i.test(value)?'warn':'neutral')}>{value}</span>}
function uploadStatus(c:Course){return !c.handoff?.total?'Build bundles first':c.handoff.pending?c.handoff.pending+' ready to upload':c.handoff.removal_pending?c.handoff.removal_pending+' obsolete files to review':'Uploaded — confirmed by you';}
function cloudIsCurrent(c:Course){
 return !!c.publishing?.local_release&&c.publishing.local_release===c.publishing.verified_release;
}
function projectStatus(c:Course){
 if(!c.publishing?.eligible)return uploadStatus(c);
 if(/sign-in needed|action needed|error/i.test(c.cloud_status))return c.cloud_status;
 if(cloudIsCurrent(c))return 'Verified';
 return c.publishing.local_release?'Pending publication':'Build bundles first';
}
function App(){
 const [state,setState]=useState<State|null>(null),[page,setPage]=useState(location.hash==='#activity'?'Activity':'Overview'),[query,setQuery]=useState(''),[term,setTerm]=useState('Selected courses');
 const [uploadCourse,setUploadCourse]=useState<Course|null>(null);
 const [publishReview,setPublishReview]=useState<{id:string,name:string,project:string,job:string,count:number}|null>(null);
 const [sourceChecks,setSourceChecks]=useState<Record<string,{project:string,count:number}>>({});
 const [error,setError]=useState(''),[notice,setNotice]=useState(''),[pending,setPending]=useState(false),[edit,setEdit]=useState<Course|null>(null);
 const [selected,setSelected]=useState<Course|null>(null),[library,setLibrary]=useState<{items:Item[],releases:Release[]}|null>(null),[versions,setVersions]=useState<{title:string,rows:Version[]}|null>(null);
 const [settings,setSettings]=useState<State['settings']|null>(null),[importPath,setImportPath]=useState('');
 async function refresh(){try{setState(await api('/state'));}catch(e){setError(String(e));}}
 useEffect(()=>{refresh();const t=setInterval(refresh,4000);return()=>clearInterval(t);},[]);
 useEffect(()=>{
  function showScheduledActivity(){
   if(location.hash==='#activity'){
    setPage('Activity');
    void refresh();
    history.replaceState(null,'',location.pathname+location.search);
   }
  }
  showScheduledActivity();
  window.addEventListener('hashchange',showScheduledActivity);
  return()=>window.removeEventListener('hashchange',showScheduledActivity);
 },[]);
 async function action(fn:()=>Promise<unknown>,message='Saved'){
  setPending(true);setError('');try{await fn();setNotice(message);await refresh();}catch(e){setError(e instanceof Error?e.message:String(e));}finally{setPending(false);}
 }
 async function openLibrary(c:Course){setSelected(c);setLibrary(null);setVersions(null);try{setLibrary(await api(`/courses/${c.id}/library`));}catch(e){setError(String(e));}}
 function openUpload(c:Course){setSelected(null);setEdit(null);setUploadCourse(c);}
 function run(mode='sync',c?:Course){action(()=>api('/run','POST',{mode,...(c?{courses:[c.id]}:{})}),mode==='discover'?'Course discovery started':'Run started. Progress appears below.');}
 if(!state)return <div className="loading"><BookOpen size={36}/><h2>Opening your library</h2><p>{error||'Connecting to this computer…'}</p><button onClick={refresh}>Try again</button></div>;
 const busy=pending||state.busy;
 const enabled=state.courses.filter(c=>c.enabled);
 const terms=[...new Set(state.courses.map(c=>c.semester))];
 const courses=state.courses.filter(c=>(term==='Selected courses'?c.enabled:term==='All semesters'||c.semester===term)&&c.name.toLowerCase().includes(query.toLowerCase()));
 const verified=enabled.filter(cloudIsCurrent).length;
 const reconnectNeeded=enabled.some(c=>c.local_status==='Sign-in needed');
 const automatic=enabled.filter(c=>c.publishing?.automatic).length;
 const canResume=enabled.some(c=>c.publishing?.eligible&&c.publishing.pending_release);
 const batch=state.resume_batch;
 return <div className="app"><aside><div className="brand"><div className="brand-icon"><BookOpen size={24}/></div><div>OnQ <b>Agent</b><small>COURSE AUTOMATION</small></div></div>
 <div className="workspace-label">WORKSPACE</div><nav>{[{name:'Overview',icon:LayoutDashboard},{name:'Courses',icon:BookOpen},{name:'File Library',icon:FolderOpen},{name:'Ready for ChatGPT',icon:Cloud},{name:'Automations',icon:Clock3},{name:'Activity',icon:History},{name:'Settings',icon:Settings}].map(({name,icon:Icon})=><button className={page===name?'nav active':'nav'} key={name} onClick={()=>{setPage(name);if(name==='Settings')setSettings({...state.settings});}}><Icon size={19}/>{name}{name==='Overview'&&<span>{enabled.length}</span>}</button>)}</nav>
 <div className="sidebar-bottom"><div className="local-dot"/> Laptop worker <span className="tiny">LOCAL</span><p>Private on this computer.<br/>Built to move with you.</p><button className="sidebar-link" onClick={()=>action(()=>api('/open-library','POST'),'Opened library folder')}><FolderOpen size={16}/> Open library folder <ArrowUpRight size={14}/></button></div></aside>
 <main><header><div className="breadcrumb">Workspace <ChevronRight size={14}/> {page}</div><div className="header-status"><span className={'dot '+(state.settings.paused?'paused':'')}/>{state.settings.paused?'Schedules paused':state.busy?'Worker is running':'Worker ready'}<span className="avatar">Q</span></div></header>
 <div className="content">
 {error&&<div className="alert error" role="alert"><AlertCircle size={18}/><span>{error}</span><button aria-label="Dismiss error" onClick={()=>setError('')}><X size={16}/></button></div>}
 {notice&&<div className="alert notice" role="status"><Check size={18}/><span>{notice}</span><button aria-label="Dismiss notice" onClick={()=>setNotice('')}><X size={16}/></button></div>}
 {state.active&&<div className="alert running"><RefreshCw className="spin" size={18}/><span>{state.active.course?state.active.course+' · ':''}{state.active.stage}</span></div>}
 {page==='Overview'&&<><div className="page-title"><div><div className="eyebrow">LESS COLLECTING. MORE LEARNING.</div><h1>Your courses, together.</h1><p>Originals preserved. Study files organized. Ready wherever you learn.</p></div><button className="primary" disabled={busy} onClick={()=>run()}><RefreshCw size={17}/> Sync all courses</button></div>
 <div className="stats"><div><span className="stat-icon"><BookOpen size={21}/></span><section><small>SELECTED COURSES</small><strong>{enabled.length}<em>this workspace</em></strong></section></div><div><span className="stat-icon"><HardDrive size={21}/></span><section><small>LOCAL BUNDLES</small><strong>{enabled.filter(c=>c.publishing?.local_release).length}<em>courses prepared</em></strong></section></div><div><span className="stat-icon"><Cloud size={21}/></span><section><small>VERIFIED PROJECTS</small><strong>{verified}<em>current release</em></strong></section></div></div>
 {reconnectNeeded&&<div className="alert error" role="alert"><AlertCircle size={18}/><span>Queen’s onQ needs sign-in. Reconnect in dedicated Chrome, then select Sync all courses.</span><button disabled={busy} onClick={()=>action(()=>api('/login/onq','POST'),'onQ browser opened')}>Reconnect onQ</button></div>}
 <div className="setup-banner"><ShieldCheck size={25}/><div><h3>Your files, ready for your course projects.</h3><p>Automatic publishing is enabled for {automatic} of {enabled.length} selected courses.</p><small>Review publishing and manual uploads in Ready for ChatGPT.</small></div><button onClick={()=>setPage('Ready for ChatGPT')}>Ready for ChatGPT</button></div>
 <div className="section-toolbar"><div className="section-heading"><h2>Course library</h2><span>{courses.length}</span></div><div className="filters"><div className="search"><Search size={16}/><input aria-label="Search courses" placeholder="Find a course" value={query} onChange={e=>setQuery(e.target.value)}/></div><select aria-label="Semester" value={term} onChange={e=>setTerm(e.target.value)}><option>Selected courses</option><option>All semesters</option>{terms.map(t=><option key={t}>{t}</option>)}</select><button disabled={busy} onClick={()=>run('discover')}><Plus size={16}/> Discover courses</button></div></div>
 <div className="course-grid">{courses.map((c,i)=><article className={'course-card '+(!c.enabled?'disabled-course':'')} key={c.id}><div className="course-top"><span className={'course-icon tone-'+i%4}><BookOpen size={23}/></span><label className="switch" title="Include in scheduled runs"><input aria-label={'Enable '+c.name} type="checkbox" checked={c.enabled} disabled={busy} onChange={e=>action(()=>api('/courses/'+c.id,'PATCH',{enabled:e.target.checked}))}/><span/></label></div><small className="term">{c.semester}</small><h3>{c.name}</h3><div className="statuses"><div><span><HardDrive size={15}/> Local library</span><Badge value={c.local_status}/></div><div><span><Cloud size={15}/> ChatGPT project</span><Badge value={projectStatus(c)}/></div></div><div className="course-meta"><span>Last checked <b>{date(c.last_check)}</b></span><span>Next run <b>{c.enabled?date(c.next_run):'Paused for this course'}</b></span></div><div className="course-actions"><button disabled={busy} onClick={()=>run('sync',c)}><RefreshCw size={15}/> Sync now</button><button disabled={busy} onClick={()=>openUpload(c)}>Ready for ChatGPT</button><button onClick={()=>openLibrary(c)}>Files <ChevronRight size={15}/></button><button className="icon-button" aria-label={'Settings for '+c.name} onClick={()=>setEdit({...c})}><Settings size={17}/></button></div></article>)}</div>
 {courses.length===0&&<div className="empty">No courses match. Discover courses or change your filter.</div>}
 <div className="workflow"><div><span>01</span><h4>Collect & preserve</h4><p>Every revision gets its own original.</p></div><ChevronRight/><div><span>02</span><h4>Build your bundles</h4><p>Complete pages, code, and source indexes.</p></div><ChevronRight/><div><span>03</span><h4>Publish to your project</h4><p>Prepare course files for your ChatGPT projects.</p></div></div></>}
 {page==='Ready for ChatGPT'&&<div className="page-title"><div><h1>Ready for ChatGPT</h1><p>Publish staged releases or prepare files for manual upload.</p></div></div>}
 {page==='Ready for ChatGPT'&&<section className="panel">
  <div className="section-toolbar">
   <div><h2>ChatGPT Publishing</h2>
    <p>{canResume?'Publish staged releases to their saved projects after reconnecting in dedicated Chrome.':'No eligible courses have a pending release.'}</p>
   </div>
   <button className="primary" disabled={busy||!canResume} onClick={()=>action(
    ()=>api('/chatgpt/resume-publishing','POST'),'Resume publishing started.'
   )}><RefreshCw size={16}/> Resume publishing</button>
  </div>
  {batch&&<section aria-label="Resume publishing results" aria-live="polite">
   <h3>{batch.running?'Publishing staged releases':batch.blocker?'Publishing paused':'Resume publishing results'}</h3>
   {batch.blocker&&<div className="alert error"><AlertCircle size={18}/><span>{batch.blocker}</span></div>}
   {batch.results.map(result=><div className="list-row" key={result.course}>
    <div><b>{result.name}</b><small>{result.status==='running'?state.active?.stage||result.message:result.message}</small></div>
    <Badge value={batchLabels[result.status]}/>
   </div>)}
  </section>}
  {state.courses.filter(c=>c.project&&(c.enabled||!c.publishing?.eligible)).map(c=>{
   const lastVerification=state.runs.find(r=>r.course===c.id&&r.stage==='verify-and-publish');
   const publishing=c.publishing;
   return <div className="list-row" key={c.id}>
   <div><b>{c.name}</b><small>{c.project}</small>
    <small>Local {c.publishing?.local_release?.slice(0,8)||'—'} · Verified cloud {c.publishing?.verified_release?.slice(0,8)||'—'}</small>
    <small>Last verified {date(c.publishing?.last_verified??null)} · {c.cloud_status}</small>
    {c.publishing?.local_release!==c.publishing?.verified_release&&<small>{c.publishing?.staged_count==null?'No compact snapshot staged':`${c.publishing.staged_count} files in pending release`}</small>}
    {!publishing?.eligible&&!publishing?.staged_job&&<small>{publishing?.local_release
     ? 'Use Queue desktop agent below to stage the current bundle.'
     : 'Sync this course to collect materials and stage its bundle.'}</small>}
    {lastVerification&&!publishing?.eligible&&lastVerification.status!=='running'&&<small>{lastVerification.message}</small>}
    {sourceChecks[c.id]?.project===c.project&&<small>{sourceChecks[c.id].count} visible file Sources at last check</small>}
    {state.active?.course===c.name&&<small>{state.active.stage}</small>}
   </div>
   <div className="button-row">
    {!publishing?.eligible&&<button className="primary" disabled={busy||!publishing?.staged_job} onClick={()=>{
     if(publishing?.destination&&publishing.staged_job)setPublishReview({
      id:c.id,name:c.name,project:publishing.destination,job:publishing.staged_job,count:publishing.staged_count??0
     });
    }}>Verify and publish</button>}
    <button disabled={busy||!c.publishing?.eligible||!c.publishing.local_release} onClick={()=>run('publish',c)}>Publish now</button>
    <label className="check-label"><input type="checkbox" checked={!!c.publishing?.automatic} disabled={busy||!c.publishing?.eligible} onChange={e=>action(()=>api(`/courses/${c.id}/chatgpt/automatic`,'PATCH',{enabled:e.target.checked}))}/> Publish after sync</label>
    <button disabled={busy} onClick={()=>action(()=>api(`/courses/${c.id}/chatgpt/login`,'POST'),'Sign in in dedicated Chrome, then use its menu to exit before checking Sources.')}>Open dedicated Chrome</button>
    <button disabled={busy} onClick={()=>action(async()=>{
     setSourceChecks(previous=>({...previous,[c.id]:{project:'',count:0}}));
     const checked:{visible_files:string[]}=await api(`/courses/${c.id}/chatgpt/check`,'POST');
     setSourceChecks(previous=>({...previous,[c.id]:{project:c.project,count:checked.visible_files.length}}));
    },`${c.name} Sources checked. No files were changed.`)}>Check Sources</button>
   </div>
  </div>})}
 </section>}
 {page==='Automations'&&<><div className="page-title"><div><div className="eyebrow">ON YOUR SCHEDULE</div><h1>A little less to remember.</h1><p>Choose when your laptop checks for new material.</p></div><button onClick={()=>action(()=>api('/settings','PATCH',{paused:!state.settings.paused}))}>{state.settings.paused?<Play size={16}/>:<Pause size={16}/>} {state.settings.paused?'Resume schedules':'Pause schedules'}</button></div><div className="panel"><h2>Default schedule</h2><p>Applied to courses without a custom schedule. Toronto time by default.</p><ScheduleEditor initial={state.settings.schedule} save={s=>action(()=>api('/settings','PATCH',{schedule:s}))} disabled={busy}/></div><div className="info-line"><Clock3 size={19}/><p>The worker starts at Windows sign-in. Missed scheduled runs catch up once when the laptop is awake. Sign-in prompts pause collection until you reconnect.</p></div><div className="panel"><h2>Course schedules</h2>{state.courses.map(c=><div className="list-row" key={c.id}><div><b>{c.name}</b><small>{c.schedule?'Custom schedule':'Uses default schedule'} · {c.enabled?'Next: '+date(c.next_run):'Disabled'}</small></div><button onClick={()=>setEdit({...c})}>Edit schedule</button></div>)}</div></>}
 {['Courses','File Library','Ready for ChatGPT'].includes(page)&&<>{page!=='Ready for ChatGPT'&&<div className="page-title"><div><h1>{page}</h1><p>{page==='Courses'?'Select the courses this laptop collects.':page==='File Library'?'Browse originals, revisions, and generated bundles by course.':'Prepare changed files for manual upload to your course projects.'}</p></div></div>}<div className="panel">{page==='Ready for ChatGPT'&&<h2>Manual upload</h2>}{page==='File Library'&&enabled.length===0&&<p>Select courses under Courses to see their files here.</p>}{(page==='File Library'?enabled:state.courses).map(c=><div className="list-row" key={c.id}><div><b>{c.name}</b><small>{c.semester} · {page==='Ready for ChatGPT'?uploadStatus(c):c.local_status}</small></div>{page==='Courses'?<label className="check-label"><input type="checkbox" checked={c.enabled} disabled={busy} onChange={e=>action(()=>api('/courses/'+c.id,'PATCH',{enabled:e.target.checked}))}/> Collect this course</label>:page==='File Library'?<button onClick={()=>openLibrary(c)}>Browse files</button>:<div className="button-row"><button disabled={busy} onClick={()=>openUpload(c)}>Prepare upload</button><button disabled={busy} onClick={()=>action(()=>api(`/courses/${c.id}/harness/queue`,'POST'),'Course snapshot staged for publishing.')}>Queue desktop agent</button><button onClick={()=>setEdit({...c})}>Project settings</button></div>}</div>)}</div></>}
 {page==='Activity'&&<><div className="page-title"><div><div className="eyebrow">EVERY RUN, ACCOUNTED FOR</div><h1>Your library’s activity.</h1><p>See what changed, what succeeded, and what needs attention.</p></div></div><div className="panel activity">{!state.runs.length?<div className="empty"><History size={32}/><h3>A fresh start</h3><p>Your first run will appear here.</p></div>:state.runs.map(r=><div className="activity-row" key={r.id}><span className="activity-mark">{r.status==='success'?<Check size={18}/>:<AlertCircle size={18}/>}</span><div><div className="activity-title"><b>{state.courses.find(c=>c.id===r.course)?.name||'Workspace'} <span>· {r.stage}</span></b><Badge value={r.status}/></div><p>{r.message}</p><small>{date(r.started)} · {r.changed} changed originals</small></div>{r.course&&<button disabled={busy} onClick={()=>r.stage==='verify-and-publish'?setPage('Ready for ChatGPT'):['verify','publish'].includes(r.stage)?openUpload(state.courses.find(c=>c.id===r.course)!):run(r.stage==='build'?'build':'sync',state.courses.find(c=>c.id===r.course))}>Retry</button>}</div>)}</div></>}
 {['Ready for ChatGPT','Activity'].includes(page)&&<section className="panel"><h2>Desktop agent queue</h2><p>Scheduled and manual syncs stage persistent course upload jobs. Eligible projects can publish these snapshots after sync or through Resume publishing.</p>{!(state.harness_jobs?.length)?<p>No staged jobs yet.</p>:state.harness_jobs.map(j=><div className="list-row" key={j.id}><div><b>{j.course_name}</b><small>{date(j.created)} | {j.trigger==='schedule'?'Scheduled sync':'Manual sync'}</small><p>{j.error}</p></div><Badge value={j.status==='waiting-configuration'?'Harness setup needed':j.status}/></div>)}</section>}
 {page==='Settings'&&settings&&<><div className="page-title"><div><div className="eyebrow">MAKE IT YOURS</div><h1>A library that travels well.</h1><p>Local folders, bundle limits, and settings you can take to another PC.</p></div></div><div className="panel"><h2>Storage & bundles</h2><label>Library folder<input value={settings.library} onChange={e=>setSettings({...settings,library:e.target.value})}/></label><p className="hint">Originals, current project bundles, and bundle history live here. Browser sessions stay outside this folder.</p><div className="two-fields"><label>Maximum PDF volume size (MB)<input type="number" min="1" max="1000" value={settings.max_pdf_mb} onChange={e=>setSettings({...settings,max_pdf_mb:+e.target.value})}/></label><label>Maximum pages per PDF volume<input type="number" min="1" max="1000" value={settings.max_pdf_pages} onChange={e=>setSettings({...settings,max_pdf_pages:+e.target.value})}/></label></div><button className="primary" disabled={busy} onClick={()=>action(()=>api('/settings','PATCH',{library:settings.library,max_pdf_mb:settings.max_pdf_mb,max_pdf_pages:settings.max_pdf_pages}))}>Save settings</button></div><div className="panel"><h2>Connections</h2><p>Use dedicated Chrome for onQ sign-in. Open ChatGPT projects from Ready for ChatGPT to upload files in ordinary Chrome.</p><div className="button-row"><button disabled={busy} onClick={()=>action(()=>api('/login/onq','POST'),'onQ browser opened')}>Reconnect onQ</button></div></div><div className="panel"><h2>Move to another Windows PC</h2><p>Export settings, install the app on the other computer, import settings, then sign in again. Run only one worker at a time. The transfer guide explains how to preserve your complete archive.</p><div className="button-row"><a className="button" href="/api/export"><Download size={16}/> Export settings</a><label className="button file-input">Import settings<input type="file" accept=".json" onChange={async e=>{const f=e.target.files?.[0];if(f)await action(async()=>api('/import-settings','POST',JSON.parse(await f.text())),'Settings imported');}}/></label></div></div></>}
 {page==='Overview'&&<section className="panel"><h2>Latest activity</h2><p>{state.totals?.files??'—'} source files collected · {state.totals?.versions??'—'} preserved versions</p>{state.runs.slice(0,3).map(r=><div className="list-row" key={r.id}><div><b>{state.courses.find(c=>c.id===r.course)?.name||'Workspace'} · {r.stage}</b><small>{date(r.started)} · {r.changed} new/revised originals</small><p>{r.message}</p></div><Badge value={r.status}/></div>)}<button onClick={()=>setPage('Activity')}>View full activity</button></section>}
 <footer><span><ShieldCheck size={14}/> Your originals stay yours.</span><span>OnQ Agent · Local workspace</span></footer></div></main>
 {uploadCourse&&<HandoffPanel course={uploadCourse} close={()=>setUploadCourse(null)} onUpdate={refresh}/>}
 {publishReview&&<div className="overlay"><section className="modal" role="dialog" aria-modal="true" aria-label="Confirm first publication" onKeyDown={e=>{if(e.key==='Escape')setPublishReview(null);}}>
  <h2>Verify and publish</h2>
  <p><strong>{publishReview.name}</strong></p>
  <p style={{overflowWrap:'anywhere'}}>{publishReview.project}</p>
  <p>OnQ Agent will check the project in dedicated Chrome, upload {publishReview.count} staged course files, and download each one to verify it. The project must be empty for the first attempt.</p>
  <div className="modal-actions">
   <button autoFocus onClick={()=>setPublishReview(null)}>Cancel</button>
   <button className="primary" disabled={busy} onClick={()=>{
    const review=publishReview;
    setPublishReview(null);
    void action(()=>api(`/courses/${review.id}/chatgpt/verify-and-publish`,'POST',{project:review.project,job:review.job}),
     'Verification started. Check this course or Activity for the result.');
   }}>Confirm and publish</button>
  </div>
 </section></div>}
 {edit&&<div className="overlay" onClick={()=>setEdit(null)}><section className="modal" role="dialog" aria-modal="true" aria-label="Course settings" onClick={e=>e.stopPropagation()}><div className="modal-title"><h2>Course settings</h2><button aria-label="Close" onClick={()=>setEdit(null)}><X size={20}/></button></div><div className="two-fields"><label>Course name<input value={edit.name} onChange={e=>setEdit({...edit,name:e.target.value})}/></label><label>Semester<input value={edit.semester} onChange={e=>setEdit({...edit,semester:e.target.value})}/></label></div><label>onQ course ID<input value={edit.org_id} onChange={e=>setEdit({...edit,org_id:e.target.value})}/></label><label>ChatGPT project URL<input value={edit.project} placeholder="https://chatgpt.com/g/g-p-…/project" onChange={e=>setEdit({...edit,project:e.target.value})}/></label><label className="check-label"><input type="checkbox" checked={!!edit.schedule} onChange={e=>setEdit({...edit,schedule:e.target.checked?{...state.settings.schedule}:null})}/> Use a custom schedule</label>{edit.schedule&&<ScheduleFields value={edit.schedule} onChange={s=>setEdit({...edit,schedule:s})}/>}<div className="modal-actions"><button disabled={busy} onClick={()=>openUpload(edit)}>Prepare manual upload</button><button className="primary" disabled={busy} onClick={()=>action(async()=>{await api('/courses/'+edit.id,'PATCH',edit);setEdit(null);})}>Save course</button></div><p className="hint">Save the destination, then use Verify and publish in Ready for ChatGPT to unlock automatic publishing.</p></section></div>}
 {selected&&<div className="overlay" onClick={()=>setSelected(null)}><section className="modal wide" role="dialog" aria-modal="true" aria-label="Course files" onClick={e=>e.stopPropagation()}><div className="modal-title"><div><small>{selected.semester}</small><h2>{selected.name} · Files</h2></div><button aria-label="Close files" onClick={()=>setSelected(null)}><X size={20}/></button></div><div className="button-row"><button disabled={busy} onClick={()=>run('build',selected)}><FileText size={15}/> Rebuild bundles</button><button disabled={busy} onClick={()=>openUpload(selected)}><Cloud size={15}/> Prepare manual upload</button><button onClick={()=>openLibrary(selected)}><RefreshCw size={15}/> Refresh files</button></div>{!library?<p>Loading…</p>:<><h3>Project bundles</h3>{library.releases.length===0?<p className="hint">No bundles yet. Sync the course or import a source folder below.</p>:<div className="bundle-list">{library.releases.slice(0,5).map((r,n)=><details key={r.id} open={n===0}><summary>{n===0?'Current bundles':'Previous release'} · {date(r.created)} <Badge value={r.cloud_status}/></summary><div className="bundle-links">{JSON.parse(r.manifest).files.map((f:{name:string,size:number})=><a key={f.name} href={fileLink(r.path+'/'+f.name)}><FileText size={16}/>{f.name}<small>{(f.size/1024/1024).toFixed(1)} MB</small><Download size={14}/></a>)}</div></details>)}</div>}
 <h3>Originals & coverage</h3><div className="table-wrap"><table><thead><tr><th>Source</th><th>Category</th><th>Status</th><th>History</th></tr></thead><tbody>{library.items.map(i=><tr key={i.id}><td>{i.path?<a href={fileLink(i.path)}>{i.title}</a>:i.title}<small>{i.note}</small>{i.url.startsWith('https://')&&<a className="source-link" href={i.url} target="_blank" rel="noreferrer">Original source ↗</a>}</td><td><select aria-label={'Category for '+i.title} disabled={busy} value={i.category} onChange={e=>action(async()=>{await api('/items/'+i.id,'PATCH',{category:e.target.value});await openLibrary(selected);},'Category saved. Rebuild bundles to apply it.')}>{state.categories.map(cat=><option key={cat}>{cat}</option>)}</select></td><td><Badge value={i.status}/></td><td><button onClick={async()=>setVersions({title:i.title,rows:await api('/items/'+i.id+'/versions')})}>Versions</button></td></tr>)}</tbody></table>{library.items.length===0&&<p className="empty">No originals collected yet.</p>}</div>
 {versions&&<div className="version-box"><h4>{versions.title}</h4>{versions.rows.map(v=><a key={v.id} href={fileLink(v.path)}><Download size={14}/> {date(v.created)} · {v.hash.slice(0,12)} · {(v.size/1024).toFixed(0)} KB</a>)}</div>}
 <details className="import-panel"><summary>Import an existing source folder</summary><p>Copy existing files into the archive. Their source folder is left unchanged. Imported notes remain historical source material.</p><label>Source folder path<input value={importPath} onChange={e=>setImportPath(e.target.value)} placeholder="C:\Course materials"/></label><button disabled={busy||!importPath} onClick={()=>action(()=>api(`/courses/${selected.id}/import-folder`,'POST',{path:importPath}),'Files imported; bundle build started')}>Copy into library</button></details></>}</section></div>}
 </div>
}
function ScheduleEditor({initial,save,disabled}:{initial:Schedule,save:(s:Schedule)=>void,disabled:boolean}){const [s,setS]=useState(initial);useEffect(()=>setS(initial),[JSON.stringify(initial)]);return <><ScheduleFields value={s} onChange={setS}/><button className="primary" disabled={disabled} onClick={()=>save(s)}>Save schedule</button></>}
createRoot(document.getElementById('root')!).render(<App/>);
