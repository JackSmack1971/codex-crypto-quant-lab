"""P1-D end-to-end and compatibility tests: coordinator + registry + P0 integrity + JobStore + boundaries."""
import ast
import json
import sqlite3
import subprocess
import sys
import unittest
from pathlib import Path

from research_lab.archive import EvidenceArchive
from research_lab.completion import verify
from research_lab.experiment_registry import ExperimentRegistry
from research_lab.experiments import ExperimentPlan
from research_lab.jobs import NEXT, JobStore
from research_lab.core import ContractError
from tests.p1_support import Fixture, SimulatedCrash, obs_row, run_to_end
from tests.test_experiment_registry import DIGEST, KW, PLAN, candidates, windows

ROOT = Path(__file__).resolve().parents[1]
NEW_MODULES = ('research_lab/mission_spec.py', 'research_lab/mission_coordinator.py', 'research_lab/experiment_registry.py',
               'scripts/mission.py')


class Base(unittest.TestCase):
    def setUp(self):
        self.fx = Fixture()
        self.addCleanup(self.fx.close)
        self.c = self.fx.coordinator()

    def stage_json(self, spec, stage, name):
        (directory,) = list((self.fx.ws / spec['output_dir'] / 'stages' / stage).iterdir())
        return json.loads((directory / name).read_text())


class ExperimentLinkTests(Base):
    """The mission pins registry facts by digest, so ANALYZING is a pure function of its pinned inputs."""

    def registry(self):
        return ExperimentRegistry(self.fx.ws / 'experiments.sqlite', clock=self.fx.clock)

    def register(self, registry=None):
        return (registry or self.registry()).register(PLAN, windows(), {k: v[:6] for k, v in candidates().items()}, **KW,
                                                     evaluation_method='holdout mean', dataset_digest_hex=DIGEST)

    def evaluate(self):
        return self.registry().evaluate('exp-1', PLAN, windows(), candidates(), **KW)

    def mission(self, registration_digest, result_digest=None, mission_id='m1'):
        link = {'experiment_id': 'exp-1', 'registration_digest': registration_digest}
        if result_digest:
            link['result_digest'] = result_digest
        spec = self.fx.spec(mission_id, extra={'experiment': link})
        self.c.create_mission(spec)
        return spec, run_to_end(self.c, mission_id)

    def test_unregistered_experiment_blocks_the_stage_instead_of_being_assumed(self):
        spec, results = self.mission('a' * 64)
        view = self.c.inspect_mission('m1')
        self.assertEqual((results[-1]['outcome'], results[-1]['stage'], view['job']['state']), ('BLOCKED', 'ANALYZING', 'RUNNING'))
        self.assertEqual(results[-1]['diagnostics'][0]['code'], 'REQUIRED_EVIDENCE_UNAVAILABLE')

    def test_registration_digest_must_match_the_pin(self):
        self.register()
        spec, results = self.mission('b' * 64)
        self.assertEqual((results[-1]['outcome'], results[-1]['diagnostics'][0]['code']), ('FAILED', 'STAGE_PRECONDITION_FAILED'))

    def test_registered_untouched_holdout_without_a_pinned_result_is_not_consumed_by_the_mission(self):
        info = self.register()
        spec, _ = self.mission(info['registration_digest'])
        analysis = self.stage_json(spec, 'ANALYZING', 'analysis.json')
        self.assertEqual(analysis['experiment']['holdout_result'], 'NOT_REFERENCED')
        self.assertTrue(any('no holdout result is referenced' in x for x in analysis['limitations']))
        self.assertEqual(self.registry().summary('exp-1')['holdout_exposure_count'], 0)
        self.assertEqual(self.c.inspect_mission('m1')['job']['state'], 'COMPLETED')

    def test_pinned_consumed_result_is_referenced_with_its_independence_status(self):
        info = self.register()
        result = self.evaluate()
        spec, _ = self.mission(info['registration_digest'], result['result_digest'])
        exp = self.stage_json(spec, 'ANALYZING', 'analysis.json')['experiment']
        self.assertEqual((exp['holdout_result'], exp['independent_evaluation'], exp['holdout_exposure_count']), ('REFERENCED', True, 1))
        view = self.c.inspect_mission('m1')
        self.assertEqual((view['job']['state'], view['final']['research_state']), ('COMPLETED', 'EVALUABLE'))   # never VALIDATED

    def test_pinned_result_not_yet_recorded_blocks_then_resumes_when_it_exists(self):
        info = self.register()
        digest_after = None
        spec, results = self.mission(info['registration_digest'], 'c' * 64)
        self.assertEqual((results[-1]['outcome'], results[-1]['diagnostics'][0]['code']), ('BLOCKED', 'REQUIRED_EVIDENCE_UNAVAILABLE'))
        # a different result digest than the pin => precondition failure, not a silent substitution
        self.evaluate()
        again = self.c.advance_mission('m1')
        self.assertEqual(again['diagnostics'][0]['code'], 'STAGE_PRECONDITION_FAILED')

    def test_pinned_result_arrival_unblocks_the_mission(self):
        info = self.register()
        # compute the digest the evaluation will produce in a scratch registry, pin it, then evaluate for real
        scratch = ExperimentRegistry(self.fx.ws / 'scratch.sqlite', clock=self.fx.clock)
        self.register(scratch)
        expected = scratch.evaluate('exp-1', PLAN, windows(), candidates(), **KW)['result_digest']
        spec, results = self.mission(info['registration_digest'], expected)
        self.assertEqual(results[-1]['outcome'], 'BLOCKED')
        self.evaluate()
        final = run_to_end(self.c, 'm1')
        self.assertEqual(final[-1]['job_state'], 'COMPLETED')

    def test_analyzing_replay_is_independent_of_live_registry_state(self):
        # crash after ANALYZING wrote its artifact; the experiment is then consumed; reconcile must still adopt it
        info = self.register()
        spec = self.fx.spec(extra={'experiment': {'experiment_id': 'exp-1', 'registration_digest': info['registration_digest']}})
        calls = {'n': 0}

        def hook(point, mission_id, stage):
            if point == 'after_artifact_write' and stage == 'ANALYZING' and not calls['n']:
                calls['n'] += 1
                raise SimulatedCrash(point)
        c = self.fx.coordinator(fail_at=hook)
        c.create_mission(spec)
        with self.assertRaises(SimulatedCrash):
            run_to_end(c, 'm1')
        self.evaluate()                                           # registry state changes while the mission is down
        recovered = self.fx.coordinator().reconcile_mission('m1')
        self.assertEqual((recovered['reconciliation']['decision'], recovered['reconciliation']['classification']),
                         ('ADOPTED_VERIFIED_OUTPUT', 'COMPLETE'))
        self.assertEqual(run_to_end(self.fx.coordinator(), 'm1')[-1]['job_state'], 'COMPLETED')

    def test_spec_requires_a_pinned_registration_digest(self):
        from research_lab.mission_spec import MissionError, validate_spec
        for link in ({'experiment_id': 'exp-1'}, {'experiment_id': 'exp-1', 'registration_digest': 'xyz'},
                     {'experiment_id': 'exp-1', 'registration_digest': 'a' * 64, 'result_digest': 'A' * 64},
                     {'experiment_id': 'exp-1', 'registration_digest': 'a' * 64, 'other': 1}):
            with self.assertRaises(MissionError):
                validate_spec(self.fx.spec(extra={'experiment': link}))


class P0InteractionTests(Base):
    def test_non_finite_observation_value_is_rejected_before_any_state_is_trusted(self):
        for bad in ('NaN', 'Infinity', 'abc'):
            fx = Fixture()
            self.addCleanup(fx.close)
            c = fx.coordinator()
            c.create_mission(fx.spec(observations=[obs_row('o1', bad)]))
            run_to_end(c, 'm1')
            view = c.inspect_mission('m1')
            self.assertEqual((view['job']['state'], view['stages'][1]['failure_code']), ('FAILED', 'STAGE_PRECONDITION_FAILED'), bad)

    def test_archive_tampering_between_stages_is_caught_by_p0_integrity_checks(self):
        spec = self.fx.spec()
        self.c.create_mission(spec)
        for _ in range(4):
            self.c.advance_mission('m1')                                 # through ANALYZING
        archive = self.fx.ws / spec['output_dir'] / 'evidence.sqlite'
        with sqlite3.connect(archive) as db:
            db.execute("UPDATE observations SET sha256='0' WHERE id='o1'")
        result = self.c.advance_mission('m1')                            # VERIFYING
        view = self.c.inspect_mission('m1')
        self.assertEqual((result['outcome'], view['job']['state']), ('FAILED', 'FAILED'))
        self.assertEqual(view['stages'][4]['failure_code'], 'VERIFICATION_FAILED')

    def test_independent_verification_of_the_produced_bundle_matches_the_coordinator(self):
        spec = self.fx.spec()
        self.c.create_mission(spec)
        run_to_end(self.c, 'm1')
        final = self.c.inspect_mission('m1')['final']
        bundle, archive = self.fx.ws / final['bundle_dir'], self.fx.ws / spec['output_dir'] / 'evidence.sqlite'
        proc = subprocess.run([sys.executable, str(ROOT / 'scripts' / 'verify_mission.py'), str(bundle), '--archive', str(archive)],
                              capture_output=True, text=True, timeout=120, cwd=ROOT)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(json.loads(proc.stdout)['status'], 'COMPLETE')
        # and without the archive the strict v2 bundle must fail closed rather than pass unverified
        self.assertEqual(verify(bundle)['status'], 'INCOMPLETE')
        tampered = bundle / 'evidence.json'
        tampered.write_text(tampered.read_text().replace('o1', 'o9'))
        self.assertEqual(verify(bundle, archive=archive)['status'], 'INCOMPLETE')

    def test_cli_subprocess_runs_a_mission_offline(self):
        spec = self.fx.spec()
        (self.fx.ws / 'spec.json').write_text(json.dumps(spec))
        run = lambda *a: subprocess.run([sys.executable, str(ROOT / 'scripts' / 'mission.py'), '--workspace', str(self.fx.ws), *a],  # noqa: E731
                                        capture_output=True, text=True, timeout=120, cwd=ROOT)
        self.assertEqual(run('create', str(self.fx.ws / 'spec.json')).returncode, 0)
        done = run('advance', 'm1', '--all')
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertEqual(json.loads(done.stdout)['final']['outcome'], 'COMPLETED')
        status = run('status', 'm1')
        self.assertEqual(json.loads(status.stdout)['final']['research_state'], 'EVALUABLE')
        bad = run('status', 'ghost')
        self.assertEqual((bad.returncode, json.loads(bad.stdout)['error']['code']), (2, 'MISSION_NOT_FOUND'))


class JobStoreCompatibilityTests(Base):
    def test_g01_jobstore_public_contract_unchanged(self):
        self.assertEqual(NEXT, {'NEW': {'RUNNING', 'CANCELLED'}, 'RUNNING': {'COMPLETED', 'FAILED', 'INTERRUPTED', 'CANCELLED', 'BLOCKED'},
                                'INTERRUPTED': {'RECONCILING', 'CANCELLED'}, 'RECONCILING': {'RUNNING', 'FAILED', 'BLOCKED'},
                                'COMPLETED': set(), 'FAILED': set(), 'CANCELLED': set(), 'BLOCKED': set()})
        store = JobStore(self.fx.ws / 'standalone.sqlite')
        store.create('j', {'a': 1}, '2026-01-01T00:00:00Z')
        self.assertEqual(store.get('j')['state'], 'NEW')
        with self.assertRaises(ContractError):
            store.move('j', 'NEW', 'COMPLETED', '2026-01-01T00:00:01Z', result={})
        self.assertEqual(store.move('j', 'NEW', 'RUNNING', '2026-01-01T00:00:01Z'), 1)
        with self.assertRaises(ContractError):
            store.move('j', 'NEW', 'RUNNING', '2026-01-01T00:00:02Z')           # stale
        with self.assertRaises(ContractError):
            store.move('j', 'RUNNING', 'COMPLETED', '2026-01-01T00:00:02Z')     # COMPLETED needs a result
        self.assertEqual(store.reconcile_candidates(), ['j'])
        store.move('j', 'RUNNING', 'COMPLETED', '2026-01-01T00:00:03Z', result={'ok': True})
        self.assertEqual(store.get('j')['state'], 'COMPLETED')

    def test_coordinator_missions_are_ordinary_jobstore_rows(self):
        self.c.create_mission(self.fx.spec())
        self.c.advance_mission('m1')
        store = JobStore(self.c.db_path)
        self.assertEqual(store.get('m1')['state'], 'RUNNING')
        self.assertEqual(store.reconcile_candidates(), ['m1'])                 # recovery tooling sees it
        run_to_end(self.c, 'm1')
        self.assertEqual(store.get('m1')['state'], 'COMPLETED')
        self.assertEqual(store.reconcile_candidates(), [])
        states = [r[0] for r in sqlite3.connect(self.c.db_path).execute('SELECT after FROM transitions WHERE id=? ORDER BY revision', ('m1',))]
        self.assertEqual(states, ['RUNNING', 'COMPLETED'])
        self.assertTrue(set(states) <= set(NEXT))


class BoundaryTests(unittest.TestCase):
    FORBIDDEN_IMPORTS = {'subprocess', 'socket', 'urllib', 'http', 'requests', 'ftplib', 'smtplib', 'importlib', 'ctypes',
                         'pickle', 'marshal', 'shelve', 'webbrowser', 'asyncio', 'multiprocessing', 'xmlrpc'}
    FORBIDDEN_BARE_CALLS = {'eval', 'exec', 'compile', '__import__'}        # builtins; re.compile is an attribute call
    FORBIDDEN_ATTR_CALLS = {'system', 'popen', 'Popen', 'spawn', 'execv', 'execl', 'run', 'check_output'}

    def trees(self):
        for rel in NEW_MODULES:
            yield rel, ast.parse((ROOT / rel).read_text(encoding='utf-8'))

    def test_g12_no_network_process_or_dynamic_code_capability_in_new_modules(self):
        for rel, tree in self.trees():
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    names = {a.name.split('.')[0] for a in node.names}
                elif isinstance(node, ast.ImportFrom):
                    names = {(node.module or '').split('.')[0]}
                else:
                    names = set()
                self.assertFalse(names & self.FORBIDDEN_IMPORTS, f'{rel}: {names & self.FORBIDDEN_IMPORTS}')
                if isinstance(node, ast.Call):
                    func = node.func
                    if isinstance(func, ast.Name):
                        self.assertNotIn(func.id, self.FORBIDDEN_BARE_CALLS, f'{rel}:{node.lineno}')
                    elif isinstance(func, ast.Attribute):
                        self.assertNotIn(func.attr, self.FORBIDDEN_ATTR_CALLS, f'{rel}:{node.lineno}')

    def test_g12_no_trading_wallet_signing_or_publishing_vocabulary_in_new_modules(self):
        for rel in NEW_MODULES:
            text = (ROOT / rel).read_text(encoding='utf-8').lower()
            for word in ('place_order', 'create_order', 'private_key', 'seed phrase', 'sign_transaction', 'send_transaction', 'git push',
                         'git commit', 'api_key', 'apikey', 'password'):
                self.assertNotIn(word, text, f'{rel}: {word}')

    def test_operation_registry_is_fixed_and_spec_cannot_extend_it(self):
        from research_lab.mission_coordinator import OPERATIONS
        from research_lab.mission_spec import KNOWN_OPERATIONS, STAGE_OPERATIONS, STAGES
        self.assertEqual(tuple(OPERATIONS), STAGES)
        self.assertEqual({op for op, _ in STAGE_OPERATIONS.values()}, set(KNOWN_OPERATIONS))
        self.assertEqual(len(KNOWN_OPERATIONS), 7)

    def test_sqlite_is_the_only_store_and_writes_stay_inside_the_workspace(self):
        fx = Fixture()
        self.addCleanup(fx.close)
        c = fx.coordinator()
        before = {p for p in Path(fx.tmp.name).rglob('*')}
        c.create_mission(fx.spec())
        run_to_end(c, 'm1')
        new = {p for p in Path(fx.tmp.name).rglob('*')} - before
        self.assertTrue(all(Path(fx.tmp.name) in p.parents for p in new))
        self.assertFalse((ROOT / 'reports').exists() and any((ROOT / 'reports').glob('m1*')))


if __name__ == '__main__':
    unittest.main()
