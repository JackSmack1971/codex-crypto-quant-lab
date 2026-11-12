#!/usr/bin/env python3
"""Fetch closed OHLCV candles from one venue and write a verified canonical CSV.

Venue (v1, single): Binance spot public market-data mirror
https://data-api.binance.vision (keyless). api.binance.com returns HTTP 451 from
the development machine; the mirror serves the same /api/v3/klines data.

Canonical CSV (the only contract downstream tools consume):
  timestamp_utc (ISO8601, bar OPEN time, UTC), open, high, low, close, volume
  ascending, unique, closed candles only.
Sidecar <output>.meta.json records provenance and the sha256 of the CSV.
Gaps or complete=false exit non-zero unless --allow-gaps. Every failure exits
non-zero with a message on stderr and nothing on stdout.
"""
import argparse
import hashlib
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

SOURCE = 'binance_spot_market_data'
BASE_URL = 'https://data-api.binance.vision'
ENDPOINT = BASE_URL + '/api/v3/klines'
PAGE_LIMIT = 1000
INTERVAL_MS = {
    '1m': 60_000, '5m': 300_000, '15m': 900_000, '30m': 1_800_000,
    '1h': 3_600_000, '2h': 7_200_000, '4h': 14_400_000, '1d': 86_400_000,
}
COLUMNS = ['timestamp_utc', 'open', 'high', 'low', 'close', 'volume']
META_REQUIRED = ['source', 'endpoint', 'symbol', 'interval', 'requested_start_utc',
                 'requested_end_utc', 'fetched_at_utc', 'rows', 'complete', 'gaps', 'sha256']


class DataError(Exception):
    """Any condition that must produce a non-zero exit."""


def fetch_json(url):
    # Retry/backoff pattern follows src/edge_forensics.py:fetch_json.
    for attempt in range(5):
        try:
            req = urllib.request.Request(url, headers={
                'User-Agent': 'quant-analysis-market-data/1.0', 'Accept': 'application/json'})
            with urllib.request.urlopen(req, timeout=35) as response:
                return json.load(response)
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as exc:
            if isinstance(exc, urllib.error.HTTPError) and exc.code not in (429, 500, 502, 503, 504):
                body = exc.read()[:200].decode('utf-8', 'replace')
                raise DataError(f'HTTP {exc.code} from {url}: {body}') from exc
            if attempt == 4:
                raise DataError(f'request failed after retries: {url}: {exc}') from exc
            time.sleep(min(2 ** attempt, 16))


def iso(ms):
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def parse_utc(text):
    try:
        ts = pd.Timestamp(text)
    except ValueError as exc:
        raise DataError(f'unparseable timestamp: {text!r}') from exc
    ts = ts.tz_localize('UTC') if ts.tzinfo is None else ts.tz_convert('UTC')
    return int(ts.timestamp() * 1000)


def sha256_file(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def validate_ohlcv(df):
    """Raise DataError on structural problems (columns, unique/ascending, OHLC sanity).

    Gap detection against an expected window is done by find_gaps().
    """
    if list(df.columns) != COLUMNS:
        raise DataError(f'bad columns: {list(df.columns)}')
    ts = pd.to_datetime(df['timestamp_utc'], utc=True)
    if ts.duplicated().any():
        raise DataError('duplicate timestamps')
    if not ts.is_monotonic_increasing:
        raise DataError('timestamps not ascending')
    if df[COLUMNS[1:]].isna().any().any():
        raise DataError('NaN values in OHLCV')
    bad = (df['high'] < df[['open', 'close']].max(axis=1)) | (df['low'] > df[['open', 'close']].min(axis=1)) \
        | (df['high'] < df['low'])
    if bad.any():
        raise DataError(f'inconsistent OHLC in {int(bad.sum())} rows, first at {df.loc[bad, "timestamp_utc"].iloc[0]}')
    if (df[['open', 'high', 'low', 'close']] <= 0).any().any() or (df['volume'] < 0).any():
        raise DataError('non-positive price or negative volume')


def find_gaps(open_times_ms, step_ms, first_ms, last_ms):
    """Missing-bar runs between first_ms and last_ms inclusive. Each gap: start/end bar open times, missing count."""
    have = set(open_times_ms)
    gaps, run = [], None
    for t in range(first_ms, last_ms + 1, step_ms):
        if t in have:
            if run:
                gaps.append(run)
                run = None
        elif run:
            run['end_utc'] = iso(t)
            run['missing_bars'] += 1
        else:
            run = {'start_utc': iso(t), 'end_utc': iso(t), 'missing_bars': 1}
    if run:
        gaps.append(run)
    return gaps


def fetch_ohlcv(symbol, interval, start_ms, end_ms, now_ms=None):
    """Return (df, gaps). Window is [start_ms, end_ms); start aligned up to a bar boundary.

    Only closed candles are kept. The expected bar set runs from the aligned start to the
    last bar that is both inside the window and already closed, so leading/trailing/
    internal absences all surface as gaps.
    """
    if interval not in INTERVAL_MS:
        raise DataError(f'unsupported interval {interval!r}; choose from {sorted(INTERVAL_MS)}')
    step = INTERVAL_MS[interval]
    if start_ms >= end_ms:
        raise DataError('start must be before end')
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    first = -(-start_ms // step) * step
    last_closed_open = (min(end_ms, now_ms) // step) * step - step
    if last_closed_open < first:
        raise DataError('window contains no closed bars')
    rows, cursor = {}, first
    while cursor <= last_closed_open:
        q = urllib.parse.urlencode({'symbol': symbol, 'interval': interval, 'startTime': cursor,
                                    'endTime': last_closed_open + step - 1, 'limit': PAGE_LIMIT})
        page = fetch_json(f'{ENDPOINT}?{q}')
        if not isinstance(page, list):
            raise DataError(f'unexpected response: {str(page)[:200]}')
        if not page:
            break
        for k in page:
            open_ms, close_ms = int(k[0]), int(k[6])
            if close_ms >= now_ms or open_ms > last_closed_open:
                continue
            rows[open_ms] = (iso(open_ms), float(k[1]), float(k[2]), float(k[3]), float(k[4]), float(k[5]))
        nxt = int(page[-1][0]) + step
        if nxt <= cursor:
            raise DataError('pagination did not advance')
        cursor = nxt
        time.sleep(0.05)
    if not rows:
        raise DataError(f'no candles returned for {symbol} {interval} in window')
    df = pd.DataFrame([rows[t] for t in sorted(rows)], columns=COLUMNS)
    validate_ohlcv(df)
    return df, find_gaps(rows.keys(), step, first, last_closed_open)


def write_verified(df, gaps, out_path, symbol, interval, start_ms, end_ms):
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(out, index=False, lineterminator='\n')
    meta = {
        'source': SOURCE, 'endpoint': ENDPOINT, 'symbol': symbol, 'interval': interval,
        'requested_start_utc': iso(start_ms), 'requested_end_utc': iso(end_ms),
        'fetched_at_utc': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
        'rows': len(df), 'complete': not gaps, 'gaps': gaps, 'sha256': sha256_file(out),
    }
    sidecar = Path(str(out) + '.meta.json')
    sidecar.write_text(json.dumps(meta, indent=2), encoding='utf-8')
    return meta, sidecar


def load_verified(csv_path, allow_incomplete=False):
    """Load a canonical CSV after verifying its sidecar. Returns (df, meta). Raises DataError."""
    csv_path = Path(csv_path)
    sidecar = Path(str(csv_path) + '.meta.json')
    if not csv_path.is_file():
        raise DataError(f'input CSV not found: {csv_path}')
    if not sidecar.is_file():
        raise DataError(f'sidecar missing: {sidecar}')
    try:
        meta = json.loads(sidecar.read_text(encoding='utf-8'))
    except json.JSONDecodeError as exc:
        raise DataError(f'sidecar is not valid JSON: {sidecar}') from exc
    missing = [k for k in META_REQUIRED if k not in meta]
    if missing:
        raise DataError(f'sidecar missing fields: {missing}')
    actual = sha256_file(csv_path)
    if actual != meta['sha256']:
        raise DataError(f'sha256 mismatch: sidecar {meta["sha256"]} != file {actual}')
    if not meta['complete'] and not allow_incomplete:
        raise DataError('input is complete=false (gaps present); pass --allow-incomplete to proceed')
    df = pd.read_csv(csv_path)
    if list(df.columns) != COLUMNS:
        raise DataError(f'bad columns: {list(df.columns)}')
    if len(df) != meta['rows']:
        raise DataError(f'row count {len(df)} != sidecar rows {meta["rows"]}')
    validate_ohlcv(df)
    return df, meta


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument('--symbol', required=True, help='e.g. BTCUSDT')
    p.add_argument('--interval', required=True, choices=sorted(INTERVAL_MS))
    p.add_argument('--start', required=True, help='UTC, e.g. 2026-01-01 or 2026-01-01T00:00:00')
    p.add_argument('--end', required=True, help='UTC, exclusive')
    p.add_argument('--output', required=True, help='CSV path; sidecar written to <output>.meta.json')
    p.add_argument('--allow-gaps', action='store_true', help='write output even if gaps/complete=false')
    args = p.parse_args(argv)
    try:
        start_ms, end_ms = parse_utc(args.start), parse_utc(args.end)
        df, gaps = fetch_ohlcv(args.symbol.upper(), args.interval, start_ms, end_ms)
        if gaps and not args.allow_gaps:
            total = sum(g['missing_bars'] for g in gaps)
            raise DataError(f'{total} missing bars in {len(gaps)} gap(s), first {gaps[0]}; '
                            'nothing written (use --allow-gaps to write complete=false output)')
        meta, sidecar = write_verified(df, gaps, args.output, args.symbol.upper(), args.interval, start_ms, end_ms)
    except DataError as exc:
        print(f'market_data error: {exc}', file=sys.stderr)
        return 1
    print(json.dumps({'path': str(Path(args.output)), 'sidecar': str(sidecar), 'rows': meta['rows'],
                      'first': df['timestamp_utc'].iloc[0], 'last': df['timestamp_utc'].iloc[-1],
                      'complete': meta['complete'], 'gaps': len(gaps), 'sha256': meta['sha256'],
                      'source': SOURCE}, indent=2))
    return 0


if __name__ == '__main__':
    sys.exit(main())
