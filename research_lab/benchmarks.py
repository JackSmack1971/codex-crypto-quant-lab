"""Deterministic offline benchmark runner (P2-D).

Measures *engineering correctness, evidence discipline and recovery behaviour* of the local research stack. It does
not and cannot measure profitability, alpha, predictive skill or live-provider quality. Expected outcomes live in
versioned case files authored independently of this module; the runner only loads, executes and compares. A check
with no declared expectation is ``NOT_EVALUABLE`` -- absent coverage is never reported as ``PASS`` and a failed
mandatory dimension is reported separately from any pass rate.
"""
from __future__ import annotations

import hashlib
import json
import platform
import re
import shutil
import sqlite3
import sys
import tempfile
import threading
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from importlib import metadata
from pathlib import Path
from typing import Any

from . import charts, history, reporting
from . import benchmark_mutations as _mut
from .archive import EvidenceArchive
from .core import ContractError, digest
from .experiment_registry import ExperimentRegistry, RegistryError, dataset_digest
from .experiments import ExperimentPlan
from .intelligence import ResearchIntelligence
from .mission_coordinator import MissionCoordinator
from .mission_spec import STAGE_OPERATIONS, STAGES, MissionError
from .validation import LabelWindow

SUITE_SCHEMA = 'research-benchmark-suite.v1'
CASE_SCHEMA = 'research-benchmark-case.v1'
RESULT_SCHEMA = 'research-benchmark-result.v1'
RUNNER_VERSION = '1'
DIMENSIONS = ('process_state', 'diagnostic_classification', 'evidence_reference_resolution', 'quantitative_accuracy',
              'chart_sample_lineage', 'report_contract', 'missing_data_handling', 'recovery_idempotency',
              'holdout_governance', 'deterministic_reproducibility', 'historical_comparison')
STATES = ('PASS', 'FAIL', 'NOT_EVALUABLE')
SCOPE_STATEMENT = ('Measures engineering correctness, evidence discipline and recovery behaviour on deterministic offline fixtures. '
                   'It does not measure profitability, alpha, predictive skill, statistical edge or live-provider quality.')
CASE_KEYS = {'schema', 'case_id', 'version', 'title', 'harness', 'as_of_utc', 'fixtures', 'scenario', 'expected',
             'unsupported_claims', 'limitations'}


class BenchmarkError(ContractError):
    """Invalid suite, case or fixture (distinct from a case that runs and fails its expectations)."""

    def __init__(self, code: str, message: str = ''):
        super().__init__(f'{code}: {message}' if message else code)
        self.code, self.message = code, message or code


class SimulatedCrash(BaseException):
    """Models process death for recovery cases; deliberately not an ``Exception``."""


class StepClock:
    """Thread-safe deterministic UTC clock: one second per reading, frozen start."""

    def __init__(self, start: str = '2026-02-01T00:00:00Z'):
        self._t = datetime.fromisoformat(start.replace('Z', '+00:00'))
        self._lock = threading.Lock()

    def __call__(self) -> str:
        with self._lock:
            self._t += timedelta(seconds=1)
            return self._t.isoformat(timespec='seconds').replace('+00:00', 'Z')


def _sha_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# --------------------------------------------------------------------------------------------- loading
def load_case(path: Path | str, root: Path) -> dict:
    try:
        case = json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError, UnicodeError) as exc:
        raise BenchmarkError('CASE_UNREADABLE', f'{path}: {type(exc).__name__}') from exc
    if not isinstance(case, dict) or case.get('schema') != CASE_SCHEMA or set(case) - CASE_KEYS or CASE_KEYS - set(case):
        raise BenchmarkError('CASE_SCHEMA_INVALID', f'{path} must be a complete {CASE_SCHEMA} object')
    if case['harness'] not in HARNESSES:
        raise BenchmarkError('CASE_SCHEMA_INVALID', f'unknown harness {case["harness"]!r}')
    for ref in case['fixtures']:
        file = root / ref['source']
        if not file.is_file():
            raise BenchmarkError('FIXTURE_MISSING', ref['source'])
        if _sha_file(file) != ref['sha256']:
            raise BenchmarkError('FIXTURE_DIGEST_MISMATCH', f'{ref["source"]} differs from its pinned SHA-256')
    return case


def load_suite(root: Path | str) -> tuple[dict, list[dict]]:
    root = Path(root)
    try:
        suite = json.loads((root / 'benchmarks' / 'suite.json').read_text(encoding='utf-8'))
    except (OSError, ValueError) as exc:
        raise BenchmarkError('SUITE_UNREADABLE', type(exc).__name__) from exc
    if suite.get('schema') != SUITE_SCHEMA:
        raise BenchmarkError('SUITE_SCHEMA_INVALID', f'schema must be {SUITE_SCHEMA}')
    cases = [load_case(root / 'benchmarks' / 'cases' / f'{cid}.json', root) for cid in suite['cases']]
    if [c['case_id'] for c in cases] != suite['cases']:
        raise BenchmarkError('SUITE_SCHEMA_INVALID', 'case ids must match file names and order')
    return suite, cases


def _materialize(case: dict, root: Path, ws: Path) -> None:
    for ref in case['fixtures']:
        dest = ws / ref['dest']
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(root / ref['source'], dest)


# --------------------------------------------------------------------------------------------- mission harness
class _Observed:
    """Everything a mission-style case may be compared against, gathered through public/ledgered interfaces."""

    def __init__(self):
        self.job_state = self.final_stage = self.research_state = self.completion_state = None
        self.codes: set[str] = set()
        self.trace: list[dict] = []
        self.report: dict | None = None
        self.md = self.html = ''
        self.reviews: dict | None = None
        self.archive_ids: set[str] = set()
        self.artifact_hashes: dict[str, str] = {}
        self.recovery: dict = {}
        self.attempts: dict[str, int] = {}
        self.companions: dict[str, str] = {}


def _read(c: MissionCoordinator, mission_id: str, stage: str, name: str):
    try:
        return c._read_output(mission_id, stage, name)  # noqa: SLF001 - same-package read of digest-verified stage output
    except MissionError:
        return None


def _gather(c: MissionCoordinator, ws: Path, spec: dict, obs: _Observed) -> None:
    mid = spec['mission_id']
    view = c.inspect_mission(mid)
    obs.job_state = view['job']['state']
    for row in view['stages']:
        obs.attempts[row['stage_id']] = row['attempts']
        if row['failure_code']:
            obs.codes.add(row['failure_code'])
    final = view['final'] or {}
    if final.get('code'):
        obs.codes.add(final['code'])
    for d in final.get('diagnostics', []):
        obs.codes.add(d['code'])
    obs.research_state, obs.completion_state = final.get('research_state'), final.get('completion_state')
    rec = _read(c, mid, 'RECONCILING', 'reconciliation.json')
    if rec:
        rec = json.loads(rec)
        obs.codes.update(e['code'] for e in rec['excluded'])
        obs.codes.update(x['kind'] for x in rec['conflicts'])
    ana = _read(c, mid, 'ANALYZING', 'analysis.json')
    if ana and obs.research_state is None:
        obs.research_state = json.loads(ana)['research_state']
    ver = _read(c, mid, 'VERIFYING', 'verification.json')
    if ver:
        obs.reviews = json.loads(ver)['reviews']
    for name in ('report.md', 'report.html', 'report_content.json', 'report_manifest.json'):
        data = _read(c, mid, 'RENDERING', name)
        if data is None:
            continue
        obs.artifact_hashes[name] = hashlib.sha256(data).hexdigest()
        if name == 'report.md':
            obs.md = data.decode('utf-8')
        elif name == 'report.html':
            obs.html = data.decode('utf-8')
        elif name == 'report_content.json':
            obs.report = json.loads(data)
    if obs.completion_state is None:
        mj = _read(c, mid, 'FINALIZING', 'mission.json')
        if mj:
            obs.completion_state = json.loads(mj)['completion_state']
    fv = _read(c, mid, 'FINALIZING', 'final_verification.json')
    if fv:
        obs.codes.update(d['code'] for d in json.loads(fv)['diagnostics'])
        for name in ('mission.json', 'evidence.json', 'final_verification.json'):
            data = _read(c, mid, 'FINALIZING', name)
            if data:
                obs.artifact_hashes[name] = hashlib.sha256(data).hexdigest()
    archive_path = ws / spec['output_dir'] / 'evidence.sqlite'
    if archive_path.is_file():
        try:
            with sqlite3.connect(archive_path) as db:
                ids = [r[0] for r in db.execute('SELECT id FROM observations')]
            archive = EvidenceArchive(archive_path)
            for oid in ids:
                try:
                    archive.get(oid)
                    obs.archive_ids.add(oid)
                except ContractError:
                    pass
        except sqlite3.Error:
            pass


def _step_mission(c_factory, mission_id: str, ws: Path, spec: dict, obs: _Observed, steps: list[dict]) -> None:
    c = c_factory()
    c.create_mission(spec)
    for step in steps:
        op = step['op']
        if op == 'run_to_end':
            for _ in range(12):
                result = c.advance_mission(mission_id)
                obs.trace.append({'op': 'advance', 'outcome': result['outcome'], 'stage': result['stage'], 'job_state': result['job_state']})
                obs.codes.update(d['code'] for d in result['diagnostics'])
                obs.final_stage = result['stage'] or obs.final_stage
                if result['job_state'] in ('COMPLETED', 'FAILED', 'BLOCKED', 'CANCELLED') or result['outcome'] != 'ADVANCED':
                    break
        elif op == 'advance':
            for _ in range(step.get('count', 1)):
                result = c.advance_mission(mission_id)
                obs.trace.append({'op': 'advance', 'outcome': result['outcome'], 'stage': result['stage'], 'job_state': result['job_state']})
                obs.codes.update(d['code'] for d in result['diagnostics'])
                obs.final_stage = result['stage'] or obs.final_stage
        elif op == 'tamper_archive':
            with sqlite3.connect(ws / spec['output_dir'] / 'evidence.sqlite') as db:
                db.execute("UPDATE observations SET sha256='0' WHERE id=?", (step['observation_id'],))
        elif op == 'crash':
            point, stage = step['point'], step['stage']
            fired = {'done': False}

            def hook(p, mid, st, point=point, stage=stage):
                if not fired['done'] and p == point and st == stage:
                    fired['done'] = True
                    raise SimulatedCrash(p)
            crashing = c_factory(fail_at=hook)
            for _ in range(12):
                try:
                    result = crashing.advance_mission(mission_id)
                except SimulatedCrash:
                    obs.recovery.update(crashed_at=stage, crash_point=point)
                    break
                obs.trace.append({'op': 'advance', 'outcome': result['outcome'], 'stage': result['stage'], 'job_state': result['job_state']})
            c = c_factory()  # a new process: no in-memory state survives
            result = c.advance_mission(mission_id)
            obs.recovery['advance_after_crash'] = result['outcome']
            obs.codes.update(d['code'] for d in result['diagnostics'])
        elif op == 'reconcile':
            result = c.reconcile_mission(mission_id)
            rec = result.get('reconciliation', {})
            obs.recovery.update(reconcile_outcome=result['outcome'], reconcile_classification=rec.get('classification'),
                                reconcile_decision=rec.get('decision'))
            obs.codes.update(d['code'] for d in result['diagnostics'])
            obs.final_stage = result['stage'] or obs.final_stage
        elif op == 'concurrent_advance':
            _concurrent(c_factory, mission_id, step['stage'], obs)
        elif op == 'render_v1':
            _mut.render_under_v1(c, mission_id)
            obs.recovery['legacy_render_detected'] = c.render_is_legacy(mission_id)
        elif op == 'forge_companion':
            _mut.forge_companion(c, ws, spec, step, obs.companions)
        else:
            raise BenchmarkError('CASE_SCHEMA_INVALID', f'unknown step {op!r}')
    _gather(c, ws, spec, obs)
    if any(s['op'] == 'render_v1' for s in steps) and (c.inspect_mission(mission_id)['final'] or {}).get('bundle_dir'):
        bundle = ws / c.inspect_mission(mission_id)['final']['bundle_dir']
        obs.recovery['render_contract'] = reporting.verify_bundle_companions(bundle, ws / spec['output_dir'] / 'evidence.sqlite')['status']
    stage_rows = c.inspect_mission(mission_id)['stages']
    if obs.recovery:
        obs.recovery['final_job_state'] = obs.job_state
        stage = obs.recovery.get('crashed_at') or obs.recovery.get('contested_stage')
        if stage:
            row = next(r for r in stage_rows if r['stage_id'] == stage)
            obs.recovery.update(stage_status=row['status'], stage_attempts=row['attempts'])


def _concurrent(c_factory, mission_id: str, stage: str, obs: _Observed) -> None:
    entered, release = threading.Event(), threading.Event()

    def hook(p, mid, st):
        if p == 'after_admission' and st == stage:
            entered.set()
            if not release.wait(30):
                raise SimulatedCrash('timeout')
    results: dict[str, Any] = {}

    def runner():
        results['A'] = c_factory(fail_at=hook).advance_mission(mission_id)
    # advance until the contested stage is the next to run
    pre = c_factory()
    while STAGES[[r['stage_id'] for r in pre.inspect_mission(mission_id)['stages'] if r['status'] in ('SUCCEEDED', 'SKIPPED')].__len__()] != stage:
        pre.advance_mission(mission_id)
    thread = threading.Thread(target=runner)
    thread.start()
    if not entered.wait(30):
        raise BenchmarkError('HARNESS_TIMEOUT', 'executor A never claimed the stage')
    results['B'] = c_factory().advance_mission(mission_id)       # B observes A's live claim
    release.set()
    thread.join(30)
    outcomes = sorted([results['A']['outcome'], results['B']['outcome']])
    obs.recovery.update(contested_stage=stage, executor_outcomes=outcomes, winners=outcomes.count('ADVANCED'))
    obs.codes.update(d['code'] for r in results.values() for d in r['diagnostics'])
    obs.trace.append({'op': 'concurrent_advance', 'outcomes': outcomes})
    c = c_factory()
    for _ in range(12):
        r = c.advance_mission(mission_id)
        if r['job_state'] in ('COMPLETED', 'FAILED', 'BLOCKED', 'CANCELLED') or r['outcome'] != 'ADVANCED':
            obs.final_stage = r['stage'] or obs.final_stage
            break


def _run_mission_case(case: dict, root: Path, ws: Path) -> _Observed:
    _materialize(case, root, ws)
    sc = case['scenario']
    spec = json.loads((ws / sc['spec']).read_text(encoding='utf-8'))
    clock = StepClock()
    obs = _Observed()
    _step_mission(lambda **kw: MissionCoordinator(ws, clock=clock, **kw), spec['mission_id'], ws, spec, obs, sc.get('steps', [{'op': 'run_to_end'}]))
    return obs


# --------------------------------------------------------------------------------------------- check helpers
def _chk(dimension: str, state: str, **detail) -> dict:
    return {'dimension': dimension, 'state': state, **detail}


def _dec(x: Any) -> Decimal | None:
    try:
        v = Decimal(str(x))
        return v if v.is_finite() else None
    except (InvalidOperation, ValueError):
        return None


def _eval_process(exp, obs: _Observed):
    if exp is None:
        return _chk('process_state', 'NOT_EVALUABLE', reason='no expectation declared')
    observed = {'job_state': obs.job_state, 'stage': obs.final_stage, 'research_state': obs.research_state, 'completion_state': obs.completion_state}
    diff = {k: {'expected': v, 'observed': observed.get(k)} for k, v in exp.items() if observed.get(k) != v}
    return _chk('process_state', 'FAIL' if diff else 'PASS', observed=observed, differences=diff)


def _eval_diag(exp, obs: _Observed):
    if exp is None:
        return _chk('diagnostic_classification', 'NOT_EVALUABLE', reason='no expectation declared')
    present, absent = set(exp.get('codes_present', [])), set(exp.get('codes_absent', []))
    anyof = exp.get('codes_any_of', [])
    missing = sorted(present - obs.codes)
    forbidden = sorted(absent & obs.codes)
    unmet_any = [group for group in anyof if not set(group) & obs.codes]
    diff = {'missing_codes': missing, 'forbidden_codes_seen': forbidden, 'unmet_any_of': unmet_any}
    return _chk('diagnostic_classification', 'FAIL' if (missing or forbidden or unmet_any) else 'PASS',
                observed_codes=sorted(obs.codes), differences={k: v for k, v in diff.items() if v})


def _eval_evidence(exp, obs: _Observed):
    if exp is None:
        return _chk('evidence_reference_resolution', 'NOT_EVALUABLE', reason='no expectation declared')
    if not obs.archive_ids and exp.get('required_ids'):
        return _chk('evidence_reference_resolution', 'FAIL', reason='no archived evidence resolved')
    unresolved = sorted(set(exp.get('required_ids', [])) - obs.archive_ids)
    wrongly = sorted(set(exp.get('must_not_resolve', [])) & obs.archive_ids)
    dangling = []
    if obs.report:
        dangling = sorted(set(obs.report['referenced_evidence_ids']) - obs.archive_ids)
    companion_diff = {k: {'expected': v, 'observed': obs.companions.get(k)} for k, v in exp.get('companions', {}).items()
                      if obs.companions.get(k) != v}
    bad = unresolved or wrongly or dangling or companion_diff
    return _chk('evidence_reference_resolution', 'FAIL' if bad else 'PASS',
                differences={k: v for k, v in (('unresolved', unresolved), ('must_not_resolve_but_resolved', wrongly), ('report_dangling', dangling),
                                                                                     ('companions', companion_diff)) if v})


def _eval_quant(exp, obs: _Observed):
    if exp is None:
        return _chk('quantitative_accuracy', 'NOT_EVALUABLE', reason='no expectation declared')
    if not obs.report:
        return _chk('quantitative_accuracy', 'FAIL', reason='no report content produced')
    claims = {c['claim_id']: c for c in obs.report['claims']}
    diffs = []
    for item in exp:
        c = claims.get(item['claim_id'])
        want, tol = _dec(item['value']), _dec(item.get('tolerance', '0'))
        got = _dec(c['value']) if c else None
        if c is None or got is None or want is None or tol is None or abs(got - want) > tol or c['classification'] != item.get('classification', c['classification']):
            diffs.append({'claim_id': item['claim_id'], 'expected': item['value'], 'observed': c and c['value']})
    return _chk('quantitative_accuracy', 'FAIL' if diffs else 'PASS', differences=diffs)


def _eval_chart(exp, obs: _Observed):
    if exp is None:
        return _chk('chart_sample_lineage', 'NOT_EVALUABLE', reason='no expectation declared')
    if 'rejected_any_of' in exp:
        ok = any(c in obs.codes for c in exp['rejected_any_of'])
        return _chk('chart_sample_lineage', 'PASS' if ok and obs.report is None else 'FAIL', observed_codes=sorted(obs.codes))
    if not obs.report:
        return _chk('chart_sample_lineage', 'FAIL', reason='no report content produced')
    by_id = {c['chart_id']: c for c in obs.report['charts']}
    diffs = []
    for item in exp['charts']:
        c = by_id.get(item['chart_id'])
        if c is None or c['status'] != item.get('status', 'AVAILABLE'):
            diffs.append({'chart_id': item['chart_id'], 'problem': 'missing or wrong status'})
            continue
        if c['status'] == 'AVAILABLE':
            if c['sample_count'] != item['sample_count'] or c['observation_ids'] != item['observation_ids'] \
                    or not set(c['observation_ids']) <= obs.archive_ids or len(c['input_data_digest']) != 64:
                diffs.append({'chart_id': item['chart_id'], 'problem': 'sample lineage differs'})
    return _chk('chart_sample_lineage', 'FAIL' if diffs else 'PASS', differences=diffs)


def _eval_report(exp, obs: _Observed):
    if exp is None:
        return _chk('report_contract', 'NOT_EVALUABLE', reason='no expectation declared')
    if not obs.md:
        return _chk('report_contract', 'FAIL', reason='no report produced')
    headings = [h.lower() for h in re.findall(r'^## (.+)$', obs.md, flags=re.M)]
    diffs = {}
    miss = [s for s in exp.get('sections', []) if not any(s.lower() in h for h in headings)]
    if miss:
        diffs['sections_missing'] = miss
    for key, text, want in (('md_contains', obs.md, True), ('md_absent', obs.md, False), ('html_contains', obs.html, True), ('html_absent', obs.html, False)):
        bad = [s for s in exp.get(key, []) if (s in text) != want]
        if bad:
            diffs[key] = bad
    classes = {c['classification'] for c in (obs.report or {}).get('claims', [])}
    cls = [k for k in exp.get('claim_classes_present', []) if k not in classes] + [k for k in exp.get('claim_classes_absent', []) if k in classes]
    if cls:
        diffs['claim_classes'] = cls
    if exp.get('no_scripts_or_remote_assets') and any(t in obs.html for t in ('<script', 'src="http', "src='http", 'href="http', '@import')):
        diffs['html_active_or_remote_content'] = True
    if 'VALIDATED' in obs.md.replace('never designates VALIDATED', '').replace('designate VALIDATED', ''):
        diffs['validated_claimed'] = True
    return _chk('report_contract', 'FAIL' if diffs else 'PASS', differences=diffs)


def _eval_missing(exp, obs: _Observed):
    if exp is None:
        return _chk('missing_data_handling', 'NOT_EVALUABLE', reason='no expectation declared')
    if not obs.report:
        return _chk('missing_data_handling', 'FAIL', reason='no report content produced')
    claims = {c['claim_id']: c for c in obs.report['claims']}
    diffs = {}
    miss = [i for i in exp.get('unavailable_claims', []) if i not in claims or claims[i]['classification'] != 'U']
    if miss:
        diffs['not_marked_unavailable'] = miss
    imputed = [i for i in exp.get('unavailable_claims', []) if i in claims and claims[i].get('value') is not None]
    if imputed:
        diffs['value_present_on_unavailable'] = imputed
    fabricated = [i for i in exp.get('no_claims', []) if i in claims]
    if fabricated:
        diffs['claims_that_must_not_exist'] = fabricated
    if exp.get('grade') and obs.report['research_grade'] != exp['grade']:
        diffs['grade'] = {'expected': exp['grade'], 'observed': obs.report['research_grade']}
    return _chk('missing_data_handling', 'FAIL' if diffs else 'PASS', differences=diffs)


def _eval_recovery(exp, obs: _Observed):
    if exp is None:
        return _chk('recovery_idempotency', 'NOT_EVALUABLE', reason='no expectation declared')
    diff = {k: {'expected': v, 'observed': obs.recovery.get(k)} for k, v in exp.items() if obs.recovery.get(k) != v}
    return _chk('recovery_idempotency', 'FAIL' if diff else 'PASS', observed=obs.recovery, differences=diff)


def _eval_reviews(exp, obs: _Observed):
    """Reviewer handoffs: expectations about asserted/unauthenticated state fold into report_contract's sibling check."""
    if exp is None:
        return None
    got = {n: {'state': r['state'], 'authenticated': r.get('authenticated')} for n, r in (obs.reviews or {}).items()}
    diff = {n: {'expected': want, 'observed': got.get(n)} for n, want in exp.items()
            if got.get(n, {}).get('state') != want.get('state') or got.get(n, {}).get('authenticated') != want.get('authenticated', got.get(n, {}).get('authenticated'))}
    return diff


def _evaluate_mission(case: dict, obs: _Observed) -> list[dict]:
    exp = case['expected']
    checks = [_eval_process(exp.get('process'), obs), _eval_diag(exp.get('diagnostics'), obs),
              _eval_evidence(exp.get('evidence_references'), obs), _eval_quant(exp.get('quantitative'), obs),
              _eval_chart(exp.get('chart_lineage'), obs), _eval_report(exp.get('report_contract'), obs),
              _eval_missing(exp.get('missing_data'), obs), _eval_recovery(exp.get('recovery'), obs)]
    review_diff = _eval_reviews(exp.get('reviews'), obs)
    if review_diff is not None:  # reviewer authentication status is part of the diagnostic classification
        diag = next(c for c in checks if c['dimension'] == 'diagnostic_classification')
        if review_diff:
            diag['state'] = 'FAIL'
            diag.setdefault('differences', {})['reviews'] = review_diff
        elif diag['state'] == 'NOT_EVALUABLE':
            diag.update(state='PASS', reason='reviewer authentication status matched')
    checks.append(_chk('holdout_governance', 'NOT_EVALUABLE', reason='not a registry case'))
    checks.append(_chk('deterministic_reproducibility', 'NOT_EVALUABLE', reason='single execution'))
    return checks


# --------------------------------------------------------------------------------------------- registry harness
def _registry_case(case: dict, root: Path, ws: Path) -> tuple[list[dict], dict]:
    _materialize(case, root, ws)
    data = json.loads((ws / case['scenario']['dataset']).read_text(encoding='utf-8'))
    windows = [LabelWindow(*w) for w in data['windows']]
    plan_a = ExperimentPlan(**data['plan'])
    candidates = data['candidates']
    digest_hex = dataset_digest(windows, candidates)
    reg = ExperimentRegistry(ws / 'experiments.sqlite', clock=StepClock())
    kw = dict(dataset_id=data['dataset_id'], dataset_version=data['dataset_version'])
    observed: dict[str, Any] = {}

    def register(plan, view=None):
        return reg.register(plan, windows, view or {k: v[:plan.train_end] for k, v in candidates.items()}, **kw,
                            evaluation_method=data['evaluation_method'], dataset_digest_hex=digest_hex)
    for step in case['scenario']['steps']:
        op = step['op']
        try:
            if op == 'register':
                plan = ExperimentPlan(**{**data['plan'], **step.get('plan_override', {})})
                observed['register_' + plan.experiment_id] = register(plan)['status']
            elif op == 'evaluate':
                plan = ExperimentPlan(**data['plan'])
                res = reg.evaluate(plan.experiment_id, plan, windows, candidates, **kw)
                observed['evaluate_status'] = res['status']
            elif op == 'register_expect_error':
                plan = ExperimentPlan(**{**data['plan'], **step['plan_override']})
                reg.register(plan, windows, {k: v[:plan.train_end] for k, v in candidates.items()},
                             dataset_id=step.get('dataset_id', data['dataset_id']), dataset_version=step.get('dataset_version', data['dataset_version']),
                             evaluation_method=data['evaluation_method'], dataset_digest_hex=digest_hex)
                observed[step.get('label', 'second_registration')] = 'ACCEPTED'
            else:
                raise BenchmarkError('CASE_SCHEMA_INVALID', f'unknown step {op!r}')
        except RegistryError as exc:
            observed['error_' + step.get('label', op)] = exc.code
    summary = reg.summary(data['plan']['experiment_id'])
    observed.update(status=summary['status'], independent_evaluation=summary.get('independent_evaluation'),
                    holdout_exposure_count=summary.get('holdout_exposure_count'), holdout_untouched=summary.get('holdout_untouched'),
                    result_digest_present=bool(summary.get('result_digest')))
    exp = case['expected'].get('holdout')
    if exp is None:
        check = _chk('holdout_governance', 'NOT_EVALUABLE', reason='no expectation declared')
    else:
        diff = {k: {'expected': v, 'observed': observed.get(k)} for k, v in exp.items() if observed.get(k) != v}
        check = _chk('holdout_governance', 'FAIL' if diff else 'PASS', observed=observed, differences=diff)
    checks = [_chk(d, 'NOT_EVALUABLE', reason='not applicable to a registry case') for d in DIMENSIONS if d != 'holdout_governance']
    checks.append(check)
    return checks, observed


# --------------------------------------------------------------------------------------------- history harness
def _history_case(case: dict, root: Path, ws: Path) -> tuple[list[dict], dict]:
    _materialize(case, root, ws)
    sc = case['scenario']
    store = ResearchIntelligence(ws / 'intelligence.sqlite')
    ids = [store.add(json.loads((ws / path).read_text(encoding='utf-8'))) for path in sc['revisions']]
    comparison = history.ThesisHistory(store).compare(ids[0], ids[1])
    exp = case['expected'].get('history')
    cov = comparison['evidence_coverage']
    observed = {'validity': comparison['validity'], 'coverage_assessment': comparison['coverage_assessment'],
                'recorded_in_new_revision_only': cov['recorded_in_new_revision_only'], 'no_longer_cited': cov['no_longer_cited'],
                'old_research_as_of_utc': comparison['sources']['old']['research_as_of_utc'],
                'new_research_as_of_utc': comparison['sources']['new']['research_as_of_utc'],
                'metric_status': {i['name']: i['status'] for i in comparison['metrics'].get('items', [])},
                'synthetic_change_figure_present': any('change' in i for i in comparison['metrics'].get('items', []) if i['status'] == 'NON_COMPARABLE')}
    if exp is None:
        checks = [_chk(d, 'NOT_EVALUABLE', reason='no expectation declared') for d in DIMENSIONS]
    else:
        diff = {k: {'expected': v, 'observed': observed.get(k)} for k, v in exp.items() if observed.get(k) != v}
        status = 'FAIL' if diff else 'PASS'
        checks = [_chk(d, 'NOT_EVALUABLE', reason='not applicable to a history case') for d in DIMENSIONS]
        for c in checks:
            if c['dimension'] == 'historical_comparison':
                c.update(state=status, observed=observed, differences=diff)
    return checks, observed


# --------------------------------------------------------------------------------------------- repeat harness
def _repeat_case(case: dict, root: Path, ws: Path, suite_cases: dict[str, dict]) -> tuple[list[dict], dict]:
    sc = case['scenario']
    target = suite_cases[sc['repeat_of']]
    runs = []
    for n in range(sc.get('runs', 2)):
        with tempfile.TemporaryDirectory() as tmp:
            obs = _run_mission_case(target, root, Path(tmp))
            runs.append(dict(obs.artifact_hashes))
    exp = case['expected'].get('reproducibility')
    names = exp['identical_artifacts'] if exp else []
    differing = sorted(n for n in names if len({r.get(n) for r in runs}) != 1 or runs[0].get(n) is None)
    observed = {'runs': len(runs), 'compared': names, 'differing': differing}
    checks = [_chk(d, 'NOT_EVALUABLE', reason='not applicable to a repeat case') for d in DIMENSIONS if d != 'deterministic_reproducibility']
    if exp is None:
        checks.append(_chk('deterministic_reproducibility', 'NOT_EVALUABLE', reason='no expectation declared'))
    else:
        checks.append(_chk('deterministic_reproducibility', 'FAIL' if differing or len(runs) < 2 else 'PASS', observed=observed))
    return checks, observed


HARNESSES = {'mission', 'registry', 'history', 'repeat'}


# --------------------------------------------------------------------------------------------- orchestration
def run_case(case: dict, root: Path, suite_cases: dict[str, dict] | None = None) -> dict:
    with tempfile.TemporaryDirectory(prefix=f'bench-{case["case_id"]}-') as tmp:
        ws = Path(tmp)
        if case['harness'] == 'mission':
            obs = _run_mission_case(case, root, ws)
            checks = _evaluate_mission(case, obs)
            observed = {'job_state': obs.job_state, 'final_stage': obs.final_stage, 'research_state': obs.research_state,
                        'completion_state': obs.completion_state, 'diagnostic_codes': sorted(obs.codes), 'recovery': obs.recovery,
                        'stage_attempts': obs.attempts, 'trace': obs.trace}
            artifacts = obs.artifact_hashes
        elif case['harness'] == 'registry':
            checks, observed = _registry_case(case, root, ws)
            artifacts = {}
        elif case['harness'] == 'history':
            checks, observed = _history_case(case, root, ws)
            artifacts = {}
        else:
            checks, observed = _repeat_case(case, root, ws, suite_cases or {})
            artifacts = {}
    return {'case_id': case['case_id'], 'version': case['version'], 'title': case['title'], 'harness': case['harness'],
            'as_of_utc': case['as_of_utc'], 'fixture_digests': {r['source']: r['sha256'] for r in case['fixtures']},
            'expected': case['expected'], 'observed': observed, 'checks': checks, 'artifact_hashes': artifacts,
            'unsupported_claims': case['unsupported_claims'], 'limitations': case['limitations']}


def _git_sha(root: Path) -> str:
    try:
        head = (root / '.git' / 'HEAD').read_text().strip()
        if head.startswith('ref:'):
            ref = root / '.git' / head.split(' ', 1)[1]
            return ref.read_text().strip() if ref.is_file() else _packed(root, head.split(' ', 1)[1])
        return head
    except OSError:
        return 'UNKNOWN'


def _packed(root: Path, ref: str) -> str:
    try:
        for line in (root / '.git' / 'packed-refs').read_text().splitlines():
            if line.endswith(' ' + ref):
                return line.split()[0]
    except OSError:
        pass
    return 'UNKNOWN'


def _version(name: str) -> str:
    try:
        return metadata.version(name)
    except metadata.PackageNotFoundError:
        return 'NOT_INSTALLED'


def provenance(root: Path, suite: dict, cases: list[dict]) -> dict:
    return {'suite_version': suite['version'], 'runner_version': RUNNER_VERSION, 'git_commit': _git_sha(root),
            'python': platform.python_version(), 'dependencies': {n: _version(n) for n in ('pytest', 'numpy', 'pandas')},
            'component_versions': {'coordinator_operations': {s: f'{o}@{v}' for s, (o, v) in STAGE_OPERATIONS.items()},
                                   'report_composer': reporting.COMPOSER_VERSION, 'chart_renderer': charts.CHART_RENDERER_VERSION,
                                   'history_service': history.SERVICE_VERSION},
            'fixture_digests': {r['source']: r['sha256'] for c in cases for r in c['fixtures']},
            'codex_model_runtime': 'UNKNOWN', 'network_access': 'NOT_USED',
            'execution_limitations': ['offline deterministic fixtures only', 'no live provider or exchange contact',
                                      'reviewer handoffs in fixtures are unauthenticated assertions',
                                      'results do not measure predictive performance']}


def score(results: list[dict], suite: dict, subset: bool = False) -> dict:
    per = {d: {'PASS': 0, 'FAIL': 0, 'NOT_EVALUABLE': 0} for d in DIMENSIONS}
    failures = []
    for r in results:
        for c in r['checks']:
            per[c['dimension']][c['state']] += 1
            if c['state'] == 'FAIL':
                failures.append({'case_id': r['case_id'], 'dimension': c['dimension']})
    mandatory = suite['mandatory_dimensions']
    mfail = [f for f in failures if f['dimension'] in mandatory]
    unexecuted = [d for d in DIMENSIONS if per[d]['PASS'] + per[d]['FAIL'] == 0]
    uncovered = [d for d in mandatory if d in unexecuted]
    return {'per_dimension': per, 'failures': failures, 'mandatory_dimensions': mandatory,
            'mandatory_gate': {'state': 'FAIL' if mfail else ('INCOMPLETE' if uncovered else 'PASS'), 'failures': mfail,
                               'unevaluated_mandatory_dimensions': uncovered, 'subset_run': subset},
            'dimensions_with_no_evaluable_case': unexecuted,
            'note': 'no single aggregate score is produced; a mandatory failure is never offset by passes elsewhere',
            'scope': SCOPE_STATEMENT, 'predictive_performance': 'NOT_MEASURED'}


def run_suite(root: Path | str, only: list[str] | None = None) -> dict:
    root = Path(root)
    suite, cases = load_suite(root)
    by_id = {c['case_id']: c for c in cases}
    unknown = sorted(set(only or []) - set(by_id))
    if unknown:
        raise BenchmarkError('CASE_UNKNOWN', f'unknown case ids: {unknown}')
    selected = [c for c in cases if only is None or c['case_id'] in only]
    started = datetime.now(timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')
    results = [run_case(c, root, by_id) for c in selected]
    canonical = {'schema': RESULT_SCHEMA, 'scope': SCOPE_STATEMENT, 'provenance': provenance(root, suite, selected),
                 'cases': results, 'scoring': score(results, suite, only is not None),
                 'executed_checks': [{'case_id': r['case_id'], 'dimension': c['dimension'], 'state': c['state']} for r in results for c in r['checks']
                                     if c['state'] != 'NOT_EVALUABLE']}
    out = dict(canonical)
    out['canonical_digest'] = digest(canonical)          # includes environment provenance (git commit, Python and dependency versions)
    out['artifact_digest'] = digest({'cases': [{k: r[k] for k in ('case_id', 'observed', 'checks', 'artifact_hashes')} for r in results]})
    out['run_metadata'] = {'started_utc': started, 'finished_utc': datetime.now(timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z'),
                           'note': 'wall-clock values are excluded from canonical_digest; every other field is canonical'}
    return out


def repeat_suite(root: Path | str, runs: int = 2, only: list[str] | None = None) -> dict:
    """Run the whole suite from clean temporary workspaces ``runs`` times and compare canonical digests."""
    results = [run_suite(root, only) for _ in range(runs)]
    digests = [r['canonical_digest'] for r in results]
    return {'runs': runs, 'canonical_digests': digests, 'identical': len(set(digests)) == 1, 'result': results[0],
            'intentionally_variable': ['run_metadata.started_utc', 'run_metadata.finished_utc']}
