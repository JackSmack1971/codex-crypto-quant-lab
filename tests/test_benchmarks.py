"""P2-D: the offline benchmark suite measures engineering correctness only, and can demonstrably fail."""
import copy
import json
import shutil
import socket
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from research_lab import benchmarks  # noqa: E402
from research_lab.benchmarks import BenchmarkError, DIMENSIONS, repeat_suite, run_suite  # noqa: E402

REQUIRED_SCENARIOS = [f'B{i:02d}' for i in range(1, 32)]   # B01-B22 required by the brief; B23-B25 added after independent review; B26-B31 corrective repair


class SuiteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.result = run_suite(ROOT)
        cls.cases = {c['case_id']: c for c in cls.result['cases']}

    def test_all_required_scenarios_exist_with_versioned_independent_fixtures(self):
        self.assertEqual(list(self.cases), REQUIRED_SCENARIOS)
        for cid in REQUIRED_SCENARIOS:
            raw = json.loads((ROOT / 'benchmarks' / 'cases' / f'{cid}.json').read_text())
            self.assertEqual((raw['schema'], raw['version']), ('research-benchmark-case.v1', '1'))
            self.assertTrue(raw['unsupported_claims'] and raw['limitations'] and raw['as_of_utc'] and raw['expected'])

    def test_runner_cannot_author_its_own_oracle(self):
        source = (ROOT / 'research_lab' / 'benchmarks.py').read_text()
        for token in ('write_text', 'write_bytes', '.open(', "open("):
            self.assertNotIn(token, source)       # no code path writes case or expected files

    def test_every_case_passes_and_no_dimension_is_hidden(self):
        self.assertEqual(self.result['scoring']['failures'], [])
        self.assertEqual(self.result['scoring']['mandatory_gate']['state'], 'PASS')
        self.assertEqual(self.result['scoring']['dimensions_with_no_evaluable_case'], [])
        for dim in DIMENSIONS:
            counts = self.result['scoring']['per_dimension'][dim]
            self.assertGreater(counts['PASS'], 0, dim)
            self.assertGreater(counts['NOT_EVALUABLE'], 0, dim)       # not-applicable checks stay visible, never PASS
        self.assertNotIn('score', {k.lower() for k in self.result['scoring']})
        self.assertEqual(self.result['scoring']['predictive_performance'], 'NOT_MEASURED')

    def test_negative_cases_fail_with_the_expected_diagnostics(self):
        expect = {'B02': [], 'B03': ['STALE'], 'B04': ['FUTURE_DATED'], 'B05': ['CONTRADICTORY'], 'B06': ['CORROBORATION_NOT_INDEPENDENT'],
                  'B07': ['OUT_OF_SCOPE_INSTRUMENT'], 'B08': ['REPORT_TRANSFORM_UNSUPPORTED'], 'B09': ['VERIFICATION_FAILED'],
                  'B10': ['CHART_SAMPLE_UNRESOLVED'], 'B17': ['REQUIRED_EVIDENCE_UNAVAILABLE']}
        for cid, codes in expect.items():
            observed = set(self.cases[cid]['observed']['diagnostic_codes'])
            self.assertTrue(set(codes) <= observed, (cid, observed))
        for cid, state in (('B06', 'FAILED'), ('B08', 'FAILED'), ('B09', 'FAILED'), ('B17', 'BLOCKED')):
            self.assertEqual(self.cases[cid]['observed']['job_state'], state, cid)
        self.assertEqual(self.cases['B01']['observed']['research_state'], 'EVALUABLE')
        for cid in ('B02', 'B03', 'B04', 'B05', 'B07', 'B13'):
            self.assertEqual(self.cases[cid]['observed']['research_state'], 'INSUFFICIENT_EVIDENCE', cid)

    def test_recovery_cases_record_distinct_classifications(self):
        self.assertEqual(self.cases['B14']['observed']['recovery']['reconcile_classification'], 'ABSENT')
        self.assertEqual(self.cases['B15']['observed']['recovery']['reconcile_classification'], 'COMPLETE')
        self.assertEqual(self.cases['B16']['observed']['recovery']['winners'], 1)

    def test_provenance_is_complete_and_unknown_is_not_invented(self):
        p = self.result['provenance']
        self.assertEqual(p['codex_model_runtime'], 'UNKNOWN')
        self.assertEqual(p['network_access'], 'NOT_USED')
        self.assertTrue(p['fixture_digests'] and p['python'] and p['git_commit'])
        self.assertIn('report_composer', p['component_versions'])
        self.assertTrue(self.result['executed_checks'])
        self.assertTrue(all(c['state'] in ('PASS', 'FAIL') for c in self.result['executed_checks']))
        self.assertNotIn('started_utc', json.dumps({k: v for k, v in self.result.items() if k != 'run_metadata'}))

    def test_two_clean_runs_have_identical_canonical_output(self):
        outcome = repeat_suite(ROOT, 2)
        self.assertTrue(outcome['identical'])
        self.assertEqual(len(set(outcome['canonical_digests'])), 1)
        self.assertEqual(outcome['canonical_digests'][0], self.result['canonical_digest'])

    def test_artifact_digest_is_stable_across_processes_and_hash_seeds(self):
        import os
        import subprocess
        digests = set()
        for seed in ('1', '4242'):
            env = {**os.environ, 'PYTHONHASHSEED': seed, 'PYTHONDONTWRITEBYTECODE': '1'}
            out = subprocess.run([sys.executable, str(ROOT / 'scripts' / 'benchmark.py'), '--cases', 'B01,B02,B14,B18'], capture_output=True, text=True, env=env, cwd=ROOT)
            self.assertEqual(out.returncode, 0, out.stderr)
            digests.add(json.loads(out.stdout)['artifact_digest'])
        self.assertEqual(len(digests), 1)

    def test_no_network_is_used(self):
        def deny(*a, **k):
            raise AssertionError('network attempted')
        with mock.patch.object(socket, 'socket', deny), mock.patch.object(socket, 'create_connection', deny):
            self.assertEqual(run_suite(ROOT, ['B01', 'B18', 'B21'])['scoring']['failures'], [])


class OracleSensitivityTests(unittest.TestCase):
    """The runner must be able to fail: mutate a private copy of the suite and observe the verdicts."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        shutil.copytree(ROOT / 'benchmarks', self.root / 'benchmarks')

    def mutate(self, cid, fn):
        path = self.root / 'benchmarks' / 'cases' / f'{cid}.json'
        case = json.loads(path.read_text())
        fn(case['expected'])
        path.write_text(json.dumps(case))

    def states(self, cid):
        result = run_suite(self.root, [cid])
        return {c['dimension']: c['state'] for c in result['cases'][0]['checks']}, result

    def test_subset_runs_do_not_claim_a_passing_mandatory_gate(self):
        result = run_suite(ROOT, ['B22'])
        gate = result['scoring']['mandatory_gate']
        self.assertEqual(gate['state'], 'INCOMPLETE')
        self.assertIn('recovery_idempotency', gate['unevaluated_mandatory_dimensions'])

    def test_wrong_numeric_expectation_is_a_visible_failure(self):
        self.mutate('B01', lambda e: e['quantitative'][0].update(value='11'))
        states, result = self.states('B01')
        self.assertEqual(states['quantitative_accuracy'], 'FAIL')
        self.assertEqual(result['scoring']['failures'], [{'case_id': 'B01', 'dimension': 'quantitative_accuracy'}])

    def test_mandatory_failure_stays_visible_despite_many_passes(self):
        self.mutate('B02', lambda e: e['process'].update(job_state='BLOCKED'))
        result = run_suite(self.root, ['B01', 'B02', 'B03', 'B04', 'B05'])
        self.assertEqual(result['scoring']['mandatory_gate']['state'], 'FAIL')
        self.assertEqual(result['scoring']['mandatory_gate']['failures'], [{'case_id': 'B02', 'dimension': 'process_state'}])
        self.assertGreater(result['scoring']['per_dimension']['process_state']['PASS'], 3)

    def test_wrong_diagnostic_expectation_fails(self):
        self.mutate('B03', lambda e: e['diagnostics'].update(codes_present=['FUTURE_DATED']))
        states, _ = self.states('B03')
        self.assertEqual(states['diagnostic_classification'], 'FAIL')

    def test_missing_expectation_is_not_evaluable_not_pass(self):
        self.mutate('B01', lambda e: (e.pop('quantitative'), e.pop('chart_lineage')))
        states, _ = self.states('B01')
        self.assertEqual((states['quantitative_accuracy'], states['chart_sample_lineage']), ('NOT_EVALUABLE', 'NOT_EVALUABLE'))

    def test_expectation_of_an_unproduced_artifact_fails(self):
        self.mutate('B06', lambda e: e.update(quantitative=[{'claim_id': 'D:chg', 'value': '10'}]))
        states, _ = self.states('B06')
        self.assertEqual(states['quantitative_accuracy'], 'FAIL')        # rendering was rejected: no values exist to compare

    def test_reused_holdout_expectation_is_enforced(self):
        self.mutate('B20', lambda e: e['holdout'].update(error_register_expect_error='NONE'))
        states, _ = self.states('B20')
        self.assertEqual(states['holdout_governance'], 'FAIL')

    def test_fixture_tampering_is_rejected_before_execution(self):
        fixture = next((self.root / 'benchmarks' / 'fixtures' / 'B01').glob('run.json'))
        fixture.write_text(fixture.read_text().replace('"100"', '"101"'))
        with self.assertRaises(BenchmarkError) as ctx:
            run_suite(self.root, ['B01'])
        self.assertEqual(ctx.exception.code, 'FIXTURE_DIGEST_MISMATCH')

    def test_unknown_case_and_schema_errors(self):
        with self.assertRaises(BenchmarkError) as ctx:
            run_suite(self.root, ['B99'])
        self.assertEqual(ctx.exception.code, 'CASE_UNKNOWN')
        path = self.root / 'benchmarks' / 'cases' / 'B01.json'
        case = json.loads(path.read_text())
        case['schema'] = 'other'
        path.write_text(json.dumps(case))
        with self.assertRaises(BenchmarkError) as ctx:
            run_suite(self.root)
        self.assertEqual(ctx.exception.code, 'CASE_SCHEMA_INVALID')


class CliTests(unittest.TestCase):
    def test_cli_exit_codes_and_no_overwrite(self):
        sys.path.insert(0, str(ROOT / 'scripts'))
        import benchmark
        with tempfile.TemporaryDirectory() as tmp, mock.patch('sys.stdout'):
            out = Path(tmp) / 'r.json'
            self.assertEqual(benchmark.main(['--cases', 'B18,B19', '--output', str(out)]), 0)
            self.assertEqual(json.loads(out.read_text())['scoring']['predictive_performance'], 'NOT_MEASURED')
            self.assertEqual(benchmark.main(['--cases', 'B18', '--output', str(out)]), 2)       # never overwrites
            self.assertEqual(benchmark.main(['--cases', 'B99']), 2)


if __name__ == '__main__':
    unittest.main()
