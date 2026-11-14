"""Manual project upload snapshots. A user receipt is never remote verification."""
import json
import os
import uuid
from pathlib import Path
from .store import digest,now
from .publisher import project_url

class Handoff:
    def __init__(self,store):self.store=store

    def describe(self,c):
        release=self.store.latest(c['id'])
        if not release:return {'release':None,'files':[],'removals':[],'removal_pending':0,'pending':0,'total':0,'reported':0}
        project=project_url(c['project']) if c.get('project') else ''
        receipts={r['name']:r for r in self.store.rows('SELECT * FROM manual_receipts WHERE course=? AND project=?',(c['id'],project))}
        forced={r['name'] for r in self.store.rows('SELECT name FROM manual_pending WHERE course=? AND project=?',(c['id'],project))}
        files=[]
        for f in json.loads(release['manifest'])['files']:
            if Path(f['name']).name!=f['name']:raise ValueError('Invalid bundle filename')
            previous=receipts.get(f['name'])
            files.append({**f,'upload_name':f'onq-{c["id"][:6]}-{f["name"]}',
              'changed':f['name'] in forced or not previous or previous['hash']!=f['hash'],
              'last_reported':previous['confirmed'] if previous else None})
        pending=sum(f['changed'] for f in files)
        current={f['name'] for f in files}
        removals=[{**r,'upload_name':f'onq-{c["id"][:6]}-{r["name"]}'} for r in receipts.values() if r['name'] not in current]
        return {'project':project,'release':release['id'],'release_path':release['path'],'files':files,'removals':removals,
          'removal_pending':len(removals),'pending':pending,'total':len(files),'reported':len(files)-pending}

    def summary(self,c):
        d=self.describe(c)
        return {k:d[k] for k in ('release','pending','total','reported','removal_pending')}

    def prepare(self,c):
        if not c.get('project'):raise ValueError('Set this course’s ChatGPT project URL first.')
        project=project_url(c['project']); description=self.describe(c)
        if not description['release']:raise ValueError('Build this course’s bundles first.')
        files=[f for f in description['files'] if f['changed']]
        if not files and not description['removals']:raise ValueError('No changed bundles. Mark files pending again if you need another upload.')
        signature=lambda fs:[(f['name'],f['hash']) for f in fs]
        for row in self.store.rows('SELECT data FROM handoff_tickets WHERE course=? AND project=? ORDER BY created DESC LIMIT 10',(c['id'],project)):
            candidate=json.loads(row['data'])
            if candidate['release']!=description['release'] or signature(candidate['files'])!=signature(files) or signature(candidate.get('removals',[]))!=signature(description['removals']):continue
            if all((self.store.root/candidate['path']/f['upload_name']).is_file() and digest((self.store.root/candidate['path']/f['upload_name']).read_bytes())==f['hash'] for f in files):return candidate
        tid=uuid.uuid4().hex
        relative=Path('Upload Queue')/c['id']/tid
        destination=self.store.root/relative; destination.mkdir(parents=True)
        for f in files:
            source=(self.store.root/description['release_path']/f['name']).resolve()
            if not source.is_relative_to(self.store.root):raise ValueError('Bundle is outside the managed library.')
            data=source.read_bytes()
            if digest(data)!=f['hash']:raise ValueError('A bundle changed after generation. Rebuild it before preparing uploads.')
            (destination/f['upload_name']).write_bytes(data)
        ticket={'id':tid,'course':c['id'],'course_name':c['name'],'project':project,'release':description['release'],
          'path':str(relative),'files':files,'removals':description['removals'],'created':now()}
        # Checklist is outside the selected upload files, so only course files are in the queue.
        self.store.execute('INSERT INTO handoff_tickets VALUES (?,?,?,?,?)',(tid,c['id'],project,json.dumps(ticket),now()))
        return ticket

    def ticket(self,c,tid):
        rows=self.store.rows('SELECT data FROM handoff_tickets WHERE id=? AND course=?',(tid,c['id']))
        if not rows:raise ValueError('Upload preparation not found for this course.')
        return json.loads(rows[0]['data'])

    def confirm(self,c,tid,names):
        ticket=self.ticket(c,tid)
        if ticket['project']!=project_url(c['project']):raise ValueError('The project destination changed. Prepare a new upload folder.')
        if not names or not isinstance(names,list):raise ValueError('Select the files you actually uploaded.')
        chosen={f['name']:f for f in ticket['files']}
        if any(not isinstance(n,str) or n not in chosen for n in names):raise ValueError('A selected file is not in this upload preparation.')
        for n in names:
            path=self.store.root/ticket['path']/chosen[n]['upload_name']
            if digest(path.read_bytes())!=chosen[n]['hash']:raise ValueError('Prepared upload file changed. Prepare a new folder.')
        with self.store.connect() as db:
            for n in set(names):
                db.execute('DELETE FROM manual_pending WHERE course=? AND project=? AND name=?',(c['id'],ticket['project'],n))
                db.execute('INSERT OR REPLACE INTO manual_receipts VALUES (?,?,?,?,?)',
                  (c['id'],ticket['project'],n,chosen[n]['hash'],now()))
        return self.summary(c)

    def reset(self,c,names):
        allowed={f['name'] for f in self.describe(c)['files']}
        if not isinstance(names,list) or not names or any(not isinstance(n,str) or n not in allowed for n in names):raise ValueError('Choose current bundle files to mark pending.')
        with self.store.connect() as db:
            for n in set(names):db.execute('INSERT OR IGNORE INTO manual_pending VALUES (?,?,?)',(c['id'],project_url(c['project']),n))
        return self.summary(c)

    def removal_check(self,c,tid):
        ticket=self.ticket(c,tid); current=self.describe(c)
        if ticket['project']!=project_url(c['project']) or ticket['release']!=current['release']:
            raise ValueError('The course changed. Prepare a fresh removal checklist before deleting any project files.')
        return ticket.get('removals',[])

    def confirm_removals(self,c,tid,names):
        ticket=self.ticket(c,tid)
        if ticket['project']!=project_url(c['project']):raise ValueError('The project destination changed. Prepare a new checklist.')
        allowed={f['name']:f for f in self.removal_check(c,tid)}
        current=self.describe(c);current_names={f['name'] for f in current['files']}
        if not isinstance(names,list) or not names or any(not isinstance(n,str) or n not in allowed for n in names):raise ValueError('Select obsolete files from this checklist.')
        if any(n in current_names for n in names):raise ValueError('A selected bundle is current again. Do not remove it; prepare a new checklist.')
        with self.store.connect() as db:
            for n in set(names):db.execute('DELETE FROM manual_receipts WHERE course=? AND project=? AND name=? AND hash=?',(c['id'],ticket['project'],n,allowed[n]['hash']))
        return self.summary(c)
