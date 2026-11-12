"""Immutable, searchable revisions of conclusions, not a live truth database."""
import json
import sqlite3
from contextlib import closing
from pathlib import Path
from .core import ContractError, _utc, digest, canonical_json
STATES={'UNVERIFIED','SUPPORTED','REJECTED','SUPERSEDED','INVALIDATED'}

class ResearchIntelligence:
    def __init__(self,path):
        self.path=Path(path);self.path.parent.mkdir(parents=True,exist_ok=True)
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('CREATE TABLE IF NOT EXISTS revisions (id TEXT PRIMARY KEY, thesis TEXT, state TEXT, created TEXT, content TEXT, hash TEXT)')
            db.execute('CREATE INDEX IF NOT EXISTS thesis_lookup ON revisions(thesis,created)')
    def add(self,record):
        if record.get('state') not in STATES or not all(record.get(k) for k in ('thesis_id','claim','falsifier','created_at_utc','evidence_ids')):
            raise ContractError('invalid conclusion revision')
        _utc(record['created_at_utc'])
        if len(set(record['evidence_ids']))!=len(record['evidence_ids']):raise ContractError('duplicate evidence references')
        if record.get('parent_id'):
            previous=self.get(record['parent_id'])
            if previous['thesis_id']!=record['thesis_id'] or _utc(previous['created_at_utc'])>=_utc(record['created_at_utc']):
                raise ContractError('invalid revision ancestry')
        content=canonical_json(record).decode(); h=digest(record);rid='revision:'+h
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('INSERT OR IGNORE INTO revisions VALUES(?,?,?,?,?,?)',(rid,record['thesis_id'],record['state'],record['created_at_utc'],content,h))
        return rid
    def get(self,rid):
        with closing(sqlite3.connect(self.path)) as db, db:
            row=db.execute('SELECT content,hash FROM revisions WHERE id=?',(rid,)).fetchone()
        if not row:raise ContractError('revision missing')
        obj=json.loads(row[0])
        if digest(obj)!=row[1] or 'revision:'+row[1]!=rid:raise ContractError('revision integrity failure')
        return {'revision_id':rid,**obj}
    def search(self,term='',thesis_id=None):
        with closing(sqlite3.connect(self.path)) as db, db:
            ids=[r[0] for r in db.execute('SELECT id FROM revisions ORDER BY created,id')]
        return [record for rid in ids if (record:=self.get(rid)) and
                (thesis_id is None or record['thesis_id']==thesis_id) and term.lower() in (record['claim']+' '+record['thesis_id']).lower()]
    def compare(self,old_id,new_id):
        old=self.get(old_id);new=self.get(new_id)
        if old['thesis_id']!=new['thesis_id']:raise ContractError('different theses')
        return {'changes':{key:{'old':old.get(key),'new':new.get(key)} for key in sorted(set(old)|set(new)) if old.get(key)!=new.get(key)},
                'validity':'NOT_REASSESSED'}
