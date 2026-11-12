import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path
from research_lab.core import Observation, ResearchSignal, ContractError
from research_lab.workflows import execute_research_run
from research_lab.ledger_sqlite import TransactionalResearchLedger
from research_lab.validation import LabelWindow, walk_forward_splits, select_and_evaluate_folds

class WorkflowTests(unittest.TestCase):
    def input(self):
        o=Observation('o','binance:spot:BTCUSDT','momentum','0.4','dimensionless',
            'https://example.org','2026-11-11T15:00:00Z','2026-11-11T15:00:00Z','2026-11-11T15:00:00Z')
        s=ResearchSignal('s','momentum','binance:spot:BTCUSDT','1h','2026-11-11T15:00:00Z',
            '0.4',('o',),'v1','demo')
        return {'schema':'research-run-input.v1','run_id':'run-01','as_of_utc':'2026-11-11T16:00:00Z',
                'observations':[asdict(o)],'signals':[asdict(s)],
                'strategy':{'id':'test','model_weights':[['momentum','1']]},
                'bars':[{'instrument_id':'binance:spot:BTCUSDT','closed_at_utc':'2026-11-11T15:00:00Z',
                          'available_at_utc':'2026-11-11T15:00:00Z','close':'100'},
                         {'instrument_id':'binance:spot:BTCUSDT','closed_at_utc':'2026-11-11T16:00:00Z',
                          'available_at_utc':'2026-11-11T16:00:00Z','close':'101'}]}
    def test_end_to_end_and_duplicate(self):
        with tempfile.TemporaryDirectory() as root:
            d=Path(root)
            result=execute_research_run(self.input(),archive_path=d/'e.db',ledger_path=d/'l.db')
            self.assertEqual(result['result']['simulation']['status'],'RESEARCH_SIMULATION_ONLY')
            self.assertEqual(len(TransactionalResearchLedger(d/'l.db').verify()),1)
            with self.assertRaises(ContractError):execute_research_run(self.input(),archive_path=d/'e.db',ledger_path=d/'l.db')
    def test_rejects_future_signal(self):
        with tempfile.TemporaryDirectory() as root:
            payload=self.input();payload['signals'][0]['as_of_utc']='2026-11-12T16:00:00Z'
            with self.assertRaises(ContractError):execute_research_run(payload,archive_path=Path(root)/'a.db',ledger_path=Path(root)/'l.db')
    def test_folds_score_only_test_after_train(self):
        windows=[LabelWindow(f'2026-01-{i:02d}T00:00:00Z',f'2026-01-{i:02d}T00:00:00Z') for i in range(1,13)]
        folds=walk_forward_splits(windows,4,2)
        result=select_and_evaluate_folds({'a':[.01]*12,'b':[.02]*12},folds)
        self.assertTrue(result['folds'])
        self.assertTrue(all(row['selected_candidate']=='b' for row in result['folds']))
if __name__=='__main__':unittest.main()

class ModelWorkflowTests(unittest.TestCase):
    def test_model_generation_is_point_in_time(self):
        from research_lab.models import ModelSpec, calculate_model
        from dataclasses import replace
        a=Observation('x','binance:spot:BTCUSDT','momentum','0.5','dimensionless','https://example.org',
                      '2026-11-11T14:59:00Z','2026-11-11T15:00:00Z','2026-11-11T15:00:00Z')
        spec=ModelSpec('momentum','signed_scalar','momentum','dimensionless')
        self.assertEqual(calculate_model(spec,[a],a.instrument_id,'2026-11-11T15:00:00Z').value,'0.5')
        late=replace(a,available_at_utc='2026-11-11T16:00:00Z',retrieved_at_utc='2026-11-11T16:00:00Z')
        self.assertEqual(calculate_model(spec,[late],a.instrument_id,'2026-11-11T15:00:00Z').status,'INSUFFICIENT_EVIDENCE')
        with self.assertRaises(ContractError):ModelSpec('x','made_up','momentum','dimensionless')
    def test_auto_model_replay(self):
        with tempfile.TemporaryDirectory() as root:
            p=WorkflowTests().input();p['signals']=[]
            p['models']=[{'id':'momentum','kind':'signed_scalar','metric':'momentum','unit':'dimensionless'}]
            p['observations'][0]['metric']='momentum';p['observations'][0]['unit']='dimensionless'
            result=execute_research_run(p,archive_path=Path(root)/'a.db',ledger_path=Path(root)/'l.db')
            self.assertEqual(result['result']['simulation']['status'],'RESEARCH_SIMULATION_ONLY')
            self.assertEqual(len(result['result']['generated_signals']),1)
