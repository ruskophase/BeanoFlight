"""Durable machine job journal. Network readers never share the capture thread."""
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import threading
import statistics


def encode(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


def fingerprint(value):
    return hashlib.sha256(encode(value).encode()).hexdigest()


def utc():
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as c:
            c.executescript('''
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY, digest TEXT NOT NULL,
                    request TEXT NOT NULL, state TEXT NOT NULL, status TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS events(job_id TEXT NOT NULL, sequence INTEGER NOT NULL,
                    kind TEXT NOT NULL, data TEXT NOT NULL, created_at TEXT NOT NULL,
                    PRIMARY KEY(job_id,sequence));
                CREATE TABLE IF NOT EXISTS commands(job_id TEXT NOT NULL, key TEXT NOT NULL,
                    action TEXT NOT NULL, result TEXT NOT NULL, PRIMARY KEY(job_id,key));
            ''')

    @contextmanager
    def connect(self):
        c = sqlite3.connect(self.path, timeout=10)
        c.row_factory = sqlite3.Row
        try:
            with c: yield c
        finally: c.close()

    def create(self, key, request):
        digest = fingerprint(request)
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            row = c.execute('SELECT * FROM jobs WHERE id=?', (key,)).fetchone()
            if row:
                if row['digest'] != digest: raise ValueError('Job identity already has different settings')
                return False
            if c.execute("SELECT 1 FROM jobs WHERE state NOT IN ('completed','failed')").fetchone():
                raise ValueError('Another machine job is active')
            status = {'state':'starting', 'active_seconds':0, 'elapsed_seconds':0, 'metrics':{},
                'final_sequence':None, 'error':None}
            c.execute('INSERT INTO jobs VALUES(?,?,?,?,?)', (key,digest,encode(request),'starting',encode(status)))
            return True

    def job(self, key):
        with self.connect() as c:
            row = c.execute('SELECT * FROM jobs WHERE id=?',(key,)).fetchone()
            if row is None: raise KeyError('Unknown machine job')
            cursor = c.execute('SELECT COALESCE(MAX(sequence),0) FROM events WHERE job_id=?',(key,)).fetchone()[0]
            return {'id':key, 'request':json.loads(row['request']), **json.loads(row['status']), 'cursor':cursor}

    def append(self, key, kind, data):
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            sequence = c.execute('SELECT COALESCE(MAX(sequence),0)+1 FROM events WHERE job_id=?',(key,)).fetchone()[0]
            c.execute('INSERT INTO events VALUES(?,?,?,?,?)',(key,sequence,kind,encode(data),utc()))
            return sequence

    def status(self, key, status):
        with self.connect() as c:
            c.execute('UPDATE jobs SET state=?,status=? WHERE id=?',(status['state'],encode(status),key))

    def events(self, key, after=0, limit=100):
        if after < 0 or not 1 <= limit <= 250: raise ValueError('Invalid journal page')
        with self.connect() as c:
            rows=c.execute('SELECT * FROM events WHERE job_id=? AND sequence>? ORDER BY sequence LIMIT ?', (key,after,limit))
            return [{'sequence':r['sequence'],'kind':r['kind'],'data':json.loads(r['data']), 'created_at':r['created_at']} for r in rows]

    def event(self, key, sequence):
        """Read one committed record without a journal scan or capture interaction."""
        with self.connect() as c:
            row=c.execute('SELECT * FROM events WHERE job_id=? AND sequence=?',(key,sequence)).fetchone()
            if row is None:raise KeyError('Unknown journal event')
            return {'sequence':row['sequence'],'kind':row['kind'],'data':json.loads(row['data'])}

    def command(self, key, command_id, action, execute):
        # Manager serializes commands. Persist an intent before execution; uncertain
        # commands are never re-executed after a process restart.
        with self.connect() as c:
            row=c.execute('SELECT * FROM commands WHERE job_id=? AND key=?',(key,command_id)).fetchone()
            if row:
                if row['action']!=action: raise ValueError('Command identity reused with a different action')
                return json.loads(row['result'])
            c.execute('INSERT INTO commands VALUES(?,?,?,?)',(key,command_id,action,encode({'accepted':True,'action':action})))
        return execute()

    def recover(self):
        with self.connect() as c:
            keys=[r[0] for r in c.execute("SELECT id FROM jobs WHERE state NOT IN ('completed','failed')")]
        for key in keys:
            before=self.job(key)
            # Reconstruct any active beans from measurements already committed
            # before the crash, so their evidence remains usable downstream.
            with self.connect() as c:
                tracked={};measured={};finalized=set()
                for row in c.execute('SELECT kind,data FROM events WHERE job_id=? ORDER BY sequence',(key,)):
                    data=json.loads(row['data']);seq=data.get('bean_sequence')
                    if row['kind']=='tracked':tracked[seq]=data
                    elif row['kind']=='measurement':measured.setdefault(seq,[]).append(data)
                    elif row['kind']=='bean':finalized.add(seq)
            for seq,bean in tracked.items():
                if seq in finalized:continue
                rows=measured.get(seq,[]);features={}
                if rows:
                    for name,value in rows[0].items():
                        if (name.startswith(('caml_','camr_')) or name in {'projected_area_geomean_mm2','rotational_ellipsoid_volume_proxy_mm3'}) and type(value) in (int,float):
                            features[name]=statistics.median(r[name] for r in rows if name in r)
                self.append(key,'bean',{'bean_sequence':seq,'ordinal':bean['ordinal'],'features':features,
                    'first_timestamp_ns':bean.get('first_timestamp_ns'),
                    'sample_count':len(rows),'decision':'pass','action':'not_issued','decision_measurements_used':0,
                    'measurement_status':'measured' if rows else 'missing','terminal_status':'service_interrupted'})
            sequence=self.append(key,'failed',{'reason':'Machine service restarted; run was not resumed automatically'})
            self.status(key,{**before,'state':'failed','error':'Machine service restarted; evidence may be incomplete',
                'final_sequence':sequence})
