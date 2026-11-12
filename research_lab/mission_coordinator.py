"""Deterministic, local, restart-safe research mission coordinator.

This is an execution *engine*, not an LLM runtime: it advances a fixed seven-stage plan
(PLANNED -> ACQUIRING -> RECONCILING -> ANALYZING -> VERIFYING -> RENDERING -> FINALIZING) by
calling a fixed allowlist of deterministic local operations. It performs no network access,
no provider calls, no trading/wallet/signing/publishing action and spawns no agents. A mission
state is a record, never an authorisation.

Two state domains are kept apart:
* job lifecycle (``research_lab.jobs.JobStore``): NEW/RUNNING/INTERRUPTED/RECONCILING/COMPLETED/
  FAILED/BLOCKED/CANCELLED. COMPLETED means the *acceptance condition* was reached, never that a
  hypothesis was validated.
* mission stage status (this module): PENDING/RUNNING/SUCCEEDED/FAILED/BLOCKED/SKIPPED, per stage.
The scientific ``research_state`` (NOT_EVALUABLE / INSUFFICIENT_EVIDENCE / EVALUABLE) is derived
from evidence and is never ``VALIDATED`` here.

Concurrency: a stage is claimed in a ``BEGIN IMMEDIATE`` transaction that bumps a *fence*; no
transaction spans operation execution. State and artifact commits re-check the fence, so an
executor that lost its claim (e.g. after ``reconcile_mission`` took over) cannot finalize.
Leases are deliberately not used: expiry alone never authorises a second executor.
"""
from __future__ import annotations

import html
import json
import os
import sqlite3
import tempfile
import uuid
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, timezone
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from typing import Any, Callable

from . import mission_integrity as integrity
from .archive import ArchiveDatabaseError, ArchiveIntegrityError, EvidenceArchive, EvidenceMissing
from .completion import verify as verify_bundle
from .core import ContractError, Observation, ResearchSignal, digest, evaluate_hypothesis, _utc
from .evals import evaluate_task
from .jobs import JobStore
from .mission_spec import (KNOWN_OPERATIONS, REVIEWERS, SCHEMA, STAGE_OPERATIONS, STAGES, MissionError,
                           resolve_under, spec_digest, validate_spec)

MAX_RETRYABLE_ATTEMPTS = 3
# Verifier diagnostics that merely restate declared remaining work (reviews not PASS, no eligible evidence).
EXPECTED_WHEN_INCOMPLETE = {'COMMITTEE_REVIEW_NOT_PASSED', 'EVIDENCE_REFERENCES_INVALID'}
JOB_TERMINAL = {'COMPLETED', 'FAILED', 'BLOCKED', 'CANCELLED'}
STAGE_DONE = {'SUCCEEDED', 'SKIPPED'}
STAGE_OUTPUTS = {
    'PLANNED': ('plan.json',),
    'ACQUIRING': ('acquisition.json',),
    'RECONCILING': ('reconciliation.json',),
    'ANALYZING': ('analysis.json',),
    'VERIFYING': ('verification.json',),
    'RENDERING': ('report.md', 'report.html'),
    'FINALIZING': ('mission_baseline.json', 'provider_attempts.json', 'evidence.json', 'metrics.json',
                   'hypothesis_assessment.json', 'evaluation_result.json', 'thesis_revision.json',
                   'committee_verification.json', 'report.md', 'report.html', 'mission.json',
                   'final_verification.json'),
}
FAIL_POINTS = ('before_admission', 'after_admission', 'after_artifact_write', 'after_state_commit',
               'during_render', 'during_final_verify')
SCHEMA_STORE = '''
CREATE TABLE IF NOT EXISTS missions(
 mission_id TEXT PRIMARY KEY, spec_digest TEXT NOT NULL, spec_json TEXT NOT NULL,
 output_dir TEXT NOT NULL UNIQUE, created_utc TEXT NOT NULL, final_json TEXT);
CREATE TABLE IF NOT EXISTS stages(
 mission_id TEXT NOT NULL, stage_id TEXT NOT NULL, status TEXT NOT NULL, fence INTEGER NOT NULL,
 revision INTEGER NOT NULL, operation_id TEXT, operation_version TEXT, input_digest TEXT, op_key TEXT,
 output_json TEXT, output_digest TEXT, attempts INTEGER NOT NULL, failure_code TEXT, failure_detail TEXT,
 retryable INTEGER, started_utc TEXT, completed_utc TEXT, PRIMARY KEY(mission_id, stage_id));
CREATE TABLE IF NOT EXISTS attempts(
 mission_id TEXT NOT NULL, stage_id TEXT NOT NULL, attempt INTEGER NOT NULL, fence INTEGER NOT NULL,
 op_key TEXT NOT NULL, started_utc TEXT NOT NULL, ended_utc TEXT, outcome TEXT, code TEXT,
 PRIMARY KEY(mission_id, stage_id, attempt));
CREATE TABLE IF NOT EXISTS reconciliations(
 id INTEGER PRIMARY KEY AUTOINCREMENT, mission_id TEXT NOT NULL, stage_id TEXT, utc TEXT NOT NULL,
 decision TEXT NOT NULL, classification TEXT NOT NULL, detail_json TEXT NOT NULL);
'''


class OperationFailure(Exception):
    """Typed, classified operation failure. ``blocked`` => required external evidence/handoff is not
    (yet) available; the stage is BLOCKED and may be re-attempted once it appears."""

    def __init__(self, code: str, message: str = '', *, retryable: bool = False, blocked: bool = False):
        super().__init__(code)
        self.code, self.message, self.retryable, self.blocked = code, message or code, retryable, blocked


class StageFenced(Exception):
    """The executor no longer owns the stage (a newer fence exists) or the job is no longer RUNNING."""

    def __init__(self, code: str = 'STAGE_FENCED', message: str = 'stage claim superseded'):
        super().__init__(code)
        self.code, self.message = code, message


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')


def _j(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False, allow_nan=False) + '\n').encode('utf-8')


def _sha(data: bytes) -> str:
    return sha256(data).hexdigest()


def write_immutable(path: Path, data: bytes) -> str:
    """Create ``path`` atomically and never overwrite it. If it exists its bytes must be identical
    (an existing file is evidence, never proof of success, so it is digest-checked)."""
    want = _sha(data)
    path.parent.mkdir(parents=True, exist_ok=True)
    if os.path.lexists(path):
        if path.is_symlink() or _sha(path.read_bytes()) != want:
            raise MissionError('STAGE_OUTPUT_DIGEST_MISMATCH', f'existing artifact differs: {path.name}')
        return want
    tmp = path.with_name(f'.{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp')
    try:
        with open(tmp, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(tmp, path)  # atomic create-if-absent
        except FileExistsError:
            if _sha(path.read_bytes()) != want:
                raise MissionError('STAGE_OUTPUT_DIGEST_MISMATCH', f'concurrent artifact differs: {path.name}')
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass
    return want


class Ctx:
    """Read-only view handed to operations: validated spec, pinned inputs and verified upstream outputs."""

    def __init__(self, coordinator: 'MissionCoordinator', spec: dict, stage_id: str):
        self.coordinator, self.spec, self.stage_id = coordinator, spec, stage_id
        self.mission_id = spec['mission_id']
        self.out = coordinator.output_root(spec)
        self.as_of = spec['as_of_utc']

    def fail_at(self, point: str) -> None:
        self.coordinator._fail_at(point, self.mission_id, self.stage_id)

    def read_pinned(self, ref: dict) -> bytes:
        try:
            path = resolve_under(self.coordinator.workspace, ref['path'])
        except MissionError as exc:
            raise OperationFailure('STAGE_PRECONDITION_FAILED', exc.message) from exc
        try:
            data = path.read_bytes()
        except FileNotFoundError as exc:
            raise OperationFailure('REQUIRED_EVIDENCE_UNAVAILABLE', f'declared input not present: {ref["path"]}',
                                   blocked=True) from exc
        except OSError as exc:
            raise OperationFailure('OPERATION_ERROR', f'{type(exc).__name__} reading input', retryable=True) from exc
        if _sha(data) != ref['sha256']:
            raise OperationFailure('STAGE_PRECONDITION_FAILED', f'input digest differs from the pinned digest: {ref["path"]}')
        return data

    def pinned_json(self, ref: dict) -> Any:
        try:
            return json.loads(self.read_pinned(ref).decode('utf-8'))
        except (ValueError, UnicodeError) as exc:
            raise OperationFailure('STAGE_PRECONDITION_FAILED', f'input is not valid JSON: {ref["path"]}') from exc

    def up(self, stage: str, name: str) -> Any:
        return json.loads(self.coordinator._read_output(self.mission_id, stage, name).decode('utf-8'))

    @property
    def archive_path(self) -> Path:
        return resolve_under(self.coordinator.workspace, f"{self.spec['output_dir']}/evidence.sqlite")


# --------------------------------------------------------------------------- operations
def _archived(archive: EvidenceArchive, observation_id: str) -> Observation:
    """Read one archived observation; archive failures are classified by exception *type*."""
    try:
        return archive.get(observation_id)
    except (EvidenceMissing, ArchiveIntegrityError, ArchiveDatabaseError) as exc:
        raise OperationFailure('VERIFICATION_FAILED', f'archived evidence {observation_id!r} unusable: {type(exc).__name__}') from exc


def _obs_from(archive: EvidenceArchive, ids: list[str]) -> list[Observation]:
    return [_archived(archive, i) for i in ids]


def op_plan(ctx: Ctx) -> dict[str, bytes]:
    spec = ctx.spec
    plan = {'schema': 'mission-plan.v1', 'mission_id': ctx.mission_id, 'spec_digest': spec_digest(spec),
            'as_of_utc': ctx.as_of,
            'stages': [{'stage_id': s, 'operation_id': STAGE_OPERATIONS[s][0], 'operation_version': STAGE_OPERATIONS[s][1],
                        'permitted': STAGE_OPERATIONS[s][0] in spec['permitted_operations']} for s in STAGES]}
    unpermitted = [s['operation_id'] for s in plan['stages'] if not s['permitted']]
    if unpermitted:
        raise OperationFailure('STAGE_PRECONDITION_FAILED', 'operations not permitted by the spec: ' + ', '.join(unpermitted))
    return {'plan.json': _j(plan)}


def op_acquire(ctx: Ctx) -> dict[str, bytes]:
    spec, refs = ctx.spec, ctx.spec['inputs']
    payload = ctx.pinned_json(refs['research_run'])
    if not isinstance(payload, dict) or payload.get('schema') != 'research-run-input.v1':
        raise OperationFailure('STAGE_PRECONDITION_FAILED', 'research_run input must use schema research-run-input.v1')
    try:
        observations = [Observation(**row) for row in payload.get('observations', [])]
    except (TypeError, ContractError) as exc:
        raise OperationFailure('STAGE_PRECONDITION_FAILED', f'invalid observation record: {type(exc).__name__}') from exc
    if len({o.id for o in observations}) != len(observations):
        raise OperationFailure('STAGE_PRECONDITION_FAILED', 'duplicate observation ids in input')
    supplied: dict[str, dict] = {}
    if 'provider_attempts' in refs:
        record = ctx.pinned_json(refs['provider_attempts'])
        declared = {o['operation_id'] for o in spec['provider_operations']}
        rows = record.get('attempts') if isinstance(record, dict) else None
        if not isinstance(rows, list):
            raise OperationFailure('STAGE_PRECONDITION_FAILED', 'provider_attempts input needs an attempts list')
        for row in rows:
            if not (isinstance(row, dict) and row.get('operation_id') in declared
                    and row.get('status') in ('SUCCEEDED', 'FAILED', 'UNATTEMPTED')
                    and row['operation_id'] not in supplied):
                raise OperationFailure('STAGE_PRECONDITION_FAILED', 'provider attempt record is malformed, undeclared or duplicated')
            supplied[row['operation_id']] = row
    ingested = {o.id for o in observations}
    attempts = []
    for item in spec['provider_operations']:
        row = supplied.get(item['operation_id'])
        if row is None:
            attempts.append({'operation_id': item['operation_id'], 'status': 'UNATTEMPTED', 'required': item['required'],
                             'reason': 'no runtime attempt record supplied; the coordinator performs no provider calls',
                             'source': 'coordinator_default'})
        else:
            entry = {'operation_id': item['operation_id'], 'status': row['status'], 'required': item['required'],
                     'source': 'caller_supplied_record_unauthenticated'}
            for key in ('reason', 'error', 'completed_at_utc'):
                if key in row:
                    entry[key] = row[key]
            if entry['status'] == 'SUCCEEDED':
                linked = row.get('observation_ids')
                if isinstance(linked, list) and linked and all(isinstance(x, str) and x in ingested for x in linked):
                    entry['observation_ids'] = sorted(set(linked))
                else:  # a bare assertion of success is not accepted: it must cite archived evidence it produced
                    entry.update(status='UNATTEMPTED', claimed_status='SUCCEEDED',
                                 reason='SUCCEEDED claim lacks observation_ids linking it to ingested evidence; not accepted')
            if entry['status'] != 'SUCCEEDED' and not (entry.get('reason') or entry.get('error')):
                entry['reason'] = 'outcome recorded without explanation'
            attempts.append(entry)
    attempts.append({'operation_id': 'local.input_ingest', 'status': 'SUCCEEDED', 'required': False,
                     'source': 'coordinator_observed_local_operation'})
    archive = EvidenceArchive(ctx.archive_path)
    for observation in observations:
        try:
            archive.ingest(observation)  # idempotent for identical content
        except ContractError as exc:
            raise OperationFailure('OPERATION_IDENTITY_CONFLICT', 'archived observation conflicts with input') from exc
    ids = sorted(o.id for o in observations)
    for observation in observations:
        if digest(asdict(_archived(archive, observation.id))) != digest(asdict(observation)):
            raise OperationFailure('OPERATION_IDENTITY_CONFLICT', 'archive content differs from input')
    snapshot = archive.freeze(ids, ctx.as_of) if ids else None
    out = {'schema': 'mission-acquisition.v1', 'network_calls_by_coordinator': 0,
           'input_digests': {'research_run': refs['research_run']['sha256']},
           'observation_ids': ids, 'observation_digests': {o.id: digest(asdict(o)) for o in observations},
           'evidence_snapshot_id': snapshot, 'attempts': attempts}
    return {'acquisition.json': _j(out)}


def _summary(o: Observation) -> dict:
    return {'id': o.id, 'instrument_id': o.instrument_id, 'metric': o.metric, 'value': o.value, 'unit': o.unit,
            'observed_at_utc': o.observed_at_utc, 'available_at_utc': o.available_at_utc,
            'retrieved_at_utc': o.retrieved_at_utc, 'source_uri': o.source_uri, 'status': o.status}


def op_reconcile(ctx: Ctx) -> dict[str, bytes]:
    spec = ctx.spec
    acquisition = ctx.up('ACQUIRING', 'acquisition.json')
    observations = _obs_from(EvidenceArchive(ctx.archive_path), acquisition['observation_ids'])
    cutoff, tolerance = _utc(ctx.as_of), Decimal(spec.get('conflict_tolerance', '0'))
    excluded: dict[str, str] = {}
    candidates = []
    for o in observations:
        if o.status != 'OBSERVED':
            excluded[o.id] = 'NOT_OBSERVED'
        elif o.available_at_utc is None:
            excluded[o.id] = 'AVAILABILITY_UNKNOWN'
        elif o.instrument_id not in spec['instruments']:
            excluded[o.id] = 'OUT_OF_SCOPE_INSTRUMENT'
        elif any(_utc(t) > cutoff for t in (o.observed_at_utc, o.available_at_utc, o.retrieved_at_utc)):
            excluded[o.id] = 'FUTURE_DATED'
        elif (cutoff - _utc(o.observed_at_utc)).total_seconds() > spec['max_age_seconds']:
            excluded[o.id] = 'STALE'
        else:
            candidates.append(o)
    required = {(r['instrument_id'], r['metric']): r['unit'] for r in spec['required_metrics']}
    groups: dict[tuple, list[Observation]] = {}
    for o in candidates:
        groups.setdefault((o.instrument_id, o.metric), []).append(o)
    conflicts = []
    for key, members in sorted(groups.items()):
        if len({m.unit for m in members}) > 1:
            for m in members:
                excluded[m.id] = 'INCOMPATIBLE_UNIT'
            conflicts.append({'instrument_id': key[0], 'metric': key[1], 'kind': 'INCOMPATIBLE_UNIT',
                              'observation_ids': sorted(m.id for m in members)})
            continue
        if key in required:
            for m in members:
                if m.unit != required[key]:
                    excluded[m.id] = 'UNIT_MISMATCH_WITH_REQUIREMENT'
        by_time: dict[str, list[Observation]] = {}
        for m in members:
            if m.id not in excluded:
                by_time.setdefault(m.observed_at_utc, []).append(m)
        for when, same in sorted(by_time.items()):
            values = [Decimal(m.value) for m in same]
            if max(values) - min(values) > tolerance:
                for m in same:
                    excluded[m.id] = 'CONTRADICTORY'
                conflicts.append({'instrument_id': key[0], 'metric': key[1], 'kind': 'CONTRADICTORY',
                                  'observed_at_utc': when, 'observation_ids': sorted(m.id for m in same)})
    eligible = sorted(o.id for o in candidates if o.id not in excluded)
    by_id = {o.id: o for o in observations}
    coverage = {f'{i}|{m}': sorted(o for o in eligible if (by_id[o].instrument_id, by_id[o].metric) == (i, m))
                for (i, m) in sorted(required)}
    missing = [{'instrument_id': i, 'metric': m} for (i, m) in sorted(required) if not coverage[f'{i}|{m}']]
    out = {'schema': 'mission-reconciliation.v1', 'eligible_observation_ids': eligible,
           'excluded': [{'id': k, 'code': v} for k, v in sorted(excluded.items())], 'conflicts': conflicts,
           'coverage': coverage, 'missing_required': missing,
           'state': 'RECONCILED' if not missing and not conflicts else 'INSUFFICIENT',
           'observations': {o.id: _summary(o) for o in observations}}
    return {'reconciliation.json': _j(out)}


def op_analyze(ctx: Ctx) -> dict[str, bytes]:
    spec = ctx.spec
    reconciliation = ctx.up('RECONCILING', 'reconciliation.json')
    acquisition = ctx.up('ACQUIRING', 'acquisition.json')
    eligible = reconciliation['eligible_observation_ids']
    observations = _obs_from(EvidenceArchive(ctx.archive_path), eligible)
    payload = ctx.pinned_json(spec['inputs']['research_run'])
    try:
        signals = [ResearchSignal(**{**row, 'observation_ids': tuple(row['observation_ids'])})
                   for row in payload.get('signals', [])]
    except (TypeError, KeyError, ContractError) as exc:
        raise OperationFailure('STAGE_PRECONDITION_FAILED', f'invalid signal record: {type(exc).__name__}') from exc
    hypotheses = []
    known = {sig.id for sig in signals}
    for h in spec['hypotheses']:
        declared = set(h['supporting_signal_ids']) | set(h['contradicting_signal_ids'])
        unresolved = sorted(declared - known)  # a declared signal absent from the input is missing evidence, not a crash
        try:
            assessment = evaluate_hypothesis(h['id'], ctx.as_of, signals, observations, set(h['supporting_signal_ids']) & known,
                                             set(h['contradicting_signal_ids']) & known, set(h['required_metrics']))
        except ContractError as exc:
            raise OperationFailure('STAGE_PRECONDITION_FAILED', f'hypothesis {h["id"]}: {exc}') from exc
        item = asdict(assessment)
        item['unresolved_signal_ids'] = unresolved
        if unresolved:
            item['conclusion'] = 'INSUFFICIENT_EVIDENCE'
            item['missing_evidence'] = sorted(set(item['missing_evidence']) | {f'signal:{u}' for u in unresolved})
        hypotheses.append(item)
    if reconciliation['state'] == 'INSUFFICIENT':
        research_state = 'INSUFFICIENT_EVIDENCE'
    elif not hypotheses:
        research_state = 'NOT_EVALUABLE'
    elif any(h['conclusion'] == 'INSUFFICIENT_EVIDENCE' for h in hypotheses):
        research_state = 'INSUFFICIENT_EVIDENCE'
    else:
        research_state = 'EVALUABLE'  # never VALIDATED: only a separate, independently reviewed process may claim that
    task = (spec.get('analysis') or {}).get('evaluation_task')
    if task is None:
        evaluation = {'state': 'NOT_EVALUABLE', 'checks': [], 'reason': 'no evaluation task declared in the mission spec'}
    else:
        artifact = {'run_id': ctx.mission_id, 'as_of_utc': ctx.as_of, 'evidence_snapshot_id': acquisition['evidence_snapshot_id'],
                    'used_capabilities': [], 'metrics': {}, 'disclosures': ['NOT_A_TRADE_SIGNAL'],
                    'observations': [asdict(o) for o in observations]}
        try:
            evaluation = evaluate_task(task, artifact)
        except ContractError as exc:
            raise OperationFailure('STAGE_PRECONDITION_FAILED', f'evaluation task rejected: {exc}') from exc
    limitations = ['local deterministic analysis of supplied evidence; not predictive-edge evidence',
                   'the coordinator never designates VALIDATED']
    experiment = None
    if spec.get('experiment'):
        experiment = ctx.coordinator._experiment_link(spec['experiment'])
        if experiment['holdout_result'] == 'NOT_REFERENCED':
            limitations.append('the declared experiment is registered but no holdout result is referenced; no holdout evidence is included')
    out = {'schema': 'mission-analysis.v1', 'research_state': research_state, 'hypotheses': hypotheses,
           'no_hypotheses_reason': spec.get('no_hypotheses_reason'), 'evaluation': evaluation,
           'experiment': experiment, 'limitations': limitations}
    return {'analysis.json': _j(out)}


def op_verify(ctx: Ctx) -> dict[str, bytes]:
    spec = ctx.spec
    reconciliation = ctx.up('RECONCILING', 'reconciliation.json')
    eligible = reconciliation['eligible_observation_ids']
    archive = EvidenceArchive(ctx.archive_path)
    refs = []
    for oid in eligible:
        o = _archived(archive, oid)
        refs.append({'id': oid, 'instrument_id': o.instrument_id, 'metric': o.metric, 'unit': o.unit,
                     'sha256': digest(asdict(o))})
    if refs:
        diagnostics, source_state, _ = integrity.resolve_evidence(refs, archive, ctx.as_of, strict=True)
    else:
        diagnostics, source_state = [], 'INCOMPLETE'
    if source_state == 'INVALID':
        raise OperationFailure('VERIFICATION_FAILED', 'archived evidence failed provenance resolution: '
                               + ', '.join(sorted({d['code'] for d in diagnostics})))
    reviews = {}
    handoffs = spec['inputs'].get('reviewer_handoffs', {})
    for name in REVIEWERS:
        ref = handoffs.get(name)
        if ref is None:
            reviews[name] = {'state': 'NOT_PERFORMED', 'reason': 'no reviewer handoff declared in the mission spec'}
            continue
        record = ctx.pinned_json(ref)  # missing file => stage BLOCKED, digest mismatch => precondition failure
        valid = (isinstance(record, dict) and record.get('schema') == 'reviewer-handoff.v1'
                 and record.get('reviewer') == name and record.get('state') in ('PASS', 'FAIL', 'NOT_PERFORMED')
                 and isinstance(record.get('evidence'), list) and all(isinstance(e, str) for e in record['evidence']))
        if valid and record['state'] == 'PASS' and (not record['evidence'] or set(record['evidence']) - set(eligible)):
            valid = False
        if not valid:
            reviews[name] = {'state': 'NOT_PERFORMED', 'reason': 'handoff_invalid: not a reviewer-handoff.v1 record, '
                             'wrong reviewer, or PASS without evidence ids that are eligible evidence'}
            continue
        reviews[name] = {'state': record['state'], 'evidence': record['evidence'], 'handoff_sha256': ref['sha256'],
                         'asserted_by': record.get('produced_by'), 'authenticated': False}
    out = {'schema': 'mission-verification.v1', 'source_provenance': {'state': source_state, 'diagnostics': diagnostics},
           'quantitative_verification': 'NOT_APPLICABLE', 'evidence_references': refs, 'reviews': reviews}
    return {'verification.json': _j(out)}


def _table(headers, rows):
    head = '| ' + ' | '.join(headers) + ' |\n|' + '---|' * len(headers) + '\n'
    return head + ''.join('| ' + ' | '.join(str(c).replace('|', '\\|').replace('\n', ' ') for c in r) + ' |\n' for r in rows)


def op_render(ctx: Ctx) -> dict[str, bytes]:
    spec = ctx.spec
    reconciliation, analysis = ctx.up('RECONCILING', 'reconciliation.json'), ctx.up('ANALYZING', 'analysis.json')
    acquisition, verification = ctx.up('ACQUIRING', 'acquisition.json'), ctx.up('VERIFYING', 'verification.json')
    ctx.fail_at('during_render')
    state = analysis['research_state']
    grade = 'OBSERVATIONS_ONLY_NOT_A_TRADE_SIGNAL' if state == 'EVALUABLE' else 'INSUFFICIENT_EVIDENCE / NO_TRADE'
    obs = reconciliation['observations']
    ev_rows = [[i, obs[i]['instrument_id'], obs[i]['metric'], obs[i]['value'], obs[i]['unit'], obs[i]['observed_at_utc'],
                obs[i]['available_at_utc'], obs[i]['retrieved_at_utc'], obs[i]['source_uri']]
               for i in reconciliation['eligible_observation_ids']]
    ex_rows = [[e['id'], e['code']] for e in reconciliation['excluded']]
    hyp_rows = [[h['hypothesis_id'], h['conclusion'], ', '.join(h['missing_evidence']) or '-'] for h in analysis['hypotheses']]
    att_rows = [[a['operation_id'], a['status'], a.get('source', ''), a.get('reason', a.get('error', ''))] for a in acquisition['attempts']]
    rev_rows = [[n, r['state'], r.get('reason', '')] for n, r in sorted(verification['reviews'].items())]
    gaps = [f"{m['instrument_id']} / {m['metric']}: no eligible observation" for m in reconciliation['missing_required']]
    gaps += [f"conflict ({c['kind']}) {c['instrument_id']} / {c['metric']}: {', '.join(c['observation_ids'])}" for c in reconciliation['conflicts']]
    EV = ['Observation', 'Instrument', 'Metric', 'Value', 'Unit', 'Observed UTC', 'Available UTC', 'Retrieved UTC', 'Source']
    md = (f"# Research mission report: {ctx.mission_id}\n\n**As-of UTC:** {ctx.as_of}  \n**Research state:** `{state}`  \n"
          f"**Research grade:** `{grade}`  \n**Spec digest:** `{spec_digest(spec)}`\n\n"
          f"## Executive thesis\n\nNo thesis is authored by the coordinator. The mechanical result is research state `{state}`; "
          "this is an evidence ledger, not a forecast, scenario analysis or trade recommendation.\n\n"
          f"## Question and horizon\n\n{spec['decision_question']}  \nHorizon: {spec['research_horizon']}  \n"
          f"Instruments: {', '.join(spec['instruments'])}\n\n"
          f"## Evidence quality and freshness [R]\n\nMaximum permitted age: {spec['max_age_seconds']} s. Eligible observations:\n\n"
          + _table(EV, ev_rows) + "\nExcluded observations:\n\n" + _table(['Observation', 'Reason'], ex_rows)
          + f"\n## Hypotheses [D]\n\n" + (_table(['Hypothesis', 'Conclusion', 'Missing metrics'], hyp_rows) if hyp_rows
                                          else f"None declared: {analysis['no_hypotheses_reason']}\n")
          + "\n## Bullish and bearish interpretation, scenarios and invalidation [U]\n\nNot produced. These require an independently "
            "authored report_editor / methodology_reviewer handoff; the coordinator does not invent them.\n\n"
          "## Method and provider disclosure\n\nOperations: " + ', '.join(f'{op}@{ver}' for op, ver in STAGE_OPERATIONS.values())
          + f". Network calls by the coordinator: {acquisition['network_calls_by_coordinator']}.\n\n"
          + _table(['Operation', 'Status', 'Source', 'Note'], att_rows)
          + "\n## Reviews (asserted, unauthenticated)\n\n" + _table(['Reviewer', 'State', 'Note'], rev_rows)
          + "\n## Data gaps\n\n" + ('\n'.join('- ' + g for g in gaps) if gaps else '- none identified against the declared requirements') + '\n\n'
          "## Limitations\n\n" + '\n'.join('- ' + x for x in analysis['limitations']) + '\n')
    e = html.escape

    def h_table(headers, rows):
        return ('<table><thead><tr>' + ''.join(f'<th scope="col">{e(h)}</th>' for h in headers) + '</tr></thead><tbody>'
                + ''.join('<tr>' + ''.join(f'<td>{e(str(c))}</td>' for c in r) + '</tr>' for r in rows) + '</tbody></table>')
    page = ('<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>{e(ctx.mission_id)} research report</title><style>body{{font:16px/1.5 system-ui;margin:1rem auto;max-width:70rem;padding:0 1rem}}'
            'table{border-collapse:collapse;display:block;overflow-x:auto}td,th{border:1px solid #888;padding:.3rem .5rem;text-align:left}</style></head><body>'
            f'<h1>Research mission report: {e(ctx.mission_id)}</h1><p><b>As-of UTC:</b> {e(ctx.as_of)}<br><b>Research state:</b> <code>{e(state)}</code>'
            f'<br><b>Research grade:</b> <code>{e(grade)}</code></p><h2>Evidence quality and freshness</h2>' + h_table(EV, ev_rows)
            + '<h3>Excluded observations</h3>' + h_table(['Observation', 'Reason'], ex_rows) + '<h2>Hypotheses</h2>'
            + (h_table(['Hypothesis', 'Conclusion', 'Missing metrics'], hyp_rows) if hyp_rows else f'<p>None declared: {e(str(analysis["no_hypotheses_reason"]))}</p>')
            + '<h2>Interpretation, scenarios and invalidation</h2><p>Not produced: requires an independently authored handoff.</p>'
            + '<h2>Provider operations</h2>' + h_table(['Operation', 'Status', 'Source', 'Note'], att_rows)
            + '<h2>Reviews (asserted, unauthenticated)</h2>' + h_table(['Reviewer', 'State', 'Note'], rev_rows)
            + '<h2>Data gaps</h2><ul>' + (''.join(f'<li>{e(g)}</li>' for g in gaps) or '<li>none identified against the declared requirements</li>')
            + '</ul></body></html>\n')
    return {'report.md': md.encode('utf-8'), 'report.html': page.encode('utf-8')}


def op_finalize(ctx: Ctx) -> dict[str, bytes]:
    spec = ctx.spec
    acquisition, analysis = ctx.up('ACQUIRING', 'acquisition.json'), ctx.up('ANALYZING', 'analysis.json')
    verification = ctx.coordinator._read_output(ctx.mission_id, 'VERIFYING', 'verification.json')
    verification = json.loads(verification.decode('utf-8'))
    report_md = ctx.coordinator._read_output(ctx.mission_id, 'RENDERING', 'report.md')
    report_html = ctx.coordinator._read_output(ctx.mission_id, 'RENDERING', 'report.html')
    remaining = []
    for name, review in sorted(verification['reviews'].items()):
        if review['state'] != 'PASS':
            remaining.append(f"{name} review is {review['state']}: independently produced handoff required")
    for attempt in acquisition['attempts']:
        if attempt['status'] != 'SUCCEEDED':
            remaining.append(f"provider operation {attempt['operation_id']} is {attempt['status']}")
    if analysis['evaluation']['state'] == 'FAIL':
        remaining.append('evaluation_result is FAIL')
    if not verification['evidence_references']:
        remaining.append('no eligible evidence references')
    files = {
        'mission_baseline.json': {'run_id': ctx.mission_id, 'as_of_utc': ctx.as_of, 'decision_question': spec['decision_question'],
                                  'research_horizon': spec['research_horizon'], 'instruments': spec['instruments'],
                                  'required_provider_operations': [o['operation_id'] for o in spec['provider_operations'] if o['required']],
                                  'spec_digest': spec_digest(spec)},
        'provider_attempts.json': {'attempts': acquisition['attempts']},
        'evidence.json': {'references': verification['evidence_references']},
        'metrics.json': {'metrics': []},
        'hypothesis_assessment.json': {'state': analysis['research_state'], 'hypotheses': analysis['hypotheses'],
                                       'limitations': analysis['limitations']},
        'evaluation_result.json': {**analysis['evaluation'], 'experiment': analysis['experiment']},
        'thesis_revision.json': {'non_revision_reason': 'the coordinator does not author thesis revisions; none was supplied'},
        'committee_verification.json': {'reviews': verification['reviews']},
    }
    blobs = {name: _j(value) for name, value in files.items()}
    blobs['report.md'], blobs['report.html'] = report_md, report_html
    mission = {'schema': 'research-mission.v2', 'completion_state': 'COMPLETE' if not remaining else 'INCOMPLETE',
               'research_state': analysis['research_state'],
               'artifacts': {n: {'path': n, 'sha256': _sha(b)} for n, b in sorted(blobs.items())},
               'missing_artifacts': [], 'blockers': [], 'remaining_work': remaining}
    blobs['mission.json'] = _j(mission)
    ctx.fail_at('during_final_verify')
    ctx.out.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=ctx.out, prefix='.finalize-') as scratch:
        for name, data in blobs.items():
            (Path(scratch) / name).write_bytes(data)
        result = verify_bundle(Path(scratch), archive=ctx.archive_path)
    blobs['final_verification.json'] = _j(result)
    return blobs


OPERATIONS: dict[str, Callable[[Ctx], dict[str, bytes]]] = {
    'PLANNED': op_plan, 'ACQUIRING': op_acquire, 'RECONCILING': op_reconcile, 'ANALYZING': op_analyze,
    'VERIFYING': op_verify, 'RENDERING': op_render, 'FINALIZING': op_finalize}
assert set(OPERATIONS) == set(STAGES) == set(STAGE_OUTPUTS)


# --------------------------------------------------------------------------- coordinator
class MissionCoordinator:
    """Local mission engine over a workspace directory. ``workspace/mission.sqlite`` holds job and stage
    state; mission outputs live under the spec's ``output_dir`` inside the workspace."""

    def __init__(self, workspace: str | Path, *, clock: Callable[[], str] | None = None,
                 fail_at: Callable[[str, str, str], None] | None = None):
        self.workspace = Path(workspace)
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.db_path = self.workspace / 'mission.sqlite'
        self.clock = clock or _now
        self._injected = fail_at
        self.jobs = JobStore(self.db_path)
        with self._db() as db:
            db.executescript(SCHEMA_STORE)

    # -- plumbing ---------------------------------------------------------
    @contextmanager
    def _db(self, *, immediate: bool = False):
        conn = sqlite3.connect(self.db_path, timeout=30, isolation_level=None)
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

    def _fail_at(self, point: str, mission_id: str, stage_id: str) -> None:
        if point not in FAIL_POINTS:
            raise AssertionError(point)
        if self._injected is not None:
            self._injected(point, mission_id, stage_id)

    def output_root(self, spec: dict) -> Path:
        return resolve_under(self.workspace, spec['output_dir'])

    def _mission(self, mission_id: str) -> tuple[dict, str]:
        with self._db() as db:
            row = db.execute('SELECT spec_json, spec_digest FROM missions WHERE mission_id=?', (mission_id,)).fetchone()
        if row is None:
            raise MissionError('MISSION_NOT_FOUND', f'unknown mission: {mission_id}')
        return json.loads(row[0]), row[1]

    def _stages(self, mission_id: str) -> dict[str, dict]:
        with self._db() as db:
            db.row_factory = sqlite3.Row
            rows = db.execute('SELECT * FROM stages WHERE mission_id=?', (mission_id,)).fetchall()
        return {r['stage_id']: dict(r) for r in rows}

    def _stage_dir(self, spec: dict, stage_id: str, op_key: str) -> Path:
        return resolve_under(self.workspace, f"{spec['output_dir']}/stages/{stage_id}/{op_key[:16]}")

    def _ts(self, mission_id: str) -> str:
        """Clock reading never earlier than the job's last transition (JobStore rejects regressions)."""
        now = self.clock()
        try:
            last = self.jobs.get(mission_id)['utc']
        except ContractError:
            return now
        return now if _utc(now) >= _utc(last) else last

    def _move(self, mission_id: str, expected: str, new: str, result: Any = None) -> bool:
        try:
            self.jobs.move(mission_id, expected, new, self._ts(mission_id), result=result)
            return True
        except ContractError:
            return False  # lost a race with another transition; callers re-read the stored state

    def _lost(self, mission_id: str, stage: str | None = None) -> dict:
        return self._result(mission_id, 'FENCED', stage, [{'code': 'STAGE_FENCED', 'message': 'job state changed concurrently'}])

    def _ensure_job(self, mission_id: str, spec: dict) -> None:
        try:
            self.jobs.get(mission_id)
        except ContractError:
            try:
                self.jobs.create(mission_id, spec, self.clock())
            except sqlite3.IntegrityError:
                pass  # concurrent creator won the race

    def _read_output(self, mission_id: str, stage_id: str, name: str) -> bytes:
        spec, _ = self._mission(mission_id)
        row = self._stages(mission_id)[stage_id]
        refs = json.loads(row['output_json']) if row['output_json'] else {}
        if row['status'] not in STAGE_DONE or name not in refs:
            raise MissionError('STAGE_OUTPUT_MISSING', f'{stage_id}/{name} has no committed output')
        path = self._stage_dir(spec, stage_id, row['op_key']) / name
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise MissionError('STAGE_OUTPUT_MISSING', f'{stage_id}/{name} is missing on disk') from exc
        if _sha(data) != refs[name]['sha256']:
            raise MissionError('STAGE_OUTPUT_DIGEST_MISMATCH', f'{stage_id}/{name} does not match its recorded digest')
        return data

    def _experiment_link(self, link: dict) -> dict:
        """Deterministic view of a declared experiment: only facts pinned by the spec (registration digest and,
        optionally, the consumed result digest) are accepted, so replaying ANALYZING cannot change its output."""
        from .experiment_registry import ExperimentRegistry
        path = self.workspace / 'experiments.sqlite'
        summary = ExperimentRegistry(path).summary(link['experiment_id']) if path.is_file() else {'status': 'UNREGISTERED'}
        if summary['status'] == 'UNREGISTERED':
            raise OperationFailure('REQUIRED_EVIDENCE_UNAVAILABLE', 'declared experiment is not registered in this workspace', blocked=True)
        if summary['registration_digest'] != link['registration_digest']:
            raise OperationFailure('STAGE_PRECONDITION_FAILED', 'experiment registration differs from the digest pinned in the spec')
        out = {'experiment_id': link['experiment_id'], 'registration_digest': summary['registration_digest'],
               'freeze_digest': summary['freeze_digest'], 'holdout_result': 'NOT_REFERENCED'}
        pinned = link.get('result_digest')
        if pinned is not None:
            if summary['status'] in ('REGISTERED', 'EVALUATING'):
                raise OperationFailure('REQUIRED_EVIDENCE_UNAVAILABLE', f'pinned holdout result not yet recorded (experiment is {summary["status"]})', blocked=True)
            if summary['status'] != 'CONSUMED' or summary['result_digest'] != pinned:
                raise OperationFailure('STAGE_PRECONDITION_FAILED', 'experiment result differs from the digest pinned in the spec')
            out.update(holdout_result='REFERENCED', result_digest=pinned, independent_evaluation=summary['independent_evaluation'],
                       holdout_exposure_count=summary['holdout_exposure_count'],
                       recovered_after_interruption=summary['recovered_after_interruption'])
        return out

    def _result(self, mission_id: str, outcome: str, stage: str | None = None, diagnostics: list[dict] | None = None) -> dict:
        job = self.jobs.get(mission_id)
        return {'mission_id': mission_id, 'outcome': outcome, 'stage': stage, 'job_state': job['state'],
                'diagnostics': diagnostics or []}

    # -- public API ---------------------------------------------------------
    def create_mission(self, spec: dict) -> dict:
        """Create a mission. Idempotent for a byte-identical specification (returns ``created: False``);
        a different specification under an existing id raises ``MISSION_ALREADY_EXISTS``."""
        validate_spec(spec)
        mission_id, sdigest = spec['mission_id'], spec_digest(spec)
        self.output_root(spec)  # path-safety (traversal/symlink) check
        now = self.clock()
        _utc(now)
        with self._db(immediate=True) as db:
            row = db.execute('SELECT spec_digest FROM missions WHERE mission_id=?', (mission_id,)).fetchone()
            if row is not None:
                created = False
                if row[0] != sdigest:
                    raise MissionError('MISSION_ALREADY_EXISTS',
                                       'mission id exists with a different specification; use a new mission id')
            else:
                created = True
                mine = spec['output_dir']
                clash = next((r[0] for r in db.execute('SELECT output_dir FROM missions') if r[0] == mine or r[0].startswith(mine + '/')
                              or mine.startswith(r[0] + '/')), None)
                if clash:
                    raise MissionError('INVALID_MISSION_SPEC', 'output_dir equals or is nested with another mission output_dir',
                                       diagnostics=[{'code': 'SPEC_OUTPUT_DIR_IN_USE', 'field': 'output_dir', 'message': 'in use'}])
                db.execute('INSERT INTO missions VALUES(?,?,?,?,?,NULL)',
                           (mission_id, sdigest, json.dumps(spec, sort_keys=True), spec['output_dir'], now))
                for stage in STAGES:
                    db.execute('INSERT INTO stages(mission_id,stage_id,status,fence,revision,attempts) VALUES(?,?,?,?,?,?)',
                               (mission_id, stage, 'PENDING', 0, 0, 0))
        self._ensure_job(mission_id, spec)
        return {'mission_id': mission_id, 'spec_digest': sdigest, 'created': created}

    def inspect_mission(self, mission_id: str) -> dict:
        spec, sdigest = self._mission(mission_id)
        try:
            job = self.jobs.get(mission_id)
        except ContractError:
            job = {'state': 'UNREGISTERED', 'revision': None}  # interrupted creation; advance/create repairs it
        stages = self._stages(mission_id)
        with self._db() as db:
            final = db.execute('SELECT final_json FROM missions WHERE mission_id=?', (mission_id,)).fetchone()[0]
            reconciliations = [{'stage_id': r[0], 'utc': r[1], 'decision': r[2], 'classification': r[3]} for r in db.execute(
                'SELECT stage_id,utc,decision,classification FROM reconciliations WHERE mission_id=? ORDER BY id', (mission_id,))]
        rows = [{k: stages[s][k] for k in ('stage_id', 'status', 'fence', 'attempts', 'operation_id', 'operation_version',
                                            'input_digest', 'output_digest', 'failure_code', 'retryable', 'started_utc', 'completed_utc')}
                for s in STAGES]
        waiting = [r['stage_id'] for r in rows if r['status'] == 'BLOCKED']
        return {'mission_id': mission_id, 'spec_digest': sdigest, 'job': job, 'stages': rows, 'waiting_on_stage': waiting,
                'final': json.loads(final) if final else None, 'reconciliations': reconciliations,
                'note': 'job state is execution lifecycle; research_state is in final/analysis and is never VALIDATED here'}

    def cancel_mission(self, mission_id: str) -> dict:
        spec, _ = self._mission(mission_id)
        self._ensure_job(mission_id, spec)
        for _ in range(3):
            state = self.jobs.get(mission_id)['state']
            if state == 'CANCELLED':
                break
            if state not in ('NEW', 'RUNNING', 'INTERRUPTED'):
                raise MissionError('STAGE_PRECONDITION_FAILED', f'cannot cancel a mission in job state {state}')
            if self._move(mission_id, state, 'CANCELLED'):
                break
        return self._result(mission_id, 'CANCELLED', diagnostics=[
            {'code': 'MISSION_CANCELLED', 'message': 'cancelled; an in-flight executor will fail its commit'}])

    def advance_mission(self, mission_id: str) -> dict:
        """Advance at most one stage. Never auto-resumes an interrupted claim: that needs ``reconcile_mission``."""
        spec, sdigest = self._mission(mission_id)
        self._ensure_job(mission_id, spec)
        state = self.jobs.get(mission_id)['state']
        if state == 'CANCELLED':
            return self._result(mission_id, 'TERMINAL', diagnostics=[{'code': 'MISSION_CANCELLED', 'message': 'mission is cancelled'}])
        if state in JOB_TERMINAL:
            return self._result(mission_id, 'TERMINAL')
        if state in ('INTERRUPTED', 'RECONCILING'):
            return self._result(mission_id, 'RECONCILIATION_REQUIRED', diagnostics=[
                {'code': 'RECOVERY_RECONCILIATION_REQUIRED', 'message': f'job is {state}; run reconcile_mission'}])
        if state == 'NEW' and not self._move(mission_id, 'NEW', 'RUNNING'):
            if self.jobs.get(mission_id)['state'] != 'RUNNING':
                return self._result(mission_id, 'TERMINAL')
        bad = self._completed_stage_problems(mission_id, spec)
        if bad:
            if self._job_to(mission_id, 'BLOCKED'):
                self._set_final(mission_id, {'code': bad[0]['code'], 'diagnostics': bad})
            return self._result(mission_id, 'BLOCKED', diagnostics=bad)
        stages = self._stages(mission_id)
        pending = next((s for s in STAGES if stages[s]['status'] not in STAGE_DONE), None)
        if pending is None:
            return self._complete_job(mission_id, spec)
        row = stages[pending]
        if row['status'] == 'RUNNING':
            return self._result(mission_id, 'RECONCILIATION_REQUIRED', pending, [
                {'code': 'RECOVERY_RECONCILIATION_REQUIRED',
                 'message': 'stage is claimed by another executor or an interrupted one; if none is live run reconcile_mission'}])
        if row['status'] == 'FAILED' and row['retryable'] and row['attempts'] >= MAX_RETRYABLE_ATTEMPTS:
            self._job_to(mission_id, 'FAILED')
            return self._result(mission_id, 'FAILED', pending, [{'code': row['failure_code'], 'message': 'retry budget exhausted'}])
        return self._run_stage(mission_id, spec, sdigest, pending, stages)

    # -- stage execution ------------------------------------------------------
    def _input_digest(self, sdigest: str, stage_id: str, stages: dict) -> str:
        upstream = {s: stages[s]['output_digest'] for s in STAGES[:STAGES.index(stage_id)]}
        return digest({'spec': sdigest, 'stage': stage_id, 'upstream': upstream})

    def _op_key(self, mission_id, stage_id, input_digest) -> str:
        op_id, op_ver = STAGE_OPERATIONS[stage_id]
        return digest({'mission_id': mission_id, 'stage_id': stage_id, 'operation_id': op_id,
                       'input_digest': input_digest, 'operation_version': op_ver})

    def _claim(self, mission_id: str, stage_id: str, input_digest: str, op_key: str, *, takeover: bool = False) -> tuple[int, int]:
        op_id, op_ver = STAGE_OPERATIONS[stage_id]
        now = self.clock()
        with self._db(immediate=True) as db:
            job = db.execute('SELECT state FROM jobs WHERE id=?', (mission_id,)).fetchone()
            ok_states = ('RUNNING', 'RECONCILING') if takeover else ('RUNNING',)
            if job is None or job[0] not in ok_states:
                raise StageFenced('MISSION_CANCELLED' if job and job[0] == 'CANCELLED' else 'STAGE_PRECONDITION_FAILED',
                                  f'job state is {job[0] if job else None}')
            row = db.execute('SELECT status,fence,attempts,input_digest,op_key FROM stages WHERE mission_id=? AND stage_id=?',
                             (mission_id, stage_id)).fetchone()
            status, fence, attempts, stored_input, stored_key = row
            if stored_input is not None and stored_input != input_digest:
                raise MissionError('OPERATION_IDENTITY_CONFLICT',
                                   f'{stage_id} already bound to a different input digest; use a new mission')
            if status in STAGE_DONE:
                raise StageFenced('STAGE_ALREADY_FINAL', 'stage already completed')
            if status == 'RUNNING' and not takeover:
                raise StageFenced('RECOVERY_RECONCILIATION_REQUIRED', 'stage is claimed by another executor')
            fence, attempts = fence + 1, attempts + 1
            db.execute('UPDATE stages SET status=?,fence=?,revision=revision+1,attempts=?,operation_id=?,operation_version=?,'
                       'input_digest=?,op_key=?,failure_code=NULL,failure_detail=NULL,retryable=NULL,started_utc=? '
                       'WHERE mission_id=? AND stage_id=?',
                       ('RUNNING', fence, attempts, op_id, op_ver, input_digest, op_key, now, mission_id, stage_id))
            db.execute('INSERT INTO attempts(mission_id,stage_id,attempt,fence,op_key,started_utc) VALUES(?,?,?,?,?,?)',
                       (mission_id, stage_id, attempts, fence, op_key, now))
        return fence, attempts

    def _check_fence(self, mission_id: str, stage_id: str, fence: int, *, allow_reconciling: bool = False) -> None:
        with self._db() as db:
            row = db.execute('SELECT s.status,s.fence,j.state FROM stages s JOIN jobs j ON j.id=s.mission_id '
                             'WHERE s.mission_id=? AND s.stage_id=?', (mission_id, stage_id)).fetchone()
        ok = ('RUNNING', 'RECONCILING') if allow_reconciling else ('RUNNING',)
        if row[2] == 'CANCELLED':
            raise StageFenced('MISSION_CANCELLED', 'mission was cancelled')
        if row[0] != 'RUNNING' or row[1] != fence or row[2] not in ok:
            raise StageFenced()

    def _write_outputs(self, spec, stage_id, op_key, outputs) -> dict:
        directory = self._stage_dir(spec, stage_id, op_key)
        return {name: {'path': f'stages/{stage_id}/{op_key[:16]}/{name}', 'sha256': write_immutable(directory / name, data)}
                for name, data in sorted(outputs.items())}

    def _compute(self, mission_id: str, spec: dict, stage_id: str) -> dict[str, bytes]:
        op_id = STAGE_OPERATIONS[stage_id][0]
        if op_id not in spec['permitted_operations']:
            raise OperationFailure('STAGE_PRECONDITION_FAILED', f'operation {op_id} is not permitted by the mission spec')
        try:
            outputs = OPERATIONS[stage_id](Ctx(self, spec, stage_id))
        except (OperationFailure, MissionError):
            raise
        except OSError as exc:
            raise OperationFailure('OPERATION_ERROR', f'{type(exc).__name__}', retryable=True) from exc
        except Exception as exc:  # noqa: BLE001 - unclassified operation bug: fail closed, record type only
            raise OperationFailure('OPERATION_ERROR', type(exc).__name__) from exc
        if set(outputs) != set(STAGE_OUTPUTS[stage_id]):
            raise OperationFailure('STAGE_POSTCONDITION_FAILED', f'{stage_id} produced unexpected or missing artifacts')
        for name, data in outputs.items():
            if name.endswith('.json'):
                try:
                    json.loads(data.decode('utf-8'))
                except (ValueError, UnicodeError) as exc:
                    raise OperationFailure('STAGE_POSTCONDITION_FAILED', f'{name} is not valid JSON') from exc
        return outputs

    def _run_stage(self, mission_id, spec, sdigest, stage_id, stages) -> dict:
        self._fail_at('before_admission', mission_id, stage_id)
        input_digest = self._input_digest(sdigest, stage_id, stages)
        op_key = self._op_key(mission_id, stage_id, input_digest)
        try:
            fence, attempt = self._claim(mission_id, stage_id, input_digest, op_key)
        except StageFenced as exc:
            return self._result(mission_id, 'CLAIM_LOST' if exc.code != 'MISSION_CANCELLED' else 'TERMINAL', stage_id,
                                [{'code': exc.code, 'message': exc.message}])
        self._fail_at('after_admission', mission_id, stage_id)
        try:
            outputs = self._compute(mission_id, spec, stage_id)
        except (OperationFailure, MissionError) as failure:
            return self._record_failure(mission_id, stage_id, fence, attempt, failure)
        return self._finish_stage(mission_id, spec, stage_id, fence, attempt, op_key, outputs)

    def _finish_stage(self, mission_id, spec, stage_id, fence, attempt, op_key, outputs, *, allow_reconciling=False) -> dict:
        try:
            self._check_fence(mission_id, stage_id, fence, allow_reconciling=allow_reconciling)
            refs = self._write_outputs(spec, stage_id, op_key, outputs)
            self._fail_at('after_artifact_write', mission_id, stage_id)
            now = self.clock()
            with self._db(immediate=True) as db:
                row = db.execute('SELECT s.status,s.fence,j.state FROM stages s JOIN jobs j ON j.id=s.mission_id '
                                 'WHERE s.mission_id=? AND s.stage_id=?', (mission_id, stage_id)).fetchone()
                ok = ('RUNNING', 'RECONCILING') if allow_reconciling else ('RUNNING',)
                if row[2] == 'CANCELLED':
                    raise StageFenced('MISSION_CANCELLED', 'mission was cancelled before commit')
                if row[0] != 'RUNNING' or row[1] != fence or row[2] not in ok:
                    raise StageFenced()
                db.execute('UPDATE stages SET status=?,revision=revision+1,output_json=?,output_digest=?,completed_utc=?,'
                           'failure_code=NULL,failure_detail=NULL WHERE mission_id=? AND stage_id=?',
                           ('SUCCEEDED', json.dumps(refs, sort_keys=True), digest(refs), now, mission_id, stage_id))
                db.execute('UPDATE attempts SET ended_utc=?,outcome=? WHERE mission_id=? AND stage_id=? AND attempt=?',
                           (now, 'SUCCEEDED', mission_id, stage_id, attempt))
        except StageFenced as exc:
            return self._result(mission_id, 'FENCED' if exc.code != 'MISSION_CANCELLED' else 'TERMINAL', stage_id,
                                [{'code': exc.code, 'message': exc.message}])
        except MissionError as failure:
            return self._record_failure(mission_id, stage_id, fence, attempt, failure, allow_reconciling=allow_reconciling)
        except OSError as exc:  # e.g. ENOSPC, or a filesystem without hard links: retryable, recorded, never a bare traceback
            return self._record_failure(mission_id, stage_id, fence, attempt,
                                        OperationFailure('OPERATION_ERROR', type(exc).__name__, retryable=True), allow_reconciling=allow_reconciling)
        self._fail_at('after_state_commit', mission_id, stage_id)
        if stage_id == STAGES[-1]:
            return self._complete_job(mission_id, spec, stage_id)
        return self._result(mission_id, 'ADVANCED', stage_id)

    def _record_failure(self, mission_id, stage_id, fence, attempt, failure, *, allow_reconciling=False) -> dict:
        code = failure.code
        blocked = bool(getattr(failure, 'blocked', False))
        retryable = bool(getattr(failure, 'retryable', False))
        message = getattr(failure, 'message', code)
        status = 'BLOCKED' if blocked else 'FAILED'
        now = self.clock()
        try:
            with self._db(immediate=True) as db:
                row = db.execute('SELECT status,fence FROM stages WHERE mission_id=? AND stage_id=?', (mission_id, stage_id)).fetchone()
                if row[0] != 'RUNNING' or row[1] != fence:
                    raise StageFenced()
                db.execute('UPDATE stages SET status=?,revision=revision+1,failure_code=?,failure_detail=?,retryable=?,completed_utc=? '
                           'WHERE mission_id=? AND stage_id=?', (status, code, message, int(retryable), now, mission_id, stage_id))
                db.execute('UPDATE attempts SET ended_utc=?,outcome=?,code=? WHERE mission_id=? AND stage_id=? AND attempt=?',
                           (now, status, code, mission_id, stage_id, attempt))
        except StageFenced as exc:
            return self._result(mission_id, 'FENCED', stage_id, [{'code': exc.code, 'message': exc.message}])
        diag = [{'code': code, 'message': message}]
        if status == 'FAILED' and not retryable:
            if self._job_to(mission_id, 'FAILED', from_states=('RUNNING', 'RECONCILING')):
                self._set_final(mission_id, {'code': code, 'diagnostics': diag})
            return self._result(mission_id, 'FAILED', stage_id, diag)
        return self._result(mission_id, status, stage_id, diag)

    def _job_to(self, mission_id: str, new: str, *, from_states=('RUNNING',)) -> bool:
        state = self.jobs.get(mission_id)['state']
        return state in from_states and self._move(mission_id, state, new)

    def _set_final(self, mission_id: str, final: dict) -> None:
        with self._db(immediate=True) as db:
            db.execute('UPDATE missions SET final_json=? WHERE mission_id=? AND final_json IS NULL',
                       (json.dumps(final, sort_keys=True), mission_id))

    def _complete_job(self, mission_id: str, spec: dict, stage: str | None = None) -> dict:
        """Map the verified bundle onto the job lifecycle. COMPLETED only when the bundle verifies as COMPLETE
        and the research state is one the mission declared acceptable; otherwise BLOCKED with the reason."""
        read = lambda name: json.loads(self._read_output(mission_id, 'FINALIZING', name).decode('utf-8'))  # noqa: E731
        verification, mission_doc, evaluation = read('final_verification.json'), read('mission.json'), read('evaluation_result.json')
        research_state = mission_doc['research_state']
        accepted = spec['completion_requirements']['accepted_research_states']
        row = self._stages(mission_id)['FINALIZING']
        final = {'verification_status': verification['status'], 'completion_state': mission_doc['completion_state'],
                 'research_state': research_state, 'bundle_dir': str(self._stage_dir(spec, 'FINALIZING', row['op_key']).relative_to(self.workspace)),
                 'remaining_work': mission_doc['remaining_work'], 'scientific_validity': 'NOT_ESTABLISHED'}
        diagnostics = [{'code': d['code'], 'message': d['message']} for d in verification['diagnostics']]
        if verification['status'] == 'COMPLETE' and research_state in accepted:
            if self._move(mission_id, 'RUNNING', 'COMPLETED', result={'bundle_digest': row['output_digest'], **final}):
                self._set_final(mission_id, {**final, 'code': None})   # only after the job really completed (cancel may have won)
        else:
            unexpected = {d['code'] for d in diagnostics} - EXPECTED_WHEN_INCOMPLETE
            if verification['status'] == 'COMPLETE':
                code = 'COMPLETION_REQUIREMENT_UNMET'
                diagnostics.insert(0, {'code': code, 'message': f'research state {research_state} is not among the accepted states'})
            elif unexpected or mission_doc['completion_state'] != 'INCOMPLETE' or not mission_doc['remaining_work']:
                code = 'VERIFICATION_FAILED'
                diagnostics.insert(0, {'code': code, 'message': 'bundle verification reported unexpected diagnostics'})
            elif evaluation['state'] == 'FAIL':
                code = 'COMPLETION_REQUIREMENT_UNMET'
                diagnostics.insert(0, {'code': code, 'message': 'a declared evaluation failed; COMPLETE cannot contain a failed evaluation'})
            else:
                code = 'REQUIRED_EVIDENCE_UNAVAILABLE'
                diagnostics.insert(0, {'code': code, 'message': 'required evidence or independently produced handoffs are unavailable'})
            if self._job_to(mission_id, 'BLOCKED'):
                self._set_final(mission_id, {**final, 'code': code, 'diagnostics': diagnostics})
        state = self.jobs.get(mission_id)['state']
        shown = [] if state == 'COMPLETED' else [{'code': 'MISSION_CANCELLED', 'message': 'mission was cancelled'}] if state == 'CANCELLED' else diagnostics
        return self._result(mission_id, state, stage or 'FINALIZING', shown)

    def _completed_stage_problems(self, mission_id: str, spec: dict) -> list[dict]:
        problems = []
        for stage, row in self._stages(mission_id).items():
            if row['status'] not in STAGE_DONE or not row['output_json']:
                continue
            for name in json.loads(row['output_json']):
                try:
                    self._read_output(mission_id, stage, name)
                except MissionError as exc:
                    problems.append({'code': exc.code, 'message': exc.message, 'stage': stage})
        return problems

    # -- recovery ------------------------------------------------------------
    def _record_reconciliation(self, mission_id, stage_id, decision, classification, detail) -> None:
        with self._db(immediate=True) as db:
            db.execute('INSERT INTO reconciliations(mission_id,stage_id,utc,decision,classification,detail_json) VALUES(?,?,?,?,?,?)',
                       (mission_id, stage_id, self.clock(), decision, classification, json.dumps(detail, sort_keys=True)))

    def reconcile_mission(self, mission_id: str) -> dict:
        """Explicit, operator-invoked recovery. Takes over an interrupted stage claim (bumping the fence so a
        stale executor cannot commit), classifies recorded vs. on-disk effects, then either adopts verified
        effects, resumes the deterministic local operation, or blocks with retained evidence."""
        spec, sdigest = self._mission(mission_id)
        self._ensure_job(mission_id, spec)
        state = self.jobs.get(mission_id)['state']
        if state in JOB_TERMINAL or state == 'NEW':
            return self._result(mission_id, 'NOTHING_TO_RECONCILE')
        stages = self._stages(mission_id)
        running = next((s for s in STAGES if stages[s]['status'] == 'RUNNING'), None)
        bad = self._completed_stage_problems(mission_id, spec)
        if running is None and not bad and state == 'RUNNING':
            self._record_reconciliation(mission_id, None, 'NOTHING_TO_RECONCILE', 'COMPLETE', {})
            return self._result(mission_id, 'NOTHING_TO_RECONCILE')
        if state == 'RUNNING':
            if not self._move(mission_id, 'RUNNING', 'INTERRUPTED'):
                return self._lost(mission_id)
            state = 'INTERRUPTED'
        if state == 'INTERRUPTED' and not self._move(mission_id, 'INTERRUPTED', 'RECONCILING'):
            return self._lost(mission_id)
        if bad:
            self._record_reconciliation(mission_id, bad[0]['stage'], 'BLOCKED_COMPLETED_STAGE_OUTPUT_INVALID', 'CONTRADICTORY', {'problems': bad})
            if self._job_to(mission_id, 'BLOCKED', from_states=('RECONCILING',)):
                self._set_final(mission_id, {'code': 'RECOVERY_RECONCILIATION_REQUIRED', 'diagnostics': bad})
            return self._result(mission_id, 'BLOCKED', bad[0]['stage'], [
                {'code': 'RECOVERY_RECONCILIATION_REQUIRED', 'message': 'a completed stage output failed verification'},
                {'code': 'UNSAFE_REPLAY_BLOCKED', 'message': 'downstream work is not replayed on top of unverifiable upstream output'}] + bad)
        if running is None:  # job was INTERRUPTED/RECONCILING without a claimed stage: safe to resume
            if not self._move(mission_id, 'RECONCILING', 'RUNNING'):
                return self._lost(mission_id)
            self._record_reconciliation(mission_id, None, 'RESUMED', 'ABSENT', {})
            return self._result(mission_id, 'RECONCILED')
        input_digest = self._input_digest(sdigest, running, stages)
        op_key = self._op_key(mission_id, running, input_digest)
        if stages[running]['op_key'] not in (None, op_key):
            self._record_reconciliation(mission_id, running, 'BLOCKED_OPERATION_IDENTITY', 'CONTRADICTORY', {})
            self._job_to(mission_id, 'BLOCKED', from_states=('RECONCILING',))
            return self._result(mission_id, 'BLOCKED', running, [{'code': 'OPERATION_IDENTITY_CONFLICT', 'message': 'claim does not match recomputed identity'}])
        try:
            fence, attempt = self._claim(mission_id, running, input_digest, op_key, takeover=True)  # bumps fence: fences out the old executor
        except StageFenced as exc:
            return self._result(mission_id, 'FENCED', running, [{'code': exc.code, 'message': exc.message}])
        try:
            outputs = self._compute(mission_id, spec, running)
        except (OperationFailure, MissionError) as failure:
            self._record_reconciliation(mission_id, running, 'FAILED_RECOMPUTE', 'UNKNOWN', {'code': failure.code})
            result = self._record_failure(mission_id, running, fence, attempt, failure, allow_reconciling=True)
            if self.jobs.get(mission_id)['state'] == 'RECONCILING':
                # blocked-on-evidence or retryable failure: the job resumes RUNNING and the stage records why
                self._job_to(mission_id, 'RUNNING', from_states=('RECONCILING',))
            result['job_state'] = self.jobs.get(mission_id)['state']
            return result
        directory = self._stage_dir(spec, running, op_key)
        classes = {}
        for name, data in outputs.items():
            path = directory / name
            if not os.path.lexists(path):
                classes[name] = 'ABSENT'
            else:
                try:
                    classes[name] = 'MATCH' if not path.is_symlink() and _sha(path.read_bytes()) == _sha(data) else 'MISMATCH'
                except OSError:
                    classes[name] = 'MISMATCH'
        if 'MISMATCH' in classes.values():
            classification, decision = 'CONTRADICTORY', 'BLOCKED_OUTPUT_DIGEST_MISMATCH'
        elif all(v == 'MATCH' for v in classes.values()):
            classification, decision = 'COMPLETE', 'ADOPTED_VERIFIED_OUTPUT'
        elif all(v == 'ABSENT' for v in classes.values()):
            classification, decision = 'ABSENT', 'RESUMED_DETERMINISTIC_OPERATION'
        else:
            classification, decision = 'PARTIAL', 'RESUMED_DETERMINISTIC_OPERATION'
        self._record_reconciliation(mission_id, running, decision, classification, {'artifacts': classes, 'fence': fence})
        if classification == 'CONTRADICTORY':
            failure = OperationFailure('STAGE_OUTPUT_DIGEST_MISMATCH', 'on-disk artifacts contradict the deterministic expectation; '
                                       'evidence retained for manual resolution', blocked=True)
            self._record_failure(mission_id, running, fence, attempt, failure, allow_reconciling=True)
            if self._job_to(mission_id, 'BLOCKED', from_states=('RECONCILING',)):
                self._set_final(mission_id, {'code': 'RECOVERY_RECONCILIATION_REQUIRED', 'diagnostics': [
                    {'code': 'STAGE_OUTPUT_DIGEST_MISMATCH', 'message': f'{running}: {classes}'}]})
            return self._result(mission_id, 'BLOCKED', running, [
                {'code': 'RECOVERY_RECONCILIATION_REQUIRED', 'message': 'contradictory artifacts retained; manual resolution required'},
                {'code': 'UNSAFE_REPLAY_BLOCKED', 'message': 'replay would overwrite or ignore contradictory artifacts; refused'},
                {'code': 'STAGE_OUTPUT_DIGEST_MISMATCH', 'message': json.dumps(classes, sort_keys=True)}])
        if not self._move(mission_id, 'RECONCILING', 'RUNNING'):  # fence stays valid: job is RUNNING again
            return self._lost(mission_id, running)
        result = self._finish_stage(mission_id, spec, running, fence, attempt, op_key, outputs)
        result['reconciliation'] = {'decision': decision, 'classification': classification, 'artifacts': classes}
        return result
