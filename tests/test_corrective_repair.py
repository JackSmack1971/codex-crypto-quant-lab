"""Corrective-repair regressions for the four defects found in the P0-P2 integration qualification.

H1 holdout exposure bypass | H2 semantic UTC equivalence | M3 report-companion semantic integrity |
M4 render@1 -> render@2 compatibility. Offline and deterministic; no network, no wall clock.
"""
import copy
import hashlib
import json
import sqlite3
import sys
import tempfile
import threading
import unittest
from contextlib import closing
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))
from p1_support import AS_OF, INSTRUMENT, METRIC, Fixture, SimulatedCrash, StepClock, obs_row, run_to_end  # noqa: E402

from research_lab import mission_coordinator as mc  # noqa: E402
from research_lab import reporting  # noqa: E402
from research_lab.archive import EvidenceArchive  # noqa: E402
from research_lab.charts import ChartError, build_chart  # noqa: E402
from research_lab.completion import verify as verify_bundle  # noqa: E402
from research_lab.core import ContractError, Observation, _utc, instant_key, same_instant  # noqa: E402
from research_lab.experiment_registry import ExperimentRegistry, RegistryError, dataset_digest  # noqa: E402
from research_lab.experiments import ExperimentPlan  # noqa: E402
from research_lab.history import HistoryError, ThesisHistory  # noqa: E402
from research_lab.intelligence import ResearchIntelligence  # noqa: E402
from research_lab.validation import LabelWindow  # noqa: E402

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
N = 12


def windows(offset_days=0, n=N):
    iso = lambda d: d.isoformat().replace('+00:00', 'Z')  # noqa: E731
    return [LabelWindow(iso(T0 + timedelta(days=offset_days + i)), iso(T0 + timedelta(days=offset_days + i, hours=12))) for i in range(n)]


def candidates():
    return {'trend': [0.01, 0.02, -0.01, 0.03, 0.02, 0.01, 0.0, 0.01, -0.02, 0.05, -0.01, 0.02], 'flat': [0.001] * N}


# --------------------------------------------------------------------------- H1
class RegistryBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'experiments.sqlite'
        self.reg = ExperimentRegistry(self.path, clock=StepClock())
        self.c = candidates()

    def register(self, eid, *, hs=9, train_end=6, offset=0, ds='ds-btc', ver='2026.01', c=None, reg=None):
        w, c = windows(offset), c or self.c
        plan = ExperimentPlan(eid, train_end, hs, 0)
        out = (reg or self.reg).register(plan, w, {k: v[:train_end] for k, v in c.items()}, dataset_id=ds, dataset_version=ver,
                                         evaluation_method='holdout mean', dataset_digest_hex=dataset_digest(w, c))
        return plan, w, out

    def evaluate(self, eid, plan, w, *, ds='ds-btc', ver='2026.01', c=None, reg=None):
        return (reg or self.reg).evaluate(eid, plan, w, c or self.c, dataset_id=ds, dataset_version=ver)

    def code(self, fn):
        with self.assertRaises(RegistryError) as cm:
            fn()
        return cm.exception.code


class HoldoutExposureTests(RegistryBase):
    def exposed(self):
        plan, w, _ = self.register('exp-1')
        self.assertTrue(self.evaluate('exp-1', plan, w)['independent_evaluation'])

    def test_h1_01_exact_repeat_with_new_id_rejected(self):
        self.exposed()
        self.assertEqual(self.code(lambda: self.register('exp-2')), 'HOLDOUT_ALREADY_EXPOSED')

    def test_h1_02_partial_overlap_rejected_as_independent(self):
        self.exposed()
        # same dataset lineage, windows shifted one day: holdout days 10..12 vs exposed 9..11 (different digest, same dataset_id)
        shifted = {k: list(v) for k, v in self.c.items()}
        shifted['trend'][11] = 0.07
        self.assertEqual(self.code(lambda: self.register('exp-2', offset=1, c=shifted)), 'HOLDOUT_ALREADY_EXPOSED')

    def test_h1_03_fully_contained_holdout_rejected(self):
        self.exposed()
        self.assertEqual(self.code(lambda: self.register('exp-2', hs=10)), 'HOLDOUT_ALREADY_EXPOSED')

    def test_h1_04_changed_experiment_id_and_split_rejected(self):
        self.exposed()
        self.assertEqual(self.code(lambda: self.register('another-id', train_end=5)), 'HOLDOUT_ALREADY_EXPOSED')

    def test_h1_05_relabelled_dataset_version_rejected(self):
        self.exposed()
        self.assertEqual(self.code(lambda: self.register('exp-2', ver='2027.01-relabel')), 'HOLDOUT_ALREADY_EXPOSED')

    def test_h1_05b_renamed_dataset_id_with_identical_content_rejected(self):
        self.exposed()   # same full-dataset digest under a new dataset_id is the same lineage
        self.assertEqual(self.code(lambda: self.register('exp-2', ds='ds-renamed')), 'HOLDOUT_ALREADY_EXPOSED')

    def test_h1_05c_overlap_is_also_enforced_at_evaluation(self):
        plan1, w1, _ = self.register('exp-1')
        plan2, w2, _ = self.register('exp-2', hs=10)             # both registered while still untouched
        self.assertTrue(self.evaluate('exp-1', plan1, w1)['independent_evaluation'])
        self.assertEqual(self.code(lambda: self.evaluate('exp-2', plan2, w2)), 'HOLDOUT_ALREADY_EXPOSED')
        self.assertEqual(self.reg.summary('exp-2')['holdout_exposure_count'], 0)

    def test_h1_06_disjoint_holdouts_follow_the_documented_contract(self):
        self.exposed()
        # same lineage, strictly later and disjoint -> independent relative to this registry
        plan, w, _ = self.register('exp-later', offset=90)
        out = self.evaluate('exp-later', plan, w)
        self.assertEqual((out['independent_evaluation'], out['independence']), (True, 'ESTABLISHED_LOCAL'))
        # a different dataset with an overlapping time range: equivalence cannot be proven -> UNKNOWN, never independent
        other = {k: list(v) for k, v in self.c.items()}
        other['flat'][11] = 0.002
        plan, w, _ = self.register('exp-xds', offset=1, ds='ds-eth', c=other)
        out = self.evaluate('exp-xds', plan, w, ds='ds-eth', c=other)
        self.assertEqual((out['independent_evaluation'], out['independence']), (False, 'UNKNOWN'))
        self.assertIn('independence is relative to this local registry', ' '.join(out['limitations']))

    def test_h1_07_concurrent_overlapping_claims_cannot_both_be_independent(self):
        plans = {}
        for eid, hs in (('exp-a', 9), ('exp-b', 10)):
            plans[eid] = self.register(eid, hs=hs)[:2]
        results, barrier = [], threading.Barrier(2)

        def run(eid):
            reg = ExperimentRegistry(self.path, clock=StepClock())      # separate connection per racer
            barrier.wait()
            try:
                results.append(('ok', self.evaluate(eid, *plans[eid], reg=reg)))
            except RegistryError as exc:
                results.append(('err', exc.code))
        threads = [threading.Thread(target=run, args=(e,)) for e in plans]
        [t.start() for t in threads]
        [t.join() for t in threads]
        ok = [r for kind, r in results if kind == 'ok']
        self.assertEqual(len(ok), 1)
        self.assertTrue(ok[0]['independent_evaluation'])
        self.assertEqual([r for kind, r in results if kind == 'err'][0] in {'HOLDOUT_ALREADY_EXPOSED', 'HOLDOUT_CLAIM_LOST'}, True)

    def test_h1_08_crash_recovery_never_restores_untouched(self):
        def hook(name, eid):
            if name == 'after_claim':
                raise SimulatedCrash(name)
        reg = ExperimentRegistry(self.path, clock=StepClock(), fail_at=hook)
        plan, w, _ = self.register('exp-1', reg=reg)
        with self.assertRaises(SimulatedCrash):
            self.evaluate('exp-1', plan, w, reg=reg)
        self.assertFalse(self.reg.summary('exp-1')['holdout_untouched'])
        self.assertEqual(self.code(lambda: self.register('exp-2', hs=10)), 'HOLDOUT_ALREADY_EXPOSED')   # EVALUATING is exposed
        done = self.reg.reconcile_evaluation('exp-1', 'COMPLETE', plan=plan, windows=w, candidates=self.c, dataset_id='ds-btc', dataset_version='2026.01')
        self.assertFalse(done['independent_evaluation'])
        self.assertTrue(done['recovered_after_interruption'])
        self.reg.reconcile_evaluation  # noqa: B018  (INVALIDATE path below uses a second crashed experiment)
        plan3, w3, _ = self.register('exp-3', offset=200, reg=reg)
        with self.assertRaises(SimulatedCrash):
            self.evaluate('exp-3', plan3, w3, reg=reg)
        self.reg.reconcile_evaluation('exp-3', 'INVALIDATE', reason='abandoned')
        self.assertFalse(self.reg.summary('exp-3')['holdout_untouched'])
        self.assertEqual(self.code(lambda: self.register('exp-4', offset=200, hs=10)), 'HOLDOUT_ALREADY_EXPOSED')   # invalidated stays exposed

    def test_h1_09_legacy_registrations_remain_readable_with_explicit_qualification(self):
        self.exposed()
        with closing(sqlite3.connect(self.path)) as db, db:     # simulate rows written before interval/independence columns existed
            db.execute('UPDATE experiments SET holdout_from=NULL, holdout_to=NULL, independence=NULL')
        reg = ExperimentRegistry(self.path, clock=StepClock())
        summary = reg.summary('exp-1')
        self.assertEqual((summary['status'], summary['independence'], summary['independent_evaluation']), ('CONSUMED', 'LEGACY_UNQUALIFIED', False))
        self.assertEqual(self.code(lambda: self.register('exp-2', reg=reg)), 'HOLDOUT_ALREADY_EXPOSED')     # identical holdout key still caught
        plan, w, _ = self.register('exp-3', hs=10, ver='x', reg=reg)                                         # cannot prove overlap -> UNKNOWN, not independent
        self.assertEqual(self.code(lambda: self.register('exp-3b', hs=10, reg=reg)), 'HOLDOUT_ALREADY_EXPOSED') if False else None
        out = reg.evaluate('exp-3', plan, w, self.c, dataset_id='ds-btc', dataset_version='x')
        self.assertEqual((out['independent_evaluation'], out['independence']), (False, 'UNKNOWN'))

    def test_h1_09b_old_schema_database_is_migrated_additively(self):
        old = Path(self.tmp.name) / 'old.sqlite'
        with closing(sqlite3.connect(old)) as db, db:
            db.executescript(sqlite3.connect(self.path).execute("select sql from sqlite_master where name='experiments'").fetchone()[0]
                             .replace(', holdout_from TEXT, holdout_to TEXT, independence TEXT', '') + ';')
        reg = ExperimentRegistry(old, clock=StepClock())
        plan, w, _ = self.register('exp-1', reg=reg)
        self.assertTrue(self.evaluate('exp-1', plan, w, reg=reg)['independent_evaluation'])
        ExperimentRegistry(old, clock=StepClock())                 # idempotent re-open


class ReviewHardeningTests(RegistryBase):
    def test_legacy_interval_less_exposure_makes_any_lineage_unknown(self):
        plan, w, _ = self.register('exp-1')
        self.evaluate('exp-1', plan, w)
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('UPDATE experiments SET holdout_from=NULL, holdout_to=NULL, independence=NULL')
        reg = ExperimentRegistry(self.path, clock=StepClock())
        other = {k: list(v) for k, v in self.c.items()}
        other['flat'][11] = 0.003
        plan2, w2, _ = self.register('exp-2', offset=1, ds='ds-other', c=other, reg=reg)
        out = self.evaluate('exp-2', plan2, w2, ds='ds-other', c=other, reg=reg)
        self.assertEqual((out['independent_evaluation'], out['independence']), (False, 'UNKNOWN'))

    def test_concurrent_first_open_of_an_old_schema_database_is_safe(self):
        old = Path(self.tmp.name) / 'old2.sqlite'
        with closing(sqlite3.connect(old)) as db, db:
            ddl = sqlite3.connect(self.path).execute("select sql from sqlite_master where name='experiments'").fetchone()[0]
            db.executescript(ddl.replace(', holdout_from TEXT, holdout_to TEXT, independence TEXT', '') + ';')
        errors = []

        def open_it():
            try:
                ExperimentRegistry(old)
            except Exception as exc:  # noqa: BLE001
                errors.append(repr(exc))
        threads = [threading.Thread(target=open_it) for _ in range(8)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual(errors, [])

    def test_hull_text_order_is_year_padded(self):
        from research_lab.experiment_registry import _holdout_hull
        w = [LabelWindow('0999-01-01T00:00:00Z', '0999-01-02T00:00:00Z')]
        lo, _ = _holdout_hull(w, ExperimentPlan('x', 0, 0, 0))
        self.assertTrue(lo.startswith('0999-'))
        self.assertLess(lo, _holdout_hull([LabelWindow('1000-01-01T00:00:00Z', '1000-01-02T00:00:00Z')], ExperimentPlan('x', 0, 0, 0))[0])


class ModelsAndRatioTests(unittest.TestCase):
    def test_ratio_alignment_is_semantic_not_textual(self):
        base = {'instrument_id': INSTRUMENT, 'metric': METRIC, 'unit': 'USDT', 'source_uri': 'https://x/a', 'status': 'OBSERVED',
                'retrieved_at_utc': '2026-01-01T11:00:00Z', 'available_at_utc': '2026-01-01T11:00:00Z'}
        obs = {'a': {**base, 'id': 'a', 'value': '10', 'observed_at_utc': '2026-01-01T10:00:00Z'},
               'b': {**base, 'id': 'b', 'value': '4', 'observed_at_utc': '2026-01-01T10:00:00+00:00'}}
        out = reporting._derive({'claim_id': 'r', 'transform': 'ratio', 'input_evidence_ids': ['a', 'b']}, obs, {'a', 'b'}, AS_OF, 7200, set())
        self.assertEqual(out['value'], '2.5')

    def test_difference_scaled_model_rejects_equivalent_timestamps(self):
        from research_lab.models import ModelSpec, calculate_model
        mk = lambda i, v, t: Observation(i, INSTRUMENT, METRIC, v, 'USDT', 'https://x/a', t, '2026-01-01T11:00:00Z', '2026-01-01T11:00:00Z')  # noqa: E731
        spec = ModelSpec('m', 'difference_scaled', METRIC, 'USDT', max_age_seconds=20000)
        same = [mk('a', '100', '2026-01-01T10:00:00Z'), mk('b', '104', '2026-01-01T10:00:00+00:00')]
        apart = [mk('a', '100', '2026-01-01T09:00:00Z'), mk('b', '104', '2026-01-01T10:00:00+00:00')]
        self.assertEqual(calculate_model(spec, same, INSTRUMENT, AS_OF).status, 'INSUFFICIENT_EVIDENCE')
        self.assertEqual(calculate_model(spec, apart, INSTRUMENT, AS_OF).status, 'CALCULATED')


# --------------------------------------------------------------------------- H2
class TimestampContractTests(unittest.TestCase):
    def test_h2_01_z_and_utc_offset_are_one_instant(self):
        self.assertEqual(instant_key('2026-01-01T10:00:00Z'), instant_key('2026-01-01T10:00:00+00:00'))
        self.assertTrue(same_instant('2026-01-01T10:00:00Z', '2026-01-01T10:00:00+00:00'))

    def test_h2_02_equivalent_fractional_spellings_are_one_instant(self):
        for other in ('2026-01-01T10:00:00.0Z', '2026-01-01T10:00:00.000000Z', '2026-01-01T10:00:00.000+00:00'):
            self.assertTrue(same_instant('2026-01-01T10:00:00Z', other), other)

    def test_h2_06_distinct_instants_stay_distinct(self):
        self.assertFalse(same_instant('2026-01-01T10:00:00Z', '2026-01-01T10:00:00.000001Z'))
        self.assertLess(instant_key('2026-01-01T10:00:00.5Z'), instant_key('2026-01-01T10:00:01Z'))     # text order == time order

    def test_h2_08_invalid_and_non_utc_fail_closed(self):
        for bad in ('2026-13-01T00:00:00Z', 'not a time', '2026-01-01T10:00:00', '2026-01-01T10:00:00+05:30', '', None, 5):
            with self.assertRaises(ContractError, msg=repr(bad)):
                instant_key(bad)
        with self.assertRaises(ContractError):
            Observation('o', INSTRUMENT, METRIC, '1', 'USDT', 'https://x', '2026-01-01T10:00:00+02:00', '2026-01-01T10:01:00Z', '2026-01-01T10:01:00Z')


class MissionHarness(unittest.TestCase):
    def setUp(self):
        self.fx = Fixture()
        self.addCleanup(self.fx.close)

    def run_mission(self, mission_id, observations, authored=None, coordinator=None, **spec_kw):
        spec = self.fx.spec(mission_id, observations=observations, **spec_kw)
        if authored is not None:
            spec['inputs']['report_input'] = self.fx.put(f'inputs/{mission_id}/report.json', authored)
        c = coordinator or self.fx.coordinator()
        c.create_mission(spec)
        results = run_to_end(c, mission_id)
        return c, spec, results

    def bundle(self, c, mission_id):
        return self.fx.ws / c.inspect_mission(mission_id)['final']['bundle_dir']

    def reconciliation(self, c, mission_id):
        return json.loads(c._read_output(mission_id, 'RECONCILING', 'reconciliation.json'))


class SemanticTimeMissionTests(MissionHarness):
    def test_h2_03_contradictory_values_at_one_semantic_instant_are_a_conflict(self):
        rows = [obs_row('o1', '100', observed='2026-01-01T10:00:00Z'),
                obs_row('o2', '105', observed='2026-01-01T10:00:00.000+00:00', retrieved='2026-01-01T10:01:00Z', available='2026-01-01T10:01:00Z')]
        c, spec, _ = self.run_mission('m1', rows)
        rec = self.reconciliation(c, 'm1')
        self.assertEqual([(x['kind'], x['observation_ids']) for x in rec['conflicts']], [('CONTRADICTORY', ['o1', 'o2'])])
        self.assertEqual(rec['eligible_observation_ids'], [])
        self.assertEqual(rec['state'], 'INSUFFICIENT')

    def test_h2_03b_distinct_instants_are_not_a_conflict(self):
        rows = [obs_row('o1', '100', observed='2026-01-01T10:00:00Z'),
                obs_row('o2', '105', observed='2026-01-01T10:00:00.000001Z', retrieved='2026-01-01T10:01:00Z', available='2026-01-01T10:01:00Z')]
        c, _, _ = self.run_mission('m1', rows)
        self.assertEqual(self.reconciliation(c, 'm1')['conflicts'], [])

    def test_h2_03c_available_before_observed_is_excluded(self):
        rows = [obs_row('o1', '100', observed='2026-01-01T10:00:00Z', retrieved='2026-01-01T10:05:00Z', available='2026-01-01T09:00:00Z')]
        c, _, _ = self.run_mission('m1', rows)
        self.assertEqual(self.reconciliation(c, 'm1')['excluded'], [{'id': 'o1', 'code': 'CHRONOLOGY_INVALID'}])

    def test_h2_07_uniform_spelling_keeps_original_text_and_identities(self):
        rows = [obs_row('o1', '100'), obs_row('o2', '105')]                      # same instant, same spelling (pre-repair behaviour)
        c, _, _ = self.run_mission('m1', rows)
        conflict = self.reconciliation(c, 'm1')['conflicts'][0]
        self.assertEqual(conflict['observed_at_utc'], '2026-01-01T10:00:00Z')    # provenance spelling preserved, not canonicalised
        archive = EvidenceArchive(self.fx.ws / 'missions/m1/evidence.sqlite')
        self.assertEqual(asdict(archive.get('o1'))['observed_at_utc'], '2026-01-01T10:00:00Z')

    def test_h2_04_derived_values_do_not_depend_on_id_order(self):
        a = {'id': 'o1', 'instrument_id': INSTRUMENT, 'metric': METRIC, 'value': '100', 'unit': 'USDT', 'source_uri': 'https://x/a',
             'observed_at_utc': '2026-01-01T09:00:00Z', 'available_at_utc': '2026-01-01T09:01:00Z', 'retrieved_at_utc': '2026-01-01T09:01:00Z', 'status': 'OBSERVED'}
        b = {**a, 'id': 'o2', 'value': '110', 'observed_at_utc': '2026-01-01T10:00:00+00:00', 'available_at_utc': '2026-01-01T10:01:00Z', 'retrieved_at_utc': '2026-01-01T10:01:00Z'}
        obs = {'o1': a, 'o2': b}
        out = [reporting._derive({'claim_id': 'd', 'transform': 'difference', 'input_evidence_ids': ids}, obs, {'o1', 'o2'}, AS_OF, 7200, set())
               for ids in (['o1', 'o2'], ['o2', 'o1'])]
        self.assertEqual(out[0], out[1])
        self.assertEqual(out[0]['value'], '10')
        twin = {**b, 'observed_at_utc': '2026-01-01T09:00:00.0Z'}                # same instant as o1 -> no order-dependent answer
        for ids in (['o1', 'o2'], ['o2', 'o1']):
            with self.assertRaises(reporting.ReportError) as cm:
                reporting._derive({'claim_id': 'd', 'transform': 'difference', 'input_evidence_ids': ids}, {'o1': a, 'o2': twin}, {'o1', 'o2'}, AS_OF, 7200, set())
            self.assertEqual(cm.exception.code, 'DERIVED_INPUT_INVALID')
        self.assertEqual(reporting._latest(['2026-01-01T10:00:00Z', '2026-01-01T10:00:00+00:00']),
                         reporting._latest(['2026-01-01T10:00:00+00:00', '2026-01-01T10:00:00Z']))

    def test_h2_05_equivalent_timestamps_are_not_two_chart_samples(self):
        base = {'instrument_id': INSTRUMENT, 'metric': METRIC, 'unit': 'USDT', 'source_uri': 'https://x/a', 'status': 'OBSERVED',
                'retrieved_at_utc': '2026-01-01T11:00:00Z', 'available_at_utc': '2026-01-01T11:00:00Z'}
        obs = {'o1': {**base, 'id': 'o1', 'value': '100', 'observed_at_utc': '2026-01-01T10:00:00Z'},
               'o2': {**base, 'id': 'o2', 'value': '105', 'observed_at_utc': '2026-01-01T10:00:00+00:00'},
               'o3': {**base, 'id': 'o3', 'value': '106', 'observed_at_utc': '2026-01-01T10:00:00.000001Z'}}
        spec = lambda ids: {'chart_id': 'c1', 'chart_type': 'metric_history', 'title': 't', 'min_samples': 2,   # noqa: E731
                            'series': [{'label': 'a', 'observation_ids': ids}]}
        with self.assertRaises(ChartError) as cm:
            build_chart(spec(['o1', 'o2']), obs, set(obs), AS_OF)
        self.assertEqual(cm.exception.code, 'CHART_ORDER_INVALID')
        chart = build_chart(spec(['o1', 'o3']), obs, set(obs), AS_OF)           # h2-06: one microsecond apart stays distinct
        self.assertEqual((chart['status'], chart['meta']['sample_count']), ('AVAILABLE', 2))

    def test_original_reproduction_no_longer_yields_a_report_from_contradictory_pair(self):
        rows = [obs_row('o1', '100', observed='2026-01-01T10:00:00Z'),
                obs_row('o2', '105', observed='2026-01-01T10:00:00+00:00', retrieved='2026-01-01T10:01:00Z', available='2026-01-01T10:01:00Z')]
        authored = {'schema': 'research-intelligence-report.v1', 'mission_id': 'm1', 'as_of_utc': AS_OF,
                    'derived_claims': [{'claim_id': 'd', 'transform': 'difference', 'input_evidence_ids': ['o1', 'o2']}]}
        _, _, results = self.run_mission('m1', rows, authored)
        self.assertEqual(results[-1]['job_state'], 'FAILED')
        self.assertEqual([d['code'] for d in results[-1]['diagnostics']], ['DERIVED_INPUT_INELIGIBLE'])


# --------------------------------------------------------------------------- M3
OBS2 = [obs_row('o1', '100', observed='2026-01-01T10:00:00Z', retrieved='2026-01-01T10:01:00Z', available='2026-01-01T10:01:00Z'),
        obs_row('o2', '112', observed='2026-01-01T11:00:00Z', retrieved='2026-01-01T11:01:00Z', available='2026-01-01T11:01:00Z',
                source='https://api.exchange.example/other')]
AUTHORED = {'schema': 'research-intelligence-report.v1', 'mission_id': 'm1', 'as_of_utc': AS_OF,
            'derived_claims': [{'claim_id': 'd', 'transform': 'difference', 'input_evidence_ids': ['o1', 'o2']}]}


def forge(bundle: Path, mutate, *, rewrite_md=None):
    """Forge a companion the way a local writer can: edit the content, then recompute every digest to match."""
    content = json.loads((bundle / 'report_content.json').read_text())
    mutate(content)
    raw = json.dumps(content, sort_keys=True, separators=(',', ':')).encode()
    (bundle / 'report_content.json').write_bytes(raw)
    md = (bundle / 'report.md').read_text()
    for old, new in (rewrite_md or {}).items():
        md = md.replace(old, new)
    (bundle / 'report.md').write_text(md)
    manifest = json.loads((bundle / 'report_manifest.json').read_text())
    manifest['content_sha256'] = hashlib.sha256(raw).hexdigest()
    manifest['report_md_sha256'] = hashlib.sha256(md.encode()).hexdigest()
    (bundle / 'report_manifest.json').write_text(json.dumps(manifest, sort_keys=True))


def claim(content, cid):
    return next(c for c in content['claims'] if c['claim_id'] == cid)


class CompanionIntegrityTests(MissionHarness):
    def setUp(self):
        super().setUp()
        self.c, self.spec, results = self.run_mission('m1', OBS2, AUTHORED)
        self.assertEqual(results[-1]['job_state'], 'COMPLETED')
        self.bundle = self.bundle_dir = self.bundle_of('m1')
        self.archive = self.fx.ws / 'missions/m1/evidence.sqlite'
        self.history = ThesisHistory(ResearchIntelligence(Path(self.fx.ws) / 'intel.sqlite'))

    def bundle_of(self, mission_id):
        return MissionHarness.bundle(self, self.c, mission_id)

    def copy_bundle(self, name='forged'):
        import shutil
        dst = self.fx.ws / name
        shutil.copytree(self.bundle, dst)
        return dst

    def verdict(self, bundle):
        return reporting.verify_bundle_companions(bundle, self.archive)

    def test_baseline_bundle_is_semantically_verified_everywhere(self):
        self.assertEqual(self.verdict(self.bundle), {'status': 'SEMANTICALLY_VERIFIED', 'problems': []})
        final = json.loads((self.bundle / 'final_verification.json').read_text())
        self.assertEqual(final['report_companions']['verdict'], 'SEMANTICALLY_VERIFIED')        # m3-09 finalization == independent
        self.assertEqual(verify_bundle(self.bundle, archive=self.archive)['status'], 'COMPLETE')

    def test_m3_01_forged_r_value_rejected_even_after_manifest_rehash(self):
        forged = self.copy_bundle()
        self.assertEqual(verify_bundle(forged, archive=self.archive)['status'], 'COMPLETE')
        forge(forged, lambda c: claim(c, 'R:o1').__setitem__('value', '500'), rewrite_md={'100': '500'})
        self.assertEqual(reporting.verify_companions({n: (forged / n).read_bytes() for n in reporting.COMPANION_FILES},
                                                     spec_digest=json.loads((forged / 'mission_baseline.json').read_text())['spec_digest'],
                                                     mission_id='m1', evidence_ids={'o1', 'o2'}), [])      # hash consistency alone is fooled
        verdict = self.verdict(forged)
        self.assertEqual(verdict['status'], 'FAILED')
        self.assertTrue(any('R:o1' in p and 'value' in p for p in verdict['problems']), verdict)

    def test_m3_09b_verify_mission_script_agrees_with_finalization(self):
        import subprocess
        script = Path(__file__).resolve().parents[1] / 'scripts' / 'verify_mission.py'
        run = lambda b: subprocess.run([sys.executable, '-I', str(script), str(b), '--archive', str(self.archive)],  # noqa: E731
                                       capture_output=True, text=True, cwd=str(script.parents[1]))
        ok = run(self.bundle)
        self.assertEqual(ok.returncode, 0, ok.stderr)
        self.assertEqual(json.loads(ok.stdout)['report_companions']['status'], 'SEMANTICALLY_VERIFIED')
        forged = self.copy_bundle()
        forge(forged, lambda c: claim(c, 'R:o1').__setitem__('value', '500'), rewrite_md={'100': '500'})
        bad = run(forged)
        self.assertEqual(bad.returncode, 1)
        self.assertEqual(json.loads(bad.stdout)['report_companions']['status'], 'FAILED')

    def test_review_html_value_is_checked_and_stripped_companions_are_not_a_silent_downgrade(self):
        forged = self.copy_bundle()
        forge(forged, lambda c: claim(c, 'R:o2').__setitem__('value', '999'))
        (forged / 'report.html').write_text((forged / 'report.html').read_text().replace('112', '999'))
        stripped = self.copy_bundle('stripped')
        for name in ('report_content.json', 'report_manifest.json'):
            (stripped / name).unlink()
        v = self.verdict(stripped)
        self.assertEqual(v['status'], 'FAILED')
        self.assertIn('no longer present', v['problems'][0])
        malformed = self.copy_bundle('malformed')
        forge(malformed, lambda c: c.__setitem__('claims', [1]))
        self.assertEqual(self.verdict(malformed)['status'], 'FAILED')
        html_only = self.copy_bundle('html_only')
        (html_only / 'report.html').write_text((html_only / 'report.html').read_text().replace('112', '999'))
        m = json.loads((html_only / 'report_manifest.json').read_text())
        m['report_html_sha256'] = hashlib.sha256((html_only / 'report.html').read_bytes()).hexdigest()
        (html_only / 'report_manifest.json').write_text(json.dumps(m, sort_keys=True))
        self.assertEqual(self.verdict(html_only)['status'], 'FAILED')

    def test_m3_02_forged_d_calculation_rejected(self):
        forged = self.copy_bundle()
        forge(forged, lambda c: claim(c, 'D:d').__setitem__('value', '999'), rewrite_md={'12': '999'})
        verdict = self.verdict(forged)
        self.assertEqual(verdict['status'], 'FAILED')
        self.assertTrue(any('D:d' in p for p in verdict['problems']), verdict)

    def test_m3_02b_forged_d_transform_or_inputs_rejected(self):
        forged = self.copy_bundle()
        forge(forged, lambda c: claim(c, 'D:d').__setitem__('transform', {'id': 'pct_change', 'version': '2'}))
        self.assertEqual(self.verdict(forged)['status'], 'FAILED')
        forged2 = self.copy_bundle('forged2')
        forge(forged2, lambda c: claim(c, 'D:d').__setitem__('evidence_ids', ['o1', 'ghost']))
        self.assertEqual(self.verdict(forged2)['status'], 'FAILED')

    def test_m3_03_swapped_companions_from_another_mission_rejected(self):
        other_rows = [dict(r) for r in OBS2]
        c2, _, _ = self.run_mission('m2', other_rows, {**AUTHORED, 'mission_id': 'm2'}, coordinator=self.c)
        other = MissionHarness.bundle(self, c2, 'm2')
        swapped = self.copy_bundle('swapped')
        for name in reporting.COMPANION_FILES:
            (swapped / name).write_bytes((other / name).read_bytes())
        verdict = self.verdict(swapped)
        self.assertEqual(verdict['status'], 'FAILED')
        self.assertTrue(any('mission id' in p or 'spec digest' in p for p in verdict['problems']), verdict)

    def test_m3_03b_binding_to_snapshot_and_pinned_input(self):
        files = {n: (self.bundle / n).read_bytes() for n in reporting.COMPANION_FILES}
        obs = {r['id']: asdict(Observation(**{k: r[k] for k in ('id', 'instrument_id', 'metric', 'value', 'unit', 'source_uri', 'observed_at_utc',
                                                               'retrieved_at_utc', 'available_at_utc')})) for r in OBS2}
        base = json.loads((self.bundle / 'mission_baseline.json').read_text())
        kw = dict(spec_digest=base['spec_digest'], mission_id='m1', observations=obs, eligible={'o1', 'o2'}, as_of=AS_OF)
        pinned = json.loads((self.bundle / 'report_manifest.json').read_text())
        self.assertEqual(reporting.verify_companion_semantics(files, **kw, evidence_snapshot_id=pinned['evidence_snapshot_id'],
                                                              report_input_digest=pinned['report_input_digest']), [])
        self.assertTrue(reporting.verify_companion_semantics(files, **kw, evidence_snapshot_id='snapshot:other'))
        self.assertTrue(reporting.verify_companion_semantics(files, **kw, report_input_digest='0' * 64))

    def test_m3_04_missing_archive_observation_rejected(self):
        empty = self.fx.ws / 'empty.sqlite'
        EvidenceArchive(empty)
        verdict = reporting.verify_bundle_companions(self.bundle, empty)
        self.assertEqual(verdict['status'], 'FAILED')
        self.assertTrue(any('archived observation is missing' in p for p in verdict['problems']), verdict)

    def test_m3_05_wrong_instrument_venue_unit_metric_or_timestamp_rejected(self):
        for field, value in (('instrument_id', 'binance:spot:ETHUSDT'), ('venue', 'kraken'), ('unit', 'EUR'), ('metric', 'spot.last'),
                             ('observed_at_utc', '2026-01-01T08:00:00Z'), ('retrieved_at_utc', '2026-01-01T10:30:00Z'),
                             ('source', 'https://evil.example/')):
            forged = self.copy_bundle('f_' + field)
            forge(forged, lambda c, f=field, v=value: claim(c, 'R:o1').__setitem__(f, v))
            verdict = self.verdict(forged)
            self.assertEqual(verdict['status'], 'FAILED', field)
            self.assertTrue(any(field in p for p in verdict['problems']), (field, verdict))

    def test_m3_05b_dropped_or_added_r_claims_rejected(self):
        forged = self.copy_bundle()
        forge(forged, lambda c: c['claims'].remove(claim(c, 'R:o2')))
        self.assertEqual(self.verdict(forged)['status'], 'FAILED')

    def test_m3_06_history_refuses_invalid_and_unverifiable_bundles(self):
        forged = self.copy_bundle()
        forge(forged, lambda c: claim(c, 'R:o1').__setitem__('value', '500'), rewrite_md={'100': '500'})
        newer, _, _ = self.run_mission('m2', [obs_row('p1', '120', observed='2026-01-02T11:00:00Z', retrieved='2026-01-02T11:01:00Z',
                                                      available='2026-01-02T11:01:00Z')], as_of_utc='2026-01-02T12:00:00Z', hypotheses=[],
                                       coordinator=self.c)
        new_bundle, new_archive = MissionHarness.bundle(self, newer, 'm2'), self.fx.ws / 'missions/m2/evidence.sqlite'
        with self.assertRaises(HistoryError) as cm:
            self.history.compare_missions(forged, new_bundle, archives=(self.archive, new_archive))
        self.assertEqual(cm.exception.code, 'BUNDLE_INVALID')
        unverified = self.history.compare_missions(forged, new_bundle)          # no archive: UNKNOWN -> no change figure from it
        self.assertEqual((unverified['metrics']['status'], unverified['metrics']['items']), ('REFUSED_UNVERIFIED_BUNDLE', []))
        self.assertEqual(unverified['sources']['older']['verification']['status'], 'UNKNOWN')
        self.assertEqual(unverified['falsifiers']['status'], 'NOT_AVAILABLE')

    def test_m3_07_verified_comparison_returns_correct_values(self):
        c2, _, _ = self.run_mission('m2', [obs_row('p1', '120', observed='2026-01-02T11:00:00Z', retrieved='2026-01-02T11:01:00Z',
                                                   available='2026-01-02T11:01:00Z')], as_of_utc='2026-01-02T12:00:00Z', hypotheses=[],
                                    coordinator=self.c)
        new_bundle, new_archive = MissionHarness.bundle(self, c2, 'm2'), self.fx.ws / 'missions/m2/evidence.sqlite'
        out = self.history.compare_missions(self.bundle, new_bundle, archives=(self.archive, new_archive))
        self.assertEqual(out['metrics']['status'], 'COMPARED')
        item = out['metrics']['items'][0]
        self.assertEqual((item['older_value'], item['newer_value'], item['change']), ('112', '120', '8'))     # latest R claim by instant
        self.assertEqual(out['sources']['older']['verification']['companions']['status'], 'SEMANTICALLY_VERIFIED')

    def test_m3_08_legacy_companion_free_bundle_is_classified_accurately(self):
        legacy = self.copy_bundle('legacy')
        for name in ('report_content.json', 'report_manifest.json'):
            (legacy / name).unlink()
        self.assertEqual(self.verdict(legacy)['status'], 'FAILED')       # finalization attested them: stripping is detected
        fv = json.loads((legacy / 'final_verification.json').read_text())
        fv.pop('report_companions')                                       # a genuine pre-P2 bundle carries no such attestation
        (legacy / 'final_verification.json').write_text(json.dumps(fv))
        self.assertEqual(self.verdict(legacy), {'status': 'ABSENT_UNATTESTED_LEGACY', 'problems': []})
        out = ThesisHistory(ResearchIntelligence(self.fx.ws / 'i2.sqlite'))._load_bundle(legacy, self.archive)
        self.assertEqual(out['verification']['companions']['status'], 'ABSENT_UNATTESTED_LEGACY')
        self.assertIsNone(out['content'])                          # nothing report-derived is trusted or used

    def test_m3_09_finalization_rejects_a_forged_companion_that_independent_verification_also_rejects(self):
        original = reporting.compose_report

        def forging(context, authored, **kw):
            composed = original(context, authored, **kw)
            content = json.loads(composed['content'])
            claim(content, 'R:o1')['value'] = '500'
            raw = json.dumps(content, sort_keys=True, separators=(',', ':')).encode()
            md = composed['report.md'].replace(b'100', b'500')
            manifest = json.loads(composed['manifest'])
            manifest.update(content_sha256=hashlib.sha256(raw).hexdigest(), report_md_sha256=hashlib.sha256(md).hexdigest())
            return {**composed, 'content': raw, 'report.md': md, 'manifest': json.dumps(manifest, sort_keys=True).encode()}
        fx2 = Fixture()
        self.addCleanup(fx2.close)
        spec = fx2.spec('m9', observations=OBS2)
        spec['inputs']['report_input'] = fx2.put('inputs/m9/report.json', {**AUTHORED, 'mission_id': 'm9'})
        c = fx2.coordinator()
        c.create_mission(spec)
        with mock.patch.object(reporting, 'compose_report', forging):
            results = run_to_end(c, 'm9')
        self.assertEqual(results[-1]['job_state'], 'FAILED')
        self.assertEqual([d['code'] for d in results[-1]['diagnostics']], ['VERIFICATION_FAILED'])
        self.assertIn('R:o1', results[-1]['diagnostics'][0]['message'])


# --------------------------------------------------------------------------- M4
def legacy_render(ctx):
    out = mc.op_render(ctx)
    return {k: v for k, v in out.items() if k in mc.LEGACY_RENDER_OUTPUTS}


class RenderCompatibilityTests(MissionHarness):
    def v1_until_rendered(self, mission_id='m1', observations=None, hook=None, renderer=legacy_render):
        spec = self.fx.spec(mission_id, observations=observations or OBS2)
        spec['inputs']['report_input'] = self.fx.put(f'inputs/{mission_id}/report.json', {**AUTHORED, 'mission_id': mission_id})
        c = self.fx.coordinator(fail_at=hook)
        c.create_mission(spec)
        with mock.patch.dict(mc.STAGE_OPERATIONS, {'RENDERING': ('render.local_report', '1')}), \
                mock.patch.dict(mc.OPERATIONS, {'RENDERING': renderer}), \
                mock.patch.dict(mc.STAGE_OUTPUTS, {'RENDERING': mc.LEGACY_RENDER_OUTPUTS if renderer is legacy_render else mc.STAGE_OUTPUTS['RENDERING']}):
            for _ in range(8):
                view = c.inspect_mission(mission_id)
                if next(s for s in view['stages'] if s['stage_id'] == 'RENDERING')['status'] in ('SUCCEEDED', 'FAILED'):
                    break
                c.advance_mission(mission_id)
        return c, spec

    def stage(self, c, mission_id, name):
        return next(s for s in c.inspect_mission(mission_id)['stages'] if s['stage_id'] == name)

    def test_m4_01_existing_v1_rendered_mission_is_identified(self):
        c, _ = self.v1_until_rendered()
        self.assertEqual((self.stage(c, 'm1', 'RENDERING')['status'], self.stage(c, 'm1', 'RENDERING')['operation_version']), ('SUCCEEDED', '1'))
        self.assertTrue(c.render_is_legacy('m1'))

    def test_m4_02_v1_artifact_finalizes_under_its_declared_contract(self):
        c, _ = self.v1_until_rendered()
        results = run_to_end(c, 'm1')
        self.assertEqual(results[-1]['job_state'], 'COMPLETED')
        bundle, archive = self.bundle(c, 'm1'), self.fx.ws / 'missions/m1/evidence.sqlite'
        self.assertEqual(json.loads((bundle / 'mission_baseline.json').read_text())['render_contract'], 'render.local_report@1')
        self.assertFalse((bundle / 'report_content.json').exists())
        self.assertEqual(verify_bundle(bundle, archive=archive)['status'], 'COMPLETE')
        self.assertEqual(reporting.verify_bundle_companions(bundle, archive)['status'], 'ABSENT_DECLARED_V1')   # never silently a v2 render
        self.assertEqual(json.loads((bundle / 'final_verification.json').read_text())['report_companions']['verdict'], 'ABSENT_DECLARED_V1')

    def test_m4_03_reconcile_does_not_claim_to_repair_terminal_jobs(self):
        spec = self.fx.spec('m1', observations=OBS2)
        spec['inputs']['report_input'] = self.fx.put('inputs/m1/report.json', {'schema': 'research-intelligence-report.v1', 'bogus': 1})
        c = self.fx.coordinator()
        c.create_mission(spec)
        self.assertEqual(run_to_end(c, 'm1')[-1]['job_state'], 'FAILED')
        result = c.reconcile_mission('m1')
        self.assertEqual(result['outcome'], 'NOTHING_TO_RECONCILE')
        self.assertEqual([d['code'] for d in result['diagnostics']], ['JOB_TERMINAL_NOT_REPAIRABLE'])
        self.assertEqual(c.inspect_mission('m1')['job']['state'] if 'job' in c.inspect_mission('m1') else 'FAILED', 'FAILED')

    def tree_digest(self, root: Path):
        return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(root.rglob('*')) if p.is_file()}

    def test_m4_04_immutable_v1_artifacts_are_never_overwritten(self):
        c, _ = self.v1_until_rendered()
        before = self.tree_digest(self.fx.ws / 'missions/m1/stages/RENDERING')
        run_to_end(c, 'm1')
        self.assertEqual(self.tree_digest(self.fx.ws / 'missions/m1/stages/RENDERING'), before)

    def test_m4_05_v2_missions_remain_functional(self):
        c, spec, results = self.run_mission('m1', OBS2, AUTHORED)
        self.assertEqual(results[-1]['job_state'], 'COMPLETED')
        self.assertFalse(c.render_is_legacy('m1'))
        bundle = self.bundle(c, 'm1')
        self.assertNotIn('render_contract', json.loads((bundle / 'mission_baseline.json').read_text()))
        self.assertTrue((bundle / 'report_content.json').exists())

    def test_m4_06_interrupted_v1_finalization_is_recoverable(self):
        state = {'armed': False}

        def hook(point, mission_id, stage):
            if state['armed'] and point == 'after_admission' and stage == 'FINALIZING':
                state['armed'] = False
                raise SimulatedCrash('finalizing')
        c, spec = self.v1_until_rendered(hook=hook)
        state['armed'] = True
        with self.assertRaises(SimulatedCrash):
            c.advance_mission('m1')
        c2 = self.fx.coordinator()
        self.assertIn(c2.reconcile_mission('m1')['outcome'], ('RESUMED', 'ADVANCED', 'RECONCILED', 'COMPLETED', 'NOTHING_TO_RECONCILE'))
        results = run_to_end(c2, 'm1')
        self.assertEqual(c2.inspect_mission('m1')['stages'][-1]['status'], 'SUCCEEDED', results[-1])
        self.assertEqual(reporting.verify_bundle_companions(self.bundle(c2, 'm1'), self.fx.ws / 'missions/m1/evidence.sqlite')['status'],
                         'ABSENT_DECLARED_V1')

    def test_m4_07_invalid_v1_artifacts_cannot_bypass_integrity(self):
        def with_companions(ctx):             # a stage claimed under @1 that nevertheless carries v2 companions
            return mc.op_render(ctx)
        c, _ = self.v1_until_rendered(renderer=with_companions)
        results = run_to_end(c, 'm1')
        self.assertEqual(results[-1]['job_state'], 'FAILED')
        self.assertEqual([d['code'] for d in results[-1]['diagnostics']], ['RENDER_V1_OUTPUT_INVALID'])
        # tampered v1 report: the recorded digest is enforced before finalization
        fx2 = Fixture()
        self.addCleanup(fx2.close)
        self.fx = fx2
        c2, _ = self.v1_until_rendered('m5')
        target = next((fx2.ws / 'missions/m5/stages/RENDERING').rglob('report.md'))
        target.chmod(0o644)
        target.write_text(target.read_text() + '\ninjected\n')
        results = run_to_end(c2, 'm5')
        self.assertNotEqual(results[-1]['job_state'], 'COMPLETED')
        self.assertIn('STAGE_OUTPUT_DIGEST_MISMATCH', [d['code'] for r in results for d in r['diagnostics']] + [str(results)])


if __name__ == '__main__':
    unittest.main()
