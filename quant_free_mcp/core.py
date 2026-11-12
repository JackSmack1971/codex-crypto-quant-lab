"""Bounded public GET client and consistent provenance envelopes. No arbitrary URL interface."""
from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, build_opener, HTTPRedirectHandler
import json
import os
import re

from . import acquisition as _acq

ORIGINS = {
    'binance_spot': 'https://data-api.binance.vision',
    'binance_usdm': 'https://fapi.binance.com',
    'defillama': 'https://api.llama.fi',
    'dexscreener': 'https://api.dexscreener.com',
    'mempool': 'https://mempool.space',
}
# Exactly these routes; do not accept a user-controlled URL/path.
ROUTES = {
    'spot_book': ('binance_spot', '/api/v3/depth'),
    'spot_ticker': ('binance_spot', '/api/v3/ticker/bookTicker'),
    'spot_trades': ('binance_spot', '/api/v3/trades'),
    'spot_candles': ('binance_spot', '/api/v3/klines'),
    'futures_mark': ('binance_usdm', '/fapi/v1/premiumIndex'),
    'futures_oi': ('binance_usdm', '/fapi/v1/openInterest'),
    'futures_funding': ('binance_usdm', '/fapi/v1/fundingRate'),
    'defi_protocol': ('defillama', '/protocol/{slug}'),
    'defi_protocols': ('defillama', '/protocols'),
    'defi_chain': ('defillama', '/v2/historicalChainTvl/{slug}'),
    'dex_pair': ('dexscreener', '/latest/dex/pairs/{chain}/{pair}'),
    'dex_token': ('dexscreener', '/token-pairs/v1/{chain}/{pair}'),
    'dex_search': ('dexscreener', '/latest/dex/search'),
    'btc_fees': ('mempool', '/api/v1/fees/recommended'),
    'btc_mempool': ('mempool', '/api/mempool'),
    'btc_tip': ('mempool', '/api/blocks/tip/height'),
    'btc_hashrate': ('mempool', '/api/v1/mining/hashrate/{period}'),
}
MAX_BYTES = 2_000_000
_SYMBOL = re.compile(r'^[A-Z0-9]{4,22}$')
_SAFE = re.compile(r'^[A-Za-z0-9_-]{1,70}$')
_EVM = re.compile(r'^0x[0-9a-fA-F]{40}$')
_BASE58 = re.compile(r'^[1-9A-HJ-NP-Za-km-z]{32,44}$')


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec='seconds').replace('+00:00','Z')


def decimal(value: Any) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise ValueError('missing_or_invalid_decimal')
    try:
        x = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError('invalid_decimal') from exc
    if not x.is_finite():
        raise ValueError('non_finite_decimal')
    return x


def valid_symbol(s: str) -> str:
    if not isinstance(s, str) or not _SYMBOL.fullmatch(s):
        raise ValueError('invalid_exchange_symbol')
    return s


def valid_slug(s: str) -> str:
    if not isinstance(s, str) or not _SAFE.fullmatch(s):
        raise ValueError('invalid_slug_or_chain')
    return s


def valid_address(s: str, chain: str) -> str:
    valid_slug(chain)
    if not isinstance(s, str) or not (_EVM.fullmatch(s) or (chain.lower() == 'solana' and _BASE58.fullmatch(s))):
        raise ValueError('invalid_chain_address')
    return s


def utc_epoch(ms: Any) -> str:
    return datetime.fromtimestamp(int(ms)/1000, tz=timezone.utc).isoformat(timespec='milliseconds').replace('+00:00','Z')


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError('redirect_forbidden')


_OPENER = build_opener(NoRedirect())
_ACQUIRER: _acq.Acquirer | None = None


def set_acquirer(acquirer: '_acq.Acquirer | None') -> None:
    """Dependency-injection seam for tests and embedding; None restores the default."""
    global _ACQUIRER
    _ACQUIRER = acquirer


def get_acquirer() -> '_acq.Acquirer':
    """Default: bounded retry, quota, breaker, no redirects. SQLite caching is opt-in via
    QUANT_FREE_MCP_CACHE_PATH (a local file path; never a secret or URL)."""
    global _ACQUIRER
    if _ACQUIRER is None:
        cache_path = os.environ.get('QUANT_FREE_MCP_CACHE_PATH')
        _ACQUIRER = _acq.Acquirer(_acq.default_transport(_OPENER),
                                  cache=_acq.ResponseCache(cache_path) if cache_path else None)
    return _ACQUIRER


def build_url(route: str, params: dict[str, str | int] | None = None,
              parts: dict[str, str] | None = None) -> tuple[str, str, str]:
    """Validate a fixed route and return (provider, path, url). No other URL can be formed."""
    if route not in ROUTES:
        raise ValueError('unsupported_route')
    provider, path = ROUTES[route]
    for key, value in (parts or {}).items():
        if key not in ('slug','chain','pair','period'):
            raise ValueError('unsupported_path_parameter')
        if key == 'pair':
            valid_address(value, (parts or {}).get('chain',''))
        elif key == 'period':
            if value not in ('1m','3m','6m','1y','2y','3y'):
                raise ValueError('invalid_mining_period')
        else:
            valid_slug(value)
        if '{'+key+'}' not in path:
            raise ValueError('unused_path_parameter')
        path = path.replace('{'+key+'}',value)
    if '{' in path:
        raise ValueError('missing_path_parameter')
    args = params or {}
    if len(args) > 6 or any(k not in ('symbol','limit','interval','startTime','endTime','q') for k in args):
        raise ValueError('unsupported_query_parameter')
    if 'symbol' in args: valid_symbol(str(args['symbol']))
    if 'limit' in args and not 1 <= int(args['limit']) <= 1000:
        raise ValueError('invalid_limit')
    if 'interval' in args and args['interval'] not in ('1m','5m','15m','30m','1h','2h','4h','1d'):
        raise ValueError('invalid_interval')
    if 'q' in args and (not isinstance(args['q'],str) or not 2<=len(args['q'])<=80):
        raise ValueError('invalid_search_query')
    return provider, path, ORIGINS[provider] + path + ('?' + urlencode(args) if args else '')


def fetch(route: str, *, params: dict[str, str | int] | None = None, parts: dict[str,str] | None = None,
          allow_stale: bool = False) -> tuple[Any, dict[str,Any]]:
    """Fixed-route GET only. No redirects; bounded payload/retries; fail closed on malformed JSON.

    ``source['cache']`` states whether the body is LIVE, FRESH_CACHE or (opt-in) STALE_CACHE;
    ``retrieved_at_utc`` is always the original network retrieval time."""
    provider, path, url = build_url(route, params, parts)
    result = get_acquirer().get(provider, route, url, allow_stale=allow_stale, require_json=route != 'btc_tip')
    data = result.body
    if len(data) > MAX_BYTES:
        raise ValueError('response_too_large')
    if route == 'btc_tip':
        try: body = int(data.strip())
        except ValueError as exc: raise ValueError('schema_changed') from exc
    else:
        try: body = json.loads(data)
        except (UnicodeError, json.JSONDecodeError) as exc: raise ValueError('schema_changed') from exc
    return body, {'provider':provider,'origin':ORIGINS[provider], 'endpoint':path,
                  'retrieved_at_utc':_acq.iso(result.retrieved_at), 'route':route,
                  'cache':result.cache, 'attempts':result.attempts}


def envelope(metric: str, instrument_id: str, value: Any, unit: str, source: dict[str,Any],
             observed_at: str | None = None, *, method: str = 'source_observation', coverage: str = 'snapshot',
             flags: list[str] | None = None) -> dict[str,Any]:
    return {'schema_version':'1.0','status':'ok','metric':metric,'instrument_id':instrument_id,
            'value':value,'unit':unit,'observation_time_utc':observed_at,
            'retrieved_at_utc':source['retrieved_at_utc'],'source':source,'methodology':method,
            'coverage':coverage,'quality_flags':flags or (['source_time_unavailable'] if not observed_at else []),
            'fallback_used':False}


def unavailable(metric: str, instrument: str, exc: Exception) -> dict[str,Any]:
    reason = str(exc)
    status = ('invalid_request' if isinstance(exc,ValueError) and reason.startswith(('invalid_','unsupported_','missing_'))
              else 'rate_limited' if reason in ('rate_limited','local_rate_limit','quota_exceeded') else
              'schema_changed' if reason in ('schema_changed','invalid_content_type','response_too_large') else 'unavailable')
    return {'schema_version':'1.0','status':status,'metric':metric,'instrument_id':instrument,
            'value':None,'unit':None,'observation_time_utc':None,'retrieved_at_utc':utcnow(),
            'source':None,'reason':reason,'retryable': status in ('rate_limited','unavailable'),
            'fallback_used':False,'quality_flags':['no_verified_observation']}
