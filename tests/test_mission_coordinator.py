"""P1-A coordinator tests: spec contract, stage pipeline, evidence gates, CLI. Offline and deterministic."""
import copy
import json
import os
import sqlite3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))

import mission as mission_cli  # noqa: E402
from research_lab.completion import verify  # noqa: E402
from research_lab.jobs import JobStore  # noqa: E402
from research_lab.mission_spec import MissionError, spec_digest, validate_spec  # noqa: E402
from tests.p1_support import (AS_OF, INSTRUMENT, METRIC, REVIEWERS, Fixture, obs_row, run_to_end, signal_row, ts)  # noqa: E402


class Base(unittest.TestCase):
    def setUp(self):
        self.fx = Fixture()
        self.addCleanup(self.fx.close)
        self.c = self.fx.coordinator()

    def run_mission(self, spec=None, **kw):
        spec = spec or self.fx.spec(**kw)
        self.c.create_mission(spec)
        results = run_to_end(self.c, spec['mission_id'])
        return spec, results, self.c.inspect_mission(spec['mission_id'])

    def stage_json(self, spec, stage, name):
        base = self.fx.ws / spec['output_dir'] / 'stages' / stage
        (directory,) = list(base.iterdir())
        return json.loads((directory / name).read_text())

    def codes(self, results):
        return [d['code'] for r in results for d in r['diagnostics']]


class SpecContractTests(Base):
    def invalid(self, spec, code=None):
        with self.assertRaises(MissionError) as cm:
            validate_spec(spec)
        self.assertEqual(cm.exception.code, 'INVALID_MISSION_SPEC')
        if code:
            self.assertIn(code, [d['code'] for d in cm.exception.diagnostics])
        return cm.exception

    def test_valid_spec_and_canonical_digest(self):
        spec = self.fx.spec()
        self.assertIs(validate_spec(spec), spec)
        reordered = dict(reversed(list(spec.items())))
        self.assertEqual(spec_digest(spec), spec_digest(reordered))

    def test_missing_unsupported_and_wrong_schema_have_distinct_diagnostics(self):
        spec = self.fx.spec()
        broken = {k: v for k, v in spec.items() if k != 'decision_question'}
        broken['command'] = 'rm -rf /'
        broken['schema'] = 'research-mission-execution.v0'
        err = self.invalid(broken)
        codes = {d['code'] for d in err.diagnostics}
        self.assertEqual(codes, {'SPEC_FIELD_MISSING', 'SPEC_FIELD_UNSUPPORTED', 'SPEC_SCHEMA_UNSUPPORTED'})
        self.invalid('not an object', 'SPEC_NOT_OBJECT')

    def test_no_executable_content_unknown_operation_rejected(self):
        spec = self.fx.spec()
        spec['permitted_operations'] = ['plan.freeze_spec', 'os.system', 'builtins:eval']
        self.invalid(spec, 'SPEC_OPERATION_UNKNOWN')
        for key in ('script', 'shell', 'import', 'command', 'url'):
            self.invalid({**self.fx.spec(), key: 'x'}, 'SPEC_FIELD_UNSUPPORTED')

    def test_unsafe_paths_rejected(self):
        for bad in ('../escape', '/abs/path', 'a/../b', 'a\\b', '', 'a//b', './a'):
            self.invalid(self.fx.spec(output_dir=bad), 'SPEC_PATH_UNSAFE')
        spec = self.fx.spec()
        spec['inputs']['research_run']['path'] = '../outside.json'
        self.invalid(spec)

    def test_symlink_output_dir_rejected_at_creation(self):
        (self.fx.ws / 'real').mkdir()
        os.symlink(self.fx.ws / 'real', self.fx.ws / 'linked')
        spec = self.fx.spec(output_dir='linked/mission')
        with self.assertRaises(MissionError) as cm:
            self.c.create_mission(spec)
        self.assertEqual(cm.exception.code, 'INVALID_MISSION_SPEC')

    def test_field_validation(self):
        base = self.fx.spec()
        cases = {
            'mission_id': 'bad id!', 'as_of_utc': '2026-01-01T12:00:00', 'max_age_seconds': -1,
            'instruments': [], 'required_metrics': [], 'permitted_operations': [],
        }
        for field, value in cases.items():
            self.invalid({**base, field: value})
        self.invalid({**base, 'max_age_seconds': True})
        self.invalid({**base, 'conflict_tolerance': 'NaN'})
        self.invalid({**base, 'costs': {'x': float('nan')}}, 'SPEC_NOT_CANONICAL')
        self.invalid({**base, 'hypotheses': []}, 'SPEC_FIELD_MISSING')   # absence needs an explicit reason
        self.invalid({**base, 'no_hypotheses_reason': 'x'})              # reason only valid with no hypotheses

    def test_coordinator_never_accepts_validated_as_an_outcome(self):
        spec = self.fx.spec(accepted=('VALIDATED',))
        self.invalid(spec, 'SPEC_FIELD_INVALID')

    def test_required_reviews_are_fixed_to_the_verifier_gate(self):
        spec = self.fx.spec()
        spec['completion_requirements']['required_reviews'] = ['source_verifier']
        self.invalid(spec)


class CreationTests(Base):
    def test_t01_creation_records_immutable_identity(self):
        spec = self.fx.spec()
        info = self.c.create_mission(spec)
        self.assertEqual((info['created'], info['spec_digest']), (True, spec_digest(spec)))
        view = self.c.inspect_mission('m1')
        self.assertEqual(view['spec_digest'], spec_digest(spec))
        self.assertEqual(view['job']['state'], 'NEW')
        self.assertEqual([s['status'] for s in view['stages']], ['PENDING'] * 7)
        self.assertEqual(view['job']['payload_digest'], spec_digest(spec))

    def test_t02_identical_creation_is_idempotent(self):
        spec = self.fx.spec()
        self.c.create_mission(spec)
        self.c.advance_mission('m1')
        again = self.c.create_mission(copy.deepcopy(spec))
        self.assertFalse(again['created'])
        self.assertEqual(self.c.inspect_mission('m1')['stages'][0]['status'], 'SUCCEEDED')  # progress untouched

    def test_t03_changed_inputs_under_same_id_rejected(self):
        self.c.create_mission(self.fx.spec())
        changed = self.fx.spec(max_age=3600)
        with self.assertRaises(MissionError) as cm:
            self.c.create_mission(changed)
        self.assertEqual(cm.exception.code, 'MISSION_ALREADY_EXISTS')
        self.assertEqual(self.c.inspect_mission('m1')['spec_digest'], spec_digest(self.fx.spec()))

    def test_output_dir_cannot_be_shared_between_missions(self):
        self.c.create_mission(self.fx.spec('a', output_dir='shared'))
        with self.assertRaises(MissionError):
            self.c.create_mission(self.fx.spec('b', output_dir='shared'))

    def test_unknown_mission(self):
        with self.assertRaises(MissionError) as cm:
            self.c.advance_mission('nope')
        self.assertEqual(cm.exception.code, 'MISSION_NOT_FOUND')

    def test_interrupted_creation_is_repaired_by_idempotent_create(self):
        spec = self.fx.spec()
        self.c.create_mission(spec)
        with sqlite3.connect(self.c.db_path) as db:
            db.execute('DELETE FROM jobs')
        self.assertEqual(self.c.inspect_mission('m1')['job']['state'], 'UNREGISTERED')
        self.c.create_mission(spec)
        self.assertEqual(self.c.inspect_mission('m1')['job']['state'], 'NEW')


class FullMissionTests(Base):
    def test_t04_g03_complete_offline_mission_produces_verified_outputs(self):
        spec, results, view = self.run_mission()
        self.assertEqual([r['stage'] for r in results], list(__import__('research_lab.mission_spec', fromlist=['STAGES']).STAGES))
        self.assertEqual((results[-1]['outcome'], view['job']['state']), ('COMPLETED', 'COMPLETED'))
        self.assertTrue(all(s['status'] == 'SUCCEEDED' and s['attempts'] == 1 for s in view['stages']))
        final = view['final']
        self.assertEqual((final['verification_status'], final['completion_state'], final['research_state']),
                         ('COMPLETE', 'COMPLETE', 'EVALUABLE'))
        bundle = self.fx.ws / final['bundle_dir']
        independent = verify(bundle, archive=self.fx.ws / spec['output_dir'] / 'evidence.sqlite')
        self.assertEqual((independent['status'], independent['diagnostics']), ('COMPLETE', []))
        self.assertEqual(independent['layers']['source_provenance']['state'], 'ARCHIVE_RESOLVED')
        self.assertEqual(independent['authenticity'], 'NOT_AUTHENTICATED')
        for name in ('report.md', 'report.html', 'mission.json', 'final_verification.json'):
            self.assertTrue((bundle / name).is_file(), name)

    def test_g02_job_state_and_scientific_state_are_separate_and_never_validated(self):
        _, _, view = self.run_mission()
        self.assertEqual(view['job']['state'], 'COMPLETED')
        self.assertEqual(view['final']['scientific_validity'], 'NOT_ESTABLISHED')
        self.assertNotIn('VALIDATED', json.dumps(view['final']))
        self.assertNotIn(view['final']['research_state'], {'VALIDATED'})
        self.assertEqual(set(view['job']) >= {'state', 'revision', 'payload_digest', 'result_digest'}, True)

    def test_report_is_deterministic_and_honest_about_what_it_did_not_author(self):
        spec, _, view = self.run_mission()
        bundle = self.fx.ws / view['final']['bundle_dir']
        md = (bundle / 'report.md').read_text()
        self.assertIn('Network calls by the coordinator: 0', md)
        self.assertIn('Not produced', md)                       # bull/bear/scenarios are not invented
        self.assertIn(AS_OF, md)
        self.assertNotIn('Generated', md)
        fx2 = Fixture()
        self.addCleanup(fx2.close)
        c2 = fx2.coordinator()
        c2.create_mission(fx2.spec())
        run_to_end(c2, 'm1')
        other = fx2.ws / c2.inspect_mission('m1')['final']['bundle_dir']
        self.assertEqual((other / 'report.md').read_bytes(), (bundle / 'report.md').read_bytes())
        self.assertEqual((other / 'report.html').read_bytes(), (bundle / 'report.html').read_bytes())

    def test_repeated_advance_after_completion_is_terminal_and_adds_no_attempts(self):
        _, _, before = self.run_mission()
        for _ in range(3):
            self.assertEqual(self.c.advance_mission('m1')['outcome'], 'TERMINAL')
        after = self.c.inspect_mission('m1')
        self.assertEqual([s['attempts'] for s in after['stages']], [1] * 7)
        self.assertEqual(before['job']['revision'], after['job']['revision'])

    def test_state_domains_use_the_documented_stage_status_vocabulary(self):
        _, _, view = self.run_mission()
        self.assertTrue({s['status'] for s in view['stages']} <= {'PENDING', 'RUNNING', 'SUCCEEDED', 'FAILED', 'BLOCKED', 'SKIPPED'})
        self.assertTrue(view['job']['state'] in {'NEW', 'RUNNING', 'INTERRUPTED', 'RECONCILING', 'COMPLETED', 'FAILED', 'BLOCKED', 'CANCELLED'})


class EvidenceGateTests(Base):
    def test_t05_missing_mandatory_metric_is_explicit_insufficient_evidence(self):
        spec = self.fx.spec(required_metrics=[{'instrument_id': INSTRUMENT, 'metric': METRIC, 'unit': 'USDT'},
                                              {'instrument_id': INSTRUMENT, 'metric': 'spot.depth', 'unit': 'USDT'}])
        _, _, view = self.run_mission(spec)
        self.assertEqual(view['final']['research_state'], 'INSUFFICIENT_EVIDENCE')
        recon = self.stage_json(spec, 'RECONCILING', 'reconciliation.json')
        self.assertEqual(recon['missing_required'], [{'instrument_id': INSTRUMENT, 'metric': 'spot.depth'}])
        self.assertEqual(recon['state'], 'INSUFFICIENT')

    def test_t05_no_evidence_at_all_never_completes(self):
        spec, results, view = self.run_mission(observations=[], signals=[])
        self.assertEqual(view['job']['state'], 'BLOCKED')
        self.assertEqual(view['final']['code'], 'REQUIRED_EVIDENCE_UNAVAILABLE')
        self.assertEqual(view['final']['research_state'], 'INSUFFICIENT_EVIDENCE')
        self.assertIn('no eligible evidence references', view['final']['remaining_work'])

    def test_g04_required_provider_operation_is_never_synthesised(self):
        spec, results, view = self.run_mission(provider_ops=('binance_spot.book',))
        attempts = self.stage_json(spec, 'ACQUIRING', 'acquisition.json')['attempts']
        declared = next(a for a in attempts if a['operation_id'] == 'binance_spot.book')
        self.assertEqual(declared['status'], 'UNATTEMPTED')
        self.assertEqual(declared['source'], 'coordinator_default')
        self.assertEqual(view['job']['state'], 'BLOCKED')
        self.assertEqual(view['final']['code'], 'REQUIRED_EVIDENCE_UNAVAILABLE')
        self.assertTrue(any('binance_spot.book is UNATTEMPTED' in w for w in view['final']['remaining_work']))
        self.assertEqual(self.stage_json(spec, 'ACQUIRING', 'acquisition.json')['network_calls_by_coordinator'], 0)

    def test_supplied_runtime_attempt_record_is_used_and_flagged_unauthenticated(self):
        spec, _, view = self.run_mission(provider_ops=('binance_spot.book',),
                                         attempts=[{'operation_id': 'binance_spot.book', 'status': 'SUCCEEDED', 'observation_ids': ['o1']}])
        attempts = self.stage_json(spec, 'ACQUIRING', 'acquisition.json')['attempts']
        declared = next(a for a in attempts if a['operation_id'] == 'binance_spot.book')
        self.assertEqual((declared['status'], declared['source']), ('SUCCEEDED', 'caller_supplied_record_unauthenticated'))
        self.assertEqual(view['job']['state'], 'COMPLETED')

    def test_bare_success_assertion_is_not_accepted_as_a_succeeded_provider_operation(self):
        for record in ({'operation_id': 'binance_spot.book', 'status': 'SUCCEEDED'},
                       {'operation_id': 'binance_spot.book', 'status': 'SUCCEEDED', 'observation_ids': []},
                       {'operation_id': 'binance_spot.book', 'status': 'SUCCEEDED', 'observation_ids': ['not-ingested']}):
            fx = Fixture()
            self.addCleanup(fx.close)
            c = fx.coordinator()
            spec = fx.spec(provider_ops=('binance_spot.book',), attempts=[record])
            c.create_mission(spec)
            run_to_end(c, 'm1')
            base = fx.ws / spec['output_dir'] / 'stages' / 'ACQUIRING'
            attempts = json.loads((next(base.iterdir()) / 'acquisition.json').read_text())['attempts']
            declared = next(a for a in attempts if a['operation_id'] == 'binance_spot.book')
            self.assertEqual((declared['status'], declared['claimed_status']), ('UNATTEMPTED', 'SUCCEEDED'), record)
            view = c.inspect_mission('m1')
            self.assertEqual((view['job']['state'], view['final']['code']), ('BLOCKED', 'REQUIRED_EVIDENCE_UNAVAILABLE'), record)

    def test_undeclared_or_duplicate_attempt_records_rejected(self):
        for attempts in ([{'operation_id': 'other', 'status': 'SUCCEEDED'}],
                         [{'operation_id': 'a', 'status': 'SUCCEEDED'}, {'operation_id': 'a', 'status': 'FAILED'}],
                         [{'operation_id': 'a', 'status': 'MAYBE'}]):
            fx = Fixture()
            self.addCleanup(fx.close)
            c = fx.coordinator()
            c.create_mission(fx.spec(provider_ops=('a',), attempts=attempts))
            run_to_end(c, 'm1')
            self.assertEqual(c.inspect_mission('m1')['stages'][1]['failure_code'], 'STAGE_PRECONDITION_FAILED')

    def test_t06_contradictory_observations_do_not_pass_as_equivalent_evidence(self):
        rows = [obs_row('o1', '100'), obs_row('o2', '101', source='https://api.other-venue.example/x'), obs_row('o3', '100', instrument=INSTRUMENT, metric='aux')]
        spec, _, view = self.run_mission(observations=rows, signals=[signal_row('s1', ('o1',))])
        recon = self.stage_json(spec, 'RECONCILING', 'reconciliation.json')
        self.assertEqual({e['id']: e['code'] for e in recon['excluded']}, {'o1': 'CONTRADICTORY', 'o2': 'CONTRADICTORY'})
        self.assertEqual(recon['conflicts'][0]['kind'], 'CONTRADICTORY')
        self.assertEqual(recon['state'], 'INSUFFICIENT')
        self.assertNotIn('o1', recon['eligible_observation_ids'])
        self.assertEqual(view['final']['research_state'], 'INSUFFICIENT_EVIDENCE')

    def test_t06_incompatible_units_are_not_merged(self):
        rows = [obs_row('o1', '100'), obs_row('o2', '100', unit='USD', observed=ts(10, 0))]
        spec, _, _ = self.run_mission(observations=rows)
        recon = self.stage_json(spec, 'RECONCILING', 'reconciliation.json')
        self.assertEqual({e['code'] for e in recon['excluded']}, {'INCOMPATIBLE_UNIT'})
        self.assertEqual(recon['eligible_observation_ids'], [])

    def test_unit_must_match_requirement(self):
        spec, _, _ = self.run_mission(observations=[obs_row('o1', '100', unit='USD')])
        recon = self.stage_json(spec, 'RECONCILING', 'reconciliation.json')
        self.assertEqual(recon['excluded'], [{'id': 'o1', 'code': 'UNIT_MISMATCH_WITH_REQUIREMENT'}])

    def test_conflict_tolerance_allows_small_disagreement(self):
        rows = [obs_row('o1', '100'), obs_row('o2', '100.5')]
        spec, _, _ = self.run_mission(observations=rows, extra={'conflict_tolerance': '1'})
        recon = self.stage_json(spec, 'RECONCILING', 'reconciliation.json')
        self.assertEqual((recon['conflicts'], recon['eligible_observation_ids']), ([], ['o1', 'o2']))

    def test_t07_future_dated_and_stale_evidence_fail_eligibility(self):
        rows = [obs_row('future', '100', observed=ts(11, 0), available=ts(13, 0), retrieved=ts(13, 1)),   # available after as_of
                obs_row('stale', '100', observed=ts(1, 0), available=ts(1, 1), retrieved=ts(1, 2)),      # 11h old, budget 2h
                obs_row('noavail', '100'),
                obs_row('fresh', '100', observed=ts(11, 0), available=ts(11, 1), retrieved=ts(11, 2))]
        rows[2]['available_at_utc'] = None
        spec, _, view = self.run_mission(observations=rows, signals=[signal_row('s1', ('fresh',))])
        recon = self.stage_json(spec, 'RECONCILING', 'reconciliation.json')
        self.assertEqual({e['id']: e['code'] for e in recon['excluded']},
                         {'future': 'FUTURE_DATED', 'stale': 'STALE', 'noavail': 'AVAILABILITY_UNKNOWN'})
        self.assertEqual(recon['eligible_observation_ids'], ['fresh'])

    def test_unavailable_observation_is_not_evidence(self):
        row = {**obs_row('o1'), 'status': 'UNAVAILABLE', 'value': None, 'observed_at_utc': None}
        spec, _, _ = self.run_mission(observations=[row], signals=[])
        recon = self.stage_json(spec, 'RECONCILING', 'reconciliation.json')
        self.assertEqual(recon['excluded'], [{'id': 'o1', 'code': 'NOT_OBSERVED'}])

    def test_signal_using_ineligible_evidence_cannot_support_a_hypothesis(self):
        rows = [obs_row('stale', '100', observed=ts(1, 0), available=ts(1, 1), retrieved=ts(1, 2)), obs_row('fresh', '100', observed=ts(11, 0), available=ts(11, 1), retrieved=ts(11, 2))]
        spec, _, view = self.run_mission(observations=rows, signals=[signal_row('s1', ('stale',))])
        analysis = self.stage_json(spec, 'ANALYZING', 'analysis.json')
        self.assertEqual(analysis['hypotheses'][0]['conclusion'], 'INSUFFICIENT_EVIDENCE')
        self.assertEqual(view['final']['research_state'], 'INSUFFICIENT_EVIDENCE')


class ReviewerHandoffTests(Base):
    def test_t13_undeclared_handoffs_are_never_synthesised_into_pass(self):
        spec, _, view = self.run_mission(handoffs='none')
        reviews = self.stage_json(spec, 'VERIFYING', 'verification.json')['reviews']
        self.assertEqual({r['state'] for r in reviews.values()}, {'NOT_PERFORMED'})
        committee = json.loads((self.fx.ws / view['final']['bundle_dir'] / 'committee_verification.json').read_text()) if (self.fx.ws / view['final']['bundle_dir']).exists() else None
        self.assertEqual(view['job']['state'], 'BLOCKED')
        self.assertEqual(view['final']['code'], 'REQUIRED_EVIDENCE_UNAVAILABLE')
        self.assertEqual(view['final']['completion_state'], 'INCOMPLETE')
        self.assertTrue(all('independently produced handoff required' in w for w in view['final']['remaining_work']))
        self.assertNotIn('PASS', {r['state'] for r in committee['reviews'].values()})

    def test_declared_but_absent_handoff_blocks_the_stage_and_resumes_when_it_appears(self):
        missing = {n: {'path': f'inputs/late/{n}.json', 'sha256': '0' * 64} for n in REVIEWERS}
        spec = self.fx.spec(handoffs=missing)
        self.c.create_mission(spec)
        results = run_to_end(self.c, 'm1')
        view = self.c.inspect_mission('m1')
        self.assertEqual((results[-1]['outcome'], results[-1]['stage']), ('BLOCKED', 'VERIFYING'))
        self.assertEqual(results[-1]['diagnostics'][0]['code'], 'REQUIRED_EVIDENCE_UNAVAILABLE')
        self.assertEqual((view['job']['state'], view['waiting_on_stage']), ('RUNNING', ['VERIFYING']))
        # the handoffs arrive, but a different file than the pinned digest is a precondition failure
        self.fx.put('inputs/late/methodology_reviewer.json', {'schema': 'reviewer-handoff.v1', 'reviewer': 'methodology_reviewer', 'state': 'PASS', 'evidence': ['o1']})
        again = self.c.advance_mission('m1')
        self.assertEqual(again['diagnostics'][0]['code'], 'STAGE_PRECONDITION_FAILED')

    def test_handoff_arrival_with_matching_pinned_digest_unblocks_the_mission(self):
        refs = {}
        for name in REVIEWERS:
            body = {'schema': 'reviewer-handoff.v1', 'reviewer': name, 'state': 'PASS', 'evidence': ['o1'], 'produced_by': 'independent-agent'}
            data = json.dumps(body, sort_keys=True).encode()
            import hashlib
            refs[name] = {'path': f'inputs/late/{name}.json', 'sha256': hashlib.sha256(data).hexdigest()}
        self.c.create_mission(self.fx.spec(handoffs=refs))
        results = run_to_end(self.c, 'm1')
        self.assertEqual(results[-1]['outcome'], 'BLOCKED')
        for name in REVIEWERS:
            self.fx.put(refs[name]['path'], {'schema': 'reviewer-handoff.v1', 'reviewer': name, 'state': 'PASS', 'evidence': ['o1'], 'produced_by': 'independent-agent'})
        final = run_to_end(self.c, 'm1')
        self.assertEqual(final[-1]['job_state'], 'COMPLETED')
        reviews = json.loads((self.fx.ws / self.c.inspect_mission('m1')['final']['bundle_dir'] / 'committee_verification.json').read_text())['reviews']
        self.assertTrue(all(r['authenticated'] is False and r['state'] == 'PASS' for r in reviews.values()))

    def test_invalid_pass_handoffs_degrade_to_not_performed(self):
        cases = [dict(evidence=[]),                       # PASS without evidence
                 dict(evidence=['not-eligible']),         # evidence outside the eligible set
                 dict(reviewer='source_verifier')]        # wrong reviewer
        for index, change in enumerate(cases):
            fx = Fixture()
            self.addCleanup(fx.close)
            handoffs = {}
            for name in REVIEWERS:
                body = {'schema': 'reviewer-handoff.v1', 'reviewer': name, 'state': 'PASS', 'evidence': ['o1']}
                if name == 'methodology_reviewer':
                    body.update(change)
                handoffs[name] = fx.put(f'inputs/h/{name}.json', body)
            c = fx.coordinator()
            c.create_mission(fx.spec(handoffs=handoffs))
            run_to_end(c, 'm1')
            view = c.inspect_mission('m1')
            self.assertEqual(view['job']['state'], 'BLOCKED', change)
            self.assertTrue(any('methodology_reviewer review is NOT_PERFORMED' in w for w in view['final']['remaining_work']), change)

    def test_failed_review_is_recorded_as_fail_and_blocks(self):
        handoffs = {n: self.fx.handoff(n, state='FAIL' if n == 'source_verifier' else 'PASS') for n in REVIEWERS}
        _, _, view = self.run_mission(self.fx.spec(handoffs=handoffs))
        self.assertEqual(view['job']['state'], 'BLOCKED')
        self.assertTrue(any('source_verifier review is FAIL' in w for w in view['final']['remaining_work']))

    def test_tampered_handoff_file_fails_precondition(self):
        spec = self.fx.spec()
        self.c.create_mission(spec)
        path = self.fx.ws / spec['inputs']['reviewer_handoffs']['source_verifier']['path']
        path.write_text('{"schema":"reviewer-handoff.v1","reviewer":"source_verifier","state":"PASS","evidence":["o1"],"x":1}')
        results = run_to_end(self.c, 'm1')
        self.assertEqual(results[-1]['outcome'], 'FAILED')
        self.assertEqual(self.c.inspect_mission('m1')['stages'][4]['failure_code'], 'STAGE_PRECONDITION_FAILED')


class CompletionSemanticsTests(Base):
    def test_t14_insufficient_evidence_outcome_is_preserved_through_finalization(self):
        spec = self.fx.spec(required_metrics=[{'instrument_id': INSTRUMENT, 'metric': METRIC, 'unit': 'USDT'},
                                              {'instrument_id': INSTRUMENT, 'metric': 'spot.depth', 'unit': 'USDT'}])
        _, _, view = self.run_mission(spec)
        self.assertEqual(view['job']['state'], 'COMPLETED')              # process reached its acceptance condition
        self.assertEqual(view['final']['research_state'], 'INSUFFICIENT_EVIDENCE')   # and says so
        self.assertEqual(view['final']['verification_status'], 'COMPLETE')
        job = JobStore(self.c.db_path).get('m1')
        self.assertIsNotNone(job['result_digest'])

    def test_unaccepted_research_state_is_not_completed(self):
        spec = self.fx.spec(hypotheses=[], accepted=('EVALUABLE',))
        _, _, view = self.run_mission(spec)
        self.assertEqual(view['final']['research_state'], 'NOT_EVALUABLE')
        self.assertEqual((view['job']['state'], view['final']['code']), ('BLOCKED', 'COMPLETION_REQUIREMENT_UNMET'))

    def test_no_hypotheses_is_not_evaluable_with_recorded_reason(self):
        spec = self.fx.spec(hypotheses=[])
        _, _, view = self.run_mission(spec)
        self.assertEqual(view['final']['research_state'], 'NOT_EVALUABLE')
        self.assertEqual(view['job']['state'], 'COMPLETED')

    def test_failed_declared_evaluation_cannot_be_completed(self):
        task = {'schema': 'research-task-eval.v1', 'task_id': 't', 'allowed_capabilities': [], 'required_metrics': ['spot.depth'],
                'required_disclosures': ['NOT_A_TRADE_SIGNAL']}
        spec, _, view = self.run_mission(extra={'analysis': {'evaluation_task': task}})
        self.assertEqual(self.stage_json(spec, 'ANALYZING', 'analysis.json')['evaluation']['state'], 'NOT_EVALUABLE')
        self.assertEqual(view['job']['state'], 'COMPLETED')   # NOT_EVALUABLE is a legitimate outcome, not a FAIL
        task2 = {'schema': 'research-task-eval.v1', 'task_id': 't', 'allowed_capabilities': [], 'required_disclosures': ['MISSING_DISCLOSURE']}
        fx = Fixture()
        self.addCleanup(fx.close)
        c = fx.coordinator()
        c.create_mission(fx.spec(extra={'analysis': {'evaluation_task': task2}}))
        run_to_end(c, 'm1')
        v2 = c.inspect_mission('m1')
        self.assertEqual((v2['job']['state'], v2['final']['code']), ('BLOCKED', 'COMPLETION_REQUIREMENT_UNMET'))

    def test_input_tamper_after_creation_fails_and_never_runs_on_altered_data(self):
        spec = self.fx.spec()
        self.c.create_mission(spec)
        (self.fx.ws / spec['inputs']['research_run']['path']).write_text('{"schema":"research-run-input.v1","observations":[]}')
        results = run_to_end(self.c, 'm1')
        view = self.c.inspect_mission('m1')
        self.assertEqual((results[-1]['outcome'], view['job']['state']), ('FAILED', 'FAILED'))
        self.assertEqual(view['stages'][1]['failure_code'], 'STAGE_PRECONDITION_FAILED')

    def test_missing_pinned_input_blocks_stage_but_job_stays_running(self):
        spec = self.fx.spec()
        self.c.create_mission(spec)
        (self.fx.ws / spec['inputs']['research_run']['path']).unlink()
        results = run_to_end(self.c, 'm1')
        view = self.c.inspect_mission('m1')
        self.assertEqual((results[-1]['outcome'], view['job']['state'], view['waiting_on_stage']), ('BLOCKED', 'RUNNING', ['ACQUIRING']))
        self.assertEqual(results[-1]['diagnostics'][0]['code'], 'REQUIRED_EVIDENCE_UNAVAILABLE')

    def test_operation_not_permitted_by_spec_fails_closed(self):
        spec = self.fx.spec()
        spec['permitted_operations'].remove('reconcile.sources')
        self.c.create_mission(spec)
        results = run_to_end(self.c, 'm1')
        self.assertEqual(results[-1]['outcome'], 'FAILED')
        self.assertEqual(self.c.inspect_mission('m1')['stages'][0]['failure_code'], 'STAGE_PRECONDITION_FAILED')

    def test_cancel_is_terminal(self):
        self.c.create_mission(self.fx.spec())
        self.c.advance_mission('m1')
        out = self.c.cancel_mission('m1')
        self.assertEqual(out['diagnostics'][0]['code'], 'MISSION_CANCELLED')
        again = self.c.advance_mission('m1')
        self.assertEqual((again['outcome'], again['diagnostics'][0]['code']), ('TERMINAL', 'MISSION_CANCELLED'))
        self.assertEqual(self.c.cancel_mission('m1')['job_state'], 'CANCELLED')   # idempotent


class CliTests(Base):
    def cli(self, *args):
        return mission_cli.run(['--workspace', str(self.fx.ws), *args])

    def test_create_status_advance_resume_cancel(self):
        spec_path = self.fx.ws / 'spec.json'
        spec_path.write_text(json.dumps(self.fx.spec()))
        out, code = self.cli('create', str(spec_path))
        self.assertEqual((code, out['created']), (0, True))
        out, code = self.cli('status', 'm1')
        self.assertEqual((code, out['job']['state']), (0, 'NEW'))
        out, code = self.cli('advance', 'm1')
        self.assertEqual((code, out['stage']), (0, 'PLANNED'))
        out, code = self.cli('resume', 'm1')                                  # nothing to reconcile, then advances one stage
        self.assertEqual((code, out['reconcile']['outcome'], out['advance']['stage']), (0, 'NOTHING_TO_RECONCILE', 'ACQUIRING'))
        out, code = self.cli('advance', 'm1', '--all')
        self.assertEqual((code, out['final']['outcome']), (0, 'COMPLETED'))
        out, code = self.cli('status', 'm1')
        self.assertEqual((code, out['final']['research_state']), (0, 'EVALUABLE'))

    def test_exit_codes_for_blocked_invalid_and_unknown(self):
        spec_path = self.fx.ws / 'spec.json'
        spec_path.write_text(json.dumps(self.fx.spec(handoffs='none')))
        self.cli('create', str(spec_path))
        out, code = self.cli('advance', 'm1', '--all')
        self.assertEqual((code, out['final']['outcome']), (1, 'BLOCKED'))
        out, code = self.cli('status', 'm1')
        self.assertEqual(code, 1)
        spec_path.write_text(json.dumps({'schema': 'x'}))
        out, code = self.cli('create', str(spec_path))
        self.assertEqual((code, out['error']['code']), (2, 'INVALID_MISSION_SPEC'))
        out, code = self.cli('status', 'ghost')
        self.assertEqual((code, out['error']['code']), (2, 'MISSION_NOT_FOUND'))
        out, code = self.cli('create', str(self.fx.ws / 'absent.json'))
        self.assertEqual((code, out['error']['code']), (2, 'INVALID_REQUEST'))

    def test_cancel_via_cli(self):
        spec_path = self.fx.ws / 'spec.json'
        spec_path.write_text(json.dumps(self.fx.spec()))
        self.cli('create', str(spec_path))
        out, code = self.cli('cancel', 'm1')
        self.assertEqual((code, out['job_state']), (0, 'CANCELLED'))


if __name__ == '__main__':
    unittest.main()
