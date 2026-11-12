"""Reusable read-only acquisition layer: cache, quota, bounded retry, circuit breaker.

Security posture is unchanged from ``core``: callers pass an already validated
absolute HTTPS URL built from the fixed route registry; there is no redirect
following, no credential handling and no arbitrary-URL entry point. Clock,
transport, sleep and jitter are injected so every failure path is testable
offline and deterministically.

Cache provenance is explicit: every result states whether it came from the
network (LIVE), a fresh cache entry (FRESH_CACHE) or an expired entry served
only because the caller opted in after an upstream failure (STALE_CACHE).
A cache hit never rewrites ``retrieved_at_utc``; it keeps the original value.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from threading import RLock
from typing import Any, Callable, Protocol
import hashlib
import json
import random
import sqlite3
import time

CLOSED, OPEN, HALF_OPEN = 'CLOSED', 'OPEN', 'HALF_OPEN'
LIVE, FRESH_CACHE, STALE_CACHE = 'LIVE', 'FRESH_CACHE', 'STALE_CACHE'
RETRYABLE_HTTP = frozenset({500, 502, 503, 504})
# 418 is Binance's IP-ban signal: never retried, treated like a rate limit.
RATE_LIMIT_HTTP = frozenset({418, 429})
# Failures after which an already-cached same-URL body may be served as STALE_CACHE (opt-in).
_STALE_ELIGIBLE = frozenset({'rate_limited', 'network_unavailable', 'circuit_open', 'quota_exceeded',
                             'upstream_timeout', 'local_rate_limit'})


class AcquisitionError(RuntimeError):
    """Fail-closed acquisition failure. ``str(exc)`` is a stable reason code that
    ``core.unavailable`` already maps (rate_limited, network_unavailable, ...)."""

    def __init__(self, code: str, *, retry_after_seconds: float | None = None,
                 attempts: int = 0, detail: str | None = None):
        super().__init__(code)
        self.code, self.retry_after_seconds = code, retry_after_seconds
        self.attempts, self.detail = attempts, detail


class TransportTimeout(Exception):
    pass


class TransportError(Exception):
    pass


@dataclass(frozen=True)
class TransportResponse:
    status: int
    headers: dict[str, str]
    body: bytes


class Transport(Protocol):
    def __call__(self, url: str, *, headers: dict[str, str], timeout: float,
                 max_bytes: int) -> TransportResponse: ...


class Clock(Protocol):
    def now(self) -> float: ...  # epoch seconds
    def sleep(self, seconds: float) -> None: ...


class SystemClock:
    def now(self) -> float:
        return time.time()

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


class FakeClock:
    """Deterministic clock; ``sleep`` advances time and records the request."""

    def __init__(self, start: float = 1_700_000_000.0):
        self.t, self.sleeps = float(start), []

    def now(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.t += max(0.0, seconds)

    def advance(self, seconds: float) -> None:
        self.t += seconds


def iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')


@dataclass(frozen=True)
class ProviderPolicy:
    """All bounds are explicit; there is no unbounded loop or wait anywhere."""
    min_gap_seconds: float = 0.5
    quota_budget: int = 600          # weight units per window
    quota_window_seconds: float = 60.0
    max_attempts: int = 3            # total tries including the first
    backoff_base_seconds: float = 0.5
    backoff_cap_seconds: float = 8.0
    max_retry_after_wait_seconds: float = 10.0  # longer Retry-After => do not sleep, fail closed
    breaker_failure_threshold: int = 3
    breaker_open_seconds: float = 30.0
    timeout_seconds: float = 12.0


@dataclass(frozen=True)
class CachePolicy:
    ttl_seconds: float = 0.0              # 0 => never serve a fresh hit
    stale_if_error_seconds: float = 0.0   # max age beyond expiry usable on failure
    max_entry_bytes: int = 2_000_000


DEFAULT_PROVIDER_POLICIES = {
    'binance_spot': ProviderPolicy(min_gap_seconds=.4, quota_budget=1200),
    'binance_usdm': ProviderPolicy(min_gap_seconds=.6, quota_budget=1200),
    'dexscreener': ProviderPolicy(min_gap_seconds=.5, quota_budget=300),
    'defillama': ProviderPolicy(min_gap_seconds=1.0, quota_budget=300),
    'mempool': ProviderPolicy(min_gap_seconds=1.0, quota_budget=300),
}
# Conservative request weights (approximate; Binance weights depend on limit).
ROUTE_WEIGHTS = {'spot_book': 10, 'spot_candles': 2, 'futures_mark': 1, 'futures_oi': 1,
                 'futures_funding': 1}
# Explicit cache expiry per route (seconds). Routes absent here are not cached.
DEFAULT_CACHE_POLICIES = {
    'spot_book': CachePolicy(2, 30), 'spot_ticker': CachePolicy(2, 30),
    'spot_trades': CachePolicy(2, 30), 'spot_candles': CachePolicy(30, 300),
    'futures_mark': CachePolicy(2, 30), 'futures_oi': CachePolicy(10, 120),
    'futures_funding': CachePolicy(300, 3600), 'defi_protocol': CachePolicy(300, 3600),
    'defi_protocols': CachePolicy(300, 3600), 'defi_chain': CachePolicy(300, 3600),
    'dex_pair': CachePolicy(5, 60), 'dex_token': CachePolicy(5, 60),
    'btc_fees': CachePolicy(10, 120), 'btc_mempool': CachePolicy(10, 120),
    'btc_tip': CachePolicy(10, 120), 'btc_hashrate': CachePolicy(300, 3600),
}

CACHE_SCHEMA = '''
CREATE TABLE IF NOT EXISTS responses (
 key TEXT PRIMARY KEY, provider TEXT NOT NULL, route TEXT NOT NULL, url TEXT NOT NULL,
 retrieved_at REAL NOT NULL, expires_at REAL NOT NULL, stale_until REAL NOT NULL,
 sha256 TEXT NOT NULL, body BLOB NOT NULL
);
'''


class ResponseCache:
    """SQLite response cache. Entries are digest-checked on read; a corrupt row is a miss."""

    def __init__(self, path: str | Path = ':memory:'):
        self._lock = RLock()
        if str(path) != ':memory:':
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), timeout=15, check_same_thread=False)
        self._db.executescript(CACHE_SCHEMA)

    def put(self, key, provider, route, url, body: bytes, retrieved_at: float, policy: CachePolicy) -> bool:
        if policy.ttl_seconds <= 0 and policy.stale_if_error_seconds <= 0:
            return False
        if len(body) > policy.max_entry_bytes:
            return False
        expires = retrieved_at + policy.ttl_seconds
        with self._lock, self._db:
            self._db.execute('INSERT OR REPLACE INTO responses VALUES(?,?,?,?,?,?,?,?,?)',
                             (key, provider, route, url, retrieved_at, expires,
                              expires + policy.stale_if_error_seconds,
                              hashlib.sha256(body).hexdigest(), body))
        return True

    def get(self, key):
        with self._lock:
            row = self._db.execute('SELECT retrieved_at,expires_at,stale_until,sha256,body FROM responses WHERE key=?',
                                   (key,)).fetchone()
        if row is None or hashlib.sha256(row[4]).hexdigest() != row[3]:
            return None
        return {'retrieved_at': row[0], 'expires_at': row[1], 'stale_until': row[2], 'body': bytes(row[4])}

    def purge_expired(self, now: float) -> int:
        with self._lock, self._db:
            return self._db.execute('DELETE FROM responses WHERE stale_until<?', (now,)).rowcount

    def close(self) -> None:
        self._db.close()

    def __del__(self):
        try:
            self._db.close()
        except Exception:  # noqa: BLE001 - interpreter shutdown safe
            pass


class _Ticket:
    """Admission ticket. ``probe`` is True only for the single HALF_OPEN probe.
    Each ticket settles exactly once (success, failure or release); later calls are no-ops."""
    __slots__ = ('probe', 'settled')

    def __init__(self, probe: bool):
        self.probe, self.settled = probe, False


class CircuitBreaker:
    """Thread-safe breaker. ``allow`` returns a ticket (or None when rejected); the holder must
    settle it through ``success``/``failure``/``release``. A probe ticket that is never settled
    would wedge the breaker, so callers settle in a ``finally``."""

    def __init__(self, threshold: int, open_seconds: float):
        self.threshold, self.open_seconds = threshold, open_seconds
        self.state, self.failures, self.opened_until = CLOSED, 0, 0.0
        self._probe_in_flight = False
        self._lock = RLock()

    def allow(self, now: float) -> '_Ticket | None':
        with self._lock:
            if self.state == OPEN:
                if now < self.opened_until:
                    return None
                self.state, self._probe_in_flight = HALF_OPEN, False
            if self.state == HALF_OPEN:
                if self._probe_in_flight:
                    return None  # exactly one probe at a time
                self._probe_in_flight = True
                return _Ticket(True)
            return _Ticket(False)

    def release(self, ticket: '_Ticket | None') -> None:
        """Settle without a health verdict (rejected locally, permanent client error, redirect,
        cancellation). A probe ticket frees the probe slot so the next caller may probe."""
        with self._lock:
            if ticket is None or ticket.settled:
                return
            ticket.settled = True
            if ticket.probe:
                self._probe_in_flight = False

    def success(self, ticket: '_Ticket | None') -> None:
        with self._lock:
            if ticket is None or ticket.settled:
                return
            ticket.settled = True
            if ticket.probe:
                self._probe_in_flight = False
                self.state, self.failures = CLOSED, 0
            elif self.state == CLOSED:
                self.failures = 0
            # a late success from a pre-open request must not close an OPEN/HALF_OPEN breaker

    def failure(self, ticket: '_Ticket | None', now: float, *, cooldown: float = 0.0) -> None:
        with self._lock:
            if ticket is None or ticket.settled:
                return
            ticket.settled = True
            self.failures += 1
            if ticket.probe:
                self._probe_in_flight = False
                self.state = OPEN
            elif self.state == CLOSED and (self.failures >= self.threshold or cooldown > 0):
                self.state = OPEN
            else:
                return  # already OPEN/HALF_OPEN: count it, keep existing window
            self.opened_until = now + max(self.open_seconds, cooldown)


@dataclass
class _Health:
    requests: int = 0
    successes: int = 0
    failures: int = 0
    retries: int = 0
    rate_limited: int = 0
    timeouts: int = 0
    cache_hits: int = 0
    stale_served: int = 0
    circuit_rejections: int = 0
    quota_rejections: int = 0
    last_error: str | None = None
    last_success_at: float | None = None


@dataclass(frozen=True)
class AcquisitionResult:
    body: bytes
    provider: str
    route: str
    url: str
    retrieved_at: float
    cache: dict[str, Any]
    attempts: int


def default_transport(opener) -> Transport:
    """Wrap a urllib opener (core supplies the no-redirect one). Size-capped read."""
    from urllib.error import HTTPError, URLError
    from urllib.request import Request
    import socket

    def call(url, *, headers, timeout, max_bytes):
        req = Request(url, headers=headers, method='GET')
        try:
            with opener.open(req, timeout=timeout) as resp:
                data = resp.read(max_bytes + 1)
                return TransportResponse(resp.status, {k.lower(): v for k, v in resp.headers.items()}, data)
        except HTTPError as exc:
            return TransportResponse(exc.code, {k.lower(): v for k, v in exc.headers.items()}, b'')
        except (socket.timeout, TimeoutError) as exc:
            raise TransportTimeout(str(exc)) from exc
        except URLError as exc:
            if isinstance(exc.reason, (socket.timeout, TimeoutError)):
                raise TransportTimeout(str(exc)) from exc
            raise TransportError(str(exc)) from exc
    return call


def parse_retry_after(value: str | None, now: float) -> float | None:
    """Seconds from either delta-seconds or HTTP-date form; None if absent/invalid."""
    if not value:
        return None
    value = value.strip()
    try:
        seconds = float(value)
        return seconds if 0 <= seconds < 1e7 else None
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(value)
        if when.tzinfo is None:
            return None
        return max(0.0, when.timestamp() - now)
    except (TypeError, ValueError):
        return None


class Acquirer:
    def __init__(self, transport: Transport, *, clock: Clock | None = None,
                 cache: ResponseCache | None = None,
                 provider_policies: dict[str, ProviderPolicy] | None = None,
                 cache_policies: dict[str, CachePolicy] | None = None,
                 jitter: Callable[[], float] | None = None, max_bytes: int = 2_000_000,
                 user_agent: str = 'quant-free-mcp/0.2'):
        self.transport, self.clock = transport, clock or SystemClock()
        self.cache, self.max_bytes = cache, max_bytes
        self.provider_policies = provider_policies or dict(DEFAULT_PROVIDER_POLICIES)
        self.cache_policies = cache_policies if cache_policies is not None else dict(DEFAULT_CACHE_POLICIES)
        self.jitter = jitter or random.random
        self.headers = {'Accept': 'application/json', 'User-Agent': user_agent}
        # ``_registry`` only guards creation of per-provider objects. All provider state is guarded
        # by that provider's own lock; no lock is ever held across transport I/O or a sleep.
        self._registry = RLock()
        self._plocks: dict[str, RLock] = {}
        self._breakers: dict[str, CircuitBreaker] = {}
        self._health: dict[str, _Health] = {}
        self._quota: dict[str, list[tuple[float, int]]] = {}
        self._next_at: dict[str, float] = {}
        self._cooldown: dict[str, float] = {}

    # -- helpers -----------------------------------------------------------
    def _policy(self, provider):
        return self.provider_policies.get(provider) or ProviderPolicy()

    def _plock(self, provider) -> RLock:
        with self._registry:
            return self._plocks.setdefault(provider, RLock())

    def _breaker(self, provider):
        with self._registry:
            if provider not in self._breakers:
                p = self._policy(provider)
                self._breakers[provider] = CircuitBreaker(p.breaker_failure_threshold, p.breaker_open_seconds)
            return self._breakers[provider]

    def _hl(self, provider) -> _Health:
        with self._registry:
            return self._health.setdefault(provider, _Health())

    def _charge_quota(self, provider, weight, now) -> bool:
        p = self._policy(provider)
        window = [(t, w) for t, w in self._quota.get(provider, []) if now - t < p.quota_window_seconds]
        if sum(w for _, w in window) + weight > p.quota_budget:
            self._quota[provider] = window
            return False
        window.append((now, weight))
        self._quota[provider] = window
        return True

    @staticmethod
    def cache_key(url: str) -> str:
        return hashlib.sha256(url.encode()).hexdigest()

    def _cache_meta(self, status, entry, now):
        return {'status': status, 'served_from_cache': status != LIVE,
                'retrieved_at_utc': iso(entry['retrieved_at']), 'age_seconds': round(now - entry['retrieved_at'], 3),
                'expires_at_utc': iso(entry['expires_at'])}

    # -- main entry point ----------------------------------------------------
    def get(self, provider: str, route: str, url: str, *, allow_stale: bool = False,
            require_json: bool = True) -> AcquisitionResult:
        pol, cpol = self._policy(provider), self.cache_policies.get(route, CachePolicy())
        key = self.cache_key(url)
        health, plock = self._hl(provider), self._plock(provider)
        now = self.clock.now()
        entry = self.cache.get(key) if self.cache is not None else None  # cache has its own lock
        if entry and now < entry['expires_at'] and now >= entry['retrieved_at']:
            with plock:
                health.cache_hits += 1
            return AcquisitionResult(entry['body'], provider, route, url, entry['retrieved_at'],
                                     self._cache_meta(FRESH_CACHE, entry, now), 0)
        try:
            return self._network(provider, route, url, key, pol, cpol, health, require_json)
        except AcquisitionError as exc:
            now = self.clock.now()
            # Stale fallback is same URL (same metric/venue/params) only, and opt-in.
            usable = (allow_stale and entry is not None
                      and entry['retrieved_at'] <= now <= entry['stale_until']
                      and (exc.code in _STALE_ELIGIBLE or exc.code.startswith('upstream_http_5')))
            if usable:
                with plock:
                    health.stale_served += 1
                meta = self._cache_meta(STALE_CACHE, entry, now)
                meta['stale_reason'] = exc.code
                return AcquisitionResult(entry['body'], provider, route, url, entry['retrieved_at'], meta, exc.attempts)
            raise

    def _network(self, provider, route, url, key, pol, cpol, health, require_json) -> AcquisitionResult:
        attempts = 0
        while attempts < pol.max_attempts:  # bounded: max_attempts is the hard ceiling
            attempts, result, delay = self._attempt(provider, route, url, key, pol, cpol, health, require_json, attempts)
            if result is not None:
                return result
            self.clock.sleep(delay)  # backoff / honoured Retry-After: no lock held
        raise AcquisitionError('retries_exhausted', attempts=attempts)  # defensive; loop is bounded

    def _attempt(self, provider, route, url, key, pol, cpol, health, require_json, attempts):
        """One bounded attempt. Returns (attempts, result, None) or (attempts, None, delay_before_retry),
        or raises. The breaker ticket is settled exactly once on every exit path."""
        plock, breaker, weight = self._plock(provider), self._breaker(provider), ROUTE_WEIGHTS.get(route, 1)
        ticket = None
        try:
            with plock:  # short critical section: admission only
                now = self.clock.now()
                cooldown = self._cooldown.get(provider, 0.0)
                if now < cooldown:
                    health.circuit_rejections += 1
                    raise AcquisitionError('rate_limited', retry_after_seconds=cooldown - now, attempts=attempts,
                                           detail='provider_cooldown_active')
                ticket = breaker.allow(now)
                if ticket is None:
                    health.circuit_rejections += 1
                    raise AcquisitionError('circuit_open', retry_after_seconds=max(0.0, breaker.opened_until - now),
                                           attempts=attempts)
                if attempts == 0 and now < self._next_at.get(provider, 0.0):
                    raise AcquisitionError('local_rate_limit', attempts=0)
                if not self._charge_quota(provider, weight, now):
                    health.quota_rejections += 1
                    raise AcquisitionError('quota_exceeded', attempts=attempts)
                self._next_at[provider] = now + pol.min_gap_seconds
                attempts += 1
                health.requests += 1
            code, retry_after, retryable = None, None, False
            try:  # network I/O: no lock held
                resp = self.transport(url, headers=self.headers, timeout=pol.timeout_seconds, max_bytes=self.max_bytes)
            except TransportTimeout:
                with plock:
                    health.timeouts += 1
                code, retryable = 'upstream_timeout', True
            except TransportError:
                code, retryable = 'network_unavailable', True
            except ValueError as exc:
                if str(exc) != 'redirect_forbidden':  # core.NoRedirect surfaces a redirect as this error
                    raise  # unexpected: ticket is released in ``finally`` and the error propagates
                with plock:
                    health.failures += 1
                    health.last_error = 'redirect_forbidden'
                raise AcquisitionError('redirect_forbidden', attempts=attempts)
            else:
                if resp.status == 200:
                    return attempts, self._accept(provider, route, url, key, cpol, health, breaker, ticket,
                                                  resp, require_json, attempts), None
                if 300 <= resp.status < 400:
                    with plock:
                        health.failures += 1
                        health.last_error = 'redirect_forbidden'
                    raise AcquisitionError('redirect_forbidden', attempts=attempts)  # ticket released in finally
                if resp.status in RATE_LIMIT_HTTP:
                    retry_after = parse_retry_after(resp.headers.get('retry-after'), self.clock.now())
                    code, retryable = 'rate_limited', retry_after is not None and resp.status == 429
                    with plock:
                        health.rate_limited += 1
                elif resp.status in RETRYABLE_HTTP:
                    code, retryable = f'upstream_http_{resp.status}', True
                    retry_after = parse_retry_after(resp.headers.get('retry-after'), self.clock.now())
                else:
                    code = f'upstream_http_{resp.status}'  # 4xx etc.: permanent, not retried
            with plock:  # short critical section: failure accounting + retry decision
                health.failures += 1
                health.last_error = code
                now = self.clock.now()
                if code == 'rate_limited':
                    wait = retry_after if retry_after is not None else pol.breaker_open_seconds
                    # Retry-After is binding: cool the provider down even if this call gives up.
                    self._cooldown[provider] = max(self._cooldown.get(provider, 0.0), now + wait)
                    will_sleep_and_retry = (retryable and attempts < pol.max_attempts
                                            and retry_after <= pol.max_retry_after_wait_seconds)
                    # If we will wait the window out in-call, the failure is counted but does not by
                    # itself open the breaker; when we give up (or the threshold trips) it does.
                    breaker.failure(ticket, now, cooldown=0.0 if will_sleep_and_retry else wait)
                elif retryable:
                    breaker.failure(ticket, now)
                else:
                    breaker.release(ticket)  # permanent client error: not a provider-health failure
                    raise AcquisitionError(code, attempts=attempts)
                give_up = (not retryable or attempts >= pol.max_attempts or breaker.state != CLOSED
                           or (code == 'rate_limited' and retry_after > pol.max_retry_after_wait_seconds))
                if give_up:  # includes breaker tripped mid-retry: stop, do not hammer
                    raise AcquisitionError(code, retry_after_seconds=retry_after, attempts=attempts)
                if code == 'rate_limited':
                    delay = retry_after
                else:
                    delay = min(pol.backoff_cap_seconds, pol.backoff_base_seconds * (2 ** (attempts - 1)))
                    delay *= 0.5 + 0.5 * self.jitter()  # bounded jitter in [0.5x, 1.0x)
                    if retry_after is not None:
                        delay = max(delay, min(retry_after, pol.max_retry_after_wait_seconds))
                health.retries += 1
            return attempts, None, delay
        finally:
            breaker.release(ticket)  # no-op if already settled; frees a probe on any other exit

    def _accept(self, provider, route, url, key, cpol, health, breaker, ticket, resp, require_json, attempts):
        plock = self._plock(provider)
        problem = None
        if len(resp.body) > self.max_bytes:
            problem = 'response_too_large'
        elif require_json and 'json' not in resp.headers.get('content-type', '').lower():
            problem = 'invalid_content_type'
        if problem:
            with plock:
                breaker.failure(ticket, self.clock.now())
                health.failures += 1
                health.last_error = problem
            raise AcquisitionError(problem, attempts=attempts)
        retrieved = self.clock.now()
        with plock:
            breaker.success(ticket)
            health.successes += 1
            health.last_success_at = retrieved
        if self.cache is not None:
            self.cache.put(key, provider, route, url, resp.body, retrieved, cpol)
        return AcquisitionResult(resp.body, provider, route, url, retrieved,
                                 {'status': LIVE, 'served_from_cache': False,
                                  'retrieved_at_utc': iso(retrieved), 'age_seconds': 0.0}, attempts)

    # -- diagnostics -----------------------------------------------------------
    def health(self) -> dict[str, dict[str, Any]]:
        now = self.clock.now()
        out = {}
        with self._registry:
            providers = sorted(set(self._health) | set(self._breakers))
        for provider in providers:
            h, b, p = self._hl(provider), self._breaker(provider), self._policy(provider)
            with self._plock(provider):
                out[provider] = self._health_row(provider, h, b, p, now)
        return out

    def _health_row(self, provider, h, b, p, now):
        used = sum(w for t, w in self._quota.get(provider, []) if now - t < p.quota_window_seconds)
        return {
            'circuit_state': b.state if not (b.state == OPEN and now >= b.opened_until) else HALF_OPEN,
            'consecutive_failures': b.failures,
            'circuit_open_until_utc': iso(b.opened_until) if b.state == OPEN and now < b.opened_until else None,
            'cooldown_until_utc': iso(self._cooldown[provider]) if self._cooldown.get(provider, 0) > now else None,
            'quota_used': used, 'quota_budget': p.quota_budget,
            'requests': h.requests, 'successes': h.successes, 'failures': h.failures,
            'retries': h.retries, 'rate_limited': h.rate_limited, 'timeouts': h.timeouts,
            'cache_hits': h.cache_hits, 'stale_served': h.stale_served,
            'circuit_rejections': h.circuit_rejections, 'quota_rejections': h.quota_rejections,
            'last_error': h.last_error,
            'last_success_at_utc': iso(h.last_success_at) if h.last_success_at else None,
            'as_of_utc': iso(now),
        }
