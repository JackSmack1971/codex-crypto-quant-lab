"""P2-A: evidence-bound report composition and its integration with the P1 RENDERING/FINALIZING stages."""
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from p1_support import AS_OF, INSTRUMENT, METRIC, Fixture, run_to_end, ts  # noqa: E402
from p1_support import obs_row as _obs_row  # noqa: E402

from research_lab import reporting  # noqa: E402
from research_lab.reporting import ReportError  # noqa: E402

def obs_row(id, value, observed=None, **kw):
    """Observation whose retrieval/availability follow the observation by one minute."""
    if observed is None:
        observed = ts(10, 0)
    from datetime import datetime, timedelta
    later = (datetime.fromisoformat(observed.replace('Z', '+00:00')) + timedelta(minutes=1)).isoformat().replace('+00:00', 'Z')
    kw.setdefault('retrieved', later)
    kw.setdefault('available', later)
    return _obs_row(id, value, observed=observed, **kw)


OBS = [obs_row('o1', '100', observed=ts(10, 0)), obs_row('o2', '110', observed=ts(10, 30)),
       obs_row('o3', '121', observed=ts(11, 0), source='https://api.example.org/alt'),
       obs_row('o4', '2', unit='USD', metric='other.metric', observed=ts(11, 0))]


def report(**over):
    base = {'schema': 'research-intelligence-report.v1', 'mission_id': 'm1', 'as_of_utc': AS_OF}
    base.update(over)
    return base


class Harness(unittest.TestCase):
    def setUp(self):
        self.fx = Fixture()
        self.addCleanup(self.fx.close)

    def mission(self, authored=None, observations=OBS, mission_id='m1', **spec_kw):
        spec = self.fx.spec(mission_id, observations=observations, **spec_kw)
        if authored is not None:
            authored = {**authored, 'mission_id': mission_id} if 'mission_id' in authored else authored
            spec['inputs']['report_input'] = self.fx.put(f'inputs/{mission_id}/report.json', authored)
        c = self.fx.coordinator()
        c.create_mission(spec)
        results = run_to_end(c, mission_id)
        return c, results, spec

    def content(self, c, spec):
        view = c.inspect_mission(spec['mission_id'])
        bundle = self.fx.ws / view['final']['bundle_dir']
        return bundle, json.loads((bundle / 'report_content.json').read_text()), json.loads((bundle / 'report_manifest.json').read_text())


class ClaimTests(Harness):
    def test_retrieved_claims_carry_full_lineage(self):
        c, results, spec = self.mission(report())
        self.assertEqual(results[-1]['job_state'], 'COMPLETED')
        _, content, _ = self.content(c, spec)
        claim = next(x for x in content['claims'] if x['claim_id'] == 'R:o1')
        for key in ('classification', 'metric', 'instrument_id', 'venue', 'value', 'unit', 'observed_at_utc', 'retrieved_at_utc',
                    'available_at_utc', 'evidence_ids', 'source', 'freshness', 'limitations'):
            self.assertIn(key, claim)
        self.assertEqual((claim['classification'], claim['venue'], claim['freshness']['status']), ('R', 'binance', 'FRESH'))

    def test_derived_values_are_recomputed_with_decimal(self):
        authored = report(derived_claims=[
            {'claim_id': 'chg', 'transform': 'difference', 'input_evidence_ids': ['o2', 'o1'], 'expected_value': '10'},
            {'claim_id': 'pct', 'transform': 'pct_change', 'input_evidence_ids': ['o1', 'o3']},
            {'claim_id': 'avg', 'transform': 'mean', 'input_evidence_ids': ['o1', 'o2', 'o3']}])
        c, results, spec = self.mission(authored)
        self.assertEqual(results[-1]['job_state'], 'COMPLETED')
        _, content, _ = self.content(c, spec)
        got = {x['claim_id']: (x['value'], x['unit'], x['classification']) for x in content['claims']}
        self.assertEqual(got['D:chg'], ('10', 'USDT', 'D'))
        self.assertEqual(got['D:pct'], ('21', 'percent', 'D'))
        self.assertEqual(got['D:avg'], ('110.3333333333333333333333333', 'USDT', 'D'))

    def assertRenderFails(self, authored, code, observations=OBS, **kw):
        c, results, spec = self.mission(authored, observations=observations, **kw)
        last = results[-1]
        self.assertEqual((last['outcome'], last['stage'], last['job_state']), ('FAILED', 'RENDERING', 'FAILED'))
        self.assertEqual(last['diagnostics'][0]['code'], code)
        self.assertIsNone(c.inspect_mission(spec['mission_id'])['stages'][-1]['output_digest'])  # never finalized
        return c

    def test_unsupported_transform_is_rejected(self):
        self.assertRenderFails(report(derived_claims=[{'claim_id': 'x', 'transform': 'ema', 'input_evidence_ids': ['o1', 'o2']}]),
                               'REPORT_TRANSFORM_UNSUPPORTED')

    def test_unit_instrument_and_alignment_mismatches_are_rejected(self):
        self.assertRenderFails(report(derived_claims=[{'claim_id': 'x', 'transform': 'difference', 'input_evidence_ids': ['o1', 'o4']}]),
                               'DERIVED_IDENTITY_MISMATCH')
        rows = [obs_row('a', '1', unit='USDT'), obs_row('b', '2', unit='USD', observed=ts(10, 30))]
        self.assertRenderFails(report(derived_claims=[{'claim_id': 'x', 'transform': 'difference', 'input_evidence_ids': ['a', 'b']}]),
                               'DERIVED_INPUT_INELIGIBLE', observations=rows, mission_id='m2')

    def test_ratio_requires_aligned_times_and_nonzero_denominator(self):
        self.assertRenderFails(report(derived_claims=[{'claim_id': 'r', 'transform': 'ratio', 'input_evidence_ids': ['o1', 'o3']}]),
                               'DERIVED_TIME_MISALIGNED')
        rows = [obs_row('a', '5'), obs_row('z', '0', metric='zero.metric')]
        self.assertRenderFails(report(derived_claims=[{'claim_id': 'r', 'transform': 'ratio', 'input_evidence_ids': ['a', 'z']}]),
                               'DERIVED_DIVISION_INVALID', observations=rows, mission_id='m3')

    def test_future_dated_or_unresolved_inputs_cannot_be_derived(self):
        future = [obs_row('o1', '100'), obs_row('f', '5', observed=ts(13, 0), available=ts(13, 1), retrieved=ts(13, 1))]
        self.assertRenderFails(report(derived_claims=[{'claim_id': 'x', 'transform': 'difference', 'input_evidence_ids': ['o1', 'f']}]),
                               'DERIVED_INPUT_INELIGIBLE', observations=future)
        self.assertRenderFails(report(derived_claims=[{'claim_id': 'x', 'transform': 'difference', 'input_evidence_ids': ['o1', 'ghost']}]),
                               'DERIVED_INPUT_UNRESOLVED', mission_id='m4')

    def test_pct_change_from_a_negative_base_has_the_economic_sign(self):
        rows = [obs_row('a', '-10', observed=ts(10, 0), metric='funding.rate'), obs_row('b', '-5', observed=ts(10, 30), metric='funding.rate'),
                obs_row('z', '0', observed=ts(10, 0), metric='zero.base'), obs_row('y', '4', observed=ts(10, 30), metric='zero.base')]
        c, results, spec = self.mission(report(derived_claims=[{'claim_id': 'p', 'transform': 'pct_change', 'input_evidence_ids': ['a', 'b']}]), observations=rows)
        _, content, _ = self.content(c, spec)
        self.assertEqual(next(x for x in content['claims'] if x['claim_id'] == 'D:p')['value'], '50')
        self.assertEqual(next(x for x in content['claims'] if x['claim_id'] == 'D:p')['transform']['version'], '2')
        self.assertRenderFails(report(derived_claims=[{'claim_id': 'p', 'transform': 'pct_change', 'input_evidence_ids': ['z', 'y']}]),
                               'DERIVED_DIVISION_INVALID', observations=rows, mission_id='m10')

    def test_ratio_records_operand_order_and_null_lists_are_rejected(self):
        rows = [obs_row('a', '6', observed=ts(10, 0)), obs_row('b', '3', observed=ts(10, 0), metric='other.metric', unit='USDT')]
        c, results, spec = self.mission(report(derived_claims=[{'claim_id': 'r', 'transform': 'ratio', 'input_evidence_ids': ['b', 'a']}]), observations=rows)
        _, content, _ = self.content(c, spec)
        claim = next(x for x in content['claims'] if x['claim_id'] == 'D:r')
        self.assertEqual((claim['value'], claim['transform']['numerator_id'], claim['transform']['denominator_id']), ('0.5', 'b', 'a'))
        for key in ('interpretations', 'risks', 'corroborations', 'derived_claims', 'charts', 'key_findings'):
            self.assertRenderFails(report(**{key: None}), 'REPORT_INPUT_INVALID', mission_id='nl-' + key.replace('_', '-'))

    def test_wrong_authored_expected_value_is_rejected(self):
        self.assertRenderFails(report(derived_claims=[{'claim_id': 'x', 'transform': 'difference', 'input_evidence_ids': ['o1', 'o2'], 'expected_value': '11'}]),
                               'DERIVED_VALUE_MISMATCH')


class InterpretationTests(Harness):
    def assertRenderFails(self, authored, code, **kw):
        c, results, spec = self.mission(authored, **kw)
        self.assertEqual(results[-1]['stage'], 'RENDERING')
        self.assertEqual(results[-1]['diagnostics'][0]['code'], code)

    def scenario(self, text='Recorded mid of 100 is the only anchor', refs=('R:o1',), stance='bull'):
        return {'id': 'sc1', 'stance': stance, 'text': text, 'claim_refs': list(refs), 'assumptions': ['venue quote stays representative'],
                'falsifiers': ['a later observation below the recorded 100 mid']}

    def test_supported_interpretation_is_labelled_inference(self):
        c, results, spec = self.mission(report(interpretations=[self.scenario()], executive_thesis={'text': 'One recorded mid of 100 exists.', 'claim_refs': ['R:o1']}))
        self.assertEqual(results[-1]['job_state'], 'COMPLETED')
        bundle, content, manifest = self.content(c, spec)
        md = (bundle / 'report.md').read_text()
        self.assertIn('[I] BULL (sc1)', md)
        self.assertIn('Falsifier: a later observation', md)
        self.assertEqual(manifest['claim_counts']['I'], 2)

    def test_invented_number_is_rejected(self):
        self.assertRenderFails(report(interpretations=[self.scenario('Price should reach 250 soon')]), 'INTERPRETATION_INTRODUCES_NUMBER')

    def test_interpretation_without_available_support_is_rejected(self):
        rows = [obs_row('o9', '1', metric='aux.metric')]
        missing = [{'instrument_id': INSTRUMENT, 'metric': METRIC, 'unit': 'USDT'}]
        c, results, spec = self.mission(report(interpretations=[self.scenario(refs=('U:%s|%s' % (INSTRUMENT, METRIC),))]), observations=rows,
                                        required_metrics=missing)
        self.assertEqual(results[-1]['diagnostics'][0]['code'], 'INTERPRETATION_UNSUPPORTED')

    def test_validation_claim_and_unknown_reference_rejected(self):
        self.assertRenderFails(report(interpretations=[self.scenario('This thesis is VALIDATED')]), 'INTERPRETATION_UNSUPPORTED', mission_id='m5')
        self.assertRenderFails(report(interpretations=[self.scenario(refs=('R:nope',))]), 'CLAIM_REFERENCE_UNRESOLVED', mission_id='m6')

    def test_shared_provider_is_not_independent_corroboration(self):
        self.assertRenderFails(report(corroborations=[{'id': 'c1', 'evidence_ids': ['o1', 'o2'], 'assertion': 'INDEPENDENT'}]), 'CORROBORATION_NOT_INDEPENDENT')

    def test_distinct_hosts_carry_an_explicit_limitation(self):
        twin = obs_row('o5', '100', observed=ts(10, 0), source='https://api.example.org/alt')
        c, results, spec = self.mission(report(corroborations=[{'id': 'c1', 'evidence_ids': ['o1', 'o5'], 'assertion': 'INDEPENDENT'}]), observations=OBS + [twin])
        self.assertEqual(results[-1]['job_state'], 'COMPLETED')
        _, content, _ = self.content(c, spec)
        self.assertIn('do not prove distinct underlying data', content['corroborations'][0]['limitation'])

    def test_corroboration_must_measure_the_same_thing_at_the_same_time(self):
        self.assertRenderFails(report(corroborations=[{'id': 'c1', 'evidence_ids': ['o1', 'o3'], 'assertion': 'INDEPENDENT'}]), 'CORROBORATION_NOT_INDEPENDENT')
        self.assertRenderFails(report(corroborations=[{'id': 'c1', 'evidence_ids': ['o3', 'o4'], 'assertion': 'INDEPENDENT'}]), 'CORROBORATION_NOT_INDEPENDENT', mission_id='m8')

    def test_number_check_bypasses_are_closed(self):
        for i, text in enumerate(('reached 1e3 soon', 'price 1.5e2', 'Leverage 100x', 'a ½ move', 'in Q4', 'at 10:00', 'fully validated and proven', 'a \uff11\uff15\uff10 print')):
            self.assertRenderFails(report(interpretations=[{'id': 's', 'stance': 'bull', 'text': text, 'claim_refs': ['R:o1'],
                                                              'assumptions': ['demand persists'], 'falsifiers': ['demand fades']}]),
                                   'INTERPRETATION_UNSUPPORTED' if 'validated' in text else 'INTERPRETATION_INTRODUCES_NUMBER', mission_id=f'n{i}')

    def test_html_or_code_fields_are_unsupported(self):
        self.assertRenderFails(report(raw_html='<script>alert(1)</script>'), 'REPORT_INPUT_INVALID')
        self.assertRenderFails(report(as_of_utc='2026-01-02T00:00:00Z'), 'REPORT_INPUT_INVALID', mission_id='m7')

    def test_authored_text_is_escaped(self):
        c, results, spec = self.mission(report(interpretations=[self.scenario('Recorded 100 <script>x</script> anchor')]))
        bundle, _, _ = self.content(c, spec)
        page = (bundle / 'report.html').read_text()
        self.assertNotIn('<script>x', page)
        self.assertIn('&lt;script&gt;', page)
        self.assertNotIn('<script', page)
        self.assertNotIn('http://', page.replace('http://www.w3.org', ''))


class MissingAndDeterminismTests(Harness):
    def test_missing_required_metric_is_unavailable_never_imputed(self):
        missing = [{'instrument_id': INSTRUMENT, 'metric': 'absent.metric', 'unit': 'USDT'}]
        c, results, spec = self.mission(report(), required_metrics=missing, hypotheses=[])
        bundle, content, _ = self.content(c, spec) if results[-1]['job_state'] == 'COMPLETED' else (None, None, None)
        view = c.inspect_mission('m1')
        art = self.fx.ws / 'missions/m1/stages/RENDERING'
        content = json.loads(next(art.glob('*/report_content.json')).read_text())
        un = [x for x in content['claims'] if x['classification'] == 'U']
        self.assertEqual(len(un), 1)
        self.assertIsNone(un[0]['value'])
        self.assertEqual(content['research_grade'], 'INSUFFICIENT_EVIDENCE / NO_TRADE')
        md = next(art.glob('*/report.md')).read_text()
        self.assertIn('UNAVAILABLE', md)
        self.assertIn('NO_TRADE', md)
        self.assertEqual(view['job']['state'], 'COMPLETED')  # process completion is not research sufficiency
        self.assertEqual(view['final']['research_state'], 'INSUFFICIENT_EVIDENCE')

    def test_identical_inputs_give_identical_canonical_bytes(self):
        digests = []
        for _ in range(2):
            fx = Fixture()
            self.addCleanup(fx.close)
            spec = fx.spec('m1', observations=OBS)
            spec['inputs']['report_input'] = fx.put('inputs/m1/report.json', report(derived_claims=[
                {'claim_id': 'chg', 'transform': 'difference', 'input_evidence_ids': ['o1', 'o2']}]))
            c = fx.coordinator()
            c.create_mission(spec)
            run_to_end(c, 'm1')
            bundle = fx.ws / c.inspect_mission('m1')['final']['bundle_dir']
            digests.append({n: (bundle / n).read_bytes() for n in ('report_content.json', 'report_manifest.json', 'report.md', 'report.html')})
        self.assertEqual(digests[0], digests[1])
        manifest = json.loads(digests[0]['report_manifest.json'])
        self.assertEqual(manifest['run_metadata']['creation_timestamp'], 'EXCLUDED_FROM_CANONICAL_CONTENT')

    def test_report_without_input_keeps_legacy_artifacts_and_schema(self):
        c, results, spec = self.mission()
        bundle, _, manifest = self.content(c, spec)
        mission = json.loads((bundle / 'mission.json').read_text())
        self.assertEqual(mission['schema'], 'research-mission.v2')
        self.assertNotIn('report_manifest.json', mission['artifacts'])
        self.assertIsNone(manifest['report_input_digest'])
        self.assertEqual(json.loads((bundle / 'final_verification.json').read_text())['report_companions']['state'], 'PASS')

    def test_finalizing_independently_rejects_inconsistent_companions(self):
        c, results, spec = self.mission()
        bundle, _, _ = self.content(c, spec)
        files = {n: (bundle / n).read_bytes() for n in ('report_content.json', 'report_manifest.json', 'report.md', 'report.html')}
        ids = set(json.loads(files['report_content.json'])['referenced_evidence_ids'])
        sd = json.loads(files['report_manifest.json'])['input_spec_digest']
        self.assertEqual(reporting.verify_companions(files, spec_digest=sd, mission_id='m1', evidence_ids=ids), [])
        bad = {**files, 'report.md': files['report.md'] + b'x'}
        self.assertTrue(reporting.verify_companions(bad, spec_digest=sd, mission_id='m1', evidence_ids=ids))
        self.assertTrue(reporting.verify_companions(files, spec_digest=sd, mission_id='m1', evidence_ids=set()))
        self.assertTrue(reporting.verify_companions(files, spec_digest='0' * 64, mission_id='m1', evidence_ids=ids))

    def test_tampered_rendering_output_blocks_finalization(self):
        c, results, spec = self.mission()
        c2 = self.fx.coordinator()
        c2.create_mission(self.fx.spec('m9', observations=OBS))
        for _ in range(6):
            c2.advance_mission('m9')
        path = next((self.fx.ws / 'missions/m9/stages/RENDERING').glob('*/report_manifest.json'))
        path.write_text('{}')
        result = c2.advance_mission('m9')
        self.assertEqual(result['outcome'], 'BLOCKED')
        self.assertEqual(result['diagnostics'][0]['code'], 'STAGE_OUTPUT_DIGEST_MISMATCH')

    def test_coordinator_never_emits_validated(self):
        c, results, spec = self.mission(report())
        _, content, _ = self.content(c, spec)
        self.assertNotIn('VALIDATED', json.dumps(content).replace('never designates VALIDATED', '').replace('designate VALIDATED', ''))


class BoundaryTests(unittest.TestCase):
    def test_p2_modules_have_no_execution_network_or_trading_surface(self):
        import re
        root = Path(__file__).resolve().parents[1]
        forbidden = [r'\bsubprocess\b', r'\bsocket\b', r'urllib\.request', r'\brequests\b', r'http\.client', r'os\.system', r'\beval\(', r'\bexec\(',
                     r'__import__', r'importlib\.import_module', r'\bws3\b', r'private_key', r'place_order', r'sign_transaction']
        for name in ('reporting.py', 'charts.py', 'history.py', 'benchmarks.py'):
            source = (root / 'research_lab' / name).read_text()
            for pattern in forbidden:
                if name == 'benchmarks.py' and pattern == r'\bsocket\b':
                    continue  # the benchmark *tests* patch socket; the module itself never imports it (asserted below)
                self.assertIsNone(re.search(pattern, source), (name, pattern))
            self.assertNotIn('import socket', source)


if __name__ == '__main__':
    unittest.main()
