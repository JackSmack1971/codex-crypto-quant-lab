import json
import subprocess
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

import backtest as bt
from conftest import synth_ohlcv, write_fixture

SRC = Path(__file__).resolve().parent.parent / 'src'


class OneBarLookahead:
    """Exactly a one-bar lookahead: signal at close of bar i looks at close[i+1]."""
    name = 'one_bar_lookahead'
    initial_position = 0.0
    params = {}

    def signal(self, df):
        return (df['close'].shift(-1) > df['close']).astype(float).to_numpy()


# ---- convention pinned by hand ----------------------------------------------------

def test_timing_convention_hand_computed():
    # SMA(1) vs SMA(2) == "close[i] > close[i-1]". signal on close: bar1=1, bar2=0, bar3=0, bar4=1.
    # => buy fills at OPEN of bar 2, sell at OPEN of bar 3, buy at OPEN of bar 5 (never at bar 1 or on a close).
    o = [100, 100, 104, 102, 99, 101]
    c = [100, 102, 101, 100, 100.5, 103]
    df = pd.DataFrame({'timestamp_utc': [f'2020-01-01T0{i}:00:00Z' for i in range(6)], 'open': o,
                       'high': [max(a, b) + 1 for a, b in zip(o, c)], 'low': [min(a, b) - 1 for a, b in zip(o, c)],
                       'close': c, 'volume': [1.0] * 6})
    fee_bps, slip_bps = 10.0, 50.0
    res = bt.run_backtest(df, bt.SmaCross(1, 2), fee_bps, slip_bps)
    fills = res['fills']
    assert [(f[0], f[1]) for f in fills] == [(2, 'buy'), (3, 'sell'), (5, 'buy')]

    buy_px = 104 * 1.005                       # open[2] * (1 + 50bps)
    units = 1.0 / (buy_px * 1.001)             # all cash, 10bps fee on notional
    sell_px = 102 * 0.995                      # open[3] * (1 - 50bps)
    cash = units * sell_px * 0.999
    buy2_px = 101 * 1.005                      # open[5] * (1 + 50bps)
    units2 = cash / (buy2_px * 1.001)
    assert fills[0][2] == pytest.approx(buy_px, rel=1e-12) and fills[0][3] == pytest.approx(units, rel=1e-12)
    assert fills[1][2] == pytest.approx(sell_px, rel=1e-12)
    assert fills[2][2] == pytest.approx(buy2_px, rel=1e-12) and fills[2][3] == pytest.approx(units2, rel=1e-12)
    eq = res['equity']
    assert eq[1] == 1.0                                         # still flat after bar 1's signal
    assert eq[2] == pytest.approx(units * 101, rel=1e-12)       # marked to close of bar 2
    assert eq[3] == pytest.approx(cash, rel=1e-12)              # flat at close of bar 3
    assert eq[5] == pytest.approx(units2 * 103, rel=1e-12)


# ---- leak check ----------------------------------------------------------------------

def test_one_bar_lookahead_is_detected_and_clean_sma_passes_at_same_indices():
    df = synth_ohlcv(200, seed=3)
    idx = list(range(1, len(df)))
    for strat, leaky in ((OneBarLookahead(), True), (bt.SmaCross(5, 20), False)):
        base = bt._run(df, strat, 5.0, 5.0)
        if leaky:
            with pytest.raises(bt.LeakError, match='lookahead'):
                bt.leak_check(df, strat, base, 5.0, 5.0, indices=idx)
        else:
            cov = bt.leak_check(df, strat, base, 5.0, 5.0, indices=idx)
            assert cov['tested_indices'] == len(idx) and cov['result'] == 'passed'


def test_run_backtest_raises_on_lookahead_strategy():
    with pytest.raises(bt.LeakError):
        bt.run_backtest(synth_ohlcv(120, seed=5), OneBarLookahead(), 1.0, 1.0)


def test_leak_is_real_when_guard_is_off(monkeypatch):
    """With the guard disabled the lookahead strategy runs and prints money, so the guard matters."""
    df = synth_ohlcv(300, seed=2)
    monkeypatch.setattr(bt, 'leak_check', lambda *a, **k: {'result': 'disabled-in-test'})
    leaky = bt.run_backtest(df, OneBarLookahead(), 0.0, 0.0)
    clean = bt.run_backtest(df, bt.SmaCross(5, 20), 0.0, 0.0)
    assert leaky['equity'][-1] > 3.0 and leaky['equity'][-1] > 3 * clean['equity'][-1]


def test_leak_coverage_rules():
    assert bt.leak_indices(100)['mode'] == 'all' and bt.leak_indices(100)['indices'] == list(range(1, 100))
    big = bt.leak_indices(bt.LEAK_ALL_MAX_BARS + 1000)
    assert big['mode'] == 'sample' and big['seed'] == bt.LEAK_SEED and 1 in big['indices']
    assert big == bt.leak_indices(bt.LEAK_ALL_MAX_BARS + 1000)  # reproducible


# ---- CLI contract -----------------------------------------------------------------

@pytest.fixture
def csv_path(tmp_path):
    p = tmp_path / 'd.csv'
    write_fixture(synth_ohlcv(150, seed=4), p)
    return p


def run_cli(*args):
    return subprocess.run([sys.executable, str(SRC / 'backtest.py'), *args], capture_output=True, text=True)


def test_costs_are_required_no_defaults(csv_path):
    for args in (['--fee-bps', '5'], ['--slippage-bps', '5'], []):
        r = run_cli('--input', str(csv_path), *args)
        assert r.returncode != 0 and r.stdout == ''


def test_output_echoes_costs_basis_benchmark_and_hash(csv_path, capsys):
    assert bt.main(['--input', str(csv_path), '--fee-bps', '10', '--slippage-bps', '5']) == 0
    out = json.loads(capsys.readouterr().out)
    meta = json.loads(Path(str(csv_path) + '.meta.json').read_text())
    assert out['costs'] == {'fee_bps': 10.0, 'slippage_bps': 5.0}
    assert out['basis'] == {
        'annualization_days': 365,
        'periods_per_year': 8760.0,
        'risk_free_rate': 0.0,
        'sharpe_method': 'arithmetic mean of per-bar returns divided by sample standard deviation; annualized by sqrt(periods_per_year)',
        'annualized_return_method': 'geometric compounding of per-bar returns, annualized over periods_per_year',
    }
    assert out['input']['sha256'] == meta['sha256']
    assert out['benchmark']['name'] == 'buy_and_hold' and out['benchmark']['same_window_and_costs'] is True
    assert out['benchmark']['stats']['bars'] == out['strategy_stats']['bars'] == 150
    assert out['convention'] == bt.CONVENTION and out['leak_check']['result'] == 'passed'


def test_folds_split_into_contiguous_sub_periods_without_oos_wording(csv_path, capsys):
    assert bt.main(['--input', str(csv_path), '--fee-bps', '10', '--slippage-bps', '5', '--folds', '3']) == 0
    text = capsys.readouterr().out
    out = json.loads(text)
    subs = out['sub_period_stats']
    assert len(subs) == 3 and sum(s['strategy']['bars'] for s in subs) == 150
    assert all('benchmark' in s for s in subs)
    assert 'one continuous run' in out['sub_period_stats_note']
    assert 'entry cost appears only in the period containing the entry' in out['sub_period_stats_note']
    assert 'Annualized figures for short sub-periods are descriptive only' in out['sub_period_stats_note']
    low = text.lower()
    assert 'out-of-sample' not in low and 'out_of_sample' not in low and 'walk' not in low


def test_no_oos_wording_in_sources():
    for f in (SRC / 'backtest.py', SRC / 'indicators.py', SRC / 'market_data.py'):
        low = f.read_text(encoding='utf-8').lower()
        assert 'out-of-sample' not in low and 'out of sample' not in low and 'walk-forward' not in low, f


def test_tampered_or_incomplete_input_fails(csv_path, tmp_path):
    p2 = tmp_path / 'inc.csv'
    write_fixture(synth_ohlcv(150, seed=4), p2, gaps=[{'start_utc': 'a', 'end_utc': 'b', 'missing_bars': 1}])
    assert run_cli('--input', str(p2), '--fee-bps', '1', '--slippage-bps', '1').returncode == 1
    assert run_cli('--input', str(p2), '--fee-bps', '1', '--slippage-bps', '1', '--allow-incomplete').returncode == 0
    csv_path.write_text(csv_path.read_text() + '\n')
    assert run_cli('--input', str(csv_path), '--fee-bps', '1', '--slippage-bps', '1').returncode == 1


def test_negative_cost_rejected(csv_path):
    assert run_cli('--input', str(csv_path), '--fee-bps', '-1', '--slippage-bps', '1').returncode == 1


def test_stats_known_values():
    rets = np.array([0.1, -0.1])
    s = bt.stats(rets, np.array([1.0, 1.0]), [], 2)
    assert s['total_return'] == pytest.approx(1.1 * 0.9 - 1)
    assert s['max_drawdown'] == pytest.approx(-0.1)
    assert s['sharpe'] == pytest.approx(0.0, abs=1e-12) and s['exposure'] == 1.0
