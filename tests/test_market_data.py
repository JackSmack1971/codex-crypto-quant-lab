import json
from urllib.parse import parse_qs, urlparse

import pytest

import market_data as md
from conftest import HOUR_MS, T0_MS, synth_ohlcv, write_fixture


def kline(open_ms, price=100.0):
    return [open_ms, str(price), str(price + 1), str(price - 1), str(price + 0.5), '10',
            open_ms + HOUR_MS - 1, '0', 1, '0', '0', '0']


def make_fetcher(bars, calls=None):
    """Fake fetch_json serving klines from a dict {open_ms: kline} honoring startTime/endTime/limit."""
    def fake(url):
        q = {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}
        if calls is not None:
            calls.append(q)
        lo, hi, lim = int(q['startTime']), int(q['endTime']), int(q['limit'])
        return [bars[t] for t in sorted(bars) if lo <= t <= hi][:lim]
    return fake


# ---- validation -----------------------------------------------------------------

def test_validate_catches_duplicate_unsorted_and_bad_ohlc():
    good = synth_ohlcv(5)
    md.validate_ohlcv(good)
    dup = good.copy(); dup.loc[1, 'timestamp_utc'] = dup.loc[0, 'timestamp_utc']
    with pytest.raises(md.DataError, match='duplicate'):
        md.validate_ohlcv(dup)
    unsorted = good.iloc[[1, 0, 2, 3, 4]].reset_index(drop=True)
    with pytest.raises(md.DataError, match='ascending'):
        md.validate_ohlcv(unsorted)
    bad = good.copy(); bad.loc[2, 'high'] = bad.loc[2, 'low'] - 1
    with pytest.raises(md.DataError, match='inconsistent OHLC'):
        md.validate_ohlcv(bad)


def test_find_gaps_reports_runs():
    have = [T0_MS + i * HOUR_MS for i in (0, 1, 4, 5, 7)]
    gaps = md.find_gaps(have, HOUR_MS, T0_MS, T0_MS + 7 * HOUR_MS)
    assert [g['missing_bars'] for g in gaps] == [2, 1]
    assert gaps[0]['start_utc'] == '2020-01-01T02:00:00Z' and gaps[0]['end_utc'] == '2020-01-01T03:00:00Z'


# ---- fetching -------------------------------------------------------------------

def test_in_progress_candle_is_dropped(monkeypatch):
    bars = {T0_MS + i * HOUR_MS: kline(T0_MS + i * HOUR_MS) for i in range(5)}
    monkeypatch.setattr(md, 'fetch_json', make_fetcher(bars))
    now = T0_MS + 4 * HOUR_MS + 1000  # bar 4 opened but has not closed
    df, gaps = md.fetch_ohlcv('X', '1h', T0_MS, T0_MS + 10 * HOUR_MS, now_ms=now)
    assert len(df) == 4 and df['timestamp_utc'].iloc[-1] == '2020-01-01T03:00:00Z'
    assert gaps == []


def test_missing_internal_bar_surfaces_as_gap(monkeypatch):
    bars = {T0_MS + i * HOUR_MS: kline(T0_MS + i * HOUR_MS) for i in range(6) if i != 3}
    monkeypatch.setattr(md, 'fetch_json', make_fetcher(bars))
    df, gaps = md.fetch_ohlcv('X', '1h', T0_MS, T0_MS + 6 * HOUR_MS, now_ms=T0_MS + 100 * HOUR_MS)
    assert len(df) == 5 and gaps == [{'start_utc': '2020-01-01T03:00:00Z', 'end_utc': '2020-01-01T03:00:00Z', 'missing_bars': 1}]


def test_leading_gap_when_venue_history_starts_late(monkeypatch):
    bars = {T0_MS + i * HOUR_MS: kline(T0_MS + i * HOUR_MS) for i in range(2, 6)}
    monkeypatch.setattr(md, 'fetch_json', make_fetcher(bars))
    _, gaps = md.fetch_ohlcv('X', '1h', T0_MS, T0_MS + 6 * HOUR_MS, now_ms=T0_MS + 100 * HOUR_MS)
    assert gaps[0]['missing_bars'] == 2


def test_pagination_covers_more_than_one_page(monkeypatch):
    n = md.PAGE_LIMIT + 50
    bars = {T0_MS + i * HOUR_MS: kline(T0_MS + i * HOUR_MS) for i in range(n)}
    calls = []
    monkeypatch.setattr(md, 'fetch_json', make_fetcher(bars, calls))
    monkeypatch.setattr(md.time, 'sleep', lambda s: None)
    df, gaps = md.fetch_ohlcv('X', '1h', T0_MS, T0_MS + n * HOUR_MS, now_ms=T0_MS + (n + 5) * HOUR_MS)
    assert len(df) == n and gaps == [] and len(calls) >= 2


# ---- CLI ------------------------------------------------------------------------

ARGS = ['--symbol', 'xusdt', '--interval', '1h', '--start', '2020-01-01', '--end', '2020-01-01T06:00:00']


def test_cli_writes_csv_and_complete_sidecar(monkeypatch, tmp_path, capsys):
    bars = {T0_MS + i * HOUR_MS: kline(T0_MS + i * HOUR_MS) for i in range(6)}
    monkeypatch.setattr(md, 'fetch_json', make_fetcher(bars))
    out = tmp_path / 'x.csv'
    assert md.main(ARGS + ['--output', str(out)]) == 0
    meta = json.loads((tmp_path / 'x.csv.meta.json').read_text())
    for k in md.META_REQUIRED:
        assert k in meta
    assert meta['symbol'] == 'XUSDT' and meta['rows'] == 6 and meta['complete'] is True
    assert meta['sha256'] == md.sha256_file(out)
    assert json.loads(capsys.readouterr().out)['complete'] is True


def test_cli_gaps_fail_without_allow_gaps_and_write_nothing(monkeypatch, tmp_path, capsys):
    bars = {T0_MS + i * HOUR_MS: kline(T0_MS + i * HOUR_MS) for i in range(6) if i != 2}
    monkeypatch.setattr(md, 'fetch_json', make_fetcher(bars))
    out = tmp_path / 'g.csv'
    assert md.main(ARGS + ['--output', str(out)]) == 1
    assert not out.exists() and not (tmp_path / 'g.csv.meta.json').exists()
    captured = capsys.readouterr()
    assert captured.out == '' and 'missing bars' in captured.err
    assert md.main(ARGS + ['--output', str(out), '--allow-gaps']) == 0
    assert json.loads((tmp_path / 'g.csv.meta.json').read_text())['complete'] is False


def test_cli_http_failure_exits_nonzero(monkeypatch, tmp_path, capsys):
    def boom(url):
        raise md.DataError('HTTP 400: Invalid symbol')
    monkeypatch.setattr(md, 'fetch_json', boom)
    assert md.main(ARGS + ['--output', str(tmp_path / 'b.csv')]) == 1
    assert 'Invalid symbol' in capsys.readouterr().err and not (tmp_path / 'b.csv').exists()


# ---- load_verified --------------------------------------------------------------

def test_load_verified_roundtrip_and_tamper_detection(tmp_path):
    p = tmp_path / 'd.csv'
    write_fixture(synth_ohlcv(10), p)
    df, meta = md.load_verified(p)
    assert len(df) == 10 and meta['complete']
    p.write_text(p.read_text().replace('2020-01-01T00:00:00Z', '2020-01-01T00:00:01Z', 1))
    with pytest.raises(md.DataError, match='sha256 mismatch'):
        md.load_verified(p)


def test_load_verified_requires_sidecar(tmp_path):
    p = tmp_path / 'n.csv'
    write_fixture(synth_ohlcv(10), p)
    (tmp_path / 'n.csv.meta.json').unlink()
    with pytest.raises(md.DataError, match='sidecar missing'):
        md.load_verified(p)


def test_load_verified_refuses_incomplete_unless_allowed(tmp_path):
    p = tmp_path / 'i.csv'
    write_fixture(synth_ohlcv(10), p, gaps=[{'start_utc': 'a', 'end_utc': 'b', 'missing_bars': 1}])
    with pytest.raises(md.DataError, match='complete=false'):
        md.load_verified(p)
    df, meta = md.load_verified(p, allow_incomplete=True)
    assert meta['complete'] is False
