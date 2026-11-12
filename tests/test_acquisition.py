"""Deterministic offline fault-injection tests for the read-only acquisition layer.

No network: transport, clock and jitter are injected. Public API availability is
never a prerequisite for these tests.
"""
import json
import unittest

from quant_free_mcp import acquisition as acq
from quant_free_mcp import core
from research_lab.provider_policy import MetricContract, qualified_fallback

URL = 'https://data-api.binance.vision/api/v3/ticker/bookTicker?symbol=BTCUSDT'
OK = json.dumps({'bidPrice': '1', 'askPrice': '2'}).encode()
JSON_H = {'content-type': 'application/json'}


class Script:
    """Scripted transport: each item is a TransportResponse or an exception to raise."""
    def __init__(self, *items):
        self.items, self.calls = list(items), 0

    def __call__(self, url, *, headers, timeout, max_bytes):
        self.calls += 1
        item = self.items.pop(0) if self.items else self.items_default
        if isinstance(item, Exception):
            raise item
        return item
    items_default = acq.TransportResponse(200, JSON_H, OK)


def resp(status=200, body=OK, **headers):
    h = dict(JSON_H) if status == 200 else {}
    h.update({k.replace('_', '-'): v for k, v in headers.items()})
    return acq.TransportResponse(status, h, body)


def make(transport, *, cache=True, policy=None, cpol=None, clock=None):
    clock = clock or acq.FakeClock()
    policy = policy or acq.ProviderPolicy(min_gap_seconds=0)
    a = acq.Acquirer(transport, clock=clock, cache=acq.ResponseCache() if cache else None,
                     provider_policies={'binance_spot': policy},
                     cache_policies={'spot_ticker': cpol or acq.CachePolicy(10, 100)},
                     jitter=lambda: 0.0)
    return a, clock


def get(a, **kw):
    return a.get('binance_spot', 'spot_ticker', URL, **kw)


class CacheTests(unittest.TestCase):
    def test_live_then_fresh_cache_keeps_original_retrieval_time(self):
        t = Script(resp())
        a, clock = make(t)
        first = get(a)
        clock.advance(5)
        second = get(a)
        self.assertEqual((first.cache['status'], second.cache['status']), (acq.LIVE, acq.FRESH_CACHE))
        self.assertEqual(t.calls, 1)
        self.assertEqual(second.retrieved_at, first.retrieved_at)
        self.assertEqual(second.cache['age_seconds'], 5)
        self.assertTrue(second.cache['served_from_cache'])

    def test_expired_entry_is_refetched(self):
        t = Script(resp(), resp())
        a, clock = make(t)
        get(a)
        clock.advance(10)  # ttl boundary is exclusive
        self.assertEqual(get(a).cache['status'], acq.LIVE)
        self.assertEqual(t.calls, 2)

    def test_stale_cache_requires_opt_in_and_is_bounded(self):
        t = Script(resp(), acq.TransportTimeout(), acq.TransportTimeout(), acq.TransportTimeout(),
                   acq.TransportTimeout(), acq.TransportTimeout(), acq.TransportTimeout(),
                   acq.TransportTimeout(), acq.TransportTimeout(), acq.TransportTimeout())
        a, clock = make(t, policy=acq.ProviderPolicy(min_gap_seconds=0, breaker_failure_threshold=99))
        get(a)
        clock.advance(20)  # expired, inside stale window
        with self.assertRaises(acq.AcquisitionError):
            get(a)  # default: never serve stale silently
        served = get(a, allow_stale=True)
        self.assertEqual(served.cache['status'], acq.STALE_CACHE)
        self.assertEqual(served.cache['stale_reason'], 'upstream_timeout')
        clock.advance(200)  # beyond stale_until
        with self.assertRaises(acq.AcquisitionError):
            get(a, allow_stale=True)

    def test_recovery_after_failure_returns_live(self):
        t = Script(resp(), acq.TransportError(), resp())
        a, clock = make(t, policy=acq.ProviderPolicy(min_gap_seconds=0, max_attempts=1, breaker_failure_threshold=99))
        get(a)
        clock.advance(20)
        with self.assertRaises(acq.AcquisitionError):
            get(a)
        self.assertEqual(get(a).cache['status'], acq.LIVE)

    def test_corrupt_cache_row_is_a_miss(self):
        t = Script(resp(), resp())
        a, clock = make(t)
        get(a)
        a.cache._db.execute('UPDATE responses SET body=?', (b'tampered',))
        self.assertEqual(get(a).cache['status'], acq.LIVE)
        self.assertEqual(t.calls, 2)


class RetryTests(unittest.TestCase):
    def test_timeout_retries_with_deterministic_backoff_then_succeeds(self):
        t = Script(acq.TransportTimeout(), acq.TransportTimeout(), resp())
        a, clock = make(t)
        r = get(a)
        self.assertEqual((r.attempts, t.calls), (3, 3))
        self.assertEqual(clock.sleeps, [0.25, 0.5])  # base*2^n * (0.5 + 0.5*0)

    def test_jitter_is_bounded(self):
        t = Script(acq.TransportTimeout(), resp())
        a, clock = make(t)
        a.jitter = lambda: 0.999999
        get(a)
        self.assertTrue(0.25 <= clock.sleeps[0] < 0.5)

    def test_persistent_timeout_is_bounded_by_max_attempts(self):
        t = Script(*[acq.TransportTimeout() for _ in range(50)])
        a, _ = make(t, policy=acq.ProviderPolicy(min_gap_seconds=0, max_attempts=4, breaker_failure_threshold=99))
        with self.assertRaises(acq.AcquisitionError) as cm:
            get(a)
        self.assertEqual((t.calls, cm.exception.code, cm.exception.attempts), (4, 'upstream_timeout', 4))

    def test_http_429_honours_short_retry_after_once_per_attempt(self):
        t = Script(resp(429, retry_after='2'), resp())
        a, clock = make(t)
        r = get(a)
        self.assertEqual((r.attempts, clock.sleeps), (2, [2.0]))

    def test_http_429_long_retry_after_does_not_sleep_or_loop(self):
        t = Script(resp(429, retry_after='120'))
        a, clock = make(t)
        with self.assertRaises(acq.AcquisitionError) as cm:
            get(a)
        self.assertEqual((cm.exception.code, t.calls, clock.sleeps), ('rate_limited', 1, []))
        self.assertEqual(cm.exception.retry_after_seconds, 120)
        for _ in range(5):  # provider cooldown: no further HTTP while cooling down
            with self.assertRaises(acq.AcquisitionError):
                get(a)
        self.assertEqual(t.calls, 1)
        clock.advance(121)
        self.assertEqual(get(a).cache['status'], acq.LIVE)
        self.assertEqual(t.calls, 2)

    def test_http_429_without_retry_after_is_not_retried(self):
        t = Script(resp(429))
        a, _ = make(t)
        with self.assertRaises(acq.AcquisitionError) as cm:
            get(a)
        self.assertEqual((cm.exception.code, t.calls), ('rate_limited', 1))

    def test_retry_after_http_date_form(self):
        self.assertEqual(acq.parse_retry_after('Thu, 01 Jan 1970 00:00:10 GMT', 4.0), 6.0)
        self.assertIsNone(acq.parse_retry_after('soon', 0))
        self.assertIsNone(acq.parse_retry_after('-5', 0))

    def test_repeated_429_cannot_produce_uncontrolled_loop(self):
        t = Script(*[resp(429, retry_after='1') for _ in range(100)])
        a, clock = make(t)
        with self.assertRaises(acq.AcquisitionError):
            get(a)
        self.assertLessEqual(t.calls, 3)
        self.assertLessEqual(sum(clock.sleeps), 3)

    def test_http_418_ban_signal_not_retried(self):
        t = Script(resp(418, retry_after='600'))
        a, _ = make(t)
        with self.assertRaises(acq.AcquisitionError) as cm:
            get(a)
        self.assertEqual((cm.exception.code, t.calls), ('rate_limited', 1))

    def test_client_error_is_permanent_and_does_not_trip_breaker(self):
        t = Script(*[resp(400) for _ in range(10)])
        a, _ = make(t)
        for _ in range(5):
            with self.assertRaises(acq.AcquisitionError) as cm:
                get(a)
            self.assertEqual(cm.exception.code, 'upstream_http_400')
        self.assertEqual(a.health()['binance_spot']['circuit_state'], acq.CLOSED)
        self.assertEqual(t.calls, 5)

    def test_5xx_retried_and_reported(self):
        t = Script(resp(503), resp())
        a, _ = make(t)
        self.assertEqual(get(a).attempts, 2)


class IntegrityTests(unittest.TestCase):
    def test_redirect_is_forbidden_and_not_followed(self):
        t = Script(resp(302, location='https://evil.example/'))
        a, _ = make(t)
        with self.assertRaises(acq.AcquisitionError) as cm:
            get(a)
        self.assertEqual((cm.exception.code, t.calls), ('redirect_forbidden', 1))

    def test_oversize_body_rejected_and_not_cached(self):
        t = Script(resp(body=b'x' * 11))
        a, _ = make(t)
        a.max_bytes = 10
        with self.assertRaises(acq.AcquisitionError) as cm:
            get(a)
        self.assertEqual(cm.exception.code, 'response_too_large')
        self.assertIsNone(a.cache.get(a.cache_key(URL)))

    def test_non_json_content_type_rejected_before_caching(self):
        t = Script(acq.TransportResponse(200, {'content-type': 'text/html'}, b'<html>'))
        a, _ = make(t)
        with self.assertRaises(acq.AcquisitionError) as cm:
            get(a)
        self.assertEqual(cm.exception.code, 'invalid_content_type')
        self.assertIsNone(a.cache.get(a.cache_key(URL)))

    def test_cache_disabled_without_policy(self):
        t = Script(resp(), resp())
        a, clock = make(t, cpol=acq.CachePolicy())
        get(a)
        get(a)
        self.assertEqual(t.calls, 2)

    def test_local_min_gap_fails_fast_without_waiting(self):
        t = Script(resp(), resp())
        a, clock = make(t, cache=False, policy=acq.ProviderPolicy(min_gap_seconds=5))
        get(a)
        with self.assertRaises(acq.AcquisitionError) as cm:
            get(a)
        self.assertEqual((cm.exception.code, t.calls, clock.sleeps), ('local_rate_limit', 1, []))


class BreakerAndQuotaTests(unittest.TestCase):
    def policy(self):
        return acq.ProviderPolicy(min_gap_seconds=0, max_attempts=1, breaker_failure_threshold=3,
                                  breaker_open_seconds=30)

    def test_breaker_opens_blocks_requests_then_recovers_via_half_open(self):
        t = Script(resp(503), resp(503), resp(503), resp())
        a, clock = make(t, cache=False, policy=self.policy())
        for _ in range(3):
            with self.assertRaises(acq.AcquisitionError):
                get(a)
        self.assertEqual(a.health()['binance_spot']['circuit_state'], acq.OPEN)
        with self.assertRaises(acq.AcquisitionError) as cm:
            get(a)
        self.assertEqual((cm.exception.code, t.calls), ('circuit_open', 3))
        clock.advance(31)
        self.assertEqual(a.health()['binance_spot']['circuit_state'], acq.HALF_OPEN)
        self.assertEqual(get(a).cache['status'], acq.LIVE)
        self.assertEqual(a.health()['binance_spot']['circuit_state'], acq.CLOSED)

    def test_half_open_probe_failure_reopens(self):
        t = Script(resp(503), resp(503), resp(503), resp(503))
        a, clock = make(t, cache=False, policy=self.policy())
        for _ in range(3):
            with self.assertRaises(acq.AcquisitionError):
                get(a)
        clock.advance(31)
        with self.assertRaises(acq.AcquisitionError):
            get(a)
        self.assertEqual(a.health()['binance_spot']['circuit_state'], acq.OPEN)
        with self.assertRaises(acq.AcquisitionError) as cm:
            get(a)
        self.assertEqual(cm.exception.code, 'circuit_open')

    def test_circuit_open_can_serve_opt_in_stale_cache(self):
        t = Script(resp(), resp(503), resp(503), resp(503))
        a, clock = make(t, policy=self.policy())
        get(a)
        clock.advance(20)
        for _ in range(3):
            with self.assertRaises(acq.AcquisitionError):
                get(a)
        self.assertEqual(get(a, allow_stale=True).cache['stale_reason'], 'circuit_open')

    def test_quota_budget_rejects_without_http(self):
        pol = acq.ProviderPolicy(min_gap_seconds=0, quota_budget=2, quota_window_seconds=60)
        t = Script()
        a, clock = make(t, cache=False, policy=pol)
        get(a); get(a)
        with self.assertRaises(acq.AcquisitionError) as cm:
            get(a)
        self.assertEqual((cm.exception.code, t.calls), ('quota_exceeded', 2))
        clock.advance(61)
        get(a)
        self.assertEqual(t.calls, 3)

    def test_health_diagnostics_structure(self):
        t = Script(resp(429, retry_after='60'))
        a, _ = make(t)
        with self.assertRaises(acq.AcquisitionError):
            get(a)
        h = a.health()['binance_spot']
        self.assertEqual((h['rate_limited'], h['last_error'], h['requests']), (1, 'rate_limited', 1))
        self.assertIsNotNone(h['cooldown_until_utc'])
        self.assertEqual(h['circuit_state'], acq.OPEN)


class CoreIntegrationTests(unittest.TestCase):
    def tearDown(self):
        core.set_acquirer(None)

    def fetch_with(self, *items):
        a = acq.Acquirer(Script(*items), clock=acq.FakeClock(),
                         provider_policies={'binance_spot': acq.ProviderPolicy(min_gap_seconds=0)})
        core.set_acquirer(a)
        return core.fetch('spot_ticker', params={'symbol': 'BTCUSDT'})

    def test_malformed_json_fails_closed(self):
        with self.assertRaisesRegex(ValueError, 'schema_changed'):
            self.fetch_with(resp(body=b'{not json'))

    def test_source_carries_cache_provenance(self):
        body, src = self.fetch_with(resp())
        self.assertEqual(body['bidPrice'], '1')
        self.assertEqual(src['cache']['status'], 'LIVE')
        self.assertEqual(src['origin'], 'https://data-api.binance.vision')

    def test_unavailable_envelope_for_rate_limit(self):
        from quant_free_mcp import adapters
        core.set_acquirer(acq.Acquirer(Script(resp(429, retry_after='60')), clock=acq.FakeClock()))
        out = adapters.spot_ticker('BTCUSDT')
        self.assertEqual((out['status'], out['value'], out['retryable']), ('rate_limited', None, True))
        self.assertFalse(out['fallback_used'])

    def test_unknown_route_and_url_forms_rejected(self):
        for kwargs in ({'route': 'https://evil.example/'}, {'route': '../etc/passwd'}):
            with self.assertRaisesRegex(ValueError, 'unsupported_route'):
                core.fetch(**kwargs)
        with self.assertRaises(ValueError):
            core.fetch('spot_ticker', params={'symbol': 'btc/usdt'})


class FallbackSemanticsTests(unittest.TestCase):
    T = '2026-01-01T00:00:00Z'

    def base(self, **over):
        c = MetricContract('venue:btc', 'funding', 'rate', 'venue', '8h', 'provider', coverage='snapshot')
        cand = {'provider': 'p', 'contract': dict(c.__dict__), 'status': 'OBSERVED', 'observed_at_utc': self.T,
                'available_at_utc': self.T, 'retrieved_at_utc': self.T}
        cand.update(over)
        return c, cand

    def decide(self, cand, c, **kw):
        return qualified_fallback(c, [cand], self.T, max_age_seconds=60, **kw)['decisions'][0]['status']

    def test_each_semantic_field_mismatch_is_incompatible(self):
        c, cand = self.base()
        for field, value in (('unit', 'bps'), ('venue', 'other'), ('method', 'derived'),
                             ('interval', '1h'), ('coverage', 'window'), ('instrument_id', 'venue:eth'),
                             ('metric', 'oi')):
            other = {**cand, 'contract': {**cand['contract'], field: value}}
            self.assertEqual(self.decide(other, c), 'INCOMPATIBLE', field)

    def test_stale_cache_rejected_unless_allowed(self):
        c, cand = self.base(cache={'status': 'STALE_CACHE'})
        self.assertEqual(self.decide(cand, c), 'STALE_CACHE_REJECTED')
        self.assertEqual(self.decide(cand, c, allow_stale_cache=True), 'QUALIFIED')

    def test_future_retrieval_and_availability_before_observation_rejected(self):
        c, cand = self.base(retrieved_at_utc='2026-01-01T00:05:00Z')
        self.assertEqual(self.decide(cand, c), 'STALE_OR_FUTURE')
        c, cand = self.base(observed_at_utc='2025-12-31T23:59:30Z', available_at_utc='2025-12-31T23:59:00Z')
        self.assertEqual(self.decide(cand, c), 'INVALID_TIMESTAMP')

    def test_no_qualified_candidate_is_insufficient_evidence_not_substitution(self):
        c, cand = self.base()
        other = {**cand, 'contract': {**cand['contract'], 'unit': 'bps'}}
        out = qualified_fallback(c, [other], self.T, max_age_seconds=60)
        self.assertEqual((out['status'], out['candidate']), ('INSUFFICIENT_EVIDENCE', None))

    def test_strict_retrieval_time_option(self):
        c, cand = self.base()
        del cand['retrieved_at_utc']
        self.assertEqual(self.decide(cand, c), 'QUALIFIED')
        self.assertEqual(self.decide(cand, c, require_retrieval_time=True), 'STALE_OR_FUTURE')


if __name__ == '__main__':
    unittest.main()
