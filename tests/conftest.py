import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'src'))

from market_data import COLUMNS, write_verified  # noqa: E402

HOUR_MS = 3_600_000
T0_MS = 1_577_836_800_000  # 2020-01-01T00:00:00Z


def synth_ohlcv(n=300, seed=1, vol=0.02):
    """Random-walk bars with open[t] == close[t-1], hourly, deterministic."""
    rng = np.random.default_rng(seed)
    close = 100 * np.exp(np.cumsum(rng.normal(0, vol, n)))
    open_ = np.concatenate([[100.0], close[:-1]])
    high = np.maximum(open_, close) * 1.001
    low = np.minimum(open_, close) * 0.999
    ts = pd.to_datetime(T0_MS + np.arange(n) * HOUR_MS, unit='ms', utc=True).strftime('%Y-%m-%dT%H:%M:%SZ')
    df = pd.DataFrame({'timestamp_utc': ts, 'open': open_, 'high': high, 'low': low, 'close': close,
                       'volume': rng.uniform(1, 10, n)})
    assert list(df.columns) == COLUMNS
    return df


def write_fixture(df, path, gaps=(), interval='1h'):
    """Write df as canonical CSV + sidecar the same way market_data does."""
    end_ms = T0_MS + len(df) * HOUR_MS
    meta, _ = write_verified(df, list(gaps), path, 'TESTUSDT', interval, T0_MS, end_ms)
    return meta
