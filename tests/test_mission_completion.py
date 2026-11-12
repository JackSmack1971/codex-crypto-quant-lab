"""Negative contract tests for the local research mission verifier."""
import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
import shutil
from pathlib import Path

from research_lab.completion import ARTIFACTS, verify


class MissionCompletionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / 'bundle'
        self.root.mkdir()
        self.build_bundle()

    def tearDown(self):
        self.temp.cleanup()

    def build_bundle(self, state='COMPLETE', research='NOT_EVALUABLE'):
        refs = {}
        bodies = {
            'mission_baseline.json': {'run_id': 'synthetic-test', 'as_of_utc': '2026-01-01T00:00:00Z', 'fixture': True,
                                      'required_provider_operations': ['synthetic.fetch']},
            'provider_attempts.json': {'attempts': [{'operation_id': 'synthetic.fetch', 'provider': 'synthetic', 'status': 'SUCCEEDED'}]},
            'evidence.json': {'references': [{'id': 'synthetic:evidence:1'}]},
            'metrics.json': {'metrics': []},
            'hypothesis_assessment.json': {'state': research, 'hypotheses': []},
            'evaluation_result.json': {'state': 'NOT_EVALUABLE', 'checks': []},
            'thesis_revision.json': {'non_revision_reason': 'synthetic fixture; no durable thesis change'},
            'committee_verification.json': {'reviews': {
                'source_verifier': {'state': 'PASS', 'evidence': ['synthetic:evidence:1']},
                'methodology_reviewer': {'state': 'PASS', 'evidence': ['synthetic:evidence:1']},
            }},
        }
        for name in ARTIFACTS:
            data = json.dumps(bodies.get(name), sort_keys=True).encode() if name.endswith('.json') else b'Synthetic fixture only; no market evidence.'
            (self.root / name).write_bytes(data)
            refs[name] = {'path': name, 'sha256': hashlib.sha256(data).hexdigest()}
        mission = {'schema': 'research-mission.v1', 'completion_state': state,
                   'research_state': research, 'artifacts': refs, 'missing_artifacts': [],
                   'blockers': [], 'remaining_work': []}
        if state == 'INCOMPLETE':
            mission['artifacts'].pop('metrics.json')
            mission['missing_artifacts'] = [{'artifact': 'metrics.json', 'reason': 'synthetic missing input'}]
            (self.root / 'metrics.json').unlink()
            mission['remaining_work'] = ['Acquire synthetic metric']
        if state == 'BLOCKED':
            mission['blockers'] = [{'id': 'synthetic-blocker-1', 'reason': 'Synthetic dependency is unavailable', 'evidence': ['synthetic:blocker:1']}]
            mission['remaining_work'] = ['Resume after synthetic dependency is restored']
        (self.root / 'mission.json').write_text(json.dumps(mission), encoding='utf-8')

    def mission(self):
        return json.loads((self.root / 'mission.json').read_text(encoding='utf-8'))

    def save(self, mission):
        (self.root / 'mission.json').write_text(json.dumps(mission), encoding='utf-8')

    def replace_json_artifact(self, name, obj):
        data = json.dumps(obj, sort_keys=True).encode()
        (self.root / name).write_bytes(data)
        mission = self.mission()
        mission['artifacts'][name]['sha256'] = hashlib.sha256(data).hexdigest()
        self.save(mission)

    def codes(self, result=None):
        result = result or verify(self.root)
        return {item['code'] for item in result['diagnostics']}

    def test_valid_complete_not_evaluable_bundle(self):
        result = verify(self.root)
        self.assertEqual(result['status'], 'COMPLETE')
        self.assertEqual(result['diagnostics'], [])

    def test_missing_required_artifact(self):
        (self.root / 'metrics.json').unlink()
        result = verify(self.root)
        self.assertEqual(result['status'], 'INCOMPLETE')
        self.assertIn('ARTIFACT_MISSING', self.codes(result))

    def test_corrupted_sha256_digest(self):
        mission = self.mission()
        mission['artifacts']['report.md']['sha256'] = '0' * 64
        self.save(mission)
        result = verify(self.root)
        self.assertEqual(result['status'], 'INCOMPLETE')
        self.assertIn('ARTIFACT_HASH_MISMATCH', self.codes(result))

    def test_missing_raw_evidence_reference(self):
        self.replace_json_artifact('evidence.json', {'references': []})
        result = verify(self.root)
        self.assertEqual(result['status'], 'INCOMPLETE')
        self.assertIn('EVIDENCE_REFERENCES_INVALID', self.codes(result))

    def test_inconsistent_complete_declaration(self):
        self.replace_json_artifact('evaluation_result.json', {'state': 'FAIL', 'checks': []})
        result = verify(self.root)
        self.assertEqual(result['status'], 'INCOMPLETE')
        self.assertIn('COMPLETE_HAS_FAILED_EVALUATION', self.codes(result))

    def test_not_evaluable_research_can_be_process_complete(self):
        result = verify(self.root)
        self.assertEqual(result['status'], 'COMPLETE')
        self.assertEqual(self.mission()['research_state'], 'NOT_EVALUABLE')

    def test_provider_failure_and_unattempted_are_distinct(self):
        self.replace_json_artifact('provider_attempts.json', {'attempts': [
            {'operation_id': 'synthetic.fetch', 'status': 'FAILED', 'error': 'HTTP 503'},
            {'operation_id': 'synthetic.fallback', 'status': 'UNATTEMPTED', 'reason': 'not scheduled'},
        ]})
        result = verify(self.root)
        self.assertEqual(result['status'], 'INCOMPLETE')
        self.assertIn('COMPLETE_HAS_UNSUCCESSFUL_PROVIDER_ATTEMPT', self.codes(result))
        self.assertNotIn('UNATTEMPTED_HAS_ERROR', self.codes(result))

    def test_required_provider_operation_must_be_recorded(self):
        self.replace_json_artifact('provider_attempts.json', {'attempts': [
            {'operation_id': 'synthetic.other', 'status': 'SUCCEEDED'}]})
        result = verify(self.root)
        self.assertEqual(result['status'], 'INCOMPLETE')
        self.assertIn('PROVIDER_OPERATION_UNRECORDED', self.codes(result))

    def test_missing_committee_verification(self):
        self.replace_json_artifact('committee_verification.json', {'reviews': {'source_verifier': {'state': 'PASS'}}})
        result = verify(self.root)
        self.assertEqual(result['status'], 'INCOMPLETE')
        self.assertIn('COMMITTEE_REVIEW_MISSING', self.codes(result))

    def test_failed_committee_verification(self):
        self.replace_json_artifact('committee_verification.json', {'reviews': {
            'source_verifier': {'state': 'FAIL'}, 'methodology_reviewer': {'state': 'PASS'}}})
        result = verify(self.root)
        self.assertEqual(result['status'], 'INCOMPLETE')
        self.assertIn('COMMITTEE_REVIEW_NOT_PASSED', self.codes(result))

    def test_blocked_state_requires_attributable_evidence(self):
        self.build_bundle('BLOCKED')
        result = verify(self.root)
        self.assertEqual(result['status'], 'BLOCKED')
        self.assertEqual(result['diagnostics'], [])

    def test_invalid_blocked_transition_and_unsupported_declaration(self):
        mission = self.mission()
        mission['completion_state'] = 'BLOCKED'
        mission['blockers'] = []
        mission['remaining_work'] = []
        self.save(mission)
        result = verify(self.root)
        self.assertEqual(result['status'], 'INCOMPLETE')
        self.assertIn('BLOCKED_WITHOUT_EVIDENCE', self.codes(result))
        mission['completion_state'] = 'PAUSED'
        self.save(mission)
        result = verify(self.root)
        self.assertEqual(result['status'], 'INCOMPLETE')
        self.assertIn('COMPLETION_STATE_INVALID', self.codes(result))
        mission['completion_state'] = 'BLOCKED'
        mission['blockers'] = [{'id': 'invalid-blocker', 'reason': 'dependency unavailable', 'evidence': [{}]}]
        mission['remaining_work'] = ['wait for dependency']
        self.save(mission)
        result = verify(self.root)
        self.assertEqual(result['status'], 'INCOMPLETE')
        self.assertIn('BLOCKER_EVIDENCE_INVALID', self.codes(result))

    def test_blocked_bundle_can_record_artifact_prevented_by_blocker(self):
        mission = self.mission()
        mission['completion_state'] = 'BLOCKED'
        mission['artifacts'].pop('report.html')
        mission['missing_artifacts'] = [{'artifact': 'report.html', 'reason': 'synthetic renderer dependency unavailable', 'blocker_id': 'renderer-dependency'}]
        mission['blockers'] = [{'id': 'renderer-dependency', 'reason': 'Synthetic renderer dependency unavailable', 'evidence': ['synthetic:blocker:renderer']}]
        mission['remaining_work'] = ['Render report.html after dependency recovery']
        (self.root / 'report.html').unlink()
        self.save(mission)
        result = verify(self.root)
        self.assertEqual(result['status'], 'BLOCKED')
        self.assertEqual(result['diagnostics'], [])

    def test_blocked_missing_artifact_must_link_to_its_blocker(self):
        mission = self.mission()
        mission['completion_state'] = 'BLOCKED'
        mission['artifacts'].pop('report.html')
        mission['missing_artifacts'] = [{'artifact': 'report.html', 'reason': 'renderer unavailable', 'blocker_id': 'unrelated-blocker'}]
        mission['blockers'] = [{'id': 'network-blocker', 'reason': 'network unavailable', 'evidence': ['synthetic:blocker:network']}]
        mission['remaining_work'] = ['Render report.html']
        (self.root / 'report.html').unlink()
        self.save(mission)
        result = verify(self.root)
        self.assertEqual(result['status'], 'INCOMPLETE')
        self.assertIn('MISSING_ARTIFACT_BLOCKER_LINK_INVALID', self.codes(result))

    def test_evidence_reference_ids_must_be_attributable_strings(self):
        self.replace_json_artifact('evidence.json', {'references': [{'id': 42}]})
        result = verify(self.root)
        self.assertEqual(result['status'], 'INCOMPLETE')
        self.assertIn('EVIDENCE_REFERENCE_INVALID', self.codes(result))
        self.replace_json_artifact('committee_verification.json', {'reviews': {
            'source_verifier': {'state': 'PASS', 'evidence': [{}]},
            'methodology_reviewer': {'state': 'PASS', 'evidence': ['synthetic:review']},
        }})
        result = verify(self.root)
        self.assertEqual(result['status'], 'INCOMPLETE')
        self.assertIn('COMMITTEE_REVIEW_EVIDENCE_MISSING', self.codes(result))

    def test_explicit_missing_artifact_record_supports_incomplete(self):
        mission = self.mission()
        mission['completion_state'] = 'INCOMPLETE'
        mission['artifacts'].pop('metrics.json')
        mission['missing_artifacts'] = [{'artifact': 'metrics.json', 'reason': 'source unavailable'}]
        (self.root / 'metrics.json').unlink()
        mission['remaining_work'] = ['Retry after source recovery']
        self.save(mission)
        result = verify(self.root)
        self.assertEqual(result['status'], 'INCOMPLETE')
        self.assertEqual(result['diagnostics'], [])

    def test_incomplete_records_unfinished_analysis_even_with_all_artifacts(self):
        mission = self.mission()
        mission['completion_state'] = 'INCOMPLETE'
        mission['remaining_work'] = ['Complete interpretation and falsifier review']
        self.save(mission)
        result = verify(self.root)
        self.assertEqual(result['status'], 'INCOMPLETE')
        self.assertEqual(result['diagnostics'], [])

    def test_missing_required_json_fields_have_codes(self):
        mission = self.mission()
        del mission['research_state']
        self.save(mission)
        result = verify(self.root)
        self.assertEqual(result['status'], 'INCOMPLETE')
        self.assertIn('MISSION_FIELD_MISSING', self.codes(result))
        self.replace_json_artifact('provider_attempts.json', {'operations': []})
        result = verify(self.root)
        self.assertEqual(result['status'], 'INCOMPLETE')
        self.assertIn('PROVIDER_ATTEMPTS_INVALID', self.codes(result))

    def test_validated_state_rejects_untyped_statistical_evidence(self):
        mission = self.mission()
        mission['research_state'] = 'VALIDATED'
        self.save(mission)
        self.replace_json_artifact('hypothesis_assessment.json', {'state': 'VALIDATED', 'hypotheses': []})
        self.replace_json_artifact('evaluation_result.json', {'state': 'PASS', 'statistical_evidence': True})
        result = verify(self.root)
        self.assertEqual(result['status'], 'INCOMPLETE')
        self.assertIn('VALIDATED_UNSUPPORTED', self.codes(result))

    def test_validated_claim_is_checked_independently_of_process_state(self):
        mission = self.mission()
        mission['completion_state'] = 'INCOMPLETE'
        mission['research_state'] = 'VALIDATED'
        mission['remaining_work'] = ['Finish report review']
        self.save(mission)
        self.replace_json_artifact('hypothesis_assessment.json', {'state': 'VALIDATED', 'hypotheses': []})
        self.replace_json_artifact('evaluation_result.json', {'state': 'PASS'})
        result = verify(self.root)
        self.assertEqual(result['status'], 'INCOMPLETE')
        self.assertIn('VALIDATED_UNSUPPORTED', self.codes(result))

    def test_path_traversal_is_rejected(self):
        mission = self.mission()
        mission['artifacts']['report.md']['path'] = '../outside.md'
        self.save(mission)
        result = verify(self.root)
        self.assertEqual(result['status'], 'INCOMPLETE')
        self.assertIn('ARTIFACT_PATH_INVALID', self.codes(result))

    def test_self_reference_and_digest_defects_are_rejected(self):
        mission = self.mission()
        mission['artifacts']['metrics.json'] = {'path': 'mission.json', 'sha256': '0' * 64}
        self.save(mission)
        result = verify(self.root)
        self.assertEqual(result['status'], 'INCOMPLETE')
        self.assertIn('ARTIFACT_SELF_REFERENCE', self.codes(result))

    def test_cli_valid_invalid_incomplete_and_blocked_results(self):
        script = Path(__file__).resolve().parents[1] / 'scripts' / 'verify_mission.py'
        fixture = Path(__file__).resolve().parent / 'fixtures' / 'synthetic-mission-bundle'
        valid = subprocess.run([sys.executable, str(script), str(fixture)], capture_output=True, text=True)
        self.assertEqual(valid.returncode, 2, valid.stdout + valid.stderr)        # integrity passed; evidence checks not performed: not a success
        self.assertEqual((json.loads(valid.stdout)['status'], json.loads(valid.stdout)['verification_status']), ('COMPLETE', 'PARTIALLY_VERIFIED'))
        allowed = subprocess.run([sys.executable, str(script), str(fixture), '--allow-partial'], capture_output=True, text=True)
        self.assertEqual(allowed.returncode, 0, allowed.stdout + allowed.stderr)
        shutil.copytree(fixture, self.root, dirs_exist_ok=True)
        def run():
            return subprocess.run([sys.executable, str(script), str(self.root)], capture_output=True, text=True)
        (self.root / 'report.md').write_text('tampered', encoding='utf-8')
        corrupt = run()
        self.assertNotEqual(corrupt.returncode, 0)
        self.assertIn('ARTIFACT_HASH_MISMATCH', {d['code'] for d in json.loads(corrupt.stdout)['diagnostics']})
        shutil.copytree(fixture, self.root, dirs_exist_ok=True)
        (self.root / 'metrics.json').unlink()
        missing = run()
        self.assertEqual(missing.returncode, 1)
        self.assertIn('ARTIFACT_MISSING', {d['code'] for d in json.loads(missing.stdout)['diagnostics']})
        self.build_bundle('INCOMPLETE')
        incomplete = run()
        self.assertEqual(incomplete.returncode, 1)
        self.assertEqual(json.loads(incomplete.stdout)['status'], 'INCOMPLETE')
        self.build_bundle('BLOCKED')
        blocked = run()
        self.assertEqual(blocked.returncode, 1)
        self.assertEqual(json.loads(blocked.stdout)['status'], 'BLOCKED')


if __name__ == '__main__':
    unittest.main()
