"""Local metadata and immutable source versions. No credentials in this database."""
import hashlib
import json
import os
import re
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

CATEGORIES = ['lectures', 'assignments', 'practice-tests', 'solutions', 'course-info', 'code', 'media']
def now():
    return datetime.now(timezone.utc).isoformat()

def filename_stamp(created):
    return datetime.fromisoformat(created).astimezone(
        ZoneInfo('America/Toronto')
    ).strftime('%Y-%m-%d_%H-%M-%S.%f%z')


def safe(value):
    value = re.sub(r'[^\w .()-]', '_', str(value)).replace('..','_').strip(' .')[:100]
    if not value or value.upper().split('.')[0] in {'CON','PRN','AUX','NUL',*(f'COM{i}' for i in range(10)),*(f'LPT{i}' for i in range(10))}:
        value = '_' + value
    return value

def digest(data):
    return hashlib.sha256(data).hexdigest()

def default_documents():
    if os.name == 'nt':
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r'Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders') as key:
            return Path(os.path.expandvars(winreg.QueryValueEx(key, 'Personal')[0]))
    return Path.home() / 'Documents'

def validate_library(path):
    path = Path(path).expanduser().resolve()
    if not path.is_absolute() or path == path.anchor or '.chatgpt-projects' in path.parts or 'sources' in path.parts:
        raise ValueError('Choose a dedicated library outside ChatGPT project mirrors and sources folders.')
    if path.name.lower() == 'queens':
        raise ValueError('Choose a dedicated library folder for the managed archive.')
    return path

class Store:
    def __init__(self, state=None, library=None):
        self.state = Path(state or os.environ.get('ONQ_STATE', Path(os.environ.get('LOCALAPPDATA', Path.home())) / 'onQ Library'))
        self.state.mkdir(parents=True, exist_ok=True)
        self.db = self.state / 'library.sqlite3'
        with self.connect() as db:
            db.executescript('''
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS courses(id TEXT PRIMARY KEY, data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS items(id TEXT PRIMARY KEY, course TEXT NOT NULL, source_key TEXT NOT NULL,
              title TEXT, url TEXT, category TEXT, status TEXT, note TEXT DEFAULT '', current_hash TEXT, last_seen TEXT,
              UNIQUE(course,source_key));
            CREATE TABLE IF NOT EXISTS versions(id TEXT PRIMARY KEY, item TEXT NOT NULL, hash TEXT NOT NULL,
              path TEXT NOT NULL, created TEXT NOT NULL, size INTEGER, UNIQUE(item,hash));
            CREATE TABLE IF NOT EXISTS releases(id TEXT PRIMARY KEY, course TEXT, fingerprint TEXT, path TEXT,
              created TEXT, manifest TEXT, cloud_status TEXT DEFAULT 'pending');
            CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY, started TEXT, finished TEXT, course TEXT, stage TEXT,
              status TEXT, message TEXT, changed INTEGER DEFAULT 0);
            CREATE TABLE IF NOT EXISTS remote_files(project TEXT, name TEXT, hash TEXT, release TEXT, status TEXT,
              PRIMARY KEY(project,name));
            CREATE TABLE IF NOT EXISTS harness_jobs(id TEXT PRIMARY KEY,course TEXT NOT NULL,project TEXT NOT NULL,
              release TEXT NOT NULL,status TEXT NOT NULL,trigger TEXT NOT NULL,created TEXT NOT NULL,
              updated TEXT NOT NULL,data TEXT NOT NULL,error TEXT NOT NULL DEFAULT '',UNIQUE(course,project,release));
            CREATE TABLE IF NOT EXISTS source_uploads(project TEXT NOT NULL, name TEXT NOT NULL,
              course TEXT NOT NULL, job TEXT NOT NULL, hash TEXT NOT NULL, size INTEGER NOT NULL,
              backup TEXT NOT NULL, status TEXT NOT NULL, updated TEXT NOT NULL, PRIMARY KEY(project,name));
            CREATE TABLE IF NOT EXISTS source_removals(project TEXT NOT NULL, name TEXT NOT NULL,
              replacement_job TEXT NOT NULL, baseline TEXT NOT NULL, PRIMARY KEY(project,name));
            CREATE TABLE IF NOT EXISTS manual_pending(course TEXT,project TEXT,name TEXT,PRIMARY KEY(course,project,name));
            CREATE TABLE IF NOT EXISTS handoff_tickets(id TEXT PRIMARY KEY,course TEXT,project TEXT,data TEXT,created TEXT);
            CREATE TABLE IF NOT EXISTS manual_receipts(course TEXT,project TEXT,name TEXT,hash TEXT,confirmed TEXT,
              PRIMARY KEY(course,project,name));
            ''')
        if self.setting('library') is None:
            self.set_setting('library', str(validate_library(library or default_documents() / 'onQ Library')))
            self.set_setting('schedule', {'kind':'daily','time':'19:00','weekday':0,'hours':6,'timezone':'America/Toronto'})
            self.set_setting('paused', False)
            self.set_setting('startup_catchup', True)
            self.set_setting('cloud_verified', False)
            self.set_setting('max_pdf_mb', 40)
            self.set_setting('max_pdf_pages', 400)
        self.root.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute("UPDATE runs SET status='interrupted',finished=?,message='App stopped during this run. Retry is safe.' WHERE status='running'", (now(),))

    @property
    def root(self): return validate_library(self.setting('library'))

    def connect(self):
        db = sqlite3.connect(self.db, timeout=30)
        db.row_factory = sqlite3.Row
        return db

    def rows(self, sql, args=()):
        with self.connect() as db: return [dict(r) for r in db.execute(sql,args).fetchall()]

    def execute(self, sql, args=()):
        with self.connect() as db: db.execute(sql,args)

    def setting(self,key):
        rows = self.rows('SELECT value FROM settings WHERE key=?',(key,))
        return json.loads(rows[0]['value']) if rows else None

    def set_setting(self,key,value):
        self.execute('INSERT OR REPLACE INTO settings VALUES (?,?)',(key,json.dumps(value)))

    def courses(self): return sorted([json.loads(x['data']) for x in self.rows('SELECT data FROM courses')],key=lambda c:(not c['enabled'],c['semester'],c['name']))

    def course(self,cid):
        return next((c for c in self.courses() if c['id']==cid),None)

    def save_course(self,c): self.execute('INSERT OR REPLACE INTO courses VALUES (?,?)',(c['id'],json.dumps(c)))

    def update_course(self,cid,**kwargs):
        c=self.course(cid)
        c.update(kwargs)
        self.save_course(c)
        return c

    def items(self,cid):
        return self.rows('''SELECT i.*,v.path,v.created,v.size FROM items i LEFT JOIN versions v
          ON v.item=i.id AND v.hash=i.current_hash WHERE i.course=? ORDER BY i.category,i.title,i.id''',(cid,))

    def ingest(self,c,source_key,title,url,data,filename,category=None):
        sha=digest(data)
        iid=digest((c['id']+'\0'+source_key).encode())[:32]
        existing=self.rows('SELECT * FROM items WHERE id=?',(iid,))
        category=existing[0]['category'] if existing else (category or classify(title+' '+filename))
        suffix=Path(filename).suffix[:12]
        filename=safe(Path(filename).stem)[:85]+suffix
        created = now()
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            prior = db.execute('SELECT path FROM versions WHERE item=? AND hash=?', (iid, sha)).fetchone()
            if prior:
                rel = Path(prior['path'])
            else:
                folder = Path('Originals') / safe(c['semester']) / (safe(c['name'])+'-'+c['id'][:6]) / iid
                stamp = filename_stamp(created)
                rel = folder / f'{stamp}-{filename}'
                counter = 2
                while (self.root / rel).exists() or db.execute(
                    'SELECT 1 FROM versions WHERE path=?', (str(rel),)
                ).fetchone():
                    rel = folder / f'{stamp}-{counter}-{filename}'
                    counter += 1
            target = self.root / rel
            if not target.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                tmp = target.with_name(target.name+'.partial')
                tmp.write_bytes(data)
                os.replace(tmp,target)
            db.execute('''INSERT INTO items VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
              title=excluded.title,url=excluded.url,status='available',note='',current_hash=excluded.current_hash,last_seen=excluded.last_seen''',
              (iid,c['id'],source_key,title,url,category,'available','',sha,now()))
            db.execute('INSERT OR IGNORE INTO versions VALUES (?,?,?,?,?,?)',(uuid.uuid4().hex,iid,sha,str(rel),created,len(data)))
        return not existing or existing[0]['current_hash']!=sha

    def issue(self,c,key,title,url,status,note):
        iid=digest((c['id']+'\0'+key).encode())[:32]
        self.execute('''INSERT INTO items VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
          status=excluded.status,note=excluded.note,last_seen=excluded.last_seen''',
          (iid,c['id'],key,title,url,classify(title),status,note,None,now()))

    def mark_missing(self,cid,seen,prefix):
        for i in self.items(cid):
            if i['source_key'].startswith(prefix) and i['source_key'] not in seen:
                parent=i['source_key'].split(':zip:',1)[0].split(':coverage',1)[0]
                if parent in seen:
                    continue
                self.execute("UPDATE items SET status='unavailable',note='Absent from the latest complete listing; preserved locally.' WHERE id=?",(i['id'],))

    def latest(self,cid):
        r=self.rows('SELECT * FROM releases WHERE course=? ORDER BY created DESC LIMIT 1',(cid,))
        return r[0] if r else None

    def start_run(self,cid,stage):
        rid=uuid.uuid4().hex
        self.execute('INSERT INTO runs VALUES (?,?,?,?,?,?,?,?)',(rid,now(),None,cid,stage,'running','Starting',0))
        return rid

    def finish_run(self,rid,status,message,changed=0):
        self.execute('UPDATE runs SET finished=?,status=?,message=?,changed=? WHERE id=?',(now(),status,message,changed,rid))

def classify(text):
    t=text.lower()
    if re.search(r'solution|answer|corrig',t): return 'solutions'
    if re.search(r'practice|sample|test|exam|quiz',t): return 'practice-tests'
    if re.search(r'assignment|\ba\d\b|homework',t): return 'assignments'
    if re.search(r'\.ipynb|\.py\b|\.java|\.c\b|notebook|code',t): return 'code'
    if re.search(r'\.mp4|\.mp3|\.mov|video|recording',t): return 'media'
    if re.search(r'lecture|slide|note|worksheet',t): return 'lectures'
    return 'course-info'
