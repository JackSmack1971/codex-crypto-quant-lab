---
name: crypto-indicators
description: Compute causal technical indicators (returns, realized vol, RSI, ATR, SMA/EMA, z-score, drawdown, vol regime) from a verified OHLCV CSV. Use when the user asks for current indicator readings or a regime/volatility snapshot of a crypto pair.
---

# Crypto indicators

Input must come from the `crypto-ohlcv-fetch` skill (CSV plus `.meta.json` sidecar). Run from the repo root:

```
python src/indicators.py --input data/btc1h.csv [--output data/btc1h_ind.csv] [--allow-incomplete]
```

## Output contract (stdout JSON)
`input` (path, **sha256**, rows, complete, symbol, interval, source, first, last), `basis` (365-day year; `periods_per_year` derived from the interval), `params` (windows used), `latest` (values at the last bar; `null` where undefined). `--output` also writes the full series CSV.

Indicators: log return, annualized realized vol (30 bars), RSI 14 (Wilder), ATR 14 (Wilder), SMA 20/50, EMA 20, z-score 20, drawdown from running peak, vol regime (`high` if realized vol exceeds its trailing 90-bar median).

All indicators are causal: the value at bar t uses bars up to t only.

## Failure behavior
Missing sidecar, sha256 mismatch, or `complete=false` input exits non-zero (stderr message, stdout empty). `--allow-incomplete` accepts gappy input; state that in your report. Indicator values describe the past; they are not predictions.

Importable: `from indicators import compute_all, rsi, atr, sma, ema`.
