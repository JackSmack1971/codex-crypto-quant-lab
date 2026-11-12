import json
import tempfile
import unittest
from pathlib import Path
from scripts.research_explorer import render
from research_lab.strategies import StrategySpec, cost_sensitivity
from research_lab.core import ContractError

class ExplorerTests(unittest.TestCase):
    def test_accessible_provenance_and_no_overwrite(self):
        body={'schema':'research-explorer-input.v1','series':[{'name':'BTC <close>','units':'USD','source_uri':'https://example.org/data',
             'method':'provider close','timestamps_utc':['2026-01-01T00:00:00Z','2026-01-02T00:00:00Z'],
             'values':[100,110],'observation_ids':['a','b']}]}
        with tempfile.TemporaryDirectory() as temp:
            folder=Path(temp);src=folder/'x.json';out=folder/'a.html';src.write_text(json.dumps(body))
            result=render(src,out)
            self.assertEqual(result['series'],1)
            self.assertIn('BTC &lt;close&gt;',out.read_text())
            with self.assertRaises(ValueError):render(src,out)
            body['series'][0]['source_uri']='javascript:alert(1)';src.write_text(json.dumps(body))
            with self.assertRaises(ValueError):render(src,folder/'b.html')
    def test_bounded_costs_and_no_data(self):
        from research_lab.strategies import ClosedBar
        bars=[ClosedBar('venue:BTC','2026-01-01T00:00:00Z','2026-01-01T00:00:00Z','100'),
              ClosedBar('venue:BTC','2026-01-02T00:00:00Z','2026-01-02T00:00:00Z','110')]
        spec=StrategySpec('x',(('model','1'),))
        assumptions={'fee_bps':'10','spread_bps':'10','slippage_bps':'10'}
        result=cost_sensitivity(spec,bars,[],[],[assumptions])
        self.assertIsNone(result['scenarios'][0]['final_equity'])
        with self.assertRaises(ContractError):cost_sensitivity(spec,bars,[],[],[assumptions,assumptions])

if __name__=='__main__':unittest.main()
