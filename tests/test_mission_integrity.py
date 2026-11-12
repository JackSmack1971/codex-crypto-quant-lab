"""Negative/positive tests separating artifact integrity, source provenance, review provenance,
quantitative verification and scientific validity. Offline, stdlib only."""
import copy
import hashlib
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from research_lab.archive import EvidenceArchive
from research_lab.completion import ARTIFACTS, verify
from research_lab.core import Observation, canonical_json, digest
from dataclasses import asdict

ROOT = Path(__file__).resolve().parents[1]
T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
AS_OF = '2026-01-01T12:00:00Z'


def ts(hours=0, minutes=0):
    return (T0 + timedelta(hours=hours, minutes=minutes)).isoformat().replace('+00:00', 'Z')


def obs(id, value, *, unit='USDT', instrument='binance:spot:BTCUSDT', metric='spot.quote.mid', available=ts(10, 1), retrieved=ts(10, 1)):
    return Observation(id, instrument, metric, value, unit, 'https://data-api.binance.vision/api/v3/x',
                       ts(10), retrieved, available)


def windows(n=12, minutes=59):
    return [{'start_utc': ts(i), 'end_utc': ts(i, minutes)} for i in range(n)]


def cv(folds, label_windows=None):
    result = {'schema': 'research-crossvalidation.v1',
              'folds': [{'train_indices': t, 'test_indices': u} for t, u in folds]}
    if label_windows is not None:
        result['label_windows'] = label_windows
    return result


class Base(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.dir = Path(self.temp.name)
        self.root = self.dir / 'bundle'
        self.root.mkdir()
        self.archive_path = self.dir / 'archive.sqlite'
        self.archive = EvidenceArchive(self.archive_path)
        self.archive.ingest(obs('obs:a', '100'))
        self.archive.ingest(obs('obs:b', '102'))
        self.build()

    def tearDown(self):
        self.temp.cleanup()

    def build(self, *, schema='research-mission.v2', research='NOT_EVALUABLE', metrics=None, evaluation=None,
              evidence=None):
        metrics = metrics if metrics is not None else [
            {'id': 'm1', 'value': '101', 'unit': 'USDT', 'inputs': ['obs:a', 'obs:b'], 'transform': {'name': 'mean'}}]
        bodies = {
            'mission_baseline.json': {'run_id': 't', 'as_of_utc': AS_OF, 'required_provider_operations': ['op']},
            'provider_attempts.json': {'attempts': [{'operation_id': 'op', 'status': 'SUCCEEDED'}]},
            'evidence.json': {'references': evidence or [{'id': 'obs:a'}, {'id': 'obs:b'}]},
            'metrics.json': {'metrics': metrics},
            'hypothesis_assessment.json': {'state': research, 'hypotheses': []},
            'evaluation_result.json': evaluation or {'state': 'NOT_EVALUABLE', 'checks': []},
            'thesis_revision.json': {'non_revision_reason': 'test'},
            'committee_verification.json': {'reviews': {
                'source_verifier': {'state': 'PASS', 'evidence': ['obs:a']},
                'methodology_reviewer': {'state': 'PASS', 'evidence': ['obs:b']}}},
        }
        refs = {}
        for name in ARTIFACTS:
            data = json.dumps(bodies[name], sort_keys=True).encode() if name in bodies else b'report'
            (self.root / name).write_bytes(data)
            refs[name] = {'path': name, 'sha256': hashlib.sha256(data).hexdigest()}
        mission = {'schema': schema, 'completion_state': 'COMPLETE', 'research_state': research,
                   'artifacts': refs, 'missing_artifacts': [], 'blockers': [], 'remaining_work': []}
        (self.root / 'mission.json').write_text(json.dumps(mission))

    def patch(self, name, obj):
        data = json.dumps(obj, sort_keys=True).encode()
        (self.root / name).write_bytes(data)
        mission = json.loads((self.root / 'mission.json').read_text())
        mission['artifacts'][name]['sha256'] = hashlib.sha256(data).hexdigest()
        (self.root / 'mission.json').write_text(json.dumps(mission))

    def load(self, name):
        return json.loads((self.root / name).read_text())

    def run_verify(self, archive='default'):
        return verify(self.root, self.archive if archive == 'default' else archive)

    def codes(self, result):
        return {d['code'] for d in result['diagnostics']}

    def state_of(self, result, code):
        return next(d['state'] for d in result['diagnostics'] if d['code'] == code)

    def assertFails(self, result, code):
        self.assertEqual(result['status'], 'INCOMPLETE', result['diagnostics'])
        self.assertIn(code, self.codes(result))


class EvidenceResolutionTests(Base):
    def test_valid_v2_bundle_complete_with_layered_states(self):
        result = self.run_verify()
        self.assertEqual((result['status'], result['diagnostics']), ('COMPLETE', []))
        layers = result['layers']
        self.assertEqual(layers['artifact_integrity']['state'], 'VERIFIED')
        self.assertEqual(layers['source_provenance']['state'], 'ARCHIVE_RESOLVED')
        self.assertEqual(layers['quantitative_verification']['state'], 'VERIFIED')
        self.assertEqual(layers['review_provenance']['state'], 'ASSERTED_UNAUTHENTICATED')
        self.assertFalse(layers['review_provenance']['authenticated'])
        self.assertEqual(layers['scientific_validity'], {**layers['scientific_validity'], 'state': 'NOT_CLAIMED', 'established': False})
        self.assertEqual(result['authenticity'], 'NOT_AUTHENTICATED')

    def test_v2_without_archive_fails_closed_as_unknown(self):
        result = self.run_verify(archive=None)
        self.assertFails(result, 'EVIDENCE_UNRESOLVED_NO_ARCHIVE')
        self.assertEqual(self.state_of(result, 'EVIDENCE_UNRESOLVED_NO_ARCHIVE'), 'UNKNOWN')
        self.assertEqual(result['layers']['quantitative_verification']['state'], 'UNKNOWN')

    def test_unknown_evidence_id_rejected(self):
        self.patch('evidence.json', {'references': [{'id': 'obs:a'}, {'id': 'obs:ghost'}]})
        self.assertFails(self.run_verify(), 'EVIDENCE_NOT_IN_ARCHIVE')

    def test_tampered_archive_row_detected(self):
        with sqlite3.connect(self.archive_path) as db:
            db.execute("UPDATE observations SET content=replace(content,'\"100\"','\"999\"') WHERE id='obs:a'")
        result = self.run_verify()
        self.assertFails(result, 'EVIDENCE_ARCHIVE_INTEGRITY')
        self.assertEqual(result['layers']['source_provenance']['state'], 'INVALID')

    def test_reference_digest_must_match_archived_observation(self):
        self.patch('evidence.json', {'references': [{'id': 'obs:a', 'sha256': '0' * 64}, {'id': 'obs:b'}]})
        self.assertFails(self.run_verify(), 'EVIDENCE_REFERENCE_CONTRADICTS_ARCHIVE')
        good = digest(asdict(self.archive.get('obs:a')))
        self.patch('evidence.json', {'references': [{'id': 'obs:a', 'sha256': good, 'unit': 'USDT'}, {'id': 'obs:b'}]})
        self.assertEqual(self.run_verify()['status'], 'COMPLETE')

    def test_reference_metadata_contradicting_archive_rejected(self):
        self.patch('evidence.json', {'references': [{'id': 'obs:a', 'unit': 'BTC'}, {'id': 'obs:b'}]})
        self.assertFails(self.run_verify(), 'EVIDENCE_REFERENCE_CONTRADICTS_ARCHIVE')

    def test_future_dated_evidence_rejected(self):
        self.archive.ingest(obs('obs:late', '1', available='2026-01-01T13:00:00Z', retrieved='2026-01-01T13:00:00Z'))
        self.patch('evidence.json', {'references': [{'id': 'obs:a'}, {'id': 'obs:late'}]})
        self.assertFails(self.run_verify(), 'EVIDENCE_FUTURE_DATED')

    def test_unknown_availability_is_unknown_not_valid(self):
        self.archive.ingest(obs('obs:noavail', '1', available=None))
        self.patch('evidence.json', {'references': [{'id': 'obs:a'}, {'id': 'obs:noavail'}]})
        result = self.run_verify()
        self.assertFails(result, 'EVIDENCE_AVAILABILITY_UNKNOWN')
        self.assertEqual(self.state_of(result, 'EVIDENCE_AVAILABILITY_UNKNOWN'), 'UNKNOWN')

    def test_unavailable_record_is_not_evidence(self):
        self.archive.ingest(Observation('obs:down', 'binance:spot:BTCUSDT', 'spot.quote.mid', None, 'USDT',
                                        'unavailable:provider', None, ts(10), None, status='UNAVAILABLE'))
        self.patch('evidence.json', {'references': [{'id': 'obs:a'}, {'id': 'obs:down'}]})
        self.assertFails(self.run_verify(), 'EVIDENCE_NOT_OBSERVED')

    def test_duplicate_reference_is_contradictory(self):
        self.patch('evidence.json', {'references': [{'id': 'obs:a'}, {'id': 'obs:a'}, {'id': 'obs:b'}]})
        self.assertFails(self.run_verify(), 'EVIDENCE_REFERENCE_DUPLICATE')

    def test_malformed_as_of_and_missing_as_of(self):
        base = self.load('mission_baseline.json')
        self.patch('mission_baseline.json', {**base, 'as_of_utc': 'yesterday'})
        self.assertFails(self.run_verify(), 'BASELINE_AS_OF_INVALID')
        del base['as_of_utc']
        self.patch('mission_baseline.json', base)
        self.assertFails(self.run_verify(), 'BASELINE_AS_OF_MISSING')

    def test_missing_archive_file_is_reported_not_created(self):
        missing = self.dir / 'nope.sqlite'
        result = self.run_verify(archive=missing)
        self.assertFails(result, 'EVIDENCE_ARCHIVE_UNAVAILABLE')
        self.assertFalse(missing.exists())

    def test_review_evidence_must_be_listed_evidence_in_v2(self):
        committee = self.load('committee_verification.json')
        committee['reviews']['source_verifier']['evidence'] = ['obs:other']
        self.patch('committee_verification.json', committee)
        self.assertFails(self.run_verify(), 'REVIEW_EVIDENCE_UNRESOLVED')

    def test_legacy_v1_bundle_stays_compatible_with_unknown_provenance(self):
        self.build(schema='research-mission.v1', metrics=[])
        result = self.run_verify(archive=None)
        self.assertEqual((result['status'], result['diagnostics']), ('COMPLETE', []))
        self.assertEqual(result['layers']['source_provenance']['state'], 'UNKNOWN')
        # A v1 bundle with an archive is still checked: bad ids are invalid.
        self.patch('evidence.json', {'references': [{'id': 'obs:ghost'}]})
        self.assertFails(self.run_verify(), 'EVIDENCE_NOT_IN_ARCHIVE')

    def test_diagnostics_carry_distinct_states(self):
        self.patch('evidence.json', {'references': [{'id': 'obs:ghost'}]})
        (self.root / 'report.md').unlink()
        result = self.run_verify()
        states = {d['code']: d['state'] for d in result['diagnostics']}
        self.assertEqual(states['EVIDENCE_NOT_IN_ARCHIVE'], 'INVALID')
        self.assertEqual(states['ARTIFACT_MISSING'], 'INCOMPLETE')
        self.assertEqual(result['layers']['artifact_integrity']['state'], 'INCOMPLETE')


class MetricLineageTests(Base):
    def metric(self, **over):
        m = {'id': 'm1', 'value': '101', 'unit': 'USDT', 'inputs': ['obs:a', 'obs:b'], 'transform': {'name': 'mean'}}
        m.update(over)
        return m

    def check(self, code, **over):
        self.patch('metrics.json', {'metrics': [self.metric(**over)]})
        result = self.run_verify()
        self.assertFails(result, code)
        return result

    def test_wrong_value_detected(self):
        self.check('METRIC_VALUE_MISMATCH', value='105')

    def test_tolerance_is_explicit(self):
        self.patch('metrics.json', {'metrics': [self.metric(value='101.4', transform={'name': 'mean', 'tolerance': '0.5'})]})
        self.assertEqual(self.run_verify()['status'], 'COMPLETE')

    def test_input_not_in_declared_evidence(self):
        self.archive.ingest(obs('obs:c', '1'))
        self.check('METRIC_INPUT_NOT_DECLARED_EVIDENCE', inputs=['obs:a', 'obs:c'], value='50.5')

    def test_incompatible_units_rejected(self):
        self.archive.ingest(obs('obs:btc', '1', unit='BTC'))
        self.patch('evidence.json', {'references': [{'id': 'obs:a'}, {'id': 'obs:btc'}]})
        self.check('METRIC_INPUT_INCOMPATIBLE', inputs=['obs:a', 'obs:btc'], value='50.5')

    def test_unit_mismatch_rejected(self):
        self.check('METRIC_UNIT_MISMATCH', unit='BTC')

    def test_unit_required(self):
        self.check('METRIC_UNIT_MISSING', unit='')

    def test_ratio_and_zero_division(self):
        self.archive.ingest(obs('obs:zero', '0'))
        self.patch('evidence.json', {'references': [{'id': 'obs:a'}, {'id': 'obs:zero'}]})
        self.patch('metrics.json', {'metrics': [self.metric(inputs=['obs:a', 'obs:zero'], transform={'name': 'ratio'}, value='1', unit='ratio')]})
        self.assertFails(self.run_verify(), 'METRIC_TRANSFORM_UNDEFINED')
        self.patch('evidence.json', {'references': [{'id': 'obs:a'}, {'id': 'obs:b'}]})
        ok = self.metric(inputs=['obs:b', 'obs:a'], transform={'name': 'ratio'}, value='1.02', unit='ratio')
        self.patch('metrics.json', {'metrics': [ok]})
        self.assertEqual(self.run_verify()['status'], 'COMPLETE')  # ratio may change unit
        self.patch('metrics.json', {'metrics': [{**ok, 'value': '1.03'}]})
        self.assertFails(self.run_verify(), 'METRIC_VALUE_MISMATCH')

    def test_undeclared_transform_is_unsupported_not_verified(self):
        self.patch('metrics.json', {'metrics': [self.metric(transform={'name': 'magic'})]})
        result = self.run_verify()
        self.assertFails(result, 'METRIC_TRANSFORM_UNSUPPORTED')
        self.assertEqual(self.state_of(result, 'METRIC_TRANSFORM_UNSUPPORTED'), 'UNSUPPORTED')

    def test_strict_requires_lineage_but_v1_only_marks_unsupported(self):
        bare = {'id': 'm1', 'value': '5', 'unit': 'x'}
        self.patch('metrics.json', {'metrics': [bare]})
        self.assertFails(self.run_verify(), 'METRIC_LINEAGE_MISSING')
        self.build(schema='research-mission.v1', metrics=[bare])
        result = self.run_verify()
        self.assertEqual(result['status'], 'COMPLETE')
        self.assertEqual(result['layers']['quantitative_verification']['state'], 'UNSUPPORTED')

    def test_duplicate_metric_ids_and_bad_inputs(self):
        self.patch('metrics.json', {'metrics': [self.metric(), self.metric()]})
        self.assertFails(self.run_verify(), 'METRIC_ID_DUPLICATE')
        self.check('METRIC_INPUTS_INVALID', inputs=[])
        self.check('METRIC_VALUE_INVALID', value='NaN')

    def test_metric_with_tampered_input_value_fails_after_archive_edit(self):
        # An attacker who edits the archived input must also break the integrity digest.
        with sqlite3.connect(self.archive_path) as db:
            db.execute("UPDATE observations SET content=replace(content,'\"102\"','\"110\"') WHERE id='obs:b'")
        self.assertFails(self.run_verify(), 'EVIDENCE_ARCHIVE_INTEGRITY')


class ValidatedClaimTests(Base):
    def evaluation(self, wf=None, cpcv=None, wins='default', *, top_level=True):
        wins = windows() if wins == 'default' else wins
        wf = wf or cv([([0, 1, 2, 3], [5, 6])])
        cpcv = cpcv or cv([([0, 1, 2, 3, 8, 9], [5, 6])])
        evaluation = {'state': 'PASS', 'validation': {'walk_forward': wf, 'cpcv': cpcv}}
        if wins is not None:
            evaluation['validation']['label_windows'] = wins
        return evaluation

    def validated(self, evaluation=None, **kw):
        self.build(research='VALIDATED', evaluation=evaluation or self.evaluation(), **kw)

    def test_fully_supported_claim_passes_gates_but_is_not_established(self):
        self.validated()
        result = self.run_verify()
        self.assertEqual((result['status'], result['diagnostics']), ('COMPLETE', []))
        sv = result['layers']['scientific_validity']
        self.assertEqual((sv['state'], sv['established']), ('CLAIM_GATES_PASSED', False))

    def test_structurally_valid_json_without_archive_cannot_be_validated(self):
        self.validated()
        result = self.run_verify(archive=None)
        self.assertEqual(result['status'], 'INCOMPLETE')
        self.assertIn('VALIDATED_EVIDENCE_UNVERIFIED', self.codes(result))
        self.assertEqual(result['layers']['scientific_validity']['state'], 'UNSUPPORTED')

    def test_v1_validated_also_fails_closed_without_archive(self):
        self.validated(schema='research-mission.v1')
        result = self.run_verify(archive=None)
        self.assertIn('VALIDATED_EVIDENCE_UNVERIFIED', self.codes(result))
        self.assertIn('VALIDATED_QUANTITATIVE_UNVERIFIED', self.codes(result))

    def test_validated_without_metrics_is_unsupported(self):
        self.validated(metrics=[])
        self.assertFails(self.run_verify(), 'VALIDATED_QUANTITATIVE_UNVERIFIED')

    def test_validated_with_unresolvable_evidence_rejected(self):
        self.validated(evidence=[{'id': 'obs:a'}, {'id': 'obs:ghost'}])
        result = self.run_verify()
        self.assertIn('VALIDATED_EVIDENCE_UNVERIFIED', self.codes(result))
        self.assertIn('EVIDENCE_NOT_IN_ARCHIVE', self.codes(result))

    def test_missing_label_windows_rejected(self):
        self.validated(self.evaluation(wins=None))
        result = self.run_verify()
        self.assertFails(result, 'CV_LABEL_WINDOWS_MISSING')
        self.assertEqual(self.state_of(result, 'CV_LABEL_WINDOWS_MISSING'), 'INCOMPLETE')

    def test_overlapping_labels_between_train_and_test_is_leakage(self):
        self.validated(self.evaluation(wf=cv([([2, 3, 4], [5, 6])]), wins=windows(minutes=90)))
        self.assertFails(self.run_verify(), 'CV_LABEL_OVERLAP_LEAKAGE')

    def test_walk_forward_training_after_test_rejected(self):
        wf = cv([([8, 9], [5, 6])])
        self.validated(self.evaluation(wf=wf))
        self.assertFails(self.run_verify(), 'CV_TRAIN_NOT_BEFORE_TEST')

    def test_cpcv_may_train_after_test_when_not_overlapping(self):
        self.validated()
        self.assertEqual(self.run_verify()['status'], 'COMPLETE')

    def test_index_beyond_windows_rejected(self):
        self.validated(self.evaluation(wf=cv([([0, 1], [40])])))
        self.assertFails(self.run_verify(), 'CV_INDEX_OUT_OF_RANGE')

    def test_malformed_or_unordered_windows_rejected(self):
        bad = windows()
        bad[3], bad[4] = bad[4], bad[3]
        self.validated(self.evaluation(wins=bad))
        self.assertFails(self.run_verify(), 'CV_LABEL_WINDOWS_INVALID')
        self.validated(self.evaluation(wins=[{'start_utc': 'x', 'end_utc': 'y'}]))
        self.assertFails(self.run_verify(), 'CV_LABEL_WINDOWS_INVALID')

    def test_per_result_windows_take_precedence(self):
        wf = cv([([2, 3, 4], [5, 6])], label_windows=windows(minutes=90))
        self.validated(self.evaluation(wf=wf))
        self.assertFails(self.run_verify(), 'CV_LABEL_OVERLAP_LEAKAGE')

    def test_failed_or_not_evaluable_evaluation_cannot_be_validated(self):
        for state in ('FAIL', 'NOT_EVALUABLE'):
            evaluation = self.evaluation()
            evaluation['state'] = state
            self.validated(evaluation)
            self.assertIn('VALIDATED_UNSUPPORTED', self.codes(self.run_verify()))


class CliTests(Base):
    def test_cli_archive_flag(self):
        cmd = [sys.executable, str(ROOT / 'scripts' / 'verify_mission.py'), str(self.root)]
        bare = subprocess.run(cmd, capture_output=True, text=True)
        self.assertEqual(bare.returncode, 1)
        ok = subprocess.run(cmd + ['--archive', str(self.archive_path), '--allow-partial'], capture_output=True, text=True)
        self.assertEqual(ok.returncode, 0, ok.stdout)
        self.assertEqual(json.loads(ok.stdout)['layers']['source_provenance']['state'], 'ARCHIVE_RESOLVED')


if __name__ == '__main__':
    unittest.main()
