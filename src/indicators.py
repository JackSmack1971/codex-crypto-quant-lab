#!/usr/bin/env python3
"""Causal technical indicators over a verified canonical OHLCV CSV.

Every function is causal: the value at bar t uses bars <= t only (tested by truncation).
Annualization basis: 365-day year; periods per year = 365 days / bar length.
Input must have a valid sidecar (sha256 verified); the input hash is echoed in the output.
"""
import argparse
import json
import math
import sys

import numpy as np
import pandas as pd

from market_data import INTERVAL_MS, DataError, load_verified

ANNUALIZATION_DAYS = 365
PARAMS = {'rsi': 14, 'atr': 14, 'sma_fast': 20, 'sma_slow': 50, 'ema': 20,
          'vol_window': 30, 'zscore': 20, 'vol_regime_baseline': 90}


def periods_per_year(interval):
    return ANNUALIZATION_DAYS * 86_400_000 / INTERVAL_MS[interval]


def log_returns(close):
    return np.log(close).diff()


def sma(close, n):
    return close.rolling(n).mean()


def ema(close, n):
    return close.ewm(span=n, adjust=False, min_periods=n).mean()


def rsi(close, n):
    """Wilder RSI. Flat window (no gains, no losses) -> NaN."""
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    out = 100 - 100 / (1 + gain / loss)
    out[(loss == 0) & (gain > 0)] = 100.0
    out[(loss == 0) & (gain == 0)] = np.nan
    return out


def atr(df, n):
    """Wilder ATR on true range (first bar's TR is high-low)."""
    prev = df['close'].shift(1)
    tr = pd.concat([df['high'] - df['low'], (df['high'] - prev).abs(), (df['low'] - prev).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()


def realized_vol(returns, n, ppy):
    return returns.rolling(n).std(ddof=1) * math.sqrt(ppy)


def rolling_zscore(close, n):
    return (close - close.rolling(n).mean()) / close.rolling(n).std(ddof=1)


def drawdown(close):
    return close / close.cummax() - 1.0


def vol_regime(vol, baseline_n):
    """'high' if realized vol > its trailing median over baseline_n bars (incl. current), else 'low'; None until defined."""
    med = vol.rolling(baseline_n).median()
    out = pd.Series(np.where(vol > med, 'high', 'low'), index=vol.index, dtype=object)
    out[vol.isna() | med.isna()] = None
    return out


def compute_all(df, interval):
    ppy = periods_per_year(interval)
    p = PARAMS
    close = df['close']
    ret = log_returns(close)
    vol = realized_vol(ret, p['vol_window'], ppy)
    out = pd.DataFrame({'timestamp_utc': df['timestamp_utc'], 'close': close, 'log_return': ret,
                        'realized_vol_ann': vol, f'rsi_{p["rsi"]}': rsi(close, p['rsi']),
                        f'atr_{p["atr"]}': atr(df, p['atr']),
                        f'sma_{p["sma_fast"]}': sma(close, p['sma_fast']),
                        f'sma_{p["sma_slow"]}': sma(close, p['sma_slow']),
                        f'ema_{p["ema"]}': ema(close, p['ema']),
                        f'zscore_{p["zscore"]}': rolling_zscore(close, p['zscore']),
                        'drawdown': drawdown(close), 'vol_regime': vol_regime(vol, p['vol_regime_baseline'])})
    return out


def _clean(v):
    if v is None or (isinstance(v, float) and not math.isfinite(v)):
        return None
    return float(v) if isinstance(v, (np.floating, float)) else v


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--input', required=True, help='canonical CSV from market_data.py (sidecar required)')
    ap.add_argument('--output', help='optional CSV of the full indicator series')
    ap.add_argument('--allow-incomplete', action='store_true', help='accept complete=false (gappy) input')
    args = ap.parse_args(argv)
    try:
        df, meta = load_verified(args.input, allow_incomplete=args.allow_incomplete)
        series = compute_all(df, meta['interval'])
        if args.output:
            series.to_csv(args.output, index=False, lineterminator='\n')
    except (DataError, OSError) as exc:
        print(f'indicators error: {exc}', file=sys.stderr)
        return 1
    latest = {k: _clean(v) for k, v in series.iloc[-1].to_dict().items()}
    print(json.dumps({
        'input': {'path': args.input, 'sha256': meta['sha256'], 'rows': meta['rows'], 'complete': meta['complete'],
                  'symbol': meta['symbol'], 'interval': meta['interval'], 'source': meta['source'],
                  'first': df['timestamp_utc'].iloc[0], 'last': df['timestamp_utc'].iloc[-1]},
        'basis': {'annualization_days': ANNUALIZATION_DAYS, 'periods_per_year': periods_per_year(meta['interval'])},
        'params': PARAMS, 'latest': latest,
        'series_output': args.output,
    }, indent=2))
    return 0


if __name__ == '__main__':
    sys.exit(main())
