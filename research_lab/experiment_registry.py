"""Durable fixed-candidate experiment registry with enforced preregistration and single-use holdout.

``experiments.freeze_candidate`` / ``evaluate_frozen`` stay the only calculations here. This module adds
*process* controls around them, all in one local SQLite file:

1. ``register`` builds the freeze from the **training partition only** and persists an immutable
   registration (plan, partitions, label-window digest, candidate universe, dataset identity, costs,
   method, freeze digest). Preregistration is therefore a stored fact, never a caller-supplied digest.
2. ``evaluate`` re-verifies the request against that record *before* touching the holdout, then claims
   the holdout atomically (REGISTERED -> EVALUATING), evaluates, and persists the result
   (EVALUATING -> CONSUMED). Registered experiments cannot be edited; a modified experiment needs a
   new ``experiment_id``.
3. A crash after the claim leaves EVALUATING: the holdout is **not** untouched and is never returned to
   REGISTERED. ``reconcile_evaluation`` can complete it (recorded as recovered, not independent) or
   INVALIDATE it.

Limits (not claimed away): a local registry cannot stop anyone from reading or copying the underlying
dataset outside this interface; a correct process, correct arithmetic, credible inference and a real
predictive edge are four different claims and none implies the next.
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, timezone
from math import isinf, isnan
from pathlib import Path
from typing import Any, Callable

from .core import ContractError, _utc, digest
from .experiments import ExperimentPlan, evaluate_frozen, freeze_candidate
from .validation import LabelWindow

SCHEMA_VERSION = 'experiment-registration.v1'
STATUSES = ('REGISTERED', 'EVALUATING', 'CONSUMED', 'INVALIDATED')
LIMITATIONS = ['local registry: cannot prevent inspection or copying of the dataset outside this interface',
               'a valid registration process does not imply scientifically credible inference or a predictive edge',
               'fixed candidate streams only; no fitting, search or parameter optimisation is performed']
SCHEMA = '''
CREATE TABLE IF NOT EXISTS experiments(
 experiment_id TEXT PRIMARY KEY, schema_version TEXT NOT NULL, registration_digest TEXT NOT NULL,
 plan_json TEXT NOT NULL, frozen_json TEXT NOT NULL, freeze_digest TEXT NOT NULL, selected TEXT NOT NULL,
 universe_json TEXT NOT NULL, dataset_id TEXT NOT NULL, dataset_version TEXT NOT NULL, dataset_digest TEXT NOT NULL,
 holdout_key TEXT NOT NULL,
 windows_digest TEXT NOT NULL, boundaries_json TEXT NOT NULL, costs_json TEXT NOT NULL,
 evaluation_method TEXT NOT NULL, limitations_json TEXT NOT NULL, registered_utc TEXT NOT NULL,
 status TEXT NOT NULL, exposure_count INTEGER NOT NULL, recovered INTEGER NOT NULL DEFAULT 0,
 claim_token TEXT, claim_utc TEXT, result_json TEXT, result_digest TEXT, consumed_utc TEXT,
 invalidated_reason TEXT);
CREATE INDEX IF NOT EXISTS experiments_holdout ON experiments(holdout_key);
CREATE TABLE IF NOT EXISTS claims(
 experiment_id TEXT NOT NULL, claim_no INTEGER NOT NULL, token TEXT NOT NULL, utc TEXT NOT NULL,
 kind TEXT NOT NULL, outcome TEXT, PRIMARY KEY(experiment_id, claim_no));
'''


class RegistryError(ContractError):
    """Registry failure with a stable machine code (never classify by message text)."""

    def __init__(self, code: str, message: str = '', *, fields: list[str] | None = None):
        super().__init__(f'{code}: {message}' if message else code)
        self.code, self.message, self.fields = code, message or code, fields or []


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')


def dataset_digest(windows: list[LabelWindow], candidates: dict[str, list[float]]) -> str:
    return digest({'windows': [asdict(w) for w in windows], 'candidates': {k: candidates[k] for k in sorted(candidates)}})


def _validation_indices(plan: ExperimentPlan, windows: list[LabelWindow]) -> list[int]:
    """Same selection ``evaluate_frozen`` uses; an experiment with none can never be evaluated."""
    return [i for i in range(plan.train_end, max(plan.train_end, plan.holdout_start - plan.embargo))
            if _utc(windows[i].end_utc) < _utc(windows[plan.holdout_start].start_utc)]


def holdout_key(windows: list[LabelWindow], plan: ExperimentPlan, dataset_id: str, dataset_version: str) -> str:
    """Identity of the *holdout itself* (its label windows on a dataset), independent of experiment id,
    candidate set and training split: two experiments over the same holdout share one exposure budget."""
    return digest({'holdout_windows': [asdict(w) for w in windows[plan.holdout_start:]],
                   'dataset_id': dataset_id, 'dataset_version': dataset_version})


def _check_types(plan, windows, candidates) -> None:
    if not isinstance(plan, ExperimentPlan) or not isinstance(windows, list) or not all(isinstance(w, LabelWindow) for w in windows):
        raise RegistryError('EXPERIMENT_INVALID', 'plan must be ExperimentPlan and windows a list of LabelWindow')
    if not isinstance(candidates, dict) or not candidates or any(
            not isinstance(k, str) or not isinstance(v, list) for k, v in candidates.items()):
        raise RegistryError('EXPERIMENT_INVALID', 'candidates must be a non-empty mapping of name -> list of returns')


def _check_streams(candidates: dict, n: int) -> None:
    """Every value finite and every stream aligned, checked *before* any holdout claim so a deterministic
    input error can never burn the holdout."""
    for name, values in candidates.items():
        if len(values) != n or any(type(x) not in (int, float) or isnan(x) or isinf(x) for x in values):
            raise RegistryError('EXPERIMENT_INVALID', f'candidate {name!r} is unaligned or has non-finite/non-numeric values')


class ExperimentRegistry:
    def __init__(self, path: str | Path, *, clock: Callable[[], str] | None = None,
                 fail_at: Callable[[str, str], None] | None = None):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.clock, self._fail = clock or _now, fail_at
        with self._db() as db:
            db.executescript(SCHEMA)

    @contextmanager
    def _db(self, *, immediate: bool = False):
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute('PRAGMA busy_timeout=30000')
            if immediate:
                conn.execute('BEGIN IMMEDIATE')
            try:
                yield conn
            except BaseException:
                if immediate and conn.in_transaction:
                    conn.execute('ROLLBACK')
                raise
            else:
                if immediate and conn.in_transaction:
                    conn.execute('COMMIT')
        finally:
            conn.close()

    def _point(self, name: str, experiment_id: str) -> None:
        if self._fail is not None:
            self._fail(name, experiment_id)

    # -- registration -----------------------------------------------------------
    def register(self, plan: ExperimentPlan, windows: list[LabelWindow], training_candidates: dict[str, list[float]],
                 *, dataset_id: str, dataset_version: str, evaluation_method: str, dataset_digest_hex: str,
                 costs: dict | None = None) -> dict:
        """Persist an immutable preregistration built from the training partition only.

        ``training_candidates`` must hold exactly ``plan.train_end`` values per candidate: holdout and
        validation returns are not accepted here, so selection cannot depend on them.
        ``dataset_digest_hex`` (see :func:`dataset_digest`) is mandatory: it binds the full dataset,
        holdout values included, so a different holdout cannot be evaluated under this registration."""
        if not isinstance(plan, ExperimentPlan) or not all(isinstance(w, LabelWindow) for w in windows):
            raise RegistryError('EXPERIMENT_INVALID', 'plan must be ExperimentPlan and windows LabelWindow')
        for label, value in (('dataset_id', dataset_id), ('dataset_version', dataset_version), ('evaluation_method', evaluation_method)):
            if not isinstance(value, str) or not value.strip():
                raise RegistryError('EXPERIMENT_INVALID', f'{label} required')
        if not isinstance(dataset_digest_hex, str) or len(dataset_digest_hex) != 64 or any(c not in '0123456789abcdef' for c in dataset_digest_hex):
            raise RegistryError('EXPERIMENT_INVALID', 'dataset_digest_hex must be a lowercase SHA-256 of the full dataset')
        n = len(windows)
        if not isinstance(training_candidates, dict) or not training_candidates or any(
                not isinstance(v, list) or len(v) != plan.train_end for v in training_candidates.values()):
            raise RegistryError('EXPERIMENT_INVALID', 'training_candidates must contain exactly the training partition (train_end values)')
        padded = {k: list(v) + [0.0] * (n - plan.train_end) for k, v in training_candidates.items()}  # padding is never read: selection uses train indices only
        try:
            freeze = freeze_candidate(plan, windows, padded)
        except ContractError as exc:
            raise RegistryError('EXPERIMENT_INVALID', str(exc)) from exc
        if not _validation_indices(plan, windows):
            raise RegistryError('EXPERIMENT_INVALID', 'no valid validation periods: this experiment could never be evaluated')
        key = holdout_key(windows, plan, dataset_id, dataset_version)
        starts_ends = [(_utc(w.start_utc), _utc(w.end_utc)) for w in windows]
        holdout = range(plan.holdout_start, n)
        for i in freeze['frozen']['train_indices']:
            if any(starts_ends[i][0] <= starts_ends[j][1] and starts_ends[j][0] <= starts_ends[i][1] for j in holdout):
                raise RegistryError('EXPERIMENT_INVALID', f'training label {i} overlaps a holdout label interval')
        boundaries = {'train_end': plan.train_end, 'validation_start': plan.train_end, 'holdout_start': plan.holdout_start,
                      'embargo': plan.embargo, 'n_periods': n}
        record = {'schema': SCHEMA_VERSION, 'plan': asdict(plan), 'freeze_digest': freeze['freeze_digest'],
                  'windows_digest': freeze['frozen']['windows_digest'], 'universe': sorted(training_candidates),
                  'dataset': {'id': dataset_id, 'version': dataset_version, 'digest': dataset_digest_hex},
                  'boundaries': boundaries, 'costs': costs or {}, 'evaluation_method': evaluation_method}
        registration_digest = digest(record)
        with self._db(immediate=True) as db:
            row = db.execute('SELECT registration_digest, status FROM experiments WHERE experiment_id=?', (plan.experiment_id,)).fetchone()
            if row is None and self._exposed_elsewhere(db, key, plan.experiment_id):
                raise RegistryError('HOLDOUT_ALREADY_EXPOSED', 'another experiment already exposed this holdout; a new id does not make it untouched')
            if row is not None:
                if row['registration_digest'] != registration_digest:
                    raise RegistryError('EXPERIMENT_REGISTRATION_CONFLICT',
                                        'experiment id is registered with different content; a modified experiment needs a new id')
                return {'experiment_id': plan.experiment_id, 'registration_digest': registration_digest, 'created': False,
                        'freeze_digest': freeze['freeze_digest'], 'status': row['status']}
            db.execute('INSERT INTO experiments(experiment_id,schema_version,registration_digest,plan_json,frozen_json,freeze_digest,'
                       'selected,universe_json,dataset_id,dataset_version,dataset_digest,holdout_key,windows_digest,boundaries_json,costs_json,'
                       'evaluation_method,limitations_json,registered_utc,status,exposure_count) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                       (plan.experiment_id, SCHEMA_VERSION, registration_digest, json.dumps(asdict(plan), sort_keys=True),
                        json.dumps(freeze, sort_keys=True), freeze['freeze_digest'], freeze['frozen']['selected'],
                        json.dumps(record['universe']), dataset_id, dataset_version, dataset_digest_hex, key,
                        freeze['frozen']['windows_digest'], json.dumps(boundaries, sort_keys=True), json.dumps(record['costs'], sort_keys=True),
                        evaluation_method, json.dumps(LIMITATIONS), self.clock(), 'REGISTERED', 0))
        return {'experiment_id': plan.experiment_id, 'registration_digest': registration_digest, 'created': True,
                'freeze_digest': freeze['freeze_digest'], 'status': 'REGISTERED'}

    @staticmethod
    def _exposed_elsewhere(db, key: str, experiment_id: str) -> bool:
        return db.execute("SELECT 1 FROM experiments WHERE holdout_key=? AND experiment_id!=? AND (status!='REGISTERED' OR exposure_count>0)",
                          (key, experiment_id)).fetchone() is not None

    # -- reads ------------------------------------------------------------------
    def _row(self, experiment_id: str, db=None) -> sqlite3.Row:
        def fetch(conn):
            return conn.execute('SELECT * FROM experiments WHERE experiment_id=?', (experiment_id,)).fetchone()
        if db is not None:
            row = fetch(db)
        else:
            with self._db() as conn:
                row = fetch(conn)
        if row is None:
            raise RegistryError('EXPERIMENT_UNREGISTERED', f'no registration for {experiment_id!r}; the holdout is not accessible')
        return row

    def summary(self, experiment_id: str) -> dict:
        try:
            row = self._row(experiment_id)
        except RegistryError:
            return {'experiment_id': experiment_id, 'status': 'UNREGISTERED'}
        return {'experiment_id': experiment_id, 'status': row['status'], 'registration_digest': row['registration_digest'],
                'freeze_digest': row['freeze_digest'], 'holdout_exposure_count': row['exposure_count'],
                'holdout_untouched': row['status'] == 'REGISTERED' and row['exposure_count'] == 0,
                'independent_evaluation': row['status'] == 'CONSUMED' and row['exposure_count'] == 1 and not row['recovered'],
                'recovered_after_interruption': bool(row['recovered']), 'result_digest': row['result_digest'],
                'invalidated_reason': row['invalidated_reason'], 'limitations': json.loads(row['limitations_json'])}

    def get(self, experiment_id: str) -> dict:
        row = self._row(experiment_id)
        out = {k: row[k] for k in row.keys() if not k.endswith('_json')}
        for key in ('plan', 'boundaries', 'costs', 'universe'):
            out[key] = json.loads(row[f'{key}_json'])
        return out

    # -- evaluation ----------------------------------------------------------------
    def _verify_request(self, row, plan, windows, candidates, dataset_id, dataset_version) -> dict:
        """Pre-claim verification against the stored registration. Selection is recomputed from the training
        partition only (``v[:train_end]``); no holdout statistic is computed here."""
        _check_types(plan, windows, candidates)
        mismatches = []
        if digest(asdict(plan)) != digest(json.loads(row['plan_json'])):
            mismatches.append('plan')
        if digest([asdict(w) for w in windows]) != row['windows_digest']:
            mismatches.append('label_windows')
        if (dataset_id, dataset_version) != (row['dataset_id'], row['dataset_version']):
            mismatches.append('dataset_identity')
        if sorted(candidates) != json.loads(row['universe_json']):
            mismatches.append('candidate_universe')
        if mismatches:
            raise RegistryError('EXPERIMENT_MODIFIED', 'request differs from the registration: ' + ', '.join(mismatches), fields=mismatches)
        n = len(windows)
        _check_streams(candidates, n)
        training_view = {k: v[:plan.train_end] + [0.0] * (n - plan.train_end) for k, v in candidates.items()}
        try:
            refrozen = freeze_candidate(plan, windows, training_view)
        except ContractError as exc:
            raise RegistryError('EXPERIMENT_MODIFIED', str(exc), fields=['training_partition']) from exc
        if refrozen['freeze_digest'] != row['freeze_digest']:
            raise RegistryError('EXPERIMENT_MODIFIED', 'training partition no longer reproduces the registered freeze', fields=['training_partition'])
        if dataset_digest(windows, candidates) != row['dataset_digest']:
            raise RegistryError('EXPERIMENT_MODIFIED', 'dataset content differs from the registered digest', fields=['dataset_digest'])
        return json.loads(row['frozen_json'])

    def _stored_result(self, row) -> dict:
        result = json.loads(row['result_json'])
        if digest(result) != row['result_digest']:
            raise RegistryError('EXPERIMENT_RESULT_TAMPERED', 'stored evaluation result does not match its recorded digest')
        return result

    def evaluate(self, experiment_id: str, plan: ExperimentPlan, windows: list[LabelWindow],
                 candidates: dict[str, list[float]], *, dataset_id: str, dataset_version: str) -> dict:
        row = self._row(experiment_id)
        if row['status'] == 'CONSUMED':  # a repeat retrieves the stored result; it is not a new evaluation
            return self._outcome(row, self._stored_result(row), reused=True)
        if row['status'] == 'INVALIDATED':
            raise RegistryError('EXPERIMENT_INVALIDATED', row['invalidated_reason'] or 'invalidated')
        if row['status'] == 'EVALUATING':
            raise RegistryError('HOLDOUT_CLAIM_UNCERTAIN', 'a previous evaluation claimed the holdout and did not record a result; '
                                'the holdout is not untouched. Use reconcile_evaluation')
        frozen = self._verify_request(row, plan, windows, candidates, dataset_id, dataset_version)
        token, now = uuid.uuid4().hex, self.clock()
        with self._db(immediate=True) as db:  # atomic claim
            current = db.execute('SELECT status,exposure_count FROM experiments WHERE experiment_id=?', (experiment_id,)).fetchone()
            if current['status'] != 'REGISTERED':
                raise RegistryError('HOLDOUT_CLAIM_LOST', f'holdout is {current["status"]}; another evaluator claimed it')
            if self._exposed_elsewhere(db, row['holdout_key'], experiment_id):
                raise RegistryError('HOLDOUT_ALREADY_EXPOSED', 'a different experiment already exposed this holdout')
            count = current['exposure_count'] + 1
            db.execute('UPDATE experiments SET status=?,exposure_count=?,claim_token=?,claim_utc=? WHERE experiment_id=?',
                       ('EVALUATING', count, token, now, experiment_id))
            db.execute('INSERT INTO claims VALUES(?,?,?,?,?,NULL)', (experiment_id, count, token, now, 'EVALUATE'))
        self._point('after_claim', experiment_id)
        result = evaluate_frozen(frozen, plan, windows, candidates, registered_digest=row['freeze_digest'])
        self._point('before_result_commit', experiment_id)
        return self._commit(experiment_id, token, result, recovered=False, kind='EVALUATE')

    def _commit(self, experiment_id, token, result, *, recovered, kind) -> dict:
        now = self.clock()
        with self._db(immediate=True) as db:
            current = db.execute('SELECT status,claim_token FROM experiments WHERE experiment_id=?', (experiment_id,)).fetchone()
            if current['status'] != 'EVALUATING' or current['claim_token'] != token:
                raise RegistryError('HOLDOUT_CLAIM_LOST', 'claim superseded before the result was recorded')
            db.execute('UPDATE experiments SET status=?,result_json=?,result_digest=?,consumed_utc=?,recovered=?,'
                       'exposure_count=exposure_count+? WHERE experiment_id=?',
                       ('CONSUMED', json.dumps(result, sort_keys=True), digest(result), now, int(recovered), int(recovered), experiment_id))
            db.execute('UPDATE claims SET outcome=? WHERE experiment_id=? AND token=?', ('CONSUMED', experiment_id, token))
        return self._outcome(self._row(experiment_id), result, reused=False)

    @staticmethod
    def _outcome(row, result, *, reused: bool) -> dict:
        independent = row['exposure_count'] == 1 and not row['recovered'] and not reused
        return {'experiment_id': row['experiment_id'], 'status': row['status'], 'result': result, 'reused': reused,
                'independent_evaluation': independent, 'holdout_exposure_count': row['exposure_count'],
                'recovered_after_interruption': bool(row['recovered']), 'result_digest': row['result_digest'],
                'limitations': json.loads(row['limitations_json'])}

    def reconcile_evaluation(self, experiment_id: str, decision: str, *, plan: ExperimentPlan | None = None,
                             windows: list[LabelWindow] | None = None, candidates: dict[str, list[float]] | None = None,
                             dataset_id: str | None = None, dataset_version: str | None = None, reason: str | None = None) -> dict:
        """Resolve an EVALUATING (uncertain) holdout. ``COMPLETE`` records a deterministic result flagged as
        recovered and not independent; ``INVALIDATE`` retires the experiment. Neither restores 'untouched'."""
        if decision not in ('COMPLETE', 'INVALIDATE'):
            raise RegistryError('EXPERIMENT_INVALID', "decision must be 'COMPLETE' or 'INVALIDATE'")
        row = self._row(experiment_id)
        if row['status'] != 'EVALUATING':
            raise RegistryError('EXPERIMENT_NOT_UNCERTAIN', f'nothing to reconcile: status is {row["status"]}')
        if decision == 'INVALIDATE':
            if not reason or not reason.strip():
                raise RegistryError('EXPERIMENT_INVALID', 'invalidation requires a reason')
            with self._db(immediate=True) as db:
                db.execute('UPDATE experiments SET status=?,invalidated_reason=? WHERE experiment_id=? AND status=?',
                           ('INVALIDATED', reason, experiment_id, 'EVALUATING'))
            return self.summary(experiment_id)
        if None in (plan, windows, candidates, dataset_id, dataset_version):
            raise RegistryError('EXPERIMENT_INVALID', 'COMPLETE needs the original plan, windows, candidates and dataset identity')
        frozen = self._verify_request(row, plan, windows, candidates, dataset_id, dataset_version)
        token, now = uuid.uuid4().hex, self.clock()
        with self._db(immediate=True) as db:  # re-claim: this evaluation is another holdout exposure
            current = db.execute('SELECT status,exposure_count FROM experiments WHERE experiment_id=?', (experiment_id,)).fetchone()
            if current['status'] != 'EVALUATING':
                raise RegistryError('HOLDOUT_CLAIM_LOST', 'reconciliation raced another resolution')
            count = current['exposure_count'] + 1   # provisional claim number; exposure is counted only if a result is produced
            db.execute('UPDATE experiments SET claim_token=?,claim_utc=? WHERE experiment_id=?', (token, now, experiment_id))
            db.execute('INSERT INTO claims VALUES(?,?,?,?,?,NULL)', (experiment_id, count, token, now, 'RECONCILE'))
        result = evaluate_frozen(frozen, plan, windows, candidates, registered_digest=row['freeze_digest'])
        return self._commit(experiment_id, token, result, recovered=True, kind='RECONCILE')
