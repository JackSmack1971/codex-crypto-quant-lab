"""P0 audit-repair regressions F01-F10. Offline, deterministic, stdlib only.

Concurrency tests synchronise with ``threading.Event`` (never with sleeps or timing assertions); every
worker join has a generous timeout only so a regression fails instead of hanging the suite."""
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from quant_free_mcp import acquisition as acq
from quant_free_mcp import core
from research_lab import mission_integrity as mi
from research_lab.archive import (ArchiveDatabaseError, ArchiveIntegrityError, EvidenceArchive,
                                  EvidenceMissing)
from research_lab.completion import verify
from research_lab.core import ContractError, Observation
from research_lab.provider_policy import MetricContract, qualified_fallback

JOIN = 10  # seconds; failure-detection bound only
OK = b'{"ok":true}'
JSON_H = {'content-type': 'application/json'}
URL = 'https://data-api.binance.vision/api/v3/ticker/price?symbol=BTCUSDT'
URL2 = 'https://data-api.binance.vision/api/v3/ticker/price?symbol=ETHUSDT'
URL_B = 'https://api.llama.fi/protocols'


def resp(status=200, body=OK, **headers):
    h = dict(JSON_H) if status == 200 else {}
    h.update({k.replace('_', '-'): v for k, v in headers.items()})
    return acq.TransportResponse(status, h, body)


class Script:
    def __init__(self, *items):
        self.items, self.calls = list(items), 0

    def __call__(self, url, *, headers, timeout, max_bytes):
        self.calls += 1
        item = self.items.pop(0) if self.items else resp()
        if isinstance(item, BaseException):
            raise item
        return item


def make(transport, *, policy=None, cache=False, clock=None, extra=None):
    clock = clock or acq.FakeClock()
    pols = {'binance_spot': policy or acq.ProviderPolicy(min_gap_seconds=0)}
    pols.update(extra or {})
    a = acq.Acquirer(transport, clock=clock, cache=acq.ResponseCache() if cache else None,
                     provider_policies=pols, cache_policies={'spot_ticker': acq.CachePolicy(10, 100),
                                                             'defi_protocols': acq.CachePolicy(10, 100)},
                     jitter=lambda: 0.0)
    return a, clock


def get(a, url=URL, **kw):
    return a.get('binance_spot', 'spot_ticker', url, **kw)


def catch(fn):
    try:
        fn()
    except acq.AcquisitionError as exc:
        return exc


BREAKER = dict(min_gap_seconds=0, max_attempts=1, breaker_failure_threshold=3, breaker_open_seconds=30)


class F01HalfOpenProbeRelease(unittest.TestCase):
    def open_breaker(self, then):
        t = Script(resp(503), resp(503), resp(503), *then)
        a, clock = make(t, policy=acq.ProviderPolicy(**BREAKER))
        for _ in range(3):
            with self.assertRaises(acq.AcquisitionError):
                get(a)
        clock.advance(31)
        return a, clock, t

    def test_redirect_probe_does_not_wedge_breaker_and_recovery_repeats(self):
        a, clock, t = self.open_breaker([resp(302, location='https://evil.example/'),
                                         resp(301, location='https://evil.example/'), resp()])
        for _ in range(2):  # repeated redirected probes: each one must release the slot
            with self.assertRaises(acq.AcquisitionError) as cm:
                get(a)
            self.assertEqual(cm.exception.code, 'redirect_forbidden')
        self.assertEqual(t.calls, 5)  # every redirected probe actually reached the transport
        self.assertEqual(get(a).cache['status'], acq.LIVE)  # not circuit_open forever
        self.assertEqual(a.health()['binance_spot']['circuit_state'], acq.CLOSED)

    def test_transport_redirect_error_from_noredirect_handler_releases_probe(self):
        a, _, t = self.open_breaker([ValueError('redirect_forbidden'), resp()])
        with self.assertRaises(acq.AcquisitionError) as cm:
            get(a)
        self.assertEqual(cm.exception.code, 'redirect_forbidden')
        self.assertEqual(get(a).cache['status'], acq.LIVE)

    def test_unexpected_exception_and_cancellation_release_probe(self):
        for exc, expected in ((RuntimeError('boom'), RuntimeError), (KeyboardInterrupt(), KeyboardInterrupt)):
            a, _, t = self.open_breaker([exc, resp()])
            with self.assertRaises(expected):
                get(a)
            self.assertEqual(get(a).cache['status'], acq.LIVE, type(exc).__name__)

    def test_probe_rejected_by_quota_is_released(self):
        pol = acq.ProviderPolicy(**{**BREAKER, 'quota_budget': 3})
        a, clock = make(Script(resp(503), resp(503), resp(503), resp()), policy=pol)
        for _ in range(3):
            with self.assertRaises(acq.AcquisitionError):
                get(a)
        clock.advance(31)
        with self.assertRaises(acq.AcquisitionError) as cm:
            get(a)  # quota window (60s) still holds the 3 charges
        self.assertEqual(cm.exception.code, 'quota_exceeded')
        clock.advance(61)
        self.assertEqual(get(a).cache['status'], acq.LIVE)

    def test_ticket_settles_once(self):
        b = acq.CircuitBreaker(3, 30)
        ticket = b.allow(0.0)
        b.failure(ticket, 0.0)
        b.failure(ticket, 0.0)
        b.release(ticket)
        b.success(ticket)
        self.assertEqual(b.failures, 1)

    def test_late_success_of_pre_open_request_cannot_close_open_breaker(self):
        b = acq.CircuitBreaker(2, 30)
        slow = b.allow(0.0)
        for _ in range(2):
            b.failure(b.allow(0.0), 0.0)
        self.assertEqual(b.state, acq.OPEN)
        b.success(slow)
        self.assertEqual(b.state, acq.OPEN)


class F04NoNetworkUnderGlobalLock(unittest.TestCase):
    def test_slow_provider_does_not_block_other_provider_or_cache_hit(self):
        entered, release = threading.Event(), threading.Event()

        def transport(url, *, headers, timeout, max_bytes):
            if url == URL:
                entered.set()
                release.wait(JOIN)
            return resp()

        a, clock = make(transport, cache=True, extra={'defillama': acq.ProviderPolicy(min_gap_seconds=0)})
        get(a, URL2)  # warm a cache entry for an independent key on the same provider
        slow = threading.Thread(target=lambda: get(a), daemon=True)
        slow.start()
        self.assertTrue(entered.wait(JOIN))  # slow request is inside the transport right now
        results = {}

        def other():
            results['other'] = a.get('defillama', 'defi_protocols', URL_B).cache['status']
            results['hit'] = get(a, URL2).cache['status']
        worker = threading.Thread(target=other, daemon=True)
        worker.start()
        worker.join(JOIN)
        blocked = worker.is_alive()
        release.set()
        slow.join(JOIN)
        self.assertFalse(blocked, 'unrelated provider / cache hit blocked behind a slow request')
        self.assertEqual(results, {'other': acq.LIVE, 'hit': acq.FRESH_CACHE})

    def blocking_clock(self):
        in_sleep, finish = threading.Event(), threading.Event()

        class BlockingClock(acq.FakeClock):
            def sleep(self, seconds):
                in_sleep.set()
                finish.wait(JOIN)
                super().sleep(seconds)
        return BlockingClock(), in_sleep, finish

    def test_retry_after_sleep_does_not_block_other_provider(self):
        clock, in_sleep, finish = self.blocking_clock()
        a, _ = make(Script(resp(429, retry_after='2'), resp()), clock=clock,
                    extra={'defillama': acq.ProviderPolicy(min_gap_seconds=0)})
        th = threading.Thread(target=lambda: get(a), daemon=True)
        th.start()
        self.assertTrue(in_sleep.wait(JOIN))
        done = {}
        w = threading.Thread(target=lambda: done.update(r=a.get('defillama', 'defi_protocols', URL_B)), daemon=True)
        w.start()
        w.join(JOIN)
        stuck = w.is_alive()
        finish.set()
        th.join(JOIN)
        self.assertFalse(stuck, 'Retry-After sleep held a lock shared with another provider')
        self.assertEqual(done['r'].cache['status'], acq.LIVE)

    def test_same_provider_peer_fails_fast_during_cooldown_without_extra_request(self):
        clock, in_sleep, finish = self.blocking_clock()
        t = Script(resp(429, retry_after='2'), resp())
        a, _ = make(t, clock=clock)
        th = threading.Thread(target=lambda: get(a), daemon=True)
        th.start()
        self.assertTrue(in_sleep.wait(JOIN))
        errs = []
        w = threading.Thread(target=lambda: errs.append(catch(lambda: get(a, URL2))), daemon=True)
        w.start()
        w.join(JOIN)
        stuck = w.is_alive()
        finish.set()
        th.join(JOIN)
        self.assertFalse(stuck)
        self.assertEqual(errs[0].code, 'rate_limited')
        self.assertEqual(t.calls, 2)  # the original 429 and its retry only

    def test_concurrent_half_open_allows_exactly_one_probe(self):
        entered, release = threading.Event(), threading.Event()
        calls = []

        def transport(url, *, headers, timeout, max_bytes):
            calls.append(url)
            if len(calls) <= 3:
                return resp(503)
            entered.set()
            release.wait(JOIN)
            return resp()

        a, clock = make(transport, policy=acq.ProviderPolicy(**BREAKER))
        for _ in range(3):
            with self.assertRaises(acq.AcquisitionError):
                get(a)
        clock.advance(31)
        probe = threading.Thread(target=lambda: get(a), daemon=True)
        probe.start()
        self.assertTrue(entered.wait(JOIN))
        outcomes = []
        peers = [threading.Thread(target=lambda: outcomes.append(catch(lambda: get(a, URL2))), daemon=True)
                 for _ in range(4)]
        for p in peers:
            p.start()
        for p in peers:
            p.join(JOIN)
        release.set()
        probe.join(JOIN)
        self.assertEqual([o.code for o in outcomes], ['circuit_open'] * 4)
        self.assertEqual(len(calls), 4)  # three failures + exactly one probe
        self.assertEqual(a.health()['binance_spot']['circuit_state'], acq.CLOSED)

    def test_concurrent_accounting_is_exact(self):
        a, _ = make(Script(), policy=acq.ProviderPolicy(min_gap_seconds=0, quota_budget=10_000))
        barrier = threading.Barrier(8)

        def work():
            barrier.wait(JOIN)
            for _ in range(25):
                get(a)
        threads = [threading.Thread(target=work, daemon=True) for _ in range(8)]
        for th in threads:
            th.start()
        for th in threads:
            th.join(JOIN)
        h = a.health()['binance_spot']
        self.assertEqual((h['requests'], h['successes'], h['quota_used']), (200, 200, 200))


class F05RetryAfterBreakerState(unittest.TestCase):
    def policy(self, **over):
        return acq.ProviderPolicy(min_gap_seconds=0, max_attempts=3, breaker_failure_threshold=3,
                                  breaker_open_seconds=30, **over)

    def test_repeated_429_accumulates_failures_and_opens_at_exhaustion(self):
        t = Script(resp(429, retry_after='2'), resp(429, retry_after='2'), resp(429, retry_after='2'))
        a, clock = make(t, policy=self.policy())
        with self.assertRaises(acq.AcquisitionError) as cm:
            get(a)
        self.assertEqual((cm.exception.code, cm.exception.attempts, t.calls), ('rate_limited', 3, 3))
        self.assertEqual(clock.sleeps, [2.0, 2.0])
        h = a.health()['binance_spot']
        self.assertEqual((h['circuit_state'], h['consecutive_failures']), (acq.OPEN, 3))  # nothing discarded

    def test_breaker_tripping_mid_retry_stops_without_forcing_closed(self):
        t = Script(resp(429, retry_after='1'), resp(429, retry_after='1'), resp())
        a, clock = make(t, policy=acq.ProviderPolicy(min_gap_seconds=0, max_attempts=5,
                                                      breaker_failure_threshold=2, breaker_open_seconds=30))
        with self.assertRaises(acq.AcquisitionError):
            get(a)
        self.assertEqual(t.calls, 2)  # threshold reached on the second 429: no third request
        self.assertEqual(a.health()['binance_spot']['circuit_state'], acq.OPEN)

    def test_single_429_then_success_resets_failures(self):
        t = Script(resp(429, retry_after='2'), resp())
        a, clock = make(t, policy=self.policy())
        self.assertEqual(get(a).attempts, 2)
        h = a.health()['binance_spot']
        self.assertEqual((h['circuit_state'], h['consecutive_failures'], h['cooldown_until_utc']),
                         (acq.CLOSED, 0, None))

    def test_consecutive_failures_across_calls_accumulate_into_the_breaker(self):
        t = Script(resp(503), resp(503), resp(429, retry_after='1'))
        a, clock = make(t, policy=acq.ProviderPolicy(min_gap_seconds=0, max_attempts=1,
                                                      breaker_failure_threshold=3, breaker_open_seconds=30))
        for _ in range(3):
            clock.advance(2)
            with self.assertRaises(acq.AcquisitionError):
                get(a)
        h = a.health()['binance_spot']
        self.assertEqual((h['consecutive_failures'], h['circuit_state']), (3, acq.OPEN))
        with self.assertRaises(acq.AcquisitionError) as cm:
            get(a)
        self.assertEqual((cm.exception.code, t.calls), ('rate_limited', 3))  # cooldown precedes circuit; no request

    def test_cooldown_expiry_then_half_open_probe_recovers(self):
        t = Script(resp(429, retry_after='60'), resp())
        a, clock = make(t, policy=self.policy())
        with self.assertRaises(acq.AcquisitionError) as cm:
            get(a)
        self.assertEqual(cm.exception.code, 'rate_limited')
        with self.assertRaises(acq.AcquisitionError) as cm:
            get(a)
        self.assertEqual((cm.exception.code, t.calls), ('rate_limited', 1))  # cooldown, no request
        clock.advance(61)
        self.assertEqual(a.health()['binance_spot']['circuit_state'], acq.HALF_OPEN)
        self.assertEqual(get(a).cache['status'], acq.LIVE)
        self.assertEqual(a.health()['binance_spot']['circuit_state'], acq.CLOSED)

    def test_half_open_probe_429_reopens_then_recovers(self):
        t = Script(resp(503), resp(503), resp(503), resp(429, retry_after='5'), resp())
        a, clock = make(t, policy=acq.ProviderPolicy(**BREAKER))
        for _ in range(3):
            with self.assertRaises(acq.AcquisitionError):
                get(a)
        clock.advance(31)
        with self.assertRaises(acq.AcquisitionError):
            get(a)
        self.assertEqual(a.health()['binance_spot']['circuit_state'], acq.OPEN)
        clock.advance(31)
        self.assertEqual(get(a).cache['status'], acq.LIVE)


class F06RateLimitAuthority(unittest.TestCase):
    def test_dead_ratebucket_removed(self):
        self.assertFalse(hasattr(core, 'RateBucket'))
        self.assertFalse(hasattr(core, '_BUCKET'))

    def test_min_gap_is_per_provider_and_fails_fast(self):
        pols = {'binance_spot': acq.ProviderPolicy(min_gap_seconds=5),
                'defillama': acq.ProviderPolicy(min_gap_seconds=5)}
        t = Script()
        a = acq.Acquirer(t, clock=acq.FakeClock(), provider_policies=pols, cache_policies={}, jitter=lambda: 0.0)
        get(a)
        with self.assertRaises(acq.AcquisitionError) as cm:
            get(a, URL2)
        self.assertEqual((cm.exception.code, t.calls), ('local_rate_limit', 1))
        self.assertEqual(a.get('defillama', 'defi_protocols', URL_B).cache['status'], acq.LIVE)
        a.clock.advance(5)
        self.assertEqual(get(a, URL2).cache['status'], acq.LIVE)

    def test_default_provider_gaps_preserved(self):
        gaps = {k: v.min_gap_seconds for k, v in acq.DEFAULT_PROVIDER_POLICIES.items()}
        self.assertEqual(gaps, {'binance_spot': .4, 'binance_usdm': .6, 'dexscreener': .5,
                                'defillama': 1.0, 'mempool': 1.0})


def fake_obs(value, unit='USDT'):
    return SimpleNamespace(value=value, unit=unit, instrument_id='i', metric='m')


class F02NonFiniteDecimals(unittest.TestCase):
    def check(self, name, values, claimed='1', tolerance='0'):
        inputs = [f'o{i}' for i in range(len(values))]
        resolved = {k: fake_obs(v) for k, v in zip(inputs, values)}
        metrics = {'metrics': [{'id': 'm', 'value': claimed, 'unit': 'USDT', 'inputs': inputs,
                                'transform': {'name': name, 'tolerance': tolerance}}]}
        out, state = mi.verify_metrics(metrics, set(inputs), resolved, strict=True, archive_present=True)
        return [d['code'] for d in out], state

    def test_non_finite_archived_values_every_transform(self):
        for bad in ('NaN', 'sNaN', 'Infinity', '-Infinity', 'inf', 'abc', ''):
            for name, values in (('identity', [bad]), ('sum', ['1', bad]), ('mean', [bad, '1']),
                                 ('difference', [bad, '1']), ('ratio', ['1', bad])):
                codes, state = self.check(name, values)
                self.assertEqual((codes, state), (['METRIC_TRANSFORM_UNDEFINED'], 'INVALID'), (name, bad))

    def test_non_string_archived_value_rejected(self):
        for bad in (None, 1.5, True, ['1']):
            self.assertEqual(self.check('identity', [bad])[0], ['METRIC_TRANSFORM_UNDEFINED'], repr(bad))

    def test_decimal_overflow_is_a_diagnostic_not_a_traceback(self):
        self.assertEqual(self.check('sum', ['9E+999999', '9E+999999']),
                         (['METRIC_TRANSFORM_UNDEFINED'], 'INVALID'))

    def test_malformed_tolerance_and_claimed_values(self):
        for tol in ('NaN', 'Infinity', 'x', '-1', None, 1.5):
            self.assertEqual(self.check('identity', ['1'], tolerance=tol)[0], ['METRIC_VALUE_INVALID'], repr(tol))
        for claimed in ('NaN', '-Infinity', 'x', None, 1.5):
            self.assertEqual(self.check('identity', ['1'], claimed=claimed)[0], ['METRIC_VALUE_INVALID'], repr(claimed))

    def test_finite_paths_unchanged(self):
        self.assertEqual(self.check('mean', ['1', '3'], claimed='2'), ([], 'VERIFIED'))
        self.assertEqual(self.check('identity', ['1'], claimed='2')[0], ['METRIC_VALUE_MISMATCH'])
        self.assertEqual(self.check('ratio', ['1', '0'])[0], ['METRIC_TRANSFORM_UNDEFINED'])


def _windows(n=6):
    return [{'start_utc': f'2026-01-01T0{i}:00:00Z', 'end_utc': f'2026-01-01T0{i}:59:00Z'} for i in range(n)]


class F03ChronologyRobustness(unittest.TestCase):
    def codes(self, folds, walk_forward=False, windows=None):
        problems = mi.chronology_problems({'folds': folds}, windows or _windows(), walk_forward=walk_forward)
        return [p[0] for p in problems]

    def test_malformed_folds_give_diagnostics_not_exceptions(self):
        def fold(tr, te):
            return {'train_indices': tr, 'test_indices': te}
        cases = [
            ('CV_FOLD_MALFORMED', fold([0], [])),                       # empty test: min() ValueError before
            ('CV_FOLD_MALFORMED', fold([], [1])),
            ('CV_FOLD_MALFORMED', {'test_indices': [1]}),              # missing key: KeyError before
            ('CV_FOLD_MALFORMED', {'train_indices': [0]}),
            ('CV_FOLD_MALFORMED', fold('01', [1])),                    # not a list
            ('CV_FOLD_MALFORMED', 'fold'),                              # not an object
            ('CV_FOLD_MALFORMED', None),
            ('CV_INDEX_INVALID', fold([-1], [1])),                      # negative wrap before
            ('CV_INDEX_INVALID', fold([0], [-6])),
            ('CV_INDEX_INVALID', fold([True], [1])),                    # bool
            ('CV_INDEX_INVALID', fold([0], [1.0])),                     # float
            ('CV_INDEX_INVALID', fold([0], ['1'])),                     # str
            ('CV_INDEX_INVALID', fold([0], [None])),
            ('CV_INDEX_OUT_OF_RANGE', fold([0], [6])),                  # == len(windows)
            ('CV_INDEX_DUPLICATE', fold([0, 0], [1])),
            ('CV_INDEX_DUPLICATE', fold([0], [1, 1])),
            ('CV_TRAIN_TEST_INDEX_SHARED', fold([0, 1], [1, 2])),
        ]
        for code, bad in cases:
            self.assertEqual(self.codes([bad]), [code], repr(bad))

    def test_malformed_folds_container(self):
        for folds in (None, [], 'x', {'a': 1}, 3):
            problems = mi.chronology_problems({'folds': folds}, _windows(), walk_forward=False)
            self.assertEqual([p[0] for p in problems], ['CV_FOLDS_MALFORMED'], repr(folds))
        self.assertEqual([p[0] for p in mi.chronology_problems(None, _windows(), walk_forward=False)],
                         ['CV_FOLDS_MALFORMED'])

    def test_boundary_indices_accepted_and_negative_never_wraps(self):
        self.assertEqual(self.codes([{'train_indices': [0, 1], 'test_indices': [5]}]), [])
        self.assertEqual(self.codes([{'train_indices': [-1], 'test_indices': [4]}]), ['CV_INDEX_INVALID'])

    def test_checks_run_only_on_valid_folds_and_still_detect_leakage(self):
        overlapping = [dict(w) for w in _windows()]
        overlapping[1]['start_utc'] = '2026-01-01T00:30:00Z'  # window 1 starts inside window 0
        folds = [{'train_indices': [0], 'test_indices': []},          # malformed -> skipped, no crash
                 {'train_indices': [0], 'test_indices': [1]}]          # valid -> leakage found
        self.assertEqual(self.codes(folds, windows=overlapping), ['CV_FOLD_MALFORMED', 'CV_LABEL_OVERLAP_LEAKAGE'])
        self.assertEqual(self.codes([{'train_indices': [3], 'test_indices': [2]}], walk_forward=True),
                         ['CV_TRAIN_NOT_BEFORE_TEST'])

    def test_windows_missing_or_invalid(self):
        self.assertEqual(mi.chronology_problems({'folds': []}, None, walk_forward=False)[0][0],
                         'CV_LABEL_WINDOWS_MISSING')
        self.assertEqual(mi.chronology_problems({'folds': []}, [{'start_utc': 1}], walk_forward=False)[0][0],
                         'CV_LABEL_WINDOWS_INVALID')


def real_obs(id='obs:a', value='100'):
    return Observation(id, 'binance:spot:BTCUSDT', 'spot.quote.mid', value, 'USDT', 'https://x.example/',
                       '2026-01-01T10:00:00Z', '2026-01-01T10:01:00Z', '2026-01-01T10:01:00Z')


class FakeArchive:
    def __init__(self, exc):
        self.exc = exc

    def get(self, id):
        raise self.exc


class F07TypedArchiveFailures(unittest.TestCase):
    def code(self, archive, rid='obs:a'):
        out, state, _ = mi.resolve_evidence([rid], archive, None, strict=False)
        self.assertEqual((state, len(out)), ('INVALID', 1))
        return out[0]['code']

    def test_classification_is_by_type_not_message(self):
        self.assertEqual(self.code(FakeArchive(EvidenceMissing('anything at all'))), 'EVIDENCE_NOT_IN_ARCHIVE')
        # wording that contains "missing" must NOT make an integrity failure look absent
        self.assertEqual(self.code(FakeArchive(ArchiveIntegrityError('digest missing from row'))),
                         'EVIDENCE_ARCHIVE_INTEGRITY')
        # an untyped ContractError carries no category: fail closed as integrity, whatever its text
        self.assertEqual(self.code(FakeArchive(ContractError('observation missing'))), 'EVIDENCE_ARCHIVE_INTEGRITY')
        self.assertEqual(self.code(FakeArchive(ContractError('evidence not found'))), 'EVIDENCE_ARCHIVE_INTEGRITY')

    def test_database_and_unclassified_failures(self):
        self.assertEqual(self.code(FakeArchive(ArchiveDatabaseError('locked'))), 'EVIDENCE_ARCHIVE_DATABASE_ERROR')
        self.assertEqual(self.code(FakeArchive(sqlite3.OperationalError('disk I/O error'))),
                         'EVIDENCE_ARCHIVE_DATABASE_ERROR')
        self.assertEqual(self.code(FakeArchive(RuntimeError('x'))), 'EVIDENCE_ARCHIVE_INTEGRITY')

    def test_real_archive_raises_distinct_types(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'a.sqlite'
            arch = EvidenceArchive(path)
            with self.assertRaises(EvidenceMissing):
                arch.get('obs:none')
            for sql in ("UPDATE observations SET sha256='0'",                      # digest mismatch
                        "UPDATE observations SET content='{not json'",              # undecodable
                        "UPDATE observations SET content='{\"id\":\"obs:a\"}'"):    # wrong shape
                with sqlite3.connect(path) as db:
                    db.execute('DELETE FROM observations')
                arch.ingest(real_obs())
                with sqlite3.connect(path) as db:
                    db.execute(sql)
                with self.assertRaises(ArchiveIntegrityError, msg=sql):
                    arch.get('obs:a')
            garbage = Path(d) / 'garbage.sqlite'
            garbage.write_bytes(b'this is not a sqlite database' * 50)
            with self.assertRaises(ArchiveDatabaseError):
                mi.open_archive(garbage)
            broken = EvidenceArchive(path)
            broken.path = garbage
            with self.assertRaises(ArchiveDatabaseError):
                broken.get('obs:a')

    def test_typed_errors_remain_contract_errors(self):
        for cls in (EvidenceMissing, ArchiveIntegrityError, ArchiveDatabaseError):
            self.assertTrue(issubclass(cls, ContractError))

    def test_end_to_end_codes_with_real_archive(self):
        with tempfile.TemporaryDirectory() as d:
            arch = EvidenceArchive(Path(d) / 'a.sqlite')
            arch.ingest(real_obs())
            out, _, _ = mi.resolve_evidence(['obs:none'], arch, None, strict=False)
            self.assertEqual(out[0]['code'], 'EVIDENCE_NOT_IN_ARCHIVE')
            with sqlite3.connect(arch.path) as db:
                db.execute("UPDATE observations SET sha256='0'")
            out, _, _ = mi.resolve_evidence(['obs:a'], arch, None, strict=False)
            self.assertEqual(out[0]['code'], 'EVIDENCE_ARCHIVE_INTEGRITY')


class TrackedConnect:
    """Records every sqlite3 connection opened through ``sqlite3.connect``."""

    def __init__(self):
        self.real, self.conns = sqlite3.connect, []

    def __call__(self, *a, **kw):
        conn = self.real(*a, **kw)
        self.conns.append(conn)
        return conn

    def open_count(self):
        n = 0
        for conn in self.conns:
            try:
                conn.execute('SELECT 1')
                n += 1
            except sqlite3.ProgrammingError:
                pass
        return n


class F08ConnectionLifecycle(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.path = self.dir / 'a.sqlite'
        EvidenceArchive(self.path).ingest(real_obs())
        self.bundle = self.dir / 'bundle'
        self.bundle.mkdir()
        (self.bundle / 'mission.json').write_text('{}')

    def tearDown(self):
        self.tmp.cleanup()

    def test_repeated_verification_leaves_no_open_connection_and_file_is_releasable(self):
        tracker = TrackedConnect()
        with mock.patch.object(sqlite3, 'connect', tracker):
            for _ in range(5):
                verify(self.bundle, archive=self.path)
        self.assertGreater(len(tracker.conns), 0)
        self.assertEqual(tracker.open_count(), 0)
        self.path.rename(self.dir / 'moved.sqlite')  # Windows would refuse this with a handle still open
        (self.dir / 'moved.sqlite').unlink()

    def test_owned_archive_closed_on_success_failure_and_exception(self):
        with mock.patch.object(EvidenceArchive, 'close', create=True) as close:
            with mi.archive_session(self.path):
                pass
            self.assertEqual(close.call_count, 1)
            with self.assertRaises(RuntimeError):
                with mi.archive_session(self.path):
                    raise RuntimeError('body failed')
            self.assertEqual(close.call_count, 2)
            with mi.archive_session(self.dir / 'absent.sqlite') as (opened, error):  # open failure
                self.assertIsNone(opened)
                self.assertIsInstance(error, ContractError)
            self.assertEqual(close.call_count, 2)  # nothing was opened, nothing to close
            verify(self.bundle, archive=self.path)
            self.assertEqual(close.call_count, 3)

    def test_caller_supplied_archive_is_never_closed(self):
        class Spy:
            closed = 0

            def get(self, id):
                raise EvidenceMissing('x')

            def close(self):
                Spy.closed += 1
        spy = Spy()
        with mi.archive_session(spy) as (opened, error):
            self.assertIs(opened, spy)
        verify(self.bundle, archive=spy)
        self.assertEqual(Spy.closed, 0)
        real = EvidenceArchive(self.path)
        with mock.patch.object(EvidenceArchive, 'close', create=True) as close:
            verify(self.bundle, archive=real)
            self.assertEqual(close.call_count, 0)
        self.assertEqual(real.get('obs:a').id, 'obs:a')  # still usable by its owner

    def test_unreadable_archive_path_reported_not_raised(self):
        garbage = self.dir / 'garbage.sqlite'
        garbage.write_bytes(b'not sqlite' * 100)
        result = verify(self.bundle, archive=garbage)
        self.assertIn('EVIDENCE_ARCHIVE_UNAVAILABLE', [d['code'] for d in result['diagnostics']])


class F09MalformedReferences(unittest.TestCase):
    def test_malformed_ids_reported_precisely_and_never_as_duplicates(self):
        refs = [{}, '', '   ', {'id': 3}, {'id': None}, {'id': '  '}, 7, None, {}]
        out, _, _ = mi.resolve_evidence(refs, None, None, strict=False)
        self.assertEqual([d['code'] for d in out], ['EVIDENCE_REFERENCE_MALFORMED'] * len(refs))
        self.assertFalse(any('None' in d['message'] or 'duplicate' in d['message'] for d in out))
        self.assertIn('reference 0', out[0]['message'])
        self.assertIn('reference 8', out[-1]['message'])

    def test_legitimate_duplicates_still_reported(self):
        out, _, _ = mi.resolve_evidence(['obs:a', 'obs:a', {'id': ' obs:a '}, 'obs:b'], None, None, strict=False)
        self.assertEqual([d['code'] for d in out], ['EVIDENCE_REFERENCE_DUPLICATE'] * 2)
        self.assertTrue(all('obs:a' in d['message'] for d in out))

    def test_mixed_valid_and_invalid(self):
        with tempfile.TemporaryDirectory() as d:
            arch = EvidenceArchive(Path(d) / 'a.sqlite')
            arch.ingest(real_obs())
            out, state, resolved = mi.resolve_evidence(['obs:a', {}, 'obs:a', '', 'obs:none'], arch, None, strict=False)
            self.assertEqual(sorted(x['code'] for x in out),
                             ['EVIDENCE_NOT_IN_ARCHIVE', 'EVIDENCE_REFERENCE_DUPLICATE',
                              'EVIDENCE_REFERENCE_MALFORMED', 'EVIDENCE_REFERENCE_MALFORMED'])
            self.assertEqual((state, list(resolved)), ('INVALID', ['obs:a']))


T = '2026-01-01T00:00:00Z'


def contract():
    return MetricContract('venue:btc', 'funding', 'rate', 'venue', '8h', 'provider', coverage='snapshot')


def candidate(**over):
    cand = {'provider': 'p', 'contract': dict(contract().__dict__), 'status': 'OBSERVED', 'observed_at_utc': T,
            'available_at_utc': T, 'retrieved_at_utc': T}
    cand.update(over)
    return cand


def decide(cand, **kw):
    out = qualified_fallback(contract(), [cand], T, max_age_seconds=60, **kw)
    return out['decisions'][0]['status'], out['decisions'][0].get('warnings')


class F10ProvenanceAndCache(unittest.TestCase):
    def test_impossible_ordering_rejected(self):
        early = '2025-12-31T23:59:30Z'
        self.assertEqual(decide(candidate(retrieved_at_utc=early))[0], 'INVALID_TIMESTAMP')   # retrieved < observed
        self.assertEqual(decide(candidate(observed_at_utc=early, available_at_utc=early,
                                          retrieved_at_utc='2025-12-31T23:59:00Z'))[0], 'INVALID_TIMESTAMP')
        self.assertEqual(decide(candidate(available_at_utc=early))[0], 'INVALID_TIMESTAMP')   # available < observed
        self.assertEqual(decide(candidate(observed_at_utc=early, retrieved_at_utc=early))[0],
                         'INVALID_TIMESTAMP')                                                  # retrieved < available
        self.assertEqual(decide(candidate(observed_at_utc='garbage'))[0], 'INVALID_TIMESTAMP')

    def test_historical_retrieval_does_not_establish_availability(self):
        hist = candidate(observed_at_utc='2025-12-31T23:59:30Z', available_at_utc='2025-12-31T23:59:40Z',
                         retrieved_at_utc='2025-12-31T23:59:50Z')
        self.assertEqual(decide(hist)[0], 'QUALIFIED')
        self.assertEqual(decide(candidate(available_at_utc=None))[0], 'UNAVAILABLE')  # retrieved_at is no substitute
        self.assertEqual(decide(candidate(status='UNAVAILABLE', observed_at_utc=None, available_at_utc=None))[0],
                         'UNAVAILABLE')

    def test_future_and_stale_still_rejected(self):
        self.assertEqual(decide(candidate(retrieved_at_utc='2026-01-01T00:05:00Z'))[0], 'STALE_OR_FUTURE')
        old = '2025-12-31T00:00:00Z'
        self.assertEqual(decide(candidate(observed_at_utc=old, available_at_utc=old, retrieved_at_utc=old))[0],
                         'STALE_OR_FUTURE')

    def test_stale_cache_never_qualifies_via_missing_or_malformed_flags(self):
        stale = {'status': 'STALE_CACHE', 'served_from_cache': True}
        self.assertEqual(decide(candidate(cache=stale))[0], 'STALE_CACHE_REJECTED')
        self.assertEqual(decide(candidate(cache=stale), allow_stale_cache=True)[0], 'QUALIFIED')
        for bad in ({'served_from_cache': True},                              # status flag missing
                    {},                                                       # empty
                    {'status': 'stale_cache'},                                # unknown spelling
                    {'status': 'STALE_CACHE', 'served_from_cache': False},    # contradictory
                    {'status': 'LIVE', 'served_from_cache': True},
                    {'status': 'FRESH_CACHE', 'served_from_cache': 'yes'},
                    'STALE_CACHE', ['STALE_CACHE'], 1):
            for allow in (False, True):
                self.assertEqual(decide(candidate(cache=bad), allow_stale_cache=allow)[0],
                                 'INVALID_CACHE_METADATA', (bad, allow))

    def test_cached_candidate_requires_original_retrieval_time(self):
        cand = candidate(cache={'status': 'FRESH_CACHE', 'served_from_cache': True})
        self.assertEqual(decide(cand)[0], 'QUALIFIED')
        del cand['retrieved_at_utc']
        self.assertEqual(decide(cand)[0], 'INVALID_CACHE_METADATA')

    def test_live_and_unspecified_cache_state(self):
        self.assertEqual(decide(candidate(cache={'status': 'LIVE', 'served_from_cache': False})), ('QUALIFIED', []))
        self.assertEqual(decide(candidate()), ('QUALIFIED', ['cache_state_unspecified']))
        self.assertEqual(decide(candidate(), require_cache_state=True)[0], 'CACHE_STATE_MISSING')
        self.assertEqual(decide(candidate(cache=None), require_cache_state=True)[0], 'CACHE_STATE_MISSING')

    def test_acquisition_cache_metadata_roundtrips_through_policy(self):
        a, clock = make(Script(), cache=True)
        live = get(a)
        cached = get(a)
        self.assertEqual((live.cache['status'], cached.cache['status']), (acq.LIVE, acq.FRESH_CACHE))
        for result in (live, cached):
            self.assertEqual(decide(candidate(cache=result.cache))[0], 'QUALIFIED')


if __name__ == '__main__':
    unittest.main()
