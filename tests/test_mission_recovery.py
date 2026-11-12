"""P1-B tests: crash windows, restart reconciliation, artifact integrity, idempotency, fenced concurrency.

Crashes are modelled by raising ``SimulatedCrash`` (a BaseException) from injected failure points, so no
operation error handling or cleanup runs, exactly like process death. Concurrency tests synchronise with
Events/Barriers; the join timeouts only bound failures."""
import hashlib
import json
import os
import sqlite3
import threading
import unittest
from unittest import mock

from research_lab import mission_coordinator as mc
from research_lab.mission_coordinator import MissionCoordinator, write_immutable
from research_lab.mission_spec import STAGES, MissionError
from tests.p1_support import Fixture, SimulatedCrash, run_to_end, signal_row, obs_row

JOIN = 20


def crash(point, stage=None):
    state = {'armed': True}

    def hook(p, mission_id, s):
        if state['armed'] and p == point and (stage is None or s == stage):
            state['armed'] = False
            raise SimulatedCrash(f'{point}@{s}')
    return hook


def rows(db_path, sql, *args):
    with sqlite3.connect(db_path) as db:
        return db.execute(sql, args).fetchall()


class Base(unittest.TestCase):
    def setUp(self):
        self.fx = Fixture()
        self.addCleanup(self.fx.close)

    def start(self, mission_id='m1', hook=None, **spec_kw):
        c = self.fx.coordinator(fail_at=hook)
        c.create_mission(self.fx.spec(mission_id, **spec_kw))
        return c

    def restart(self):
        """A fresh coordinator object over the same workspace: the 'new process' after a crash."""
        return self.fx.coordinator()

    def advance_until_crash(self, c, mission_id='m1'):
        with self.assertRaises(SimulatedCrash):
            for _ in range(10):
                c.advance_mission(mission_id)

    def stage(self, c, name, mission_id='m1'):
        return next(s for s in c.inspect_mission(mission_id)['stages'] if s['stage_id'] == name)

    def finish(self, c, mission_id='m1'):
        results = run_to_end(c, mission_id)
        self.assertEqual(results[-1]['job_state'], 'COMPLETED', results[-1])
        return c.inspect_mission(mission_id)

    def transitions(self, c, mission_id='m1'):
        return [(r[0], r[1]) for r in rows(c.db_path, 'SELECT before,after FROM transitions WHERE id=? ORDER BY revision', mission_id)]


class CrashWindowTests(Base):
    def test_t08_crash_before_stage_admission_needs_no_reconciliation(self):
        c = self.start(hook=crash('before_admission', 'ACQUIRING'))
        self.advance_until_crash(c)
        c2 = self.restart()
        self.assertEqual((self.stage(c2, 'ACQUIRING')['status'], self.stage(c2, 'ACQUIRING')['attempts']), ('PENDING', 0))
        self.assertEqual(c2.reconcile_mission('m1')['outcome'], 'NOTHING_TO_RECONCILE')
        view = self.finish(c2)
        self.assertTrue(all(s['attempts'] == 1 for s in view['stages']))

    def test_t08_crash_after_admission_is_not_auto_resumed_and_reconciles_as_absent(self):
        c = self.start(hook=crash('after_admission', 'RECONCILING'))
        self.advance_until_crash(c)
        c2 = self.restart()
        self.assertEqual(self.stage(c2, 'RECONCILING')['status'], 'RUNNING')
        blocked = c2.advance_mission('m1')
        self.assertEqual((blocked['outcome'], blocked['diagnostics'][0]['code']), ('RECONCILIATION_REQUIRED', 'RECOVERY_RECONCILIATION_REQUIRED'))
        self.assertEqual(self.stage(c2, 'RECONCILING')['status'], 'RUNNING')       # not silently re-run
        recovered = c2.reconcile_mission('m1')
        self.assertEqual(recovered['reconciliation']['classification'], 'ABSENT')
        self.assertEqual(recovered['reconciliation']['decision'], 'RESUMED_DETERMINISTIC_OPERATION')
        view = self.finish(c2)
        self.assertEqual(self.stage(c2, 'RECONCILING')['attempts'], 2)
        self.assertEqual(self.transitions(c2), [('NEW', 'RUNNING'), ('RUNNING', 'INTERRUPTED'), ('INTERRUPTED', 'RECONCILING'),
                                                ('RECONCILING', 'RUNNING'), ('RUNNING', 'COMPLETED')])
        self.assertEqual([r['decision'] for r in view['reconciliations']], ['RESUMED_DETERMINISTIC_OPERATION'])

    def test_t09_crash_after_artifact_write_adopts_verified_output_without_duplicating_effects(self):
        c = self.start(hook=crash('after_artifact_write', 'ACQUIRING'))
        self.advance_until_crash(c)
        out = self.fx.ws / 'missions' / 'm1'
        written = sorted(p for p in (out / 'stages' / 'ACQUIRING').rglob('*.json'))
        self.assertEqual(len(written), 1)
        before = hashlib.sha256(written[0].read_bytes()).hexdigest()
        inode = written[0].stat().st_ino
        archive_rows = rows(out / 'evidence.sqlite', 'SELECT count(*) FROM observations')
        self.assertEqual(self.stage(c, 'ACQUIRING')['status'], 'RUNNING')           # artifact exists, state does not say so
        c2 = self.restart()
        result = c2.reconcile_mission('m1')
        self.assertEqual((result['reconciliation']['classification'], result['reconciliation']['decision']),
                         ('COMPLETE', 'ADOPTED_VERIFIED_OUTPUT'))
        self.assertEqual(written[0].stat().st_ino, inode)                           # not rewritten
        self.assertEqual(hashlib.sha256(written[0].read_bytes()).hexdigest(), before)
        self.assertEqual(rows(out / 'evidence.sqlite', 'SELECT count(*) FROM observations'), archive_rows)
        self.assertEqual(len(list((out / 'stages' / 'ACQUIRING').iterdir())), 1)    # one op-key directory only
        self.finish(c2)

    def test_t09_partial_outputs_are_completed_not_trusted(self):
        c = self.start(hook=crash('after_artifact_write', 'RENDERING'))
        self.advance_until_crash(c)
        directory = next((self.fx.ws / 'missions' / 'm1' / 'stages' / 'RENDERING').iterdir())
        (directory / 'report.html').unlink()                                        # simulate a torn write
        result = self.restart().reconcile_mission('m1')
        self.assertEqual(result['reconciliation']['classification'], 'PARTIAL')
        self.assertTrue((directory / 'report.html').is_file())
        self.finish(self.restart())

    def test_crash_after_state_commit_before_downstream_just_continues(self):
        c = self.start(hook=crash('after_state_commit', 'ANALYZING'))
        self.advance_until_crash(c)
        c2 = self.restart()
        self.assertEqual(self.stage(c2, 'ANALYZING')['status'], 'SUCCEEDED')
        self.assertEqual(c2.reconcile_mission('m1')['outcome'], 'NOTHING_TO_RECONCILE')
        self.assertEqual(c2.advance_mission('m1')['stage'], 'VERIFYING')
        self.finish(c2)

    def test_crash_after_final_stage_commit_before_job_completion_is_completed_on_resume(self):
        c = self.start(hook=crash('after_state_commit', 'FINALIZING'))
        self.advance_until_crash(c)
        c2 = self.restart()
        self.assertEqual(c2.inspect_mission('m1')['job']['state'], 'RUNNING')
        self.assertTrue(all(s['status'] == 'SUCCEEDED' for s in c2.inspect_mission('m1')['stages']))
        result = c2.advance_mission('m1')
        self.assertEqual((result['outcome'], result['job_state']), ('COMPLETED', 'COMPLETED'))
        self.assertTrue(all(s['attempts'] == 1 for s in c2.inspect_mission('m1')['stages']))

    def test_crash_during_report_generation_and_final_verification(self):
        for point, stage in (('during_render', 'RENDERING'), ('during_final_verify', 'FINALIZING')):
            fx = Fixture()
            self.addCleanup(fx.close)
            c = fx.coordinator(fail_at=crash(point, stage))
            c.create_mission(fx.spec())
            with self.assertRaises(SimulatedCrash):
                for _ in range(10):
                    c.advance_mission('m1')
            c2 = fx.coordinator()
            self.assertEqual(c2.reconcile_mission('m1')['reconciliation']['classification'], 'ABSENT', point)
            results = run_to_end(c2, 'm1')
            self.assertEqual(results[-1]['job_state'], 'COMPLETED', point)
            self.assertEqual(c2.inspect_mission('m1')['final']['verification_status'], 'COMPLETE', point)
            self.assertEqual(list((fx.ws / 'missions' / 'm1').glob('.finalize-*')), [], point)   # scratch cleaned on the clean path

    def test_reconcile_on_a_job_stuck_in_interrupted_without_a_claim_resumes_safely(self):
        c = self.start()
        c.advance_mission('m1')
        c.jobs.move('m1', 'RUNNING', 'INTERRUPTED', self.fx.clock())
        self.assertEqual(c.advance_mission('m1')['diagnostics'][0]['code'], 'RECOVERY_RECONCILIATION_REQUIRED')
        self.assertEqual(c.reconcile_mission('m1')['outcome'], 'RECONCILED')
        self.finish(c)

    def test_reconcile_survives_its_own_crash_midway(self):
        # (1) crash after the takeover wrote artifacts but before the state commit: job RUNNING + claimed stage
        c = self.start(hook=crash('after_admission', 'ACQUIRING'))
        self.advance_until_crash(c)
        c2 = self.fx.coordinator(fail_at=crash('after_artifact_write', 'ACQUIRING'))
        with self.assertRaises(SimulatedCrash):
            c2.reconcile_mission('m1')
        self.assertEqual(c2.inspect_mission('m1')['job']['state'], 'RUNNING')
        self.assertEqual(c2.advance_mission('m1')['diagnostics'][0]['code'], 'RECOVERY_RECONCILIATION_REQUIRED')
        c3 = self.restart()
        self.assertEqual(c3.reconcile_mission('m1')['reconciliation']['decision'], 'ADOPTED_VERIFIED_OUTPUT')
        self.finish(c3)

    def test_crash_while_job_is_reconciling_is_recoverable(self):
        # (2) crash inside the recompute, i.e. while the job itself is RECONCILING
        c = self.start(hook=crash('after_admission', 'RENDERING'))
        self.advance_until_crash(c)
        c2 = self.fx.coordinator(fail_at=crash('during_render', 'RENDERING'))
        with self.assertRaises(SimulatedCrash):
            c2.reconcile_mission('m1')
        self.assertEqual(c2.inspect_mission('m1')['job']['state'], 'RECONCILING')
        self.assertEqual(c2.advance_mission('m1')['diagnostics'][0]['code'], 'RECOVERY_RECONCILIATION_REQUIRED')
        c3 = self.restart()
        self.assertEqual(c3.reconcile_mission('m1')['reconciliation']['classification'], 'ABSENT')
        self.finish(c3)


class TamperTests(Base):
    def artifact(self, stage, name, mission_id='m1'):
        directory = next((self.fx.ws / 'missions' / mission_id / 'stages' / stage).iterdir())
        return directory / name

    def test_t10_tampered_completed_output_is_rejected_by_advance(self):
        c = self.start()
        for _ in range(3):
            c.advance_mission('m1')
        self.artifact('ACQUIRING', 'acquisition.json').write_text('{"tampered": true}')
        result = c.advance_mission('m1')
        self.assertEqual((result['outcome'], result['job_state']), ('BLOCKED', 'BLOCKED'))
        self.assertEqual(result['diagnostics'][0]['code'], 'STAGE_OUTPUT_DIGEST_MISMATCH')
        self.assertEqual(c.inspect_mission('m1')['stages'][3]['status'], 'PENDING')       # never ran on tampered input
        self.assertEqual(c.advance_mission('m1')['outcome'], 'TERMINAL')

    def test_t10_tampered_or_missing_output_is_rejected_by_reconcile(self):
        for mutate, code in ((lambda p: p.write_text('x'), 'STAGE_OUTPUT_DIGEST_MISMATCH'),
                             (lambda p: p.unlink(), 'STAGE_OUTPUT_MISSING'),
                             (lambda p: (p.unlink(), os.symlink(__file__, p)), 'STAGE_OUTPUT_DIGEST_MISMATCH')):
            fx = Fixture()
            self.addCleanup(fx.close)
            c = fx.coordinator(fail_at=crash('after_state_commit', 'ANALYZING'))
            c.create_mission(fx.spec())
            with self.assertRaises(SimulatedCrash):
                for _ in range(10):
                    c.advance_mission('m1')
            path = next((fx.ws / 'missions' / 'm1' / 'stages' / 'RECONCILING').iterdir()) / 'reconciliation.json'
            mutate(path)
            result = fx.coordinator().reconcile_mission('m1')
            self.assertEqual(result['outcome'], 'BLOCKED', code)
            self.assertTrue({d['code'] for d in result['diagnostics']} >= {'RECOVERY_RECONCILIATION_REQUIRED', 'UNSAFE_REPLAY_BLOCKED', code})
            view = fx.coordinator().inspect_mission('m1')
            self.assertEqual(view['job']['state'], 'BLOCKED')
            self.assertEqual(view['reconciliations'][-1]['decision'], 'BLOCKED_COMPLETED_STAGE_OUTPUT_INVALID')

    def test_t10_contradictory_partial_output_blocks_and_retains_evidence(self):
        c = self.start(hook=crash('after_artifact_write', 'RECONCILING'))
        self.advance_until_crash(c)
        path = self.artifact('RECONCILING', 'reconciliation.json')
        path.write_text('{"forged": true}')
        c2 = self.restart()
        result = c2.reconcile_mission('m1')
        self.assertEqual(result['outcome'], 'BLOCKED')
        self.assertTrue({'STAGE_OUTPUT_DIGEST_MISMATCH', 'UNSAFE_REPLAY_BLOCKED', 'RECOVERY_RECONCILIATION_REQUIRED'}
                        <= {d['code'] for d in result['diagnostics']})
        self.assertEqual(path.read_text(), '{"forged": true}')                           # evidence kept, never overwritten
        view = c2.inspect_mission('m1')
        self.assertEqual((view['job']['state'], self.stage(c2, 'RECONCILING')['status']), ('BLOCKED', 'BLOCKED'))
        self.assertEqual(view['final']['code'], 'RECOVERY_RECONCILIATION_REQUIRED')
        self.assertEqual(view['reconciliations'][-1]['classification'], 'CONTRADICTORY')

    def test_existing_file_is_never_proof_of_success(self):
        c = self.start()
        c.advance_mission('m1')
        spec, sdigest = c._mission('m1')
        key = c._op_key('m1', 'ACQUIRING', c._input_digest(sdigest, 'ACQUIRING', c._stages('m1')))
        planted = c._stage_dir(spec, 'ACQUIRING', key) / 'acquisition.json'
        planted.parent.mkdir(parents=True)
        planted.write_text('{"planted": true}')
        result = c.advance_mission('m1')
        self.assertEqual((result['outcome'], result['diagnostics'][0]['code']), ('FAILED', 'STAGE_OUTPUT_DIGEST_MISMATCH'))
        self.assertEqual(planted.read_text(), '{"planted": true}')

    def test_write_immutable_contract(self):
        path = self.fx.ws / 'x' / 'a.json'
        digest = write_immutable(path, b'one')
        inode = path.stat().st_ino
        self.assertEqual(write_immutable(path, b'one'), digest)
        self.assertEqual(path.stat().st_ino, inode)
        with self.assertRaises(MissionError) as cm:
            write_immutable(path, b'two')
        self.assertEqual(cm.exception.code, 'STAGE_OUTPUT_DIGEST_MISMATCH')
        self.assertEqual(path.read_bytes(), b'one')
        self.assertEqual([p.name for p in path.parent.iterdir()], ['a.json'])           # no stray temp files


class IdempotencyTests(Base):
    def test_g06_completed_operation_is_not_repeated(self):
        c = self.start()
        view = self.finish(c)
        digests = {s['stage_id']: s['output_digest'] for s in view['stages']}
        with mock.patch.dict(mc.OPERATIONS, {s: mock.Mock(side_effect=AssertionError('re-executed')) for s in STAGES}):
            for _ in range(3):
                self.assertEqual(c.advance_mission('m1')['outcome'], 'TERMINAL')
            self.assertEqual(c.reconcile_mission('m1')['outcome'], 'NOTHING_TO_RECONCILE')
        after = c.inspect_mission('m1')
        self.assertEqual({s['stage_id']: s['output_digest'] for s in after['stages']}, digests)
        self.assertEqual(sum(s['attempts'] for s in after['stages']), 7)

    def test_operation_identity_conflict_when_input_differs_under_same_stage(self):
        c = self.start()
        c.advance_mission('m1')
        with sqlite3.connect(c.db_path) as db:
            db.execute("UPDATE stages SET input_digest='deadbeef' WHERE mission_id='m1' AND stage_id='ACQUIRING'")
        with self.assertRaises(MissionError) as cm:
            c.advance_mission('m1')
        self.assertEqual(cm.exception.code, 'OPERATION_IDENTITY_CONFLICT')

    def test_operation_key_binds_mission_stage_operation_input_and_version(self):
        c = self.start()
        key = c._op_key('m1', 'PLANNED', 'abc')
        self.assertNotEqual(key, c._op_key('m2', 'PLANNED', 'abc'))
        self.assertNotEqual(key, c._op_key('m1', 'ACQUIRING', 'abc'))
        self.assertNotEqual(key, c._op_key('m1', 'PLANNED', 'abd'))
        with mock.patch.dict(mc.STAGE_OPERATIONS, {'PLANNED': ('plan.freeze_spec', '2')}):
            self.assertNotEqual(key, c._op_key('m1', 'PLANNED', 'abc'))

    def test_retry_budget_is_bounded_for_retryable_failures(self):
        c = self.start()
        boom = mock.Mock(side_effect=OSError('disk'))
        with mock.patch.dict(mc.OPERATIONS, {'PLANNED': boom}):
            outcomes = [c.advance_mission('m1') for _ in range(4)]
        self.assertEqual([o['outcome'] for o in outcomes], ['FAILED'] * 4)
        self.assertEqual(boom.call_count, 3)
        view = c.inspect_mission('m1')
        self.assertEqual((view['job']['state'], view['stages'][0]['attempts'], view['stages'][0]['failure_code']), ('FAILED', 3, 'OPERATION_ERROR'))

    def test_unexpected_operation_error_is_classified_by_type_not_message(self):
        c = self.start()
        with mock.patch.dict(mc.OPERATIONS, {'PLANNED': mock.Mock(side_effect=ValueError('secret detail'))}):
            result = c.advance_mission('m1')
        self.assertEqual((result['outcome'], result['diagnostics'][0]['code'], result['diagnostics'][0]['message']), ('FAILED', 'OPERATION_ERROR', 'ValueError'))
        self.assertEqual(c.inspect_mission('m1')['job']['state'], 'FAILED')

    def test_operation_returning_wrong_artifact_set_fails_postcondition(self):
        c = self.start()
        with mock.patch.dict(mc.OPERATIONS, {'PLANNED': lambda ctx: {'plan.json': b'{}', 'extra.json': b'{}'}}):
            result = c.advance_mission('m1')
        self.assertEqual(result['diagnostics'][0]['code'], 'STAGE_POSTCONDITION_FAILED')
        with mock.patch.dict(mc.OPERATIONS, {'PLANNED': lambda ctx: {'plan.json': b'not json'}}):
            fx = Fixture()
            self.addCleanup(fx.close)
            c2 = fx.coordinator()
            c2.create_mission(fx.spec())
            self.assertEqual(c2.advance_mission('m1')['diagnostics'][0]['code'], 'STAGE_POSTCONDITION_FAILED')


class Gate:
    """Hook that parks one executor at a failure point until released."""

    def __init__(self, point, stage, mission_id=None):
        self.point, self.stage, self.mission_id = point, stage, mission_id
        self.reached, self.release, self.fired = threading.Event(), threading.Event(), False

    def __call__(self, p, mission_id, s):
        if not self.fired and p == self.point and s == self.stage and self.mission_id in (None, mission_id):
            self.fired = True
            self.reached.set()
            self.release.wait(JOIN)


class ConcurrencyTests(Base):
    def spawn(self, fn):
        box = {}

        def run():
            try:
                box['result'] = fn()
            except BaseException as exc:  # noqa: BLE001
                box['error'] = exc
        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        return thread, box

    def test_t11_second_executor_cannot_claim_a_claimed_stage(self):
        gate = Gate('after_admission', 'ACQUIRING')
        a = self.start(hook=gate)
        a.advance_mission('m1')                                         # PLANNED
        thread, box = self.spawn(lambda: a.advance_mission('m1'))
        self.assertTrue(gate.reached.wait(JOIN))
        b = self.restart()
        result = b.advance_mission('m1')
        self.assertEqual((result['outcome'], result['diagnostics'][0]['code']), ('RECONCILIATION_REQUIRED', 'RECOVERY_RECONCILIATION_REQUIRED'))
        gate.release.set()
        thread.join(JOIN)
        self.assertEqual(box['result']['outcome'], 'ADVANCED')
        self.assertEqual(self.stage(b, 'ACQUIRING')['attempts'], 1)

    def test_t11_takeover_fences_out_the_stale_executor_so_only_one_finalizes(self):
        gate = Gate('after_admission', 'ACQUIRING')
        a = self.start(hook=gate)
        a.advance_mission('m1')
        thread, box = self.spawn(lambda: a.advance_mission('m1'))
        self.assertTrue(gate.reached.wait(JOIN))
        b = self.restart()
        takeover = b.reconcile_mission('m1')                              # operator takes over: fence 1 -> 2
        self.assertEqual(takeover['outcome'], 'ADVANCED')
        committed = self.stage(b, 'ACQUIRING')
        self.assertEqual((committed['status'], committed['fence']), ('SUCCEEDED', 2))
        gate.release.set()                                                # stale executor wakes and tries to finish
        thread.join(JOIN)
        self.assertEqual(box['result']['outcome'], 'FENCED')
        after = self.stage(b, 'ACQUIRING')
        self.assertEqual((after['output_digest'], after['fence'], after['status']), (committed['output_digest'], 2, 'SUCCEEDED'))
        outcomes = rows(b.db_path, 'SELECT attempt,fence,outcome FROM attempts WHERE stage_id=? ORDER BY attempt', 'ACQUIRING')
        self.assertEqual(outcomes, [(1, 1, None), (2, 2, 'SUCCEEDED')])      # the stale attempt never recorded success
        self.finish(b)

    def test_g07_many_racing_executors_never_run_a_stage_twice(self):
        coordinators = [self.fx.coordinator() for _ in range(6)]
        coordinators[0].create_mission(self.fx.spec())
        barrier = threading.Barrier(len(coordinators))

        def work(c):
            barrier.wait(JOIN)
            for _ in range(12):
                c.advance_mission('m1')
        threads = [self.spawn(lambda c=c: work(c)) for c in coordinators]
        for thread, box in threads:
            thread.join(JOIN)
            self.assertNotIn('error', box)
        view = coordinators[0].inspect_mission('m1')
        self.assertEqual(view['job']['state'], 'COMPLETED')
        self.assertEqual([s['attempts'] for s in view['stages']], [1] * 7)
        self.assertEqual(self.transitions(coordinators[0]), [('NEW', 'RUNNING'), ('RUNNING', 'COMPLETED')])
        self.assertEqual(rows(coordinators[0].db_path, 'SELECT count(*) FROM attempts'), [(7,)])

    def test_t12_different_missions_progress_while_another_is_parked_mid_stage(self):
        gate = Gate('after_admission', 'PLANNED', mission_id='slow')
        c = self.fx.coordinator(fail_at=gate)
        c.create_mission(self.fx.spec('slow'))
        c.create_mission(self.fx.spec('fast'))
        thread, box = self.spawn(lambda: c.advance_mission('slow'))
        self.assertTrue(gate.reached.wait(JOIN))
        other = self.restart()
        results = run_to_end(other, 'fast')                               # would deadlock behind a global lock
        self.assertEqual(results[-1]['job_state'], 'COMPLETED')
        self.assertEqual(self.stage(c, 'PLANNED', 'slow')['status'], 'RUNNING')
        gate.release.set()
        thread.join(JOIN)
        self.assertEqual(box['result']['outcome'], 'ADVANCED')
        self.finish(c, 'slow')

    def test_cancel_during_execution_prevents_commit(self):
        gate = Gate('after_admission', 'ACQUIRING')
        a = self.start(hook=gate)
        a.advance_mission('m1')
        thread, box = self.spawn(lambda: a.advance_mission('m1'))
        self.assertTrue(gate.reached.wait(JOIN))
        self.restart().cancel_mission('m1')
        gate.release.set()
        thread.join(JOIN)
        self.assertEqual((box['result']['outcome'], box['result']['diagnostics'][0]['code']), ('TERMINAL', 'MISSION_CANCELLED'))
        row = self.stage(a, 'ACQUIRING')
        self.assertNotEqual(row['status'], 'SUCCEEDED')
        self.assertIsNone(row['output_digest'])

    def test_unrelated_database_writer_does_not_corrupt_state(self):
        c = self.start()
        stop = threading.Event()

        def writer():
            while not stop.is_set():
                with sqlite3.connect(c.db_path, timeout=30) as db:
                    db.execute('CREATE TABLE IF NOT EXISTS noise(x)')
                    db.execute('INSERT INTO noise VALUES(1)')
                stop.wait(0.001)
        thread, _ = self.spawn(writer)
        try:
            view = self.finish(c)
        finally:
            stop.set()
            thread.join(JOIN)
        self.assertTrue(all(s['attempts'] == 1 for s in view['stages']))


class UnresolvedSignalTests(Base):
    def test_hypothesis_naming_an_absent_signal_is_insufficient_evidence_not_a_crash(self):
        c = self.start(signals=[signal_row('s1')], hypotheses=[{'id': 'h1', 'supporting_signal_ids': ['s1', 's9'],
                                                                  'contradicting_signal_ids': [], 'required_metrics': []}])
        view = self.finish(c)
        analysis = json.loads(next((self.fx.ws / 'missions' / 'm1' / 'stages' / 'ANALYZING').iterdir()).joinpath('analysis.json').read_text())
        self.assertEqual(analysis['hypotheses'][0]['conclusion'], 'INSUFFICIENT_EVIDENCE')
        self.assertIn('signal:s9', analysis['hypotheses'][0]['missing_evidence'])
        self.assertEqual(view['final']['research_state'], 'INSUFFICIENT_EVIDENCE')


class ReviewRegressionTests(Base):
    """Regressions for the independent review of P1-A/B."""

    def test_cancel_racing_final_commit_never_leaves_a_completed_looking_final(self):
        holder = {}

        def hook(point, mission_id, stage):
            if point == 'after_state_commit' and stage == 'FINALIZING':
                holder['c'].cancel_mission(mission_id)
        c = self.start(hook=hook)
        holder['c'] = c
        run_to_end(c, 'm1')
        view = c.inspect_mission('m1')
        self.assertEqual(view['job']['state'], 'CANCELLED')
        self.assertIsNone(view['final'])                                   # no COMPLETE-looking record on a cancelled job
        again = c.advance_mission('m1')
        self.assertEqual(again['diagnostics'][0]['code'], 'MISSION_CANCELLED')

    def test_cancel_racing_a_failure_does_not_write_a_failed_final(self):
        holder = {}

        def hook(point, mission_id, stage):
            if point == 'after_admission' and stage == 'PLANNED':
                holder['c'].cancel_mission(mission_id)
        c = self.start(hook=hook)
        holder['c'] = c
        with mock.patch.dict(mc.OPERATIONS, {'PLANNED': mock.Mock(side_effect=ValueError('x'))}):
            c.advance_mission('m1')
        view = c.inspect_mission('m1')
        self.assertEqual(view['job']['state'], 'CANCELLED')
        self.assertIsNone(view['final'])

    def test_malformed_spec_shapes_raise_mission_error_not_type_error(self):
        from research_lab.mission_spec import validate_spec
        base = self.fx.spec()
        for field, value in (('completion_requirements', {'accepted_research_states': [[1]], 'required_reviews': ['methodology_reviewer', 'source_verifier']}),
                             ('completion_requirements', {'accepted_research_states': [{}], 'required_reviews': []}),
                             ('hypotheses', [{'id': 'h', 'supporting_signal_ids': [[1]], 'contradicting_signal_ids': [], 'required_metrics': []}]),
                             ('provider_operations', [{'operation_id': {}, 'required': True}]),
                             ('instruments', [[1]]), ('permitted_operations', [[1]]), ('inputs', {'research_run': {'path': 3, 'sha256': 4}}),
                             ('required_metrics', [{'instrument_id': [], 'metric': 'x', 'unit': 'y'}])):
            with self.assertRaises(MissionError) as cm:
                validate_spec({**base, field: value})
            self.assertEqual(cm.exception.code, 'INVALID_MISSION_SPEC', field)

    def test_cli_reports_a_corrupt_store_as_an_error_not_a_traceback(self):
        import sys
        sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parents[1] / 'scripts'))
        import mission as cli
        (self.fx.ws / 'mission.sqlite').write_bytes(b'this is not a sqlite database' * 100)
        out, code = cli.run(['--workspace', str(self.fx.ws), 'status', 'm1'])
        self.assertEqual((code, out['error']['code']), (2, 'STORE_UNAVAILABLE'))

    def test_symlinked_stage_directory_is_refused_and_nothing_escapes_the_workspace(self):
        import tempfile
        outside = tempfile.TemporaryDirectory()
        self.addCleanup(outside.cleanup)
        c = self.start()
        out = self.fx.ws / 'missions' / 'm1'
        out.mkdir(parents=True)
        os.symlink(outside.name, out / 'stages')
        result = c.advance_mission('m1')
        self.assertEqual(result['outcome'], 'FAILED')
        self.assertEqual(os.listdir(outside.name), [])                      # nothing was written through the link

    def test_symlinked_evidence_archive_is_refused(self):
        import tempfile
        outside = tempfile.TemporaryDirectory()
        self.addCleanup(outside.cleanup)
        c = self.start()
        c.advance_mission('m1')
        os.symlink(os.path.join(outside.name, 'evil.sqlite'), self.fx.ws / 'missions' / 'm1' / 'evidence.sqlite')
        result = c.advance_mission('m1')
        self.assertEqual(result['outcome'], 'FAILED')
        self.assertEqual(os.listdir(outside.name), [])

    def test_oserror_while_writing_artifacts_is_a_retryable_recorded_failure(self):
        c = self.start()
        real = mc.write_immutable
        calls = {'n': 0}

        def flaky(path, data):
            calls['n'] += 1
            if calls['n'] == 1:
                raise OSError(28, 'No space left on device')
            return real(path, data)
        with mock.patch.object(mc, 'write_immutable', flaky):
            first = c.advance_mission('m1')
            self.assertEqual((first['outcome'], first['diagnostics'][0]['code']), ('FAILED', 'OPERATION_ERROR'))
            row = self.stage(c, 'PLANNED')
            self.assertEqual((row['status'], row['retryable']), ('FAILED', 1))            # not left RUNNING
            self.assertEqual(c.inspect_mission('m1')['job']['state'], 'RUNNING')
            second = c.advance_mission('m1')                                              # retry succeeds
        self.assertEqual(second['outcome'], 'ADVANCED')

    def test_clock_moving_backwards_cannot_wedge_job_transitions(self):
        c = self.start()
        c.advance_mission('m1')
        ticks = iter(['2000-01-01T00:00:00Z'] * 1000)                                    # clock jumps far into the past
        c.clock = lambda: next(ticks)
        view = self.finish(c)
        self.assertEqual(view['job']['state'], 'COMPLETED')

    def test_failed_job_transition_does_not_leave_a_phantom_final(self):
        c = self.start()
        with mock.patch.dict(mc.OPERATIONS, {'PLANNED': mock.Mock(side_effect=ValueError('x'))}):
            c.advance_mission('m1')
        view = c.inspect_mission('m1')
        self.assertEqual((view['job']['state'], view['final']['code']), ('FAILED', 'OPERATION_ERROR'))

    def test_nested_or_overlapping_output_directories_are_rejected(self):
        self.start('a', output_dir='out/a')
        for clash in ('out/a', 'out/a/inner', 'out'):
            with self.assertRaises(MissionError):
                self.fx.coordinator().create_mission(self.fx.spec('b', output_dir=clash))
        self.fx.coordinator().create_mission(self.fx.spec('c', output_dir='out/ab'))     # sibling with a common prefix is fine


if __name__ == '__main__':
    unittest.main()
