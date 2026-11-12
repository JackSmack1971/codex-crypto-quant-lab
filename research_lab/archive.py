"""Transactional, local, immutable evidence archive with point-in-time queries.

SQLite makes local multi-process writes atomic; this is not an external audit
anchor or evidence that a provider's availability assertions are correct.
"""
from __future__ import annotations
from dataclasses import asdict
from pathlib import Path
import json
import sqlite3
from contextlib import contextmanager
from .core import Observation, ContractError, canonical_json, digest, _utc

SCHEMA = '''
CREATE TABLE IF NOT EXISTS observations (
 id TEXT PRIMARY KEY, instrument_id TEXT NOT NULL, metric TEXT NOT NULL,
 available_at_utc TEXT, content TEXT NOT NULL, sha256 TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS observations_lookup ON observations(instrument_id,metric,available_at_utc);
CREATE TABLE IF NOT EXISTS snapshots (
 snapshot_id TEXT PRIMARY KEY, created_at_utc TEXT NOT NULL,
 ids_json TEXT NOT NULL, sha256 TEXT NOT NULL
);
'''


class EvidenceMissing(ContractError):
    """No observation/snapshot with the requested id exists."""


class ArchiveIntegrityError(ContractError):
    """A row exists but is corrupt: digest mismatch, undecodable content or invalid record."""


class ArchiveDatabaseError(ContractError):
    """The SQLite layer failed (not a database, locked, I/O). Distinct from absence and corruption."""


class EvidenceArchive:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript(SCHEMA)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(str(self.path), timeout=15)
        try:
            db.execute('PRAGMA busy_timeout=15000')
            with db:
                yield db
        finally:
            db.close()

    def ingest(self, observation: Observation) -> str:
        content = canonical_json(asdict(observation)).decode('utf8')
        h = digest(asdict(observation))
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            old = db.execute('SELECT sha256 FROM observations WHERE id=?', (observation.id,)).fetchone()
            if old:
                if old[0] != h:
                    raise ContractError('immutable observation conflict')
            else:
                db.execute('INSERT INTO observations VALUES(?,?,?,?,?,?)',
                           (observation.id, observation.instrument_id, observation.metric,
                            observation.available_at_utc, content, h))
        return h

    def get(self, id: str) -> Observation:
        try:
            with self.connect() as db:
                row = db.execute('SELECT content,sha256 FROM observations WHERE id=?',(id,)).fetchone()
        except sqlite3.Error as exc:
            raise ArchiveDatabaseError(f'archive database error: {type(exc).__name__}') from exc
        if row is None:
            raise EvidenceMissing('observation missing')
        try:
            obj = json.loads(row[0])
            if digest(obj) != row[1]:
                raise ArchiveIntegrityError('archive integrity mismatch')
            return Observation(**obj)
        except ArchiveIntegrityError:
            raise
        except (ValueError, TypeError, ContractError) as exc:  # undecodable JSON, wrong fields, invalid record
            raise ArchiveIntegrityError(f'archive record invalid: {type(exc).__name__}') from exc

    def as_of(self, instrument_id: str, metric: str, cutoff_utc: str) -> list[Observation]:
        _utc(cutoff_utc)
        with self.connect() as db:
            ids = [r[0] for r in db.execute('SELECT id FROM observations WHERE instrument_id=? AND metric=? '
                                                 'AND available_at_utc IS NOT NULL ORDER BY id',
                                                 (instrument_id,metric)).fetchall()]
        result = [self.get(i) for i in ids]
        qualified=[o for o in result if o.status == 'OBSERVED' and o.available_at_utc and o.observed_at_utc and
                   _utc(o.available_at_utc) <= _utc(cutoff_utc) and
                   _utc(o.observed_at_utc) <= _utc(cutoff_utc)]
        return sorted(qualified,key=lambda o:(_utc(o.available_at_utc),o.id))

    def freeze(self, ids: list[str], created_at_utc: str) -> str:
        _utc(created_at_utc)
        if not ids or len(set(ids)) != len(ids):
            raise ContractError('snapshot requires unique evidence ids')
        rows = [asdict(self.get(id)) for id in sorted(ids)]
        snap_id = 'snapshot:'+digest(rows)
        checksum = digest({'ids': sorted(ids), 'contents': rows})
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            old = db.execute('SELECT sha256 FROM snapshots WHERE snapshot_id=?',(snap_id,)).fetchone()
            if old and old[0] != checksum:
                raise ContractError('snapshot collision')
            if not old:
                db.execute('INSERT INTO snapshots VALUES(?,?,?,?)',
                           (snap_id,created_at_utc,json.dumps(sorted(ids)),checksum))
        return snap_id

    def load_snapshot(self, snapshot_id: str) -> list[Observation]:
        with self.connect() as db:
            row=db.execute('SELECT ids_json,sha256 FROM snapshots WHERE snapshot_id=?',(snapshot_id,)).fetchone()
        if row is None:
            raise ContractError('snapshot missing')
        ids=json.loads(row[0]); observations=[self.get(id) for id in ids]
        if digest({'ids':ids,'contents':[asdict(o) for o in observations]}) != row[1] or 'snapshot:'+digest([asdict(o) for o in observations]) != snapshot_id:
            raise ContractError('frozen snapshot checksum mismatch')
        return observations
