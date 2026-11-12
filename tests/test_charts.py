"""P2-B: source-linked static charts; every plotted sample must resolve to eligible archived evidence."""
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from p1_support import AS_OF, Fixture, run_to_end, ts  # noqa: E402
from test_report_composition import OBS, Harness, obs_row, report  # noqa: E402

from research_lab.charts import ChartError, build_chart, chart_html, chart_markdown  # noqa: E402


def summaries(*rows):
    return {r['id']: r for r in rows}


def series_obs(n=5, step=30, start_value=100, metric='spot.quote.mid', instrument='binance:spot:BTCUSDT', unit='USDT', prefix='s'):
    return [obs_row(f'{prefix}{i}', str(start_value + i * 10), observed=ts(8, i * step), instrument=instrument, metric=metric, unit=unit)
            for i in range(n)]


def spec(ids, kind='metric_history', **kw):
    return {'chart_id': kw.pop('chart_id', 'c1'), 'chart_type': kind, 'title': kw.pop('title', 'Mid history'),
            'series': [{'label': 'BTC mid', 'observation_ids': ids}], **kw}


class ChartBuildTests(unittest.TestCase):
    def setUp(self):
        self.rows = series_obs()
        self.obs = summaries(*self.rows)
        self.ids = sorted(self.obs)
        self.elig = set(self.ids)

    def build(self, s, obs=None, elig=None):
        return build_chart(s, obs if obs is not None else self.obs, elig if elig is not None else self.elig, AS_OF)

    def test_metadata_and_exact_table(self):
        chart = self.build(spec(self.ids))
        m = chart['meta']
        self.assertEqual((m['chart_type'], m['sample_count'], m['axis_unit']), ('metric_history', 5, 'USDT'))
        self.assertEqual(m['metric_definition'][0]['venue'], 'binance')
        self.assertEqual(m['sample_range_utc'], {'first': ts(8, 0), 'last': ts(10, 0)})
        self.assertEqual(m['observation_ids'], self.ids)
        self.assertIn('PRESERVE_GAPS', m['missing_data_policy'])
        self.assertEqual([s['recorded_value'] for s in chart['samples']], ['100', '110', '120', '130', '140'])
        self.assertTrue(all(s['observation_id'] in self.obs for s in chart['samples']))
        self.assertEqual(self.build(spec(self.ids))['meta']['input_data_digest'], m['input_data_digest'])

    def test_accessible_self_contained_markup(self):
        page = chart_html(self.build(spec(self.ids)))
        for needle in ('role="img"', '<title', '<desc', '<details>', '<summary>', 'scope="col"', 'UTC'):
            self.assertIn(needle, page)
        self.assertNotIn('<script', page)
        self.assertNotIn('http', page)
        self.assertIn('marker', page)                      # legend states shape/dash, not colour only
        self.assertIn('| 110 |', chart_markdown(self.build(spec(self.ids))))

    def test_sparse_series_is_unavailable_not_invented(self):
        chart = self.build(spec(self.ids[:2]))
        self.assertEqual((chart['status'], chart['reason_code']), ('UNAVAILABLE', 'INSUFFICIENT_SAMPLES'))
        self.assertNotIn('svg', chart)
        self.assertIn('Chart unavailable', chart_html(chart))

    def test_gaps_are_preserved_not_interpolated(self):
        rows = series_obs(3, step=10, prefix='a') + series_obs(3, step=10, prefix='b', start_value=500)
        for r, minutes in zip(rows[3:], (0, 10, 20)):
            r['observed_at_utc'], r['retrieved_at_utc'], r['available_at_utc'] = ts(11, minutes), ts(11, minutes + 1), ts(11, minutes + 1)
        obs = summaries(*rows)
        chart = build_chart(spec(sorted(obs)), obs, set(obs), AS_OF)
        self.assertEqual(chart['svg'].count('<polyline'), 2)         # one segment per contiguous run
        self.assertEqual(chart['svg'].count('<circle'), 6)           # every recorded sample is marked
        self.assertEqual(chart['meta']['sample_count'], 6)

    def test_lineage_violations_are_hard_errors(self):
        cases = {
            'CHART_SAMPLE_UNRESOLVED': spec(self.ids + ['ghost']),
            'CHART_TYPE_UNSUPPORTED': spec(self.ids, kind='market_depth_curve'),
            'CHART_SPEC_INVALID': {**spec(self.ids), 'raw_values': [1, 2, 3]},
        }
        for code, s in cases.items():
            with self.assertRaises(ChartError, msg=code) as ctx:
                self.build(s)
            self.assertEqual(ctx.exception.code, code)

    def test_ineligible_future_and_nonfinite_samples(self):
        with self.assertRaises(ChartError) as ctx:
            self.build(spec(self.ids), elig=set(self.ids[:-1]))
        self.assertEqual(ctx.exception.code, 'CHART_SAMPLE_INELIGIBLE')
        late = dict(self.obs)
        late['s0'] = {**late['s0'], 'observed_at_utc': '2026-01-01T13:00:00Z'}
        with self.assertRaises(ChartError) as ctx:
            self.build(spec(self.ids), obs=late)
        self.assertEqual(ctx.exception.code, 'CHART_SAMPLE_INELIGIBLE')
        for bad in ('NaN', 'Infinity', 'abc'):
            broken = {**self.obs, 's1': {**self.obs['s1'], 'value': bad}}
            with self.assertRaises(ChartError) as ctx:
                self.build(spec(self.ids), obs=broken)
            self.assertEqual(ctx.exception.code, 'CHART_NON_FINITE')

    def test_mixed_unit_instrument_metric_and_duplicate_time(self):
        for patch, code in (({'unit': 'USD'}, 'CHART_UNIT_MISMATCH'), ({'instrument_id': 'kraken:spot:BTCUSD'}, 'CHART_IDENTITY_MISMATCH'),
                            ({'metric': 'spot.volume'}, 'CHART_IDENTITY_MISMATCH'), ({'observed_at_utc': ts(8, 0)}, 'CHART_ORDER_INVALID')):
            broken = {**self.obs, 's2': {**self.obs['s2'], **patch}}
            with self.assertRaises(ChartError, msg=code) as ctx:
                self.build(spec(self.ids), obs=broken)
            self.assertEqual(ctx.exception.code, code)

    def test_normalized_comparison_never_shares_raw_axis(self):
        vol = series_obs(4, prefix='v', metric='spot.volume', unit='BTC', start_value=1000)
        obs = summaries(*self.rows, *vol)
        s = {'chart_id': 'cmp', 'chart_type': 'normalized_comparison', 'title': 'Indexed', 'series': [
            {'label': 'mid', 'observation_ids': self.ids}, {'label': 'volume', 'observation_ids': sorted(r['id'] for r in vol)}]}
        chart = build_chart(s, obs, set(obs), AS_OF)
        self.assertEqual(chart['meta']['axis_unit'], 'index (first sample = 100)')
        self.assertEqual({x['recorded_unit'] for x in chart['samples']}, {'USDT', 'BTC'})   # raw units stay in the table
        self.assertEqual(chart['samples'][0]['plotted_value'], '100')
        self.assertEqual(chart['meta']['transform']['id'], 'index_100')
        s['series'][1] = {'label': 'dup', 'observation_ids': self.ids}
        with self.assertRaises(ChartError) as ctx:
            build_chart(s, obs, set(obs), AS_OF)
        self.assertEqual(ctx.exception.code, 'CHART_IDENTITY_MISMATCH')
        zero = {**obs, 'v0': {**obs['v0'], 'value': '0'}}
        with self.assertRaises(ChartError) as ctx:
            build_chart({**s, 'series': [s['series'][0], {'label': 'volume', 'observation_ids': sorted(r['id'] for r in vol)}]}, zero, set(zero), AS_OF)
        self.assertEqual(ctx.exception.code, 'CHART_TRANSFORM_INVALID')

    def test_drawdown_uses_recorded_samples_only(self):
        rows = [obs_row(f'd{i}', v, observed=ts(8, i * 30)) for i, v in enumerate(('100', '120', '90', '120'))]
        obs = summaries(*rows)
        chart = build_chart(spec(sorted(obs), kind='drawdown'), obs, set(obs), AS_OF)
        self.assertEqual([s['plotted_value'] for s in chart['samples']], ['0', '0', '-25', '0'])
        self.assertEqual(chart['meta']['axis_unit'], 'percent')


class MarkdownSafetyTests(unittest.TestCase):
    def test_hostile_text_cannot_inject_markdown_or_html(self):
        obs = summaries(*series_obs())
        hostile = 'T|x <img src=x onerror=1>\n# H [x](javascript:alert(1)) `code` *em*'
        s = {'chart_id': 'c1', 'chart_type': 'metric_history', 'title': hostile,
             'series': [{'label': 'a|b\n| x | y |', 'observation_ids': sorted(obs)}]}
        chart = build_chart(s, obs, set(obs), AS_OF)
        md = chart_markdown(chart)
        self.assertNotIn('<img', md)
        self.assertNotIn('](', md)
        self.assertNotIn('\n# H', md)
        self.assertNotIn('`code`', md)
        self.assertNotIn('<img', chart_html(chart))


class ChartMissionTests(Harness):
    def chart_spec(self, ids=('o1', 'o2', 'o3'), **kw):
        return {'chart_id': 'mid-history', 'chart_type': 'metric_history', 'title': 'Mid history',
                'series': [{'label': 'BTC mid', 'observation_ids': list(ids)}], **kw}

    def test_report_embeds_chart_and_manifest_records_dataset_digest(self):
        c, results, spec_ = self.mission(report(charts=[self.chart_spec()]))
        self.assertEqual(results[-1]['job_state'], 'COMPLETED')
        bundle, content, manifest = self.content(c, spec_)
        page = (bundle / 'report.html').read_text()
        self.assertIn('<svg', page)
        self.assertIn('Underlying recorded values', page)
        self.assertEqual(set(manifest['chart_dataset_digests']), {'mid-history'})
        self.assertEqual(manifest['chart_dataset_digests']['mid-history'], content['charts'][0]['input_data_digest'])
        self.assertTrue(set(content['charts'][0]['observation_ids']) <= set(content['referenced_evidence_ids']) | {'o1', 'o2', 'o3'})

    def test_invalid_chart_reference_fails_rendering_not_finalization(self):
        c, results, spec_ = self.mission(report(charts=[self.chart_spec(('o1', 'o2', 'ghost'))]))
        last = results[-1]
        self.assertEqual((last['stage'], last['job_state'], last['diagnostics'][0]['code']), ('RENDERING', 'FAILED', 'CHART_SAMPLE_UNRESOLVED'))

    def test_mixed_units_on_one_axis_are_rejected(self):
        c, results, spec_ = self.mission(report(charts=[self.chart_spec(('o1', 'o2', 'o4'))]))
        self.assertEqual(results[-1]['diagnostics'][0]['code'], 'CHART_IDENTITY_MISMATCH')

    def test_sparse_chart_degrades_to_unavailable_section(self):
        c, results, spec_ = self.mission(report(charts=[self.chart_spec(('o1', 'o2'))]))
        self.assertEqual(results[-1]['job_state'], 'COMPLETED')
        bundle, content, manifest = self.content(c, spec_)
        self.assertEqual(content['charts'][0]['status'], 'UNAVAILABLE')
        self.assertEqual(manifest['chart_dataset_digests'], {})
        self.assertIn('Chart unavailable', (bundle / 'report.html').read_text())


if __name__ == '__main__':
    unittest.main()
