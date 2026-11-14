"""Read-only Brightspace collector. Unsupported/inaccessible sections stay explicit."""
import json
import re
import uuid
import io
import zipfile
import httpx
from urllib.parse import urljoin,urlparse,unquote
from pathlib import Path
from bs4 import BeautifulSoup
from markdownify import markdownify
from .browser import SignInNeeded,ActionNeeded,check_onq
from .store import classify

BASE='https://onq.queensu.ca'

def semester(name):
    m=re.search(r'\b([FWS])(?:all|inter|ummer)?\s*(\d{2,4})\b',name,re.I)
    if m:
        year=m[2] if len(m[2])==4 else '20'+m[2]
        return {'F':'Fall','W':'Winter','S':'Summer'}[m[1].upper()]+' '+year
    return 'Unassigned semester'

def topic_nodes(data):
    nodes=data.get('Modules',data.get('Structure',[])) if isinstance(data,dict) else data
    for node in nodes:
        if node.get('Type')==1 or 'TopicType' in node or 'TopicId' in node:
            yield node
        for key in ('Modules','Topics','Structure'):
            if node.get(key): yield from topic_nodes(node[key])

class Collector:
    def __init__(self,store,browsers): self.store=store; self.browsers=browsers; self.versions={}

    async def connect(self):
        self.ctx=await self.browsers.context('onq')
        self.page=self.ctx.pages[0] if self.ctx.pages else await self.ctx.new_page()
        await self.page.goto(BASE+'/d2l/home',wait_until='domcontentloaded')
        await check_onq(self.page)
        response=await self.ctx.request.get(BASE+'/d2l/api/versions/')
        if response.ok:
            try:
                versions=await response.json()
                for p in versions:
                    choices=p.get('SupportedVersions',[])
                    if choices:
                        self.versions[p['ProductCode']]=max((str(v.get('Version',v)) if isinstance(v,dict) else str(v) for v in choices),key=lambda v:tuple(map(int,v.split('.'))))
                    elif p.get('LatestVersion'): self.versions[p['ProductCode']]=p['LatestVersion']
            except (ValueError,KeyError,TypeError): pass

    async def api(self,product,path):
        version=self.versions.get(product)
        if not version: raise ActionNeeded(f'onQ did not expose the {product} API version. Browser export is required.')
        r=await self.ctx.request.get(f'{BASE}/d2l/api/{product}/{version}/{path}',timeout=60000)
        if r.status==401 or 'login.microsoftonline' in r.url: raise SignInNeeded('onQ session expired. Sign in and retry.')
        if not r.ok: raise ActionNeeded(f'onQ section returned HTTP {r.status}: {path}')
        try: return await r.json()
        except Exception as e: raise ActionNeeded('onQ returned a sign-in page or unsupported response; reconnect before retrying.') from e

    async def discover(self):
        await self.connect()
        rows=[]; bookmark=None; visited=set()
        for _ in range(100):
            endpoint='enrollments/myenrollments/'
            if bookmark:
                from urllib.parse import quote
                endpoint+='?bookmark='+quote(bookmark,safe='')
            data=await self.api('lp',endpoint)
            rows.extend(data.get('Items',[]))
            paging=data.get('PagingInfo',{})
            if not paging.get('HasMoreItems'): break
            bookmark=paging.get('Bookmark')
            if not bookmark or bookmark in visited: raise ActionNeeded('Course listing pagination is incomplete.')
            visited.add(bookmark)
        else: raise ActionNeeded('Course listing exceeded safety limit; listing is incomplete.')
        for row in rows:
            org=row.get('OrgUnit',{})
            if org.get('Type',{}).get('Id')!=3: continue
            oid=str(org['Id']); name=org['Name']; term=semester(name)
            match=next((c for c in self.store.courses() if c['org_id']==oid),None)
            if match: continue
            seed=next((c for c in self.store.courses() if not c['org_id'] and c['name'].replace(' ','').lower() in name.replace(' ','').lower() and c['semester']==term),None)
            if seed:
                self.store.update_course(seed['id'],org_id=oid)
            else:
                self.store.save_course({'id':uuid.uuid4().hex,'org_id':oid,'name':name,'semester':term,'enabled':False,'project':'',
                    'schedule':None,'next_run':None,'last_check':None,'local_status':'Not checked','cloud_status':'Not mapped','new_count':0})
        return len(rows)

    async def download(self,c,key,title,url,depth=0,parent_category=None):
        url=urljoin(BASE,url)
        parsed=urlparse(url)
        if parsed.hostname=='onq.queensu.ca' and '/quickLink' in parsed.path:
            self.store.issue(c,key,title,url,'external','Launch link to a separate course service; sign-in/launch HTML is not course material.')
            return 0
        if parsed.scheme=='http' and parsed.hostname and parsed.hostname.endswith('.queensu.ca'):
            url='https:'+url[5:]; parsed=urlparse(url)
        # The onQ-linked Binder notebook entry identifies the instructor's public repo.
        if parsed.hostname=='mybinder.org' and '/v2/gh/' in parsed.path:
            parts=parsed.path.split('/v2/gh/',1)[1].split('/')
            if len(parts)>=3:
                url=f'https://codeload.github.com/{parts[0]}/{parts[1]}/zip/{parts[2]}'
                parsed=urlparse(url)
        same_onq=parsed.hostname=='onq.queensu.ca'
        public_course=parsed.hostname in ('research.cs.queensu.ca','codeload.github.com','raw.githubusercontent.com')
        if parsed.scheme!='https' or not (same_onq or public_course):
            self.store.issue(c,key,title,url,'external','External resource: retained as a link; download coverage is not verified.')
            return False
        # Do not forward onQ cookies to any external resource.
        if not same_onq:
            async with httpx.AsyncClient(follow_redirects=False,timeout=120) as client:
                response=await client.get(url)
            class Response:
                status=response.status_code; headers=response.headers; ok=response.is_success
                async def body(self): return response.content
            r=Response()
        else: r=await self.ctx.request.get(url,timeout=120000,max_redirects=0)
        if r.status in (301,302,303,307,308):
            target=urljoin(url,r.headers.get('location',''))
            if urlparse(target).hostname!='onq.queensu.ca':
                if 'login' in target: raise SignInNeeded('onQ session expired during download.')
                self.store.issue(c,key,title,target,'external','Redirects to an external service; original not downloaded.')
                return False
            r=await self.ctx.request.get(target,timeout=120000,max_redirects=0)
        if r.status==401: raise SignInNeeded('onQ session expired during download.')
        if not r.ok:
            self.store.issue(c,key,title,url,'unavailable',f'Download returned HTTP {r.status}; earlier versions preserved.')
            return False
        data=await r.body()
        changed=0
        category=classify(title+' '+url)
        if category=='course-info' and parent_category: category=parent_category
        ct=r.headers.get('content-type','').lower()
        name=Path(unquote(parsed.path)).name or title
        disposition=r.headers.get('content-disposition','')
        m=re.search(r"filename\*?=(?:UTF-8'')?[\"]?([^\";]+)",disposition,re.I)
        if m: name=unquote(m[1])
        if 'text/html' in ct:
            soup=BeautifulSoup(data,'html.parser')
            if soup.select_one('input[type=password]') or 'Sign in to your account' in soup.get_text():
                raise SignInNeeded('onQ returned a sign-in page instead of course material.')
            # Archive exact HTML bytes. Bundle conversion happens separately.
            name=Path(name).stem+'.html'
            if depth<3:
                links=[]
                for a in soup.select('a[href],iframe[src],video[src],audio[src],source[src],img[src]'):
                    href=a.get('href') or a.get('src')
                    if not href or href.startswith(('#','mailto:','javascript:','data:')): continue
                    absolute=urljoin(url,href).split('#')[0]
                    if absolute==url or absolute in self.visited: continue
                    u=urlparse(absolute)
                    # Follow actual material links, never LMS navigation or submission actions.
                    if u.hostname=='onq.queensu.ca' and '/content/' not in u.path: continue
                    if u.hostname==parsed.hostname and u.path.endswith('/') and not same_onq: continue
                    links.append((absolute,a.get_text(' ',strip=True) or Path(unquote(u.path)).name or 'Embedded resource'))
                for linked,label in links:
                    if len(self.visited)>=1500:
                        self.store.issue(c,'link:'+linked,label,linked,'external','Per-course crawl limit reached; not downloaded.'); continue
                    self.visited.add(linked)
                    try:
                        changed+=int(await self.download(c,'link:'+linked,label,linked,depth+1,category))
                    except SignInNeeded: raise
                    except Exception as e: self.store.issue(c,'link:'+linked,label,linked,'error',str(e)[:300])
            elif soup.select('a[href],iframe[src]'):
                self.store.issue(c,key+':coverage',title+' linked resources',url,'external','Link traversal depth reached; further links are indexed but not downloaded.')
        elif data.startswith(b'%PDF'): name=Path(name).stem+'.pdf'
        elif data.startswith(b'PK\x03\x04') and not name.lower().endswith(('.pptx','.docx','.xlsx','.zip')): name=Path(name).stem+'.zip'
        changed+=int(self.store.ingest(c,key,title,url,data,name,category))
        if name.lower().endswith('.zip'):
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                entries=archive.infolist()
                if len(entries)>5000 or sum(x.file_size for x in entries)>500*1024*1024:
                    self.store.issue(c,key+':coverage',title+' archive contents',url,'external','Archive retained; expansion exceeds safe size limits.')
                else:
                    member_keys=set()
                    for entry in entries:
                        if entry.is_dir() or entry.flag_bits&1: continue
                        suffix=Path(entry.filename).suffix.lower()
                        if suffix not in {'.md','.txt','.ipynb','.py','.c','.h','.cpp','.java','.pdf','.pptx','.docx','.json','.csv','.html','.png','.jpg'}: continue
                        # No filesystem extraction: entries are stored under generated hash paths.
                        part=archive.read(entry)
                        member_keys.add(key+':zip:'+entry.filename)
                        changed+=int(self.store.ingest(c,key+':zip:'+entry.filename,entry.filename,url,part,Path(entry.filename).name,
                            'code' if suffix in {'.ipynb','.py','.c','.h','.cpp','.java'} else category))
                    self.store.mark_missing(c['id'],member_keys,key+':zip:')
        return changed

    async def collect(self,c):
        await self.connect()
        if not c['org_id']: raise ActionNeeded('Discover courses first to connect this course to its onQ enrollment.')
        org=c['org_id']; changed=0; seen=set(); problems=[]; self.visited=set()
        toc=await self.api('le',f'{org}/content/toc')
        if not isinstance(toc,dict) or 'Modules' not in toc: raise ActionNeeded('Unexpected content listing. Existing sources were not marked missing.')
        def stable(obj):
            if isinstance(obj,dict): return {k:stable(v) for k,v in obj.items() if k not in ('Unread','Bookmarked')}
            if isinstance(obj,list): return [stable(v) for v in obj]
            return obj
        changed+=int(self.store.ingest(c,'updates:toc','Content inventory',BASE+f'/d2l/le/content/{org}/Home',json.dumps(stable(toc),indent=2,ensure_ascii=False).encode(),'content-inventory.json','course-info'))
        for topic in topic_nodes(toc):
            tid=topic.get('TopicId',topic.get('Id'))
            key='content:'+str(tid); seen.add(key)
            title=topic.get('Title','Untitled'); url=topic.get('Url')
            if topic.get('ActivityType') in (7,27):
                self.store.issue(c,key,title,url or BASE+f'/d2l/le/content/{org}/Home','external','External learning-tool launch; requires a separate integration.')
                continue
            # ToC URLs can be viewer pages. Topic metadata supplies the underlying source URL.
            try:
                detail=await self.api('le',f'{org}/content/topics/{tid}')
                url=detail.get('Url') or url
            except SignInNeeded: raise
            except ActionNeeded: pass
            if not url:
                self.store.issue(c,key,title,BASE+f'/d2l/le/content/{org}/Home','unavailable','No downloadable URL exposed for this activity.')
                continue
            try: changed+=int(await self.download(c,key,title,url))
            except SignInNeeded: raise
            except Exception as e:
                problems.append(title)
                self.store.issue(c,key,title,url,'error',str(e)[:300])
        self.store.mark_missing(c['id'],seen,'content:')
        # Preserve raw source fields alongside readable text; conflicting dates remain distinct.
        for section,path in [('announcements',f'{org}/news/'),('assignments',f'{org}/dropbox/folders/'),('calendar',f'{org}/calendar/events/')]:
            key='updates:'+section
            url=BASE+f'/d2l/home/{org}'
            try:
                data=await self.api('le',path)
                raw=json.dumps(data,indent=2,ensure_ascii=False)
                rendered=[]
                def walk(obj):
                    if isinstance(obj,dict):
                        for k,v in obj.items():
                            if k in ('Html','Text','Title','Name','DueDate','StartDate','EndDate') and isinstance(v,str):
                                rendered.append(f'**{k}**\n\n'+markdownify(v))
                            else: walk(v)
                    elif isinstance(obj,list):
                        for x in obj: walk(x)
                walk(data)
                text=f'# {section.title()} — source records\n\n'+ '\n\n'.join(rendered)+'\n\n## Complete original fields\n\n```json\n'+raw+'\n```\n'
                changed+=int(self.store.ingest(c,key,section.title(),url,text.encode(),section+'.md','course-info'))
                # Collect attachment URLs explicitly supplied in the returned records.
                async def attachments(obj):
                    nonlocal changed
                    if isinstance(obj,dict):
                        html=obj.get('Html')
                        if isinstance(html,str):
                            for link in BeautifulSoup(html,'html.parser').select('a[href]'):
                                target=urljoin(BASE,link['href']).split('#')[0]
                                if target not in self.visited:
                                    self.visited.add(target)
                                    changed+=int(await self.download(c,'link:'+target,link.get_text(' ',strip=True) or 'Linked course resource',target))
                        for field in ('Attachments',):
                            for a in obj.get(field,[]) or []:
                                if isinstance(a,dict) and a.get('Url'):
                                    changed+=int(await self.download(c,f'{key}:attachment:{a.get("FileId",a["Url"])}',a.get('FileName',a.get('Name','Attachment')),a['Url']))
                        for k,v in obj.items():
                            if k!='Attachments': await attachments(v)
                    elif isinstance(obj,list):
                        for x in obj: await attachments(x)
                await attachments(data)
            except SignInNeeded: raise
            except Exception as e:
                problems.append(section)
                self.store.issue(c,key,section.title(),url,'error',str(e)[:300])
        self.store.mark_missing(c['id'],{'link:'+url for url in self.visited},'link:')
        if any(i['status']!='available' for i in self.store.items(c['id'])): problems.append('Some sources are unavailable or external')
        return changed,problems
