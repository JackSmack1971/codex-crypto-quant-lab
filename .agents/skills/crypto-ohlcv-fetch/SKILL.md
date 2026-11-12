---
name: crypto-ohlcv-fetch
description: Fetch closed OHLCV candles for a crypto spot pair into a verified CSV with a provenance sidecar. Use before any indicator or backtest work, or when the user asks for historical candles/price data.
---

# Fetch crypto OHLCV

Run from the repo root. Free, keyless; single venue in v1: Binance spot public market-data mirror (`data-api.binance.vision`; `api.binance.com` is geo-blocked from the dev machine). Source is recorded in the sidecar.

```
python src/market_data.py --symbol BTCUSDT --interval 1h --start 2026-09-01 --end 2026-10-31 --output data/btc1h.csv
```

- `--interval`: 1m 5m 15m 30m 1h 2h 4h 1d. `--start`/`--end` are UTC; end is exclusive.
- Add `--allow-gaps` only if the user accepts a gappy dataset; the output is then written with `complete=false`.

## Output contract
- `<output>` CSV: `timestamp_utc` (bar open, ISO8601 UTC), `open, high, low, close, volume`; ascending, unique, closed candles only.
- `<output>.meta.json` sidecar: source, endpoint, symbol, interval, requested window, fetch time, rows, `complete`, `gaps`, `sha256` of the CSV. Never edit the CSV by hand; downstream tools verify the hash.
- Stdout: one JSON object (`path, rows, first, last, complete, gaps, sha256, source`).

## Failure behavior
Any HTTP error, bad symbol, validation failure, or gaps (without `--allow-gaps`) exits non-zero with a message on stderr, stdout empty, and **no files written**. Report the error; do not fabricate or hand-fill data.

Importable: `from market_data import fetch_ohlcv, validate_ohlcv, load_verified`.
