import json
import tempfile
import unittest
from pathlib import Path
from scripts.render_report import render

class ReportingTests(unittest.TestCase):
    def test_escapes_html_and_preserves_unavailable(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);source=root/'inputs.json';out=root/'reports'/'run'
            body={'title':'BTC <not HTML>','observations':[
                {'schema_version':'1.0','status':'ok','metric':'spot.mid','instrument_id':'binance:spot:BTCUSDT',
                 'value':'100','unit':'USDT/BTC','retrieved_at_utc':'2026-11-11T00:00:00Z',
                 'observation_time_utc':None,'quality_flags':['source_time_unavailable'],
                 'source':{'origin':'https://data-api.binance.vision','endpoint':'/api/v3/depth'}},
                {'schema_version':'1.0','status':'unavailable','metric':'usdm.funding','instrument_id':'binance:usdm:BTCUSDT:perp',
                 'value':None,'retrieved_at_utc':'2026-11-11T00:00:00Z','quality_flags':['no_verified_observation'],
                 'source':None}]}
            source.write_text(json.dumps(body))
            render(source,out)
            html=(out/'report.html').read_text()
            self.assertIn('BTC &lt;not HTML&gt;',html)
            self.assertNotIn('<not HTML>',html)
            self.assertIn('INSUFFICIENT_EVIDENCE',html)
            self.assertEqual(json.loads((out/'manifest.json').read_text())['non_ok_observations'],1)
            with self.assertRaises(ValueError):render(source,out)
    def test_forged_success_fails(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);src=root/'x.json'
            src.write_text(json.dumps({'title':'bad result','observations':[{'schema_version':'1.0','status':'ok','metric':'x','instrument_id':'y','value':'1','unit':'USD','quality_flags':[],'retrieved_at_utc':'2026-11-11T00:00:00Z','source':None}]}))
            with self.assertRaises(ValueError):render(src,root/'output')
