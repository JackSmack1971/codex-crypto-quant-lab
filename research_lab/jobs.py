"""Transactional job status; no implicit retry or remote worker spawning."""
import sqlite3
from contextlib import closing
from pathlib import Path
from .core import ContractError, digest, _utc
NEXT={'NEW':{'RUNNING','CANCELLED'},'RUNNING':{'COMPLETED','FAILED','INTERRUPTED','CANCELLED','BLOCKED'},
      'INTERRUPTED':{'RECONCILING','CANCELLED'},'RECONCILING':{'RUNNING','FAILED','BLOCKED'},
      'COMPLETED':set(),'FAILED':set(),'CANCELLED':set(),'BLOCKED':set()}
class JobStore:
    def __init__(self,path):
        self.path=Path(path);self.path.parent.mkdir(parents=True,exist_ok=True)
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY,state TEXT,revision INTEGER,payload_digest TEXT,result_digest TEXT,utc TEXT)')
            db.execute('CREATE TABLE IF NOT EXISTS transitions (id TEXT,revision INTEGER,before TEXT,after TEXT,utc TEXT,PRIMARY KEY(id,revision))')
    def create(self,job_id,payload,utc):
        _utc(utc)
        if not job_id:raise ContractError('missing job id')
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?)',(job_id,'NEW',0,digest(payload),None,utc))
    def get(self,job_id):
        with closing(sqlite3.connect(self.path)) as db, db:
            row=db.execute('SELECT state,revision,payload_digest,result_digest,utc FROM jobs WHERE id=?',(job_id,)).fetchone()
        if row is None:raise ContractError('job missing')
        return dict(zip(('state','revision','payload_digest','result_digest','utc'),row))
    def move(self,job_id,expected,new,utc,result=None):
        _utc(utc)
        if new not in NEXT.get(expected,set()) or (new=='COMPLETED')!=(result is not None):
            raise ContractError('invalid transition/result')
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT state,revision,utc FROM jobs WHERE id=?',(job_id,)).fetchone()
            if not row or row[0]!=expected or _utc(utc)<_utc(row[2]):raise ContractError('stale job transition')
            revision=row[1]+1
            db.execute('UPDATE jobs SET state=?,revision=?,result_digest=?,utc=? WHERE id=?',(new,revision,digest(result) if result is not None else None,utc,job_id))
            db.execute('INSERT INTO transitions VALUES(?,?,?,?,?)',(job_id,revision,expected,new,utc))
        return revision
    def reconcile_candidates(self):
        with closing(sqlite3.connect(self.path)) as db, db:
            return [r[0] for r in db.execute("SELECT id FROM jobs WHERE state IN ('RUNNING','INTERRUPTED')")]
