"""Source-linked, static, accessible chart construction from archived observations (P2-B).

Every plotted sample is an archived observation: a chart specification names observation ids and a
transformation; values, times, units and identities are read from the validated observation summaries, never
from the specification. Output is plain SVG plus an exact numerical table; there is no script, remote asset,
interpolation or inferred sample. ``scale_points`` is shared with ``scripts/research_explorer.py``.
"""
from __future__ import annotations

import html
from datetime import datetime
from decimal import Decimal, InvalidOperation
from statistics import median
from typing import Any

from .core import ContractError, _utc, digest, instant_key

CHART_SCHEMA = 'research-chart-spec.v1'
CHART_RENDERER_VERSION = '1'
CHART_TYPES = {'metric_history': 'identity', 'normalized_comparison': 'index_100', 'drawdown': 'drawdown_pct'}
TRANSFORMS = {
    'identity': ('1', 'plotted = recorded value'),
    'index_100': ('1', 'plotted = 100 * value / first_value (series normalised to its own first sample; units removed)'),
    'drawdown_pct': ('1', 'plotted = 100 * (value / running_maximum - 1) over the recorded samples only'),
}
MARKERS = ('circle', 'square', 'diamond', 'triangle')
DASHES = ('', '8 4', '2 3', '10 3 2 3')
MAX_SERIES = 4
MISSING_DATA_POLICY = ('PRESERVE_GAPS: no sample is interpolated or inferred; a line segment is drawn only between consecutive '
                       'recorded samples closer than the gap threshold, and markers show every recorded sample')


class ChartError(ContractError):
    """Chart-construction failure with a stable machine code (never classified by message text)."""

    def __init__(self, code: str, message: str = ''):
        super().__init__(f'{code}: {message}' if message else code)
        self.code, self.message = code, message or code


def scale_points(values: list[float], width: int = 800, left: int = 35, span_x: int = 730, top: int = 30,
                 height: int = 145) -> str:
    """Evenly spaced polyline points (the legacy explorer layout)."""
    low, high = min(values), max(values)
    span = high - low or 1
    return ' '.join(f'{left + i * span_x / (len(values) - 1):.2f},{top + height - (v - low) / span * height:.2f}'
                    for i, v in enumerate(values))


def venue_of(instrument_id: str) -> str:
    """Venue is the leading ``venue:`` segment of the instrument id (project convention); otherwise UNKNOWN."""
    head, sep, _ = instrument_id.partition(':')
    return head if sep and head else 'UNKNOWN'


def _dec(text: Any) -> Decimal:
    try:
        value = Decimal(str(text))
    except (InvalidOperation, ValueError) as exc:
        raise ChartError('CHART_NON_FINITE', 'recorded value is not a decimal number') from exc
    if not value.is_finite():
        raise ChartError('CHART_NON_FINITE', 'recorded value is not finite')
    return value


def _fmt(value: Decimal) -> str:
    return format(value.normalize(), 'f') if value != 0 else '0'


def _series_samples(series: dict, observations: dict[str, dict], eligible: set[str], as_of: str) -> list[dict]:
    ids = series.get('observation_ids')
    if not isinstance(ids, list) or not ids or not all(isinstance(i, str) for i in ids) or len(set(ids)) != len(ids):
        raise ChartError('CHART_SPEC_INVALID', 'each series needs unique observation_ids')
    rows = []
    for oid in ids:
        if oid not in observations:
            raise ChartError('CHART_SAMPLE_UNRESOLVED', f'observation {oid!r} is not in the mission evidence')
        if oid not in eligible:
            raise ChartError('CHART_SAMPLE_INELIGIBLE', f'observation {oid!r} was excluded by reconciliation')
        o = observations[oid]
        if _utc(o['observed_at_utc']) > _utc(as_of):
            raise ChartError('CHART_SAMPLE_INELIGIBLE', f'observation {oid!r} is dated after the research as-of')
        rows.append({'observation_id': oid, 'observed_at_utc': o['observed_at_utc'], 'raw': _dec(o['value']),
                     'instrument_id': o['instrument_id'], 'metric': o['metric'], 'unit': o['unit'], 'source_uri': o['source_uri']})
    if len({(r['instrument_id'], r['metric']) for r in rows}) != 1:
        raise ChartError('CHART_IDENTITY_MISMATCH', 'a series mixes instruments, venues or metric definitions')
    if len({r['unit'] for r in rows}) != 1:
        raise ChartError('CHART_UNIT_MISMATCH', 'a series mixes units')
    rows.sort(key=lambda r: _utc(r['observed_at_utc']))
    if len({instant_key(r['observed_at_utc']) for r in rows}) != len(rows):
        raise ChartError('CHART_ORDER_INVALID', 'two samples share one observation time; the series is ambiguous')
    return rows


def _transform(kind: str, rows: list[dict]) -> list[Decimal]:
    raws = [r['raw'] for r in rows]
    if kind == 'identity':
        return raws
    if any(v <= 0 for v in raws):
        raise ChartError('CHART_TRANSFORM_INVALID', f'{kind} needs strictly positive recorded values')
    if kind == 'index_100':
        return [Decimal(100) * v / raws[0] for v in raws]
    peak, out = raws[0], []
    for v in raws:
        peak = max(peak, v)
        out.append(Decimal(100) * (v / peak - 1))
    return out


def _gap_threshold(times: list[str], explicit: Any) -> float | None:
    if explicit is not None:
        return float(explicit)
    stamps = [_utc(t) for t in times]
    if len(stamps) < 3:
        return None
    steps = [(b - a).total_seconds() for a, b in zip(stamps, stamps[1:])]
    return 3 * median(steps)


def _coord(rows_by_series: list[list[dict]], width=800, height=260):
    left, right, top, bottom = 70, 20, 20, 50
    stamps = [_utc(r['observed_at_utc']).timestamp() for s in rows_by_series for r in s]
    values = [float(r['plot']) for s in rows_by_series for r in s]
    t0, t1 = min(stamps), max(stamps)
    lo, hi = min(values), max(values)
    pad = (hi - lo) * 0.05 or 1.0
    lo, hi = lo - pad, hi + pad

    def xy(r):
        x = left + ((_utc(r['observed_at_utc']).timestamp() - t0) / ((t1 - t0) or 1)) * (width - left - right)
        y = top + (1 - (float(r['plot']) - lo) / (hi - lo)) * (height - top - bottom)
        return x, y
    return xy, (lo, hi), (left, right, top, bottom, width, height)


def _marker(kind: str, x: float, y: float) -> str:
    if kind == 'circle':
        return f'<circle cx="{x:.2f}" cy="{y:.2f}" r="3.5" fill="none" stroke="currentColor" stroke-width="1.5"/>'
    if kind == 'square':
        return f'<rect x="{x - 3.5:.2f}" y="{y - 3.5:.2f}" width="7" height="7" fill="none" stroke="currentColor" stroke-width="1.5"/>'
    if kind == 'diamond':
        return f'<path d="M{x:.2f},{y - 4.5:.2f} L{x + 4.5:.2f},{y:.2f} L{x:.2f},{y + 4.5:.2f} L{x - 4.5:.2f},{y:.2f} Z" fill="none" stroke="currentColor" stroke-width="1.5"/>'
    return f'<path d="M{x:.2f},{y - 4.5:.2f} L{x + 4.5:.2f},{y + 3.5:.2f} L{x - 4.5:.2f},{y + 3.5:.2f} Z" fill="none" stroke="currentColor" stroke-width="1.5"/>'


def _svg(chart_id: str, title: str, desc: str, groups: list[list[dict]], labels: list[str], gap: list[float | None], y_label: str) -> str:
    esc = html.escape
    xy, (lo, hi), (left, right, top, bottom, width, height) = _coord(groups)
    parts = [f'<svg viewBox="0 0 {width} {height}" role="img" aria-labelledby="{esc(chart_id)}-t {esc(chart_id)}-d" class="chart">',
             f'<title id="{esc(chart_id)}-t">{esc(title)}</title><desc id="{esc(chart_id)}-d">{esc(desc)}</desc>',
             f'<rect x="{left}" y="{top}" width="{width - left - right}" height="{height - top - bottom}" fill="none" stroke="currentColor" stroke-opacity=".5"/>',
             f'<text x="{left - 6}" y="{top + 4}" text-anchor="end" font-size="11">{esc(f"{hi:.6g}")}</text>',
             f'<text x="{left - 6}" y="{height - bottom}" text-anchor="end" font-size="11">{esc(f"{lo:.6g}")}</text>',
             f'<text x="{left}" y="{height - bottom + 16}" font-size="11">{esc(groups[0][0]["observed_at_utc"] if len(groups) == 1 else "")}</text>',
             f'<text x="12" y="{(top + height - bottom) / 2:.0f}" font-size="11" transform="rotate(-90 12 {(top + height - bottom) / 2:.0f})" text-anchor="middle">{esc(y_label)}</text>']
    for si, rows in enumerate(groups):
        run: list[str] = []
        previous = None
        for r in rows:
            x, y = xy(r)
            if previous is not None and gap[si] is not None and \
                    (_utc(r['observed_at_utc']) - _utc(previous['observed_at_utc'])).total_seconds() > gap[si]:
                if len(run) > 1:
                    parts.append(f'<polyline points="{" ".join(run)}" fill="none" stroke="currentColor" stroke-width="1.5" stroke-dasharray="{DASHES[si % 4]}"/>')
                run = []
            run.append(f'{x:.2f},{y:.2f}')
            previous = r
        if len(run) > 1:
            parts.append(f'<polyline points="{" ".join(run)}" fill="none" stroke="currentColor" stroke-width="1.5" stroke-dasharray="{DASHES[si % 4]}"/>')
        parts.extend(_marker(MARKERS[si % 4], *xy(r)) for r in rows)
    parts.append(f'<text x="{width - right}" y="{height - bottom + 16}" text-anchor="end" font-size="11">{esc(max((r["observed_at_utc"] for g in groups for r in g), key=lambda t: (instant_key(t), t)))}</text>')
    parts.append('</svg>')
    return ''.join(parts)


def build_chart(spec: Any, observations: dict[str, dict], eligible: set[str], as_of: str) -> dict:
    """Return an AVAILABLE chart (metadata, exact table, SVG) or an UNAVAILABLE explanation.

    Lineage or compatibility violations raise :class:`ChartError`; a valid specification without enough recorded
    samples yields ``status == 'UNAVAILABLE'`` (never a chart padded with invented data)."""
    if not isinstance(spec, dict):
        raise ChartError('CHART_SPEC_INVALID', 'chart specification must be an object')
    known = {'chart_id', 'chart_type', 'title', 'series', 'min_samples', 'max_gap_seconds'}
    if set(spec) - known or not {'chart_id', 'chart_type', 'title', 'series'} <= set(spec):
        raise ChartError('CHART_SPEC_INVALID', 'unsupported or missing chart fields')
    chart_id, kind = spec['chart_id'], spec['chart_type']
    if not (isinstance(chart_id, str) and chart_id.replace('-', '').replace('_', '').isalnum()):
        raise ChartError('CHART_SPEC_INVALID', 'chart_id must be alphanumeric with - or _')
    if kind not in CHART_TYPES:
        raise ChartError('CHART_TYPE_UNSUPPORTED', f'{kind!r} is not a supported chart type')
    if not isinstance(spec['title'], str) or not spec['title'].strip():
        raise ChartError('CHART_SPEC_INVALID', 'title required')
    series_specs = spec['series']
    limit = (2, MAX_SERIES) if kind == 'normalized_comparison' else (1, 1)
    if not isinstance(series_specs, list) or not limit[0] <= len(series_specs) <= limit[1] \
            or not all(isinstance(s, dict) and isinstance(s.get('label'), str) and s['label'].strip() for s in series_specs):
        raise ChartError('CHART_SPEC_INVALID', f'{kind} needs {limit[0]}..{limit[1]} labelled series')
    min_samples = spec.get('min_samples', 3)
    if isinstance(min_samples, bool) or not isinstance(min_samples, int) or min_samples < 2:
        raise ChartError('CHART_SPEC_INVALID', 'min_samples must be an integer >= 2')
    max_gap = spec.get('max_gap_seconds')
    if max_gap is not None and (isinstance(max_gap, bool) or not isinstance(max_gap, int) or max_gap <= 0):
        raise ChartError('CHART_SPEC_INVALID', 'max_gap_seconds must be a positive integer')
    transform = CHART_TYPES[kind]
    groups, plotted_rows, labels = [], [], []
    for s in series_specs:
        rows = _series_samples(s, observations, eligible, as_of)
        if len(rows) < min_samples:
            return {'status': 'UNAVAILABLE', 'chart_id': chart_id, 'title': spec['title'], 'reason_code': 'INSUFFICIENT_SAMPLES',
                    'reason': f'series {s["label"]!r} has {len(rows)} recorded sample(s); {min_samples} required. '
                              'No chart is drawn and no missing sample is inferred.'}
        for r, plot in zip(rows, _transform(transform, rows)):
            r['plot'] = plot
        groups.append(rows)
        labels.append(s['label'])
    units = {g[0]['unit'] for g in groups}
    if kind == 'metric_history' or kind == 'drawdown':
        axis_unit = 'percent' if kind == 'drawdown' else groups[0][0]['unit']
    else:
        axis_unit = 'index (first sample = 100)'
    if kind == 'normalized_comparison' and len(groups) != len({g[0]['instrument_id'] + '|' + g[0]['metric'] for g in groups}):
        raise ChartError('CHART_IDENTITY_MISMATCH', 'normalized comparison series must be distinct instrument/metric pairs')
    if kind != 'normalized_comparison' and len(units) != 1:  # pragma: no cover - guarded per series
        raise ChartError('CHART_UNIT_MISMATCH', 'mixed units on one axis')
    gaps = [_gap_threshold([r['observed_at_utc'] for r in g], max_gap) for g in groups]
    def shown(r):
        return r['plot'] if transform == 'identity' else r['plot'].quantize(Decimal('0.000001'))
    samples = [{'series': lab, 'observation_id': r['observation_id'], 'observed_at_utc': r['observed_at_utc'],
                'recorded_value': _fmt(r['raw']), 'recorded_unit': r['unit'], 'plotted_value': _fmt(shown(r))}
               for lab, g in zip(labels, groups) for r in g]
    all_rows = [r for g in groups for r in g]
    stamps = sorted((r['observed_at_utc'] for r in all_rows), key=lambda t: (instant_key(t), t))
    version = TRANSFORMS[transform][0]
    meta = {
        'chart_id': chart_id, 'chart_type': kind, 'title': spec['title'], 'status': 'AVAILABLE',
        'metric_definition': [{'series': lab, 'instrument_id': g[0]['instrument_id'], 'venue': venue_of(g[0]['instrument_id']),
                               'metric': g[0]['metric'], 'recorded_unit': g[0]['unit']} for lab, g in zip(labels, groups)],
        'axis_unit': axis_unit, 'sample_range_utc': {'first': stamps[0], 'last': stamps[-1]},
        'observation_ids': sorted({r['observation_id'] for r in all_rows}),
        'transform': {'id': transform, 'version': version, 'formula': TRANSFORMS[transform][1]},
        'renderer_version': CHART_RENDERER_VERSION, 'sample_count': len(all_rows),
        'input_data_digest': digest({'samples': samples, 'transform': [transform, version]}),
        'missing_data_policy': MISSING_DATA_POLICY,
        'gap_threshold_seconds': [g for g in gaps],
        'limitations': ['recorded observations only; a sparse snapshot series is not a continuous market series',
                        'descriptive; not evidence of predictive edge or executable liquidity'],
    }
    desc = (f'{kind.replace("_", " ")} of {", ".join(labels)}: {len(all_rows)} recorded samples from {stamps[0]} to {stamps[-1]} UTC; '
            f'axis unit {axis_unit}. Markers show recorded samples. The numerical table below the figure lists every value.')
    svg = _svg(chart_id, spec['title'], desc, groups, labels, gaps, axis_unit)
    return {'status': 'AVAILABLE', 'meta': meta, 'samples': samples, 'svg': svg, 'legend': [
        {'series': lab, 'marker': MARKERS[i % 4], 'dash': DASHES[i % 4] or 'solid'} for i, lab in enumerate(labels)]}


def chart_html(chart: dict) -> str:
    """Accessible figure: SVG, text legend (shape/dash, never colour alone) and a disclosure holding the exact table."""
    esc = html.escape
    if chart['status'] != 'AVAILABLE':
        return (f'<section class="chart-unavailable"><h3>{esc(chart["title"])}</h3><p><strong>Chart unavailable '
                f'({esc(chart["reason_code"])}).</strong> {esc(chart["reason"])}</p></section>')
    m = chart['meta']
    legend = ''.join(f'<li>{esc(x["series"])}: {esc(x["marker"])} marker, {esc(x["dash"])} line</li>' for x in chart['legend'])
    rows = ''.join(f'<tr><td>{esc(s["series"])}</td><td>{esc(s["observed_at_utc"])}</td><td>{esc(s["recorded_value"])}</td>'
                   f'<td>{esc(s["recorded_unit"])}</td><td>{esc(s["plotted_value"])}</td><td>{esc(s["observation_id"])}</td></tr>'
                   for s in chart['samples'])
    return (f'<figure><figcaption><strong>{esc(m["title"])}</strong> ({esc(m["chart_type"])}; axis unit {esc(m["axis_unit"])}; '
            f'transform {esc(m["transform"]["id"])}@{esc(m["transform"]["version"])}; {m["sample_count"]} samples; UTC)</figcaption>'
            f'{chart["svg"]}<ul class="legend">{legend}</ul><p class="note">{esc(m["missing_data_policy"])}.</p>'
            f'<details><summary>Underlying recorded values (table)</summary><div class="scroll"><table><caption>{esc(m["title"])}: samples</caption>'
            '<thead><tr><th scope="col">Series</th><th scope="col">Observed UTC</th><th scope="col">Recorded value</th>'
            '<th scope="col">Recorded unit</th><th scope="col">Plotted value</th><th scope="col">Observation ID</th></tr></thead>'
            f'<tbody>{rows}</tbody></table></div></details></figure>')


def md_escape(text: Any) -> str:
    """One-line, table-safe Markdown text: no raw HTML, no live links, no emphasis or code spans."""
    return (str(text).replace('\r', ' ').replace('\n', ' ').replace('|', '\\|').replace('<', '&lt;').replace('>', '&gt;')
            .replace('](', '] (').replace('`', "'").replace('*', '\\*'))


def chart_markdown(chart: dict) -> str:
    e = md_escape
    if chart['status'] != 'AVAILABLE':
        return f'**{e(chart["title"])}** — chart unavailable ({e(chart["reason_code"])}): {e(chart["reason"])}\n'
    m = chart['meta']
    head = (f'**{e(m["title"])}** (`{m["chart_id"]}`, {m["chart_type"]}, axis unit {e(m["axis_unit"])}, transform '
            f'{m["transform"]["id"]}@{m["transform"]["version"]}, {m["sample_count"]} samples, input digest `{m["input_data_digest"]}`)\n\n'
            '| Series | Observed UTC | Recorded value | Unit | Plotted value | Observation |\n|---|---|---|---|---|---|\n')
    return head + ''.join(f'| {e(s["series"])} | {s["observed_at_utc"]} | {s["recorded_value"]} | {e(s["recorded_unit"])} | '
                          f'{s["plotted_value"]} | {e(s["observation_id"])} |\n' for s in chart['samples'])


def parse_utc_z(timestamp: str) -> datetime:
    """Strict ISO-8601 UTC parse used by the explorer (rejects naive and non-UTC offsets)."""
    parsed = datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
    if parsed.utcoffset() is None or parsed.utcoffset().total_seconds() != 0:
        raise ValueError('timestamps must be ISO-8601 UTC')
    return parsed

