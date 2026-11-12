"""Final qualification hardening regressions: H01 independence scope, H02 fail-closed historical trust, H03 verify CLI exit semantics.

Offline and deterministic. Builds on the corrective-repair fixtures; adds no new mechanism.
"""
import json
import shutil
import sqlite3
import subprocess
import sys
import unittest
from contextlib import closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import test_corrective_repair as cr  # noqa: E402
from p1_support import StepClock, obs_row  # noqa: E402

from research_lab.experiment_registry import ExperimentRegistry  # noqa: E402
from research_lab.history import HistoryError, ThesisHistory  # noqa: E402
from research_lab.intelligence import ResearchIntelligence  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'scripts' / 'verify_mission.py'
NEWER = [obs_row('p1', '120', observed='2026-01-02T11:00:00Z', retrieved='2026-01-02T11:01:00Z', available='2026-01-02T11:01:00Z')]
NOT_ESTABLISHED = 'NOT_ESTABLISHED'


# --------------------------------------------------------------------------- H01
class IndependenceScopeTests(cr.RegistryBase):
    def scoped(self, out):
        self.assertIn('independent_evaluation', out)                          # compatibility field retained
        self.assertIn(out['independence_scope'], ('LOCAL_REGISTRY_RELATIVE', NOT_ESTABLISHED))
        self.assertTrue(out['independence_limitations'])
        return out

    def test_h01_01_established_local_is_registry_relative_with_stated_limits(self):
        plan, w, _ = self.register('exp-1')
        out = self.scoped(self.evaluate('exp-1', plan, w))
        self.assertEqual((out['independence'], out['independent_evaluation'], out['independence_scope']), ('ESTABLISHED_LOCAL', True, 'LOCAL_REGISTRY_RELATIVE'))
        text = ' '.join(out['independence_limitations'])
        for phrase in ('not proven statistical or dataset independence', 'shifted timestamps', 'changed dataset_id', 'altered dataset digest',
                       'neither content equality nor inequality proves independent samples'):
            self.assertIn(phrase, text)
        self.assertEqual(self.scoped(self.reg.summary('exp-1'))['independence_scope'], 'LOCAL_REGISTRY_RELATIVE')   # summary agrees with outcome

    def test_h01_02_unknown_is_never_reported_as_established(self):
        plan, w, _ = self.register('exp-1')
        self.evaluate('exp-1', plan, w)
        other = {k: list(v) for k, v in self.c.items()}
        other['flat'][11] = 0.002
        plan, w, _ = self.register('exp-x', offset=1, ds='ds-eth', c=other)
        out = self.scoped(self.evaluate('exp-x', plan, w, ds='ds-eth', c=other))
        self.assertEqual((out['independence'], out['independent_evaluation'], out['independence_scope']), ('UNKNOWN', False, NOT_ESTABLISHED))
        self.assertIn('do not describe this holdout as independent', ' '.join(out['independence_limitations']))

    def test_h01_03_legacy_unqualified_and_pending_claim_are_not_established(self):
        plan, w, _ = self.register('exp-1')
        pending = self.scoped(self.reg.summary('exp-1'))
        self.assertEqual((pending['independence'], pending['independent_evaluation'], pending['independence_scope']), ('PENDING_CLAIM', False, NOT_ESTABLISHED))
        self.evaluate('exp-1', plan, w)
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('UPDATE experiments SET holdout_from=NULL, holdout_to=NULL, independence=NULL')
        legacy = self.scoped(ExperimentRegistry(self.path, clock=StepClock()).summary('exp-1'))
        self.assertEqual((legacy['independence'], legacy['independent_evaluation'], legacy['independence_scope']), ('LEGACY_UNQUALIFIED', False, NOT_ESTABLISHED))

    def test_h01_04_recovered_or_reused_evaluation_is_not_established_even_with_a_local_label(self):
        plan, w, _ = self.register('exp-1')
        first = self.evaluate('exp-1', plan, w)
        again = self.scoped(self.evaluate('exp-1', plan, w))                  # reuse of the stored result
        self.assertEqual((again['reused'], again['independent_evaluation'], again['independence_scope']), (True, False, NOT_ESTABLISHED))
        self.assertEqual(first['independence_scope'], 'LOCAL_REGISTRY_RELATIVE')

    def test_h01_05_relabelled_or_shifted_data_can_defeat_the_local_lineage_and_the_response_says_so(self):
        plan, w, _ = self.register('exp-1')
        self.evaluate('exp-1', plan, w)
        # identical return values re-dated far later under a new dataset_id: the registry cannot see it is the same data
        plan2, w2, _ = self.register('exp-shifted', offset=400, ds='ds-relabelled')
        out = self.scoped(self.evaluate('exp-shifted', plan2, w2, ds='ds-relabelled'))
        self.assertEqual(out['independence'], 'ESTABLISHED_LOCAL')           # documented limitation: this is declared lineage only
        self.assertEqual(out['independence_scope'], 'LOCAL_REGISTRY_RELATIVE')
        self.assertIn('can defeat the locally declared lineage', ' '.join(out['independence_limitations']))

    def test_h01_06_response_contract_keys_are_additive(self):
        plan, w, _ = self.register('exp-1')
        out = self.evaluate('exp-1', plan, w)
        for key in ('independence', 'independent_evaluation', 'holdout_exposure_count', 'recovered_after_interruption', 'result_digest', 'limitations'):
            self.assertIn(key, out)


# --------------------------------------------------------------------------- H02
class HistoryTrustTests(cr.MissionHarness):
    def setUp(self):
        super().setUp()
        self.c, _, results = self.run_mission('m1', cr.OBS2, cr.AUTHORED)
        self.assertEqual(results[-1]['job_state'], 'COMPLETED')
        self.bundle = cr.MissionHarness.bundle(self, self.c, 'm1')
        self.archive = self.fx.ws / 'missions/m1/evidence.sqlite'
        c2, _, _ = self.run_mission('m2', NEWER, as_of_utc='2026-01-02T12:00:00Z', hypotheses=[], coordinator=self.c)
        self.new_bundle = cr.MissionHarness.bundle(self, c2, 'm2')
        self.new_archive = self.fx.ws / 'missions/m2/evidence.sqlite'
        self.history = ThesisHistory(ResearchIntelligence(Path(self.fx.ws) / 'intel.sqlite'))

    def copy_bundle(self, name):
        dst = self.fx.ws / name
        shutil.copytree(self.bundle, dst)
        return dst

    def compare(self, older, archives=None):
        return self.history.compare_missions(older, self.new_bundle, archives=archives or (self.archive, self.new_archive))

    def test_h02_01_verified_bundles_compare_authoritatively(self):
        out = self.compare(self.bundle)
        self.assertEqual((out['metrics']['status'], out['sources']['older']['bundle_trust'], out['sources']['newer']['bundle_trust']),
                         ('COMPARED', 'VERIFIED', 'VERIFIED'))

    def test_h02_02_stripped_companions_are_invalid_not_legacy(self):
        stripped = self.copy_bundle('stripped')
        for name in ('report_content.json', 'report_manifest.json'):
            (stripped / name).unlink()
        with self.assertRaises(HistoryError) as cm:                           # finalization attested the companions; their removal is tampering
            self.compare(stripped)
        self.assertEqual(cm.exception.code, 'BUNDLE_INVALID')

    def test_h02_03_altered_final_verification_never_elevates_trust(self):
        # (a) without an archive nothing can be re-derived: a forged "SEMANTICALLY_VERIFIED" attestation must not raise trust
        forged = self.copy_bundle('forged_attestation')
        fv = json.loads((forged / 'final_verification.json').read_text())
        fv['report_companions'] = {'state': 'PASS', 'verdict': 'SEMANTICALLY_VERIFIED', 'checked': True}
        (forged / 'final_verification.json').write_text(json.dumps(fv))
        out = self.history.compare_missions(forged, self.new_bundle)
        self.assertNotEqual(out['sources']['older']['bundle_trust'], 'VERIFIED')
        self.assertEqual((out['metrics']['status'], out['metrics']['items']), ('REFUSED_UNVERIFIED_BUNDLE', []))
        # (b) companions stripped AND the attestation removed: the bundle may read as legacy but is never trusted or compared
        stripped = self.copy_bundle('stripped_and_edited')
        for name in ('report_content.json', 'report_manifest.json'):
            (stripped / name).unlink()
        fv = json.loads((stripped / 'final_verification.json').read_text())
        fv.pop('report_companions', None)
        (stripped / 'final_verification.json').write_text(json.dumps(fv))
        try:
            out = self.compare(stripped)
        except HistoryError as exc:                                           # rejected outright is equally fail-closed
            self.assertEqual(exc.code, 'BUNDLE_INVALID')
        else:
            self.assertNotEqual(out['sources']['older']['bundle_trust'], 'VERIFIED')
            self.assertEqual((out['metrics']['status'], out['metrics']['items']), ('REFUSED_LEGACY_UNVERIFIED', []))
            self.assertEqual(out['falsifiers']['status'], 'NOT_AVAILABLE')

    def test_h02_04_legitimate_legacy_bundle_is_readable_but_qualified(self):
        legacy = self.copy_bundle('legacy')
        for name in ('report_content.json', 'report_manifest.json'):
            (legacy / name).unlink()
        fv = json.loads((legacy / 'final_verification.json').read_text())
        fv.pop('report_companions', None)                                     # a genuine pre-P2 bundle carries no attestation
        (legacy / 'final_verification.json').write_text(json.dumps(fv))
        loaded = self.history._load_bundle(legacy, self.archive)
        if loaded['verification']['status'] == 'INVALID':                     # final_verification is digest-covered here: tampering detected
            self.skipTest('final_verification.json is digest-covered; the edited bundle is rejected outright (stricter than required)')
        self.assertEqual(self.history._trust(loaded), 'LEGACY_UNVERIFIED')
        out = self.compare(legacy)
        self.assertEqual((out['metrics']['status'], out['metrics']['items']), ('REFUSED_LEGACY_UNVERIFIED', []))
        self.assertEqual(out['sources']['older']['bundle_trust'], 'LEGACY_UNVERIFIED')
        self.assertTrue(out['evidence_coverage']['common'] or out['evidence_coverage']['missing_now'] or out['evidence_coverage']['newly_present'])  # still readable

    def test_h02_05_trust_classes_are_distinct_and_only_verified_is_trusted(self):
        def b(status, companions):
            return {'verification': {'status': status, 'companions': {'status': companions}}}
        table = {
            ('COMPLETE', 'SEMANTICALLY_VERIFIED'): 'VERIFIED',
            ('INCOMPLETE', 'SEMANTICALLY_VERIFIED'): 'VERIFIED',
            ('COMPLETE', 'ABSENT_UNATTESTED_LEGACY'): 'LEGACY_UNVERIFIED',
            ('COMPLETE', 'ABSENT_DECLARED_V1'): 'LEGACY_UNVERIFIED',
            ('COMPLETE', 'NOT_VERIFIED_NO_ARCHIVE'): 'UNVERIFIED',
            ('UNKNOWN', 'SEMANTICALLY_VERIFIED'): 'UNVERIFIED',
            ('COMPLETE', 'FAILED'): 'UNVERIFIED',            # load_bundle already maps FAILED companions to status INVALID
            ('INVALID', 'SEMANTICALLY_VERIFIED'): 'INVALID',
        }
        for (status, companions), expected in table.items():
            self.assertEqual(ThesisHistory._trust(b(status, companions)), expected, (status, companions))
            self.assertEqual(ThesisHistory._trusted(b(status, companions)), expected == 'VERIFIED')

    def test_h02_06_loaded_bundle_trust_is_verified_with_archive(self):
        loaded = self.history._load_bundle(self.bundle, self.archive)
        self.assertEqual(self.history._trust(loaded), 'VERIFIED')


# --------------------------------------------------------------------------- H03
class VerifyCliTests(cr.MissionHarness):
    def setUp(self):
        super().setUp()
        self.c, _, _ = self.run_mission('m1', cr.OBS2, cr.AUTHORED)
        self.bundle = cr.MissionHarness.bundle(self, self.c, 'm1')
        self.archive = self.fx.ws / 'missions/m1/evidence.sqlite'

    def run_cli(self, *args):
        proc = subprocess.run([sys.executable, str(SCRIPT), *map(str, args)], capture_output=True, text=True)
        return proc.returncode, json.loads(proc.stdout) if proc.stdout.strip() else None, proc.stderr

    def test_h03_01_valid_archive_is_verified_exit_0(self):
        code, out, _ = self.run_cli(self.bundle, '--archive', self.archive)
        self.assertEqual((code, out['verification_status'], out['evidence_verification'], out['not_performed']), (0, 'VERIFIED', 'PERFORMED', []))
        self.assertEqual(out['report_companions']['status'], 'SEMANTICALLY_VERIFIED')

    def test_h03_02_missing_archive_never_succeeds(self):
        # a bundle that cites evidence cannot even reach COMPLETE without an archive: fail closed
        code, out, _ = self.run_cli(self.bundle)
        self.assertEqual((code, out['verification_status'], out['evidence_verification']), (1, 'FAILED', 'NOT_PERFORMED'))
        self.assertEqual(out['report_companions']['status'], 'NOT_VERIFIED_NO_ARCHIVE')
        self.assertIn('EVIDENCE_UNRESOLVED_NO_ARCHIVE', {d['code'] for d in out['diagnostics']})
        code, out, _ = self.run_cli(self.bundle, '--allow-partial')            # the opt-in never rescues a failure
        self.assertEqual((code, out['verification_status']), (1, 'FAILED'))

    def test_h03_02b_integrity_only_bundle_is_partial_exit_2(self):
        fixture = ROOT / 'tests' / 'fixtures' / 'synthetic-mission-bundle'      # cites no archived evidence: integrity can pass alone
        code, out, err = self.run_cli(fixture)
        self.assertEqual((code, out['status'], out['verification_status'], out['evidence_verification']), (2, 'COMPLETE', 'PARTIALLY_VERIFIED', 'NOT_PERFORMED'))
        self.assertIn('not a verification success', err)
        self.assertTrue(out['not_performed'])

    def test_h03_03_nonexistent_archive_path_is_also_partial_not_pass(self):
        code, out, _ = self.run_cli(self.bundle, '--archive', self.fx.ws / 'nope.sqlite')
        self.assertNotEqual(code, 0)
        self.assertNotEqual(out['verification_status'], 'VERIFIED')

    def test_h03_04_allow_partial_changes_exit_code_only(self):
        fixture = ROOT / 'tests' / 'fixtures' / 'synthetic-mission-bundle'
        code, out, _ = self.run_cli(fixture, '--allow-partial')
        self.assertEqual((code, out['verification_status'], out['exit_code']), (0, 'PARTIALLY_VERIFIED', 2))

    def test_h03_05_invalid_bundle_exit_1(self):
        bad = self.fx.ws / 'bad'
        shutil.copytree(self.bundle, bad)
        (bad / 'report.md').write_text('tampered', encoding='utf-8')
        code, out, _ = self.run_cli(bad, '--archive', self.archive, '--allow-partial')
        self.assertEqual((code, out['verification_status']), (1, 'FAILED'))   # --allow-partial never rescues a failure

    def test_h03_06_verified_failure_of_companions_exit_1(self):
        forged = self.fx.ws / 'forged'
        shutil.copytree(self.bundle, forged)
        cr.forge(forged, lambda c: cr.claim(c, 'R:o1').__setitem__('value', '500'), rewrite_md={'100': '500'})
        code, out, _ = self.run_cli(forged, '--archive', self.archive)
        self.assertEqual((code, out['verification_status'], out['report_companions']['status']), (1, 'FAILED', 'FAILED'))

    def test_h03_07_stripped_companions_exit_1(self):
        stripped = self.fx.ws / 'stripped'
        shutil.copytree(self.bundle, stripped)
        for name in ('report_content.json', 'report_manifest.json'):
            (stripped / name).unlink()
        code, out, _ = self.run_cli(stripped, '--archive', self.archive)
        self.assertEqual((code, out['verification_status']), (1, 'FAILED'))

    def test_h03_08_classify_is_pure_and_total(self):
        sys.path.insert(0, str(ROOT / 'scripts'))
        import verify_mission as vm
        ok = {'status': 'COMPLETE', 'layers': {'source_provenance': {'state': 'ARCHIVE_RESOLVED'}}}
        self.assertEqual(vm.classify(ok, {'status': 'SEMANTICALLY_VERIFIED'})['verification_status'], 'VERIFIED')
        self.assertEqual(vm.classify(ok, {'status': 'ABSENT_DECLARED_V1'})['verification_status'], 'PARTIALLY_VERIFIED')   # consistent with history
        self.assertEqual(vm.classify(ok, {'status': 'ABSENT_UNATTESTED_LEGACY'})['verification_status'], 'PARTIALLY_VERIFIED')
        self.assertEqual(vm.classify(ok, {'status': 'NOT_VERIFIED_NO_ARCHIVE'})['verification_status'], 'PARTIALLY_VERIFIED')
        self.assertEqual(vm.classify(ok, {'status': 'FAILED'})['verification_status'], 'FAILED')
        self.assertEqual(vm.classify({**ok, 'status': 'INCOMPLETE'}, {'status': 'SEMANTICALLY_VERIFIED'})['verification_status'], 'FAILED')
        self.assertEqual(vm.classify({'status': 'COMPLETE', 'layers': {}}, {'status': 'SEMANTICALLY_VERIFIED'})['verification_status'], 'PARTIALLY_VERIFIED')


if __name__ == '__main__':
    unittest.main()
