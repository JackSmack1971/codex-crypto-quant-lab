#!/usr/bin/env python3
"""Long/flat bar-based backtester with explicit timing, required costs and a leak check.

TIMING CONVENTION (identical in SKILL.md and tests):
  * The signal is computed on the CLOSE of bar t-1.
  * The resulting target position is filled at the OPEN of bar t.
  * Slippage is applied to that fill (buy at open*(1+slip), sell at open*(1-slip));
    the fee is charged on the traded notional of each fill.
  * A position filled at the open of bar t is marked to the close of bar t.
  * The target for the fill at bar 0's open is the strategy's `initial_position`
    (no earlier bar exists): 0 for SMA cross, 1 for the buy-and-hold benchmark.

LEAK CHECK (see leak_check): for a tested index t, every OHLCV field of every bar with
index >= t (bar t included) is perturbed. The target position decided for the fill at
bar t's open, and all positions and fills before bar t, must be unchanged. Bar t's own
fill price is excluded because it is bar t's open, which was perturbed.
Coverage: ALL indices t in 1..n-1 when n <= LEAK_ALL_MAX_BARS, otherwise a sample of
LEAK_SAMPLE_SIZE indices drawn with numpy default_rng(LEAK_SEED) plus t=1 and t=n-1.
Each tested t uses 3 perturbations (prices x0.5 / volume x3; prices x2 / volume x0.2;
independent seeded lognormal noise on every field). The check cannot be skipped.

Annualization: 365-day year, periods/year from the bar interval. Risk-free rate: 0.
Results describe a historical simulation, not a forecast.
"""
import argparse
import json
import math
import sys

import numpy as np
import pandas as pd

from indicators import ANNUALIZATION_DAYS, periods_per_year, sma
from market_data import DataError, load_verified

CONVENTION = ('signal on close of bar t-1; fill at open of bar t; slippage applied to the fill; '
              'fee on traded notional; position marked to close of bar t')
RISK_FREE_RATE = 0.0
LEAK_ALL_MAX_BARS = 3000
LEAK_SAMPLE_SIZE = 300
LEAK_SEED = 0
FIELDS = ['open', 'high', 'low', 'close', 'volume']


class LeakError(Exception):
    """The strategy's decisions depend on bars they must not see."""


class SmaCross:
    name = 'sma_cross'
    initial_position = 0.0

    def __init__(self, fast=20, slow=50):
        if not 1 <= fast < slow:
            raise DataError('require 1 <= fast < slow')
        self.fast, self.slow = fast, slow
        self.params = {'fast': fast, 'slow': slow}

    def signal(self, df):
        """Value at index i uses bars <= i only: 1 if SMA(fast) > SMA(slow), else 0."""
        f, s = sma(df['close'], self.fast), sma(df['close'], self.slow)
        return ((f > s) & f.notna() & s.notna()).astype(float).to_numpy()


class BuyHold:
    name = 'buy_and_hold'
    initial_position = 1.0
    params = {}

    def signal(self, df):
        return np.ones(len(df))


def decide_targets(df, strategy):
    """targets[t] = target position for the fill at the open of bar t."""
    sig = np.nan_to_num(np.asarray(strategy.signal(df), dtype=float), nan=0.0)
    if len(sig) != len(df) or not np.isin(sig, (0.0, 1.0)).all():
        raise DataError('strategy signal must be 0/1 with one value per bar')
    targets = np.empty(len(df))
    targets[0] = strategy.initial_position
    targets[1:] = sig[:-1]
    return targets


def simulate(opens, closes, targets, fee, slip, n):
    """Run bars 0..n-1. Returns fills [(bar, side, price, units, notional_frac_of_equity)], pos, equity."""
    cash, units = 1.0, 0.0
    fills, pos, equity = [], np.zeros(n), np.zeros(n)
    for t in range(n):
        want, held = targets[t], units > 0
        if want == 1.0 and not held:
            price = opens[t] * (1 + slip)
            units = cash / (price * (1 + fee))
            fills.append((t, 'buy', price, units, units * price / cash))
            cash = 0.0
        elif want == 0.0 and held:
            price = opens[t] * (1 - slip)
            before = units * opens[t]
            fills.append((t, 'sell', price, units, units * price / before))
            cash, units = units * price * (1 - fee), 0.0
        pos[t] = 1.0 if units > 0 else 0.0
        equity[t] = cash + units * closes[t]
    return fills, pos, equity


def _run(df, strategy, fee_bps, slip_bps):
    fee, slip = fee_bps / 1e4, slip_bps / 1e4
    targets = decide_targets(df, strategy)
    n = len(df)
    fills, pos, equity = simulate(df['open'].to_numpy(), df['close'].to_numpy(), targets, fee, slip, n)
    return {'targets': targets, 'fills': fills, 'pos': pos, 'equity': equity}


def _perturbations(df, t, rng):
    """Three perturbed copies of df where every OHLCV field of every bar >= t is changed."""
    df = df.astype({c: float for c in FIELDS})
    out = []
    for price_f, vol_f in ((0.5, 3.0), (2.0, 0.2)):
        d = df.copy()
        for c in FIELDS:
            d.loc[d.index[t:], c] = df[c].iloc[t:] * (vol_f if c == 'volume' else price_f)
        out.append(d)
    d = df.copy()
    for c in FIELDS:
        d.loc[d.index[t:], c] = df[c].iloc[t:].to_numpy() * rng.lognormal(0.0, 0.5, len(df) - t)
    out.append(d)
    return out


def leak_indices(n):
    if n <= LEAK_ALL_MAX_BARS:
        return {'mode': 'all', 'seed': None, 'indices': list(range(1, n))}
    rng = np.random.default_rng(LEAK_SEED)
    idx = sorted(set(rng.choice(np.arange(1, n), LEAK_SAMPLE_SIZE, replace=False).tolist()) | {1, n - 1})
    return {'mode': 'sample', 'seed': LEAK_SEED, 'indices': idx}


def leak_check(df, strategy, base, fee_bps, slip_bps, indices=None):
    """Raise LeakError if any tested t violates the convention. Returns coverage info."""
    fee, slip = fee_bps / 1e4, slip_bps / 1e4
    cov = leak_indices(len(df)) if indices is None else {'mode': 'explicit', 'seed': None, 'indices': list(indices)}
    rng = np.random.default_rng(LEAK_SEED)
    for t in cov['indices']:
        for d2 in _perturbations(df, t, rng):
            t2 = decide_targets(d2, strategy)
            if not np.array_equal(t2[:t + 1], base['targets'][:t + 1]):
                raise LeakError(f'lookahead: target for the fill at bar {t} changed when bars >= {t} were perturbed')
            f2, p2, _ = simulate(d2['open'].to_numpy(), d2['close'].to_numpy(), t2, fee, slip, t)
            f1 = [f for f in base['fills'] if f[0] < t]
            if f1 != f2 or not np.array_equal(p2, base['pos'][:t]):
                raise LeakError(f'lookahead: positions/fills before bar {t} changed when bars >= {t} were perturbed')
    return {'mode': cov['mode'], 'seed': cov['seed'], 'tested_indices': len(cov['indices']),
            'indices': cov['indices'] if cov['mode'] == 'sample' else '1..n-1',
            'perturbations_per_index': 3, 'perturbed_fields': FIELDS, 'result': 'passed'}


def run_backtest(df, strategy, fee_bps, slippage_bps):
    """Run strategy; leak check runs first-class and raises LeakError on violation."""
    for name, v in (('fee_bps', fee_bps), ('slippage_bps', slippage_bps)):
        if not (math.isfinite(v) and 0 <= v < 10_000):
            raise DataError(f'{name} must be finite and in [0, 10000)')
    if len(df) < 3:
        raise DataError('need at least 3 bars')
    base = _run(df, strategy, fee_bps, slippage_bps)
    base['leak_check'] = leak_check(df, strategy, base, fee_bps, slippage_bps)
    return base


def _num(x):
    return None if x is None or not math.isfinite(x) else float(x)


def stats(rets, pos, fills, ppy):
    """Stats from per-bar equity returns, position held per bar, and fills inside the range."""
    n = len(rets)
    eq = np.cumprod(1 + rets)
    total = eq[-1] - 1
    ann = eq[-1] ** (ppy / n) - 1 if eq[-1] > 0 else -1.0
    sd = rets.std(ddof=1) if n > 1 else float('nan')
    dd = np.sqrt(np.mean(np.minimum(rets, 0) ** 2))
    peak = np.maximum.accumulate(np.concatenate([[1.0], eq]))[1:]
    mdd = float((eq / peak - 1).min())
    sharpe = rets.mean() / sd * math.sqrt(ppy) if sd and sd > 0 else None
    sortino = rets.mean() / dd * math.sqrt(ppy) if dd > 0 else None
    return {'bars': n, 'total_return': _num(total), 'annualized_return': _num(ann),
            'annualized_vol': _num(sd * math.sqrt(ppy)), 'sharpe': _num(sharpe) if sharpe is not None else None,
            'sortino': _num(sortino) if sortino is not None else None, 'max_drawdown': _num(mdd),
            'calmar': _num(ann / abs(mdd)) if mdd < 0 else None,
            'turnover_equity_multiples': _num(sum(f[4] for f in fills)), 'fills': len(fills),
            'exposure': _num(pos.mean())}


def _bar_returns(equity):
    return equity / np.concatenate([[1.0], equity[:-1]]) - 1


def sub_period_stats(df, res, bench, folds, ppy):
    out = []
    for i, idx in enumerate(np.array_split(np.arange(len(df)), folds), 1):
        lo, hi = idx[0], idx[-1]
        row = {'sub_period': i, 'start_utc': df['timestamp_utc'].iloc[lo], 'end_utc': df['timestamp_utc'].iloc[hi]}
        for key, r in (('strategy', res), ('benchmark', bench)):
            rets = _bar_returns(r['equity'])[lo:hi + 1]
            fl = [f for f in r['fills'] if lo <= f[0] <= hi]
            row[key] = stats(rets, r['pos'][lo:hi + 1], fl, ppy)
        out.append(row)
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description='SMA-cross backtest vs buy-and-hold on a verified OHLCV CSV.')
    ap.add_argument('--input', required=True, help='canonical CSV from market_data.py (sidecar required)')
    ap.add_argument('--fee-bps', type=float, required=True, help='fee per fill, basis points of notional (no default)')
    ap.add_argument('--slippage-bps', type=float, required=True, help='slippage per fill, basis points (no default)')
    ap.add_argument('--fast', type=int, default=20)
    ap.add_argument('--slow', type=int, default=50)
    ap.add_argument('--folds', type=int, default=0, help='also report stats over N contiguous sub-periods')
    ap.add_argument('--allow-incomplete', action='store_true', help='accept complete=false (gappy) input')
    args = ap.parse_args(argv)
    try:
        df, meta = load_verified(args.input, allow_incomplete=args.allow_incomplete)
        strat = SmaCross(args.fast, args.slow)
        if len(df) <= args.slow + 1:
            raise DataError(f'need more than slow+1={args.slow + 1} bars, have {len(df)}')
        if args.folds and (args.folds < 1 or len(df) // args.folds < 2):
            raise DataError('--folds must be >=1 and leave at least 2 bars per sub-period')
        ppy = periods_per_year(meta['interval'])
        res = run_backtest(df, strat, args.fee_bps, args.slippage_bps)
        bench = _run(df, BuyHold(), args.fee_bps, args.slippage_bps)
        out = {
            'input': {'path': args.input, 'sha256': meta['sha256'], 'rows': meta['rows'], 'complete': meta['complete'],
                      'symbol': meta['symbol'], 'interval': meta['interval'], 'source': meta['source'],
                      'first': df['timestamp_utc'].iloc[0], 'last': df['timestamp_utc'].iloc[-1]},
            'costs': {'fee_bps': args.fee_bps, 'slippage_bps': args.slippage_bps},
            'basis': {'annualization_days': ANNUALIZATION_DAYS, 'periods_per_year': ppy,
                      'risk_free_rate': RISK_FREE_RATE,
                      'sharpe_method': 'arithmetic mean of per-bar returns divided by sample standard deviation; annualized by sqrt(periods_per_year)',
                      'annualized_return_method': 'geometric compounding of per-bar returns, annualized over periods_per_year'},
            'convention': CONVENTION,
            'strategy': {'name': strat.name, 'params': strat.params},
            'leak_check': res['leak_check'],
            'strategy_stats': stats(_bar_returns(res['equity']), res['pos'], res['fills'], ppy),
            'benchmark': {'name': 'buy_and_hold', 'same_window_and_costs': True,
                          'stats': stats(_bar_returns(bench['equity']), bench['pos'], bench['fills'], ppy)},
            'note': 'Historical simulation over the stated window; not a prediction.',
        }
        if args.folds:
            out['sub_period_stats'] = sub_period_stats(df, res, bench, args.folds, ppy)
            out['sub_period_stats_note'] = (
                'Descriptive slices of one continuous run with fixed parameters. Fill costs are attributed to the period containing each fill; '
                'an entry cost appears only in the period containing the entry. Annualized figures for short sub-periods are descriptive only.')
    except LeakError as exc:
        print(f'backtest LEAK CHECK FAILED: {exc}', file=sys.stderr)
        return 3
    except (DataError, OSError) as exc:
        print(f'backtest error: {exc}', file=sys.stderr)
        return 1
    print(json.dumps(out, indent=2))
    return 0


if __name__ == '__main__':
    sys.exit(main())
