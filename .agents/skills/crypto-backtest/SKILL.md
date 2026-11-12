---
name: crypto-backtest
description: Backtest an SMA-cross long/flat strategy against a buy-and-hold benchmark on verified crypto OHLCV, with required fees and slippage and a built-in lookahead check. Use when the user asks how a simple trend rule would have performed historically.
---

# Crypto backtest

Input must come from the `crypto-ohlcv-fetch` skill. Run from the repo root. `--fee-bps` and `--slippage-bps` are **required, no defaults** (ask the user or state your assumption; missing either exits non-zero).

```
python src/backtest.py --input data/btc1h.csv --fee-bps 10 --slippage-bps 5 [--fast 20 --slow 50] [--folds 3] [--allow-incomplete]
```

## Timing convention (exact)
The signal is computed on the **close of bar t-1**; the fill happens at the **open of bar t**; **slippage is applied to that fill** (buy at open*(1+slip), sell at open*(1-slip)); the fee is charged on traded notional; a position is marked to the close of bar t. The fill for bar 0 uses the strategy's initial position (0 for SMA cross, 1 for buy-and-hold).

## Leak check (always runs, cannot be skipped)
For a tested index t, every OHLCV field of every bar with index >= t (bar t included) is perturbed. The target position decided for the fill at bar t's open, and all positions and fills before bar t, must be unchanged (bar t's own fill price is excluded: it is bar t's open). Violation exits with code 3.
**Coverage:** all indices t = 1..n-1 when n <= 3000 bars; otherwise 300 indices sampled with `numpy.random.default_rng(0)` plus t=1 and t=n-1. Three perturbations per index. The output JSON `leak_check` states the mode, seed and indices tested; quote it when reporting.

## Output contract (stdout JSON)
`input` (incl. **sha256**), `costs` (fee_bps, slippage_bps), `basis` (365-day year, periods/year from interval, `risk_free_rate: 0`, Sharpe method, and annualized-return method), `convention`, `strategy`, `leak_check`, `strategy_stats`, `benchmark` (buy-and-hold, same window and same costs), optional `sub_period_stats` and `sub_period_stats_note`.
Stats: total/annualized return, annualized vol, Sharpe, Sortino, max drawdown, Calmar, turnover (equity multiples), fills, exposure.

Sharpe uses the arithmetic mean of per-bar returns divided by their sample standard deviation, annualized by `sqrt(periods_per_year)`. Annualized return uses geometric compounding of per-bar returns over `periods_per_year`.

`--folds N` splits the window into N contiguous **sub-periods** and reports stats for each. These are descriptive slices of one fixed-parameter run, not a held-out evaluation; no parameters are fitted, so do not describe any result as validated on unseen data.
The slices come from one continuous run, not independent runs. Costs are attributed to the sub-period containing each fill; a one-time entry cost appears only in the period containing the entry. Annualized figures for short sub-periods are descriptive only.

## Reporting rules
Always show the benchmark next to the strategy and echo the costs. A backtest is a historical simulation over one window, not a prediction; say so. Not investment advice.

## Failure behavior
Missing costs (exit 2), negative/non-finite costs, tampered or missing sidecar, or `complete=false` input without `--allow-incomplete` exit non-zero with stderr message.

Importable: `from backtest import run_backtest, SmaCross, BuyHold, stats`.
