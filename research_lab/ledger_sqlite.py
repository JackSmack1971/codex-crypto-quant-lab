"""Transactional single-database local research ledger with chain verification."""
from __future__ import annotations
import json
from pathlib import Path
import sqlite3
from contextlib import contextmanager
from typing import Any
from .core import ContractError, _required, _utc, canonical_json, digest

SCHEMA = '''CREATE TABLE IF NOT EXISTS records (
 sequence INTEGER PRIMARY KEY, run_id TEXT NOT NULL UNIQUE, as_of_utc TEXT NOT NULL,
 previous_hash TEXT, content TEXT NOT NULL, hash TEXT NOT NULL
);'''


class TransactionalResearchLedger:
    def __init__(self, path: str | Path):
        self.path=Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript(SCHEMA)

    @contextmanager
    def connect(self):
        db=sqlite3.connect(str(self.path),timeout=15)
        try:
            db.execute('PRAGMA busy_timeout=15000')
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def _verify(db):
        result=[]; previous=None
        for expected, (sequence, run_id, ts, prev, content, h) in enumerate(
            db.execute('SELECT sequence,run_id,as_of_utc,previous_hash,content,hash FROM records ORDER BY sequence')):
            if sequence != expected or prev != previous:
                raise ContractError('ledger gap or parent mismatch')
            payload=json.loads(content)
            record={'sequence':sequence,'run_id':run_id,'as_of_utc':ts,'previous_hash':prev,'payload':payload}
            if digest(record) != h:
                raise ContractError('ledger tampering detected')
            result.append({**record,'hash':h})
            previous=h
        return result

    def verify(self):
        with self.connect() as db:
            return self._verify(db)

    def append(self, run_id: str, as_of_utc: str, payload: dict[str,Any]):
        _required(run_id,'run_id'); _utc(as_of_utc)
        if not isinstance(payload,dict):
            raise ContractError('payload must be a mapping')
        content=canonical_json(payload).decode('utf8')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            previous=self._verify(db)
            sequence=len(previous)
            record={'sequence':sequence,'run_id':run_id,'as_of_utc':as_of_utc,
                    'previous_hash':previous[-1]['hash'] if previous else None,'payload':payload}
            h=digest(record)
            try:
                db.execute('INSERT INTO records VALUES(?,?,?,?,?,?)',
                           (sequence,run_id,as_of_utc,record['previous_hash'],content,h))
            except sqlite3.IntegrityError as exc:
                raise ContractError('duplicate ledger run') from exc
        return h

    def anchor(self) -> dict[str,Any]:
        """Portable tip receipt. Must be retained outside the writer's control."""
        records=self.verify()
        return {'entry_count':len(records), 'tip_hash':records[-1]['hash'] if records else None}

    def verify_anchor(self, receipt: dict[str,Any]) -> bool:
        if not isinstance(receipt,dict) or not isinstance(receipt.get('entry_count'),int):
            raise ContractError('invalid anchor')
        records=self.verify()
        n=receipt['entry_count']
        if n < 0 or len(records)<n or receipt.get('tip_hash') != (records[n-1]['hash'] if n else None):
            raise ContractError('ledger disagrees with retained anchor')
        return True
