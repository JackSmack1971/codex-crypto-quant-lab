import json

import numpy as np
import pandas as pd

import indicators as ind
from conftest import synth_ohlcv, write_fixture


def test_rsi_hand_computed():
    # n=2 Wilder (alpha=.5): gains [-,1,1,0], losses [-,0,0,1] -> avg gain/loss at idx3 = .5/.5 -> RSI 50
    out = ind.rsi(pd.Series([1.0, 2.0, 3.0, 2.0]), 2)
    assert np.isnan(out.iloc[0]) and np.isnan(out.iloc[1])
    assert out.iloc[2] == 100.0  # no losses yet
    assert abs(out.iloc[3] - 50.0) < 1e-12


def test_atr_hand_computed():
    df = pd.DataFrame({'high': [10.0, 11.0, 12.0], 'low': [8.0, 9.0, 9.0], 'close': [9.0, 10.0, 11.0]})
    # TR = [2, max(2,2,0)=2, max(3,2,1)=3]; Wilder n=2 -> [nan, 2, 2.5]
    out = ind.atr(df, 2)
    assert np.isnan(out.iloc[0]) and out.iloc[1] == 2.0 and abs(out.iloc[2] - 2.5) < 1e-12


def test_sma_and_log_returns_known_values():
    c = pd.Series([1.0, 2.0, 4.0])
    assert ind.sma(c, 2).tolist()[1:] == [1.5, 3.0]
    assert abs(ind.log_returns(c).iloc[2] - np.log(2)) < 1e-15


def test_periods_per_year_basis():
    assert ind.periods_per_year('1h') == 8760 and ind.periods_per_year('1d') == 365


def test_indicators_are_causal_by_truncation():
    df = synth_ohlcv(400, seed=7)
    full = ind.compute_all(df, '1h')
    for t in (60, 120, 250, 399):
        part = ind.compute_all(df.iloc[:t + 1].reset_index(drop=True), '1h')
        pd.testing.assert_series_equal(part.iloc[-1], full.iloc[t], check_names=False, rtol=1e-9, atol=1e-12)


def test_cli_echoes_hash_and_basis(tmp_path, capsys):
    p = tmp_path / 'd.csv'
    meta = write_fixture(synth_ohlcv(200), p)
    assert ind.main(['--input', str(p), '--output', str(tmp_path / 's.csv')]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out['input']['sha256'] == meta['sha256']
    assert out['basis'] == {'annualization_days': 365, 'periods_per_year': 8760.0}
    assert (tmp_path / 's.csv').exists()


def test_cli_rejects_tampered_and_incomplete_input(tmp_path, capsys):
    p = tmp_path / 'd.csv'
    write_fixture(synth_ohlcv(100), p, gaps=[{'start_utc': 'a', 'end_utc': 'b', 'missing_bars': 1}])
    assert ind.main(['--input', str(p)]) == 1
    assert ind.main(['--input', str(p), '--allow-incomplete']) == 0
    p.write_text(p.read_text() + '\n')
    assert ind.main(['--input', str(p), '--allow-incomplete']) == 1
