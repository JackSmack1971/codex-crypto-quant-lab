"""P1-C tests: durable preregistration and single-use holdout governance. Offline and deterministic."""
import copy
import sqlite3
import tempfile
import threading
import unittest
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from research_lab.experiment_registry import ExperimentRegistry, RegistryError, dataset_digest
from research_lab.experiments import ExperimentPlan, evaluate_frozen, freeze_candidate
from research_lab.validation import LabelWindow
from tests.p1_support import SimulatedCrash, StepClock

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
N = 12
JOIN = 20


def windows(n=N):
    iso = lambda d: d.isoformat().replace('+00:00', 'Z')  # noqa: E731
    return [LabelWindow(iso(T0 + timedelta(days=i)), iso(T0 + timedelta(days=i, hours=12))) for i in range(n)]


def candidates():
    return {'trend': [0.01, 0.02, -0.01, 0.03, 0.02, 0.01, 0.0, 0.01, -0.02, 0.05, -0.01, 0.02],
            'flat': [0.001] * N}


PLAN = ExperimentPlan('exp-1', train_end=6, holdout_start=9, embargo=0)
DIGEST = dataset_digest(windows(), candidates())   # binds the full dataset, holdout values included
KW = dict(dataset_id='ds-btc-1h', dataset_version='2026.01')


def crash_at(point):
    state = {'armed': True}

    def hook(p, experiment_id):
        if state['armed'] and p == point:
            state['armed'] = False
            raise SimulatedCrash(point)
    return hook


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'experiments.sqlite'
        self.clock = StepClock()
        self.reg = ExperimentRegistry(self.path, clock=self.clock)
        self.w, self.c = windows(), candidates()

    def train_view(self, plan=PLAN, c=None):
        return {k: v[:plan.train_end] for k, v in (c or self.c).items()}

    def register(self, reg=None, plan=PLAN, **over):
        kw = dict(**KW, evaluation_method='holdout mean return of the frozen candidate', costs={'bps': 5}, dataset_digest_hex=DIGEST)
        kw.update(over)
        return (reg or self.reg).register(plan, self.w, self.train_view(plan), **kw)

    def evaluate(self, reg=None, plan=PLAN, w=None, c=None, **over):
        kw = dict(KW)
        kw.update(over)
        return (reg or self.reg).evaluate(plan.experiment_id, plan, w or self.w, c or self.c, **kw)

    def code(self, fn):
        with self.assertRaises(RegistryError) as cm:
            fn()
        return cm.exception


class RegistrationTests(Base):
    def test_t15_frozen_candidate_is_registered_before_any_holdout_access(self):
        info = self.register()
        self.assertEqual((info['created'], info['status']), (True, 'REGISTERED'))
        summary = self.reg.summary('exp-1')
        self.assertEqual((summary['status'], summary['holdout_exposure_count'], summary['holdout_untouched']), ('REGISTERED', 0, True))
        self.assertEqual(info['freeze_digest'], freeze_candidate(PLAN, self.w, self.c)['freeze_digest'])
        stored = self.reg.get('exp-1')
        self.assertEqual((stored['schema_version'], stored['dataset_id'], stored['dataset_version'], stored['selected']),
                         ('experiment-registration.v1', 'ds-btc-1h', '2026.01', 'trend'))
        self.assertEqual((stored['boundaries']['train_end'], stored['boundaries']['holdout_start'], stored['boundaries']['embargo']), (6, 9, 0))
        self.assertEqual(stored['universe'], ['flat', 'trend'])
        self.assertEqual(stored['costs'], {'bps': 5})
        self.assertIn('windows_digest', stored)
        self.assertTrue(stored['registered_utc'].endswith('Z'))

    def test_registration_is_durable_across_instances(self):
        self.register()
        reopened = ExperimentRegistry(self.path)
        self.assertEqual(reopened.summary('exp-1')['status'], 'REGISTERED')
        self.assertEqual(self.evaluate(reg=reopened)['status'], 'CONSUMED')

    def test_registration_rejects_holdout_or_validation_returns(self):
        full = self.c
        err = self.code(lambda: self.reg.register(PLAN, self.w, full, **KW, evaluation_method='m', dataset_digest_hex=DIGEST))
        self.assertEqual(err.code, 'EXPERIMENT_INVALID')                      # selection can never see post-train values
        short = {k: v[:5] for k, v in full.items()}
        self.assertEqual(self.code(lambda: self.reg.register(PLAN, self.w, short, **KW, evaluation_method='m', dataset_digest_hex=DIGEST)).code, 'EXPERIMENT_INVALID')
        self.assertEqual(self.reg.summary('exp-1')['status'], 'UNREGISTERED')

    def test_identical_reregistration_is_idempotent_and_conflict_is_rejected(self):
        first = self.register()
        again = self.register()
        self.assertEqual((again['created'], again['registration_digest']), (False, first['registration_digest']))
        for change in (dict(evaluation_method='different'), dict(costs={'bps': 6}), dict(dataset_version='2026.02'),
                       dict(dataset_digest_hex='a' * 64)):
            self.assertEqual(self.code(lambda: self.register(**change)).code, 'EXPERIMENT_REGISTRATION_CONFLICT', change)
        other_plan = ExperimentPlan('exp-1', train_end=5, holdout_start=9, embargo=0)
        self.assertEqual(self.code(lambda: self.register(plan=other_plan)).code, 'EXPERIMENT_REGISTRATION_CONFLICT')
        distinct = ExperimentPlan('exp-1b', train_end=5, holdout_start=9, embargo=0)       # modified experiment => new identity
        self.assertTrue(self.register(plan=distinct)['created'])
        self.assertEqual(self.reg.summary('exp-1')['freeze_digest'], first['freeze_digest'])

    def test_invalid_experiments_are_rejected_at_registration(self):
        bad_plans = [ExperimentPlan('x', 6, 9, -1), ExperimentPlan('', 6, 9), ExperimentPlan('x', 1, 9), ExperimentPlan('x', 6, 6), ExperimentPlan('x', 6, N)]
        for plan in bad_plans:
            err = self.code(lambda: self.reg.register(plan, self.w, self.train_view(plan) if plan.train_end <= N else {}, **KW, evaluation_method='m', dataset_digest_hex=DIGEST))
            self.assertEqual(err.code, 'EXPERIMENT_INVALID')
        for bad in (float('nan'), float('inf'), '0.1', None, True):
            view = self.train_view()
            view['trend'][2] = bad
            self.assertEqual(self.code(lambda: self.reg.register(PLAN, self.w, view, **KW, evaluation_method='m', dataset_digest_hex=DIGEST)).code, 'EXPERIMENT_INVALID', repr(bad))
        unordered = list(reversed(self.w))
        self.assertEqual(self.code(lambda: self.reg.register(PLAN, unordered, self.train_view(), **KW, evaluation_method='m', dataset_digest_hex=DIGEST)).code, 'EXPERIMENT_INVALID')
        for blank in ('', '  '):
            self.assertEqual(self.code(lambda: self.register(dataset_id=blank)).code, 'EXPERIMENT_INVALID')
        with self.assertRaises(Exception):
            LabelWindow('2026-01-02T00:00:00Z', '2026-01-01T00:00:00Z')            # negative label interval is unconstructible
        self.assertEqual(self.code(lambda: self.reg.register('plan', self.w, {}, **KW, evaluation_method='m', dataset_digest_hex=DIGEST)).code, 'EXPERIMENT_INVALID')


class GovernanceTests(Base):
    def test_t16_unregistered_candidate_cannot_consume_a_governed_holdout(self):
        err = self.code(lambda: self.evaluate())
        self.assertEqual(err.code, 'EXPERIMENT_UNREGISTERED')
        # a caller-supplied digest cannot create a registration: the registry has no such entry point
        freeze = freeze_candidate(PLAN, self.w, self.c)
        self.assertEqual(self.reg.summary('exp-1')['status'], 'UNREGISTERED')
        self.assertFalse(hasattr(self.reg, 'evaluate_with_digest'))
        self.assertEqual(self.code(lambda: self.reg.evaluate('exp-1', PLAN, self.w, self.c, **KW)).code, 'EXPERIMENT_UNREGISTERED')
        self.assertTrue(freeze['freeze_digest'])

    def test_registered_evaluation_matches_the_unmodified_calculation(self):
        self.register()
        outcome = self.evaluate()
        direct = evaluate_frozen(freeze_candidate(PLAN, self.w, self.c), PLAN, self.w, self.c,
                                 registered_digest=freeze_candidate(PLAN, self.w, self.c)['freeze_digest'])
        self.assertEqual(outcome['result'], direct)
        self.assertEqual((outcome['status'], outcome['reused'], outcome['independent_evaluation'], outcome['holdout_exposure_count']),
                         ('CONSUMED', False, True, 1))
        self.assertTrue(any('local registry' in x for x in outcome['limitations']))

    def test_t17_modified_definitions_are_rejected_before_the_holdout_is_touched(self):
        self.register()
        mutated_train = copy.deepcopy(self.c)
        mutated_train['flat'][1] = 0.5                              # training partition edit => different freeze
        wrong_windows = windows()
        wrong_windows[10] = LabelWindow(wrong_windows[10].start_utc, '2026-01-12T20:00:00Z')
        extra = {**self.c, 'new': [0.0] * N}
        cases = [
            (dict(c=mutated_train), ['training_partition']),
            (dict(plan=ExperimentPlan('exp-1', 6, 9, 1)), ['plan']),
            (dict(plan=ExperimentPlan('exp-1', 6, 10, 0)), ['plan']),
            (dict(w=wrong_windows), ['label_windows']),
            (dict(dataset_id='other'), ['dataset_identity']),
            (dict(dataset_version='2099'), ['dataset_identity']),
            (dict(c=extra), ['candidate_universe']),
        ]
        for kwargs, fields in cases:
            err = self.code(lambda: self.evaluate(**kwargs))
            self.assertEqual((err.code, err.fields), ('EXPERIMENT_MODIFIED', fields), kwargs)
        summary = self.reg.summary('exp-1')
        self.assertEqual((summary['status'], summary['holdout_exposure_count'], summary['holdout_untouched']), ('REGISTERED', 0, True))

    def test_t17_pinned_dataset_digest_detects_holdout_substitution(self):
        self.register()
        swapped = copy.deepcopy(self.c)
        swapped['trend'][10] = 9.99                                   # holdout value changed after registration
        err = self.code(lambda: self.evaluate(c=swapped))
        self.assertEqual((err.code, err.fields), ('EXPERIMENT_MODIFIED', ['dataset_digest']))
        self.assertEqual(self.reg.summary('exp-1')['holdout_exposure_count'], 0)
        self.assertEqual(self.evaluate()['status'], 'CONSUMED')

    def test_t18_repeated_evaluation_is_the_same_stored_result_not_a_fresh_experiment(self):
        self.register()
        first = self.evaluate()
        again = self.evaluate()
        self.assertEqual((again['reused'], again['independent_evaluation'], again['holdout_exposure_count']), (True, False, 1))
        self.assertEqual((again['result'], again['result_digest']), (first['result'], first['result_digest']))
        self.assertFalse(self.reg.summary('exp-1')['holdout_untouched'])
        # even a different holdout is not evaluated again: the stored result is returned unchanged
        other = copy.deepcopy(self.c)
        other['trend'][10] = 5.0
        self.assertEqual(self.evaluate(c=other)['result'], first['result'])
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM claims').fetchone(), (1,))

    def test_stored_result_tampering_is_detected(self):
        self.register()
        self.evaluate()
        with sqlite3.connect(self.path) as db:
            db.execute("UPDATE experiments SET result_json=replace(result_json,'holdout_mean_return','holdout_mean_returnX')")
        self.assertEqual(self.code(lambda: self.evaluate()).code, 'EXPERIMENT_RESULT_TAMPERED')

    def test_t19_interrupted_evaluation_stays_uncertain_and_never_untouched(self):
        for point in ('after_claim', 'before_result_commit'):
            path = Path(self.tmp.name) / f'{point}.sqlite'
            crashing = ExperimentRegistry(path, clock=self.clock, fail_at=crash_at(point))
            crashing.register(PLAN, self.w, self.train_view(), **KW, evaluation_method='m', dataset_digest_hex=DIGEST)
            with self.assertRaises(SimulatedCrash):
                crashing.evaluate('exp-1', PLAN, self.w, self.c, **KW)
            fresh = ExperimentRegistry(path, clock=self.clock)
            summary = fresh.summary('exp-1')
            self.assertEqual((summary['status'], summary['holdout_exposure_count'], summary['holdout_untouched'],
                              summary['independent_evaluation']), ('EVALUATING', 1, False, False), point)
            self.assertEqual(self.code(lambda: fresh.evaluate('exp-1', PLAN, self.w, self.c, **KW)).code, 'HOLDOUT_CLAIM_UNCERTAIN')
            self.assertEqual(fresh.summary('exp-1')['holdout_exposure_count'], 1)           # the refused retry added no exposure

    def test_t19_reconcile_complete_records_a_recovered_non_independent_result(self):
        crashing = ExperimentRegistry(self.path, clock=self.clock, fail_at=crash_at('before_result_commit'))
        self.register(reg=crashing)
        with self.assertRaises(SimulatedCrash):
            self.evaluate(reg=crashing)
        fresh = ExperimentRegistry(self.path, clock=self.clock)
        done = fresh.reconcile_evaluation('exp-1', 'COMPLETE', plan=PLAN, windows=self.w, candidates=self.c, **KW)
        self.assertEqual((done['status'], done['recovered_after_interruption'], done['independent_evaluation'], done['holdout_exposure_count']),
                         ('CONSUMED', True, False, 2))
        direct = evaluate_frozen(freeze_candidate(PLAN, self.w, self.c), PLAN, self.w, self.c,
                                 registered_digest=freeze_candidate(PLAN, self.w, self.c)['freeze_digest'])
        self.assertEqual(done['result'], direct)
        again = fresh.evaluate('exp-1', PLAN, self.w, self.c, **KW)
        self.assertEqual((again['reused'], again['independent_evaluation']), (True, False))

    def test_t19_reconcile_invalidate_and_misuse(self):
        crashing = ExperimentRegistry(self.path, clock=self.clock, fail_at=crash_at('after_claim'))
        self.register(reg=crashing)
        self.assertEqual(self.code(lambda: crashing.reconcile_evaluation('exp-1', 'COMPLETE')).code, 'EXPERIMENT_NOT_UNCERTAIN')
        with self.assertRaises(SimulatedCrash):
            self.evaluate(reg=crashing)
        fresh = ExperimentRegistry(self.path, clock=self.clock)
        self.assertEqual(self.code(lambda: fresh.reconcile_evaluation('exp-1', 'MAYBE')).code, 'EXPERIMENT_INVALID')
        self.assertEqual(self.code(lambda: fresh.reconcile_evaluation('exp-1', 'INVALIDATE')).code, 'EXPERIMENT_INVALID')   # reason required
        self.assertEqual(self.code(lambda: fresh.reconcile_evaluation('exp-1', 'COMPLETE')).code, 'EXPERIMENT_INVALID')     # inputs required
        modified = copy.deepcopy(self.c)
        modified['flat'][0] = 1.0
        self.assertEqual(self.code(lambda: fresh.reconcile_evaluation('exp-1', 'COMPLETE', plan=PLAN, windows=self.w, candidates=modified, **KW)).code,
                         'EXPERIMENT_MODIFIED')
        out = fresh.reconcile_evaluation('exp-1', 'INVALIDATE', reason='process crashed after holdout exposure')
        self.assertEqual((out['status'], out['invalidated_reason']), ('INVALIDATED', 'process crashed after holdout exposure'))
        self.assertEqual(self.code(lambda: self.evaluate(reg=fresh)).code, 'EXPERIMENT_INVALIDATED')
        self.assertFalse(fresh.summary('exp-1')['holdout_untouched'])
        self.assertFalse(hasattr(fresh, 'reset') or hasattr(fresh, 'release_claim'))     # no path back to REGISTERED

    def test_consumed_holdout_can_never_be_presented_as_untouched(self):
        self.register()
        self.evaluate()
        s = self.reg.summary('exp-1')
        self.assertEqual((s['holdout_untouched'], s['status']), (False, 'CONSUMED'))
        self.assertFalse(self.register()['created'])                      # re-registering cannot reset state
        self.assertEqual(self.reg.summary('exp-1')['status'], 'CONSUMED')

    def test_g09_racing_evaluators_yield_exactly_one_independent_evaluation(self):
        self.register()
        regs = [ExperimentRegistry(self.path, clock=self.clock) for _ in range(6)]
        barrier = threading.Barrier(len(regs))
        results = []

        def work(reg):
            barrier.wait(JOIN)
            try:
                results.append(('ok', self.evaluate(reg=reg)))
            except RegistryError as exc:
                results.append(('err', exc.code))
        threads = [threading.Thread(target=work, args=(r,), daemon=True) for r in regs]
        for t in threads:
            t.start()
        for t in threads:
            t.join(JOIN)
        fresh_runs = [r for kind, r in results if kind == 'ok' and not r['reused']]
        self.assertEqual(len(fresh_runs), 1)
        self.assertTrue(fresh_runs[0]['independent_evaluation'])
        self.assertTrue(all(kind == 'ok' or r in {'HOLDOUT_CLAIM_LOST', 'HOLDOUT_CLAIM_UNCERTAIN'} for kind, r in results))
        for kind, r in results:
            if kind == 'ok' and r['reused']:
                self.assertFalse(r['independent_evaluation'])
        self.assertEqual(self.reg.summary('exp-1')['holdout_exposure_count'], 1)

    def test_summary_of_unknown_experiment_is_explicit(self):
        self.assertEqual(self.reg.summary('ghost'), {'experiment_id': 'ghost', 'status': 'UNREGISTERED'})


class HoldoutIntegrityTests(Base):
    """Regressions for the independent review of P1-C."""

    def test_holdout_is_single_use_per_dataset_not_per_experiment_id(self):
        other = ExperimentPlan('exp-2', train_end=5, holdout_start=9, embargo=0)        # same holdout, different id/split
        self.register()
        self.register(plan=other)                                                       # both registered while untouched
        self.evaluate()
        err = self.code(lambda: self.evaluate(plan=other))
        self.assertEqual(err.code, 'HOLDOUT_ALREADY_EXPOSED')                          # second experiment is not independent
        self.assertEqual(self.reg.summary('exp-2')['holdout_exposure_count'], 0)
        third = ExperimentPlan('exp-3', train_end=4, holdout_start=9, embargo=0)
        self.assertEqual(self.code(lambda: self.register(plan=third)).code, 'HOLDOUT_ALREADY_EXPOSED')   # nor can a new id be registered

    def test_overlapping_or_relabelled_holdouts_are_not_different_holdouts(self):
        """Repair H1: every holdout is the dataset tail, so a shorter tail or a new dataset_version overlaps."""
        self.register()
        self.evaluate()
        later = ExperimentPlan('exp-4', train_end=6, holdout_start=10, embargo=0)
        self.assertEqual(self.code(lambda: self.register(plan=later)).code, 'HOLDOUT_ALREADY_EXPOSED')
        relabel = self.code(lambda: self.register(plan=ExperimentPlan('exp-5', 6, 9, 0), dataset_version='2027.01'))
        self.assertEqual(relabel.code, 'HOLDOUT_ALREADY_EXPOSED')

    def test_holdout_values_are_bound_at_registration(self):
        self.register()
        swapped = copy.deepcopy(self.c)
        swapped['trend'][10] = 9.99
        self.assertEqual(self.code(lambda: self.evaluate(c=swapped)).fields, ['dataset_digest'])
        self.assertEqual(self.reg.summary('exp-1')['holdout_exposure_count'], 0)
        for bad in ('', 'xyz', 'A' * 64, 'a' * 63):
            self.assertEqual(self.code(lambda: self.register(dataset_digest_hex=bad)).code, 'EXPERIMENT_INVALID', bad)

    def test_non_finite_holdout_value_is_rejected_before_the_claim(self):
        self.register()
        for bad in (float('nan'), float('inf'), None, '1'):
            broken = copy.deepcopy(self.c)
            broken['trend'][10] = bad
            self.assertEqual(self.code(lambda: self.evaluate(c=broken)).code, 'EXPERIMENT_INVALID', repr(bad))
        summary = self.reg.summary('exp-1')
        self.assertEqual((summary['status'], summary['holdout_exposure_count']), ('REGISTERED', 0))   # nothing was burned
        short = copy.deepcopy(self.c)
        short['flat'] = short['flat'][:-1]
        self.assertEqual(self.code(lambda: self.evaluate(c=short)).code, 'EXPERIMENT_INVALID')

    def test_unevaluable_plan_cannot_be_registered(self):
        plan = ExperimentPlan('exp-bad', train_end=10, holdout_start=11, embargo=5)
        err = self.code(lambda: self.reg.register(plan, self.w, {k: v[:10] for k, v in self.c.items()}, **KW,
                                                  evaluation_method='m', dataset_digest_hex=DIGEST))
        self.assertEqual((err.code, 'validation' in err.message), ('EXPERIMENT_INVALID', True))
        self.assertEqual(self.reg.summary('exp-bad')['status'], 'UNREGISTERED')

    def test_malformed_request_types_are_registry_errors_not_tracebacks(self):
        self.register()
        for bad in (list(self.c.values()), None, {}, {'trend': 'abc'}, {'trend': None}):
            self.assertEqual(self.code(lambda: self.reg.evaluate('exp-1', PLAN, self.w, bad, **KW)).code, 'EXPERIMENT_INVALID', repr(bad)[:30])
        self.assertEqual(self.code(lambda: self.reg.evaluate('exp-1', 'plan', self.w, self.c, **KW)).code, 'EXPERIMENT_INVALID')
        self.assertEqual(self.code(lambda: self.reg.evaluate('exp-1', PLAN, None, self.c, **KW)).code, 'EXPERIMENT_INVALID')
        self.assertEqual(self.reg.summary('exp-1')['holdout_exposure_count'], 0)

    def test_rejected_reconciliation_does_not_inflate_exposure(self):
        crashing = ExperimentRegistry(self.path, clock=self.clock, fail_at=crash_at('after_claim'))
        self.register(reg=crashing)
        with self.assertRaises(SimulatedCrash):
            self.evaluate(reg=crashing)
        fresh = ExperimentRegistry(self.path, clock=self.clock)
        bad = copy.deepcopy(self.c)
        bad['trend'][10] = float('nan')
        for _ in range(3):
            self.assertEqual(self.code(lambda: fresh.reconcile_evaluation('exp-1', 'COMPLETE', plan=PLAN, windows=self.w, candidates=bad, **KW)).code,
                             'EXPERIMENT_INVALID')
        self.assertEqual(fresh.summary('exp-1')['holdout_exposure_count'], 1)
        done = fresh.reconcile_evaluation('exp-1', 'COMPLETE', plan=PLAN, windows=self.w, candidates=self.c, **KW)
        self.assertEqual((done['holdout_exposure_count'], done['independent_evaluation']), (2, False))


if __name__ == '__main__':
    unittest.main()
