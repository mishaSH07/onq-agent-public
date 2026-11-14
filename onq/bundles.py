import io
import json
import os
import shutil
import uuid
import subprocess
import sys
from urllib.parse import urljoin
from bs4 import BeautifulSoup
from pathlib import Path
from pypdf import PdfReader,PdfWriter
from markdownify import markdownify
from .store import safe,digest,now,CATEGORIES

TEXT_EXT={'.md','.txt','.py','.c','.h','.cpp','.java','.js','.ts','.sql','.r','.sh','.json','.csv','.html','.htm','.ipynb','.tex'}

def source_text(path,url=''):
    text=path.read_text(encoding='utf-8-sig',errors='replace')
    if path.suffix.lower() in {'.html','.htm'}:
        soup=BeautifulSoup(text,'html.parser')
        for tag in soup(['script','style','nav','header','footer']): tag.decompose()
        for tag in soup.select('[href],[src]'):
            for attr in ('href','src'):
                if tag.has_attr(attr): tag[attr]=urljoin(url,tag[attr])
        return markdownify(str(soup))
    if path.suffix.lower()=='.ipynb':
        nb=json.loads(text)
        blocks=[]
        for n,cell in enumerate(nb.get('cells',[]),1):
            src=''.join(cell.get('source',[]))
            blocks.append(f'### Cell {n}: {cell.get("cell_type", "unknown")}\n\n'+(src if cell.get('cell_type')=='markdown' else fenced(src)))
            for out in cell.get('outputs',[]):
                if 'text' in out: blocks.append(fenced(''.join(out['text'])))
                for kind,value in out.get('data',{}).items():
                    if kind.startswith('text/'):
                        blocks.append(fenced(''.join(value) if isinstance(value,list) else value))
                    else: blocks.append(f'Notebook output {kind} is preserved in the original notebook, not rendered here.')
        return '\n\n'.join(blocks)
    if path.suffix.lower() in {'.md','.txt'}: return text
    return fenced(text)

def fenced(text):
    import re
    n=max([len(x) for x in re.findall(r'`+',text)]+[2])+1
    fence='`'*n
    return f'{fence}\n{text}\n{fence}'

def build(store,c):
    items=store.items(c['id'])
    fingerprint=digest(json.dumps({'v':4,'course':[c['name'],c['semester']], 'items':[(i['id'],i['current_hash'],i['category'],i['status'],i['note']) for i in items],
        'limits':[store.setting('max_pdf_mb'),store.setting('max_pdf_pages')]},sort_keys=True).encode())
    previous=store.latest(c['id'])
    if previous and previous['fingerprint']==fingerprint:
        return previous,False
    release=uuid.uuid4().hex
    folder=Path('Project Bundles')/safe(c['semester'])/(safe(c['name'])+'-'+c['id'][:6])/release
    stage=store.root/'_staging'/release
    stage.mkdir(parents=True)
    mapping=[]
    warnings=[]
    maxbytes=int(store.setting('max_pdf_mb'))*1024*1024
    maxpages=int(store.setting('max_pdf_pages'))
    for category in CATEGORIES:
        members=[i for i in items if i['category']==category and i['path'] and i['status']=='available']
        writer=PdfWriter()
        writer_bytes=0
        volume=1
        pdfname=lambda: f'{category}-{volume:02}.pdf'
        def flush():
            nonlocal writer,volume,writer_bytes
            if writer.pages:
                writer.write(stage/pdfname())
                writer.close()
                volume+=1
                writer=PdfWriter()
                writer_bytes=0
        markdown=[]
        included={}
        for item in members:
            p=store.root/item['path']
            if not p.exists(): raise FileNotFoundError(f'Archived source is missing: {p.name}')
            if digest(p.read_bytes())!=item['current_hash']: raise ValueError(f'Archived source changed unexpectedly: {p.name}')
            if item['current_hash'] in included:
                mapping.append({'item':item['id'],'title':item['title'],'same_content_as':included[item['current_hash']],'source':item['url']})
                continue
            included[item['current_hash']]=item['id']
            if p.suffix.lower() in {'.pptx','.docx'}:
                cache=store.root/'Converted'/f'{item["current_hash"]}.pdf'
                cache.parent.mkdir(exist_ok=True)
                if not cache.exists():
                    try:
                        command=([sys.executable,'--convert-office'] if getattr(sys,'frozen',False) else [sys.executable,'-m','onq.convert_office'])
                        subprocess.run(command+[str(p),str(cache)],
                            cwd=Path(__file__).resolve().parents[1],check=True,timeout=120,capture_output=True,
                            creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
                    except Exception as e:
                        raise ValueError(f'Office could not convert {item["title"]}. Original preserved; install/activate Office and rebuild.') from e
                p=cache
            if p.suffix.lower()=='.pdf':
                try:
                    reader=PdfReader(p)
                    if reader.is_encrypted: raise ValueError('Encrypted PDF; original retained')
                    for page_no,page in enumerate(reader.pages,1):
                        if len(writer.pages)>=maxpages: flush()
                        trial=PdfWriter()
                        trial.add_page(page)
                        buffer=io.BytesIO(); trial.write(buffer)
                        pagebytes=buffer.tell()
                        if writer_bytes+pagebytes>maxbytes*.9 and writer.pages:
                            flush()
                        writer.add_page(page)
                        writer_bytes+=pagebytes
                        mapping.append({'item':item['id'],'title':item['title'],'source_page':page_no,'bundle':pdfname(),'page':len(writer.pages),'source':item['url']})
                        if pagebytes>maxbytes and len(writer.pages)==1:
                            warnings.append(f'{item["title"]}: a single page may exceed the configured upload size.')
                    if reader.attachments:
                        warnings.append(f'{item["title"]}: PDF attachments remain in the original; merged volumes contain pages only.')
                except Exception as e:
                    # Never publish a partial PDF release as complete.
                    raise ValueError(f'Cannot bundle {item["title"]}: {e}') from e
            elif p.suffix.lower() in TEXT_EXT:
                text=source_text(p,item['url'])
                markdown.append(f'# {item["title"]}\n\nSource: {item["url"]}\n\nSHA-256: `{item["current_hash"]}`\n\nStatus: {item["status"]}\n\n{text}\n')
                mapping.append({'item':item['id'],'title':item['title'],'bundle':f'{category}.md','source':item['url']})
            else:
                warnings.append(f'{item["title"]}: original {p.suffix or "file"} retained separately; not included in upload bundles.')
        flush()
        if markdown:
            # Split between sources; never truncate source text.
            parts=[]; size=0; part=1
            for block in markdown:
                if parts and size+len(block.encode())>maxbytes:
                    (stage/f'{category}-{part:02}.md').write_text('\n\n---\n\n'.join(parts),encoding='utf-8')
                    parts=[]; size=0; part+=1
                parts.append(block); size+=len(block.encode())
            if parts: (stage/f'{category}-{part:02}.md').write_text('\n\n---\n\n'.join(parts),encoding='utf-8')
            # Resolve per-source markdown mapping against actual part contents.
            for m in mapping:
                if m.get('bundle')==f'{category}.md':
                    item=next(i for i in items if i['id']==m['item'])
                    m['bundle']=next(p.name for p in sorted(stage.glob(f'{category}-*.md')) if item['current_hash'] in p.read_text(encoding='utf-8'))
    index=[f'# {c["name"]} — {c["semester"]}',f'Bundle release: {release}',
        'Complete original pages and text, without AI summaries. Dates are source statements; conflicts are not resolved automatically.',
        '## Source inventory']
    for i in items:
        index.append(f'- **{i["title"]}** — {i["category"]}; {i["status"]}. Source: {i["url"]}\n  SHA-256: {i["current_hash"] or "not downloaded"}. {i["note"]}')
    index+=['## Coverage notes']+(warnings or ['No file conversion exclusions. Check source statuses above for unavailable items.'])
    index+=['## Page and file index']
    for m in mapping:
        index.append(f'- {m["title"]}: '+(f'original page {m["source_page"]} → {m["bundle"]}, page {m["page"]}' if 'page' in m else m.get('bundle',f'duplicate of source {m.get("same_content_as")}')))
    (stage/'course-index.md').write_text('\n\n'.join(index),encoding='utf-8')
    files=[{'name':p.name,'hash':digest(p.read_bytes()),'size':p.stat().st_size} for p in sorted(stage.iterdir()) if p.is_file()]
    manifest={'release':release,'created':now(),'course':c['id'],'files':files,'mapping':mapping,'warnings':warnings}
    (stage/'manifest.json').write_text(json.dumps(manifest,indent=2),encoding='utf-8')
    dest=store.root/folder
    dest.parent.mkdir(parents=True,exist_ok=True)
    # Immutable release directories: a crash before the transaction leaves only an
    # unreferenced new directory, while the previous release stays intact.
    os.replace(stage,dest)
    with store.connect() as db:
        db.execute('INSERT INTO releases VALUES (?,?,?,?,?,?,?)',(release,c['id'],fingerprint,str(folder),now(),json.dumps(manifest),'pending'))
    return store.latest(c['id']),True
