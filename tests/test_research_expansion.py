import sqlite3
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from research_lab import Observation, ResearchSignal, ContractError
from research_lab.normalization import normalize_envelope
from research_lab.archive import EvidenceArchive
from research_lab.ledger_sqlite import TransactionalResearchLedger
from research_lab.strategies import StrategySpec, ClosedBar, blend_signals, replay_strategy
from research_lab.validation import LabelWindow, walk_forward_splits, cpcv_splits, cscv_pbo

T='2026-11-11T15:00:00Z'
def observation(id='o1',when=T):
    return Observation(id,'binance:spot:BTCUSDT','spot.momentum','0.5','normalized_score',
        'https://example.org/observations','2026-11-11T14:59:00Z',T,when)
def signal(id='s1',when=T,score='0.5'):
    return ResearchSignal(id,'momentum','binance:spot:BTCUSDT','1h',when,score,('o1',),'v1','test')

class ExpansionTests(unittest.TestCase):
    def test_mcp_mapping_strict_times(self):
        envelope={'schema_version':'1.0','status':'ok','metric':'usdm.open_interest',
            'instrument_id':'binance:usdm:BTCUSDT:perp','value':'220.4','unit':'base',
            'observation_time_utc':'2026-11-11T14:59:00Z','retrieved_at_utc':T,
            'methodology':'exchange_oi','source':{'origin':'https://fapi.binance.com','endpoint':'/fapi/v1/openInterest'}}
        o=normalize_envelope(envelope)
        self.assertIsNone(o.available_at_utc)
        self.assertEqual(o.value,'220.4')
        with self.assertRaises(ContractError):normalize_envelope(envelope,available_at_utc=T)
        self.assertEqual(normalize_envelope(envelope,available_at_utc=T,availability_basis='exchange publication rule').available_at_utc,T)
        with self.assertRaises(ContractError):normalize_envelope({**envelope,'observation_time_utc':None})
        with self.assertRaises(ContractError):normalize_envelope({**envelope,'value':{'x':1}})
        self.assertEqual(normalize_envelope({**envelope,'status':'unavailable','value':None}).status,'UNAVAILABLE')

    def test_archive_immutable_snapshot_and_asof(self):
        with tempfile.TemporaryDirectory() as d:
            a=EvidenceArchive(Path(d)/'observations.sqlite')
            o=observation(); self.assertEqual(a.ingest(o),a.ingest(o))
            with self.assertRaises(ContractError):a.ingest(replace(o,value='0.7'))
            self.assertEqual(a.as_of(o.instrument_id,o.metric,T),[o])
            self.assertEqual(a.as_of(o.instrument_id,o.metric,'2026-11-11T14:59:00Z'),[])
            sid=a.freeze(['o1'],T); self.assertEqual(a.load_snapshot(sid),[o])
            with a.connect() as db:db.execute('UPDATE observations SET content=? WHERE id=?',('{}','o1'))
            with self.assertRaises(ContractError):a.load_snapshot(sid)

    def test_transactional_ledger_and_anchor(self):
        with tempfile.TemporaryDirectory() as d:
            l=TransactionalResearchLedger(Path(d)/'ledger.sqlite')
            l.append('r1',T,{'test':1}); tip=l.anchor()
            l.append('r2',T,{'test':2})
            self.assertTrue(l.verify_anchor(tip));self.assertEqual(len(l.verify()),2)
            with self.assertRaises(ContractError):l.append('r2',T,{})
            with l.connect() as db:db.execute("UPDATE records SET content='{}' WHERE sequence=0")
            with self.assertRaises(ContractError):l.verify()

    def test_replay_causality_and_costs(self):
        spec=StrategySpec('s',(('momentum','1'),),minimum_models=1)
        bars=[ClosedBar('binance:spot:BTCUSDT',T,T,'100'),
              ClosedBar('binance:spot:BTCUSDT','2026-11-11T16:00:00Z','2026-11-11T16:00:00Z','110')]
        res=replay_strategy(spec,bars,[signal()],[observation()],fee_bps='10')
        self.assertEqual(res['status'],'RESEARCH_SIMULATION_ONLY')
        self.assertEqual(res['periods'][0]['return'],'0.0495')
        self.assertEqual(blend_signals(spec,[signal()],[observation()],T,bars[0].instrument_id),.5)
        bad=replace(observation(),available_at_utc='2026-11-11T16:00:00Z',retrieved_at_utc='2026-11-11T17:00:00Z')
        self.assertEqual(replay_strategy(spec,bars,[signal()],[bad])['status'],'INSUFFICIENT_EVIDENCE')
        with self.assertRaises(ContractError):replay_strategy(spec,bars[::-1],[signal()],[observation()])

    def test_temporal_splits_nonoverlap(self):
        from datetime import datetime,timedelta,timezone
        base=datetime(2026,1,1,tzinfo=timezone.utc)
        iso=lambda d:d.isoformat().replace('+00:00','Z')
        windows=[LabelWindow(iso(base+timedelta(days=i)),iso(base+timedelta(days=i+2))) for i in range(20)]
        wf=walk_forward_splits(windows,6,2,embargo=1)
        self.assertTrue(wf)
        self.assertTrue(all(windows[max(tr)].end_utc<windows[min(te)].start_utc for tr,te in wf))
        folds=cpcv_splits(windows,5,2,embargo=1)
        self.assertEqual(len(folds),10)
        self.assertTrue(all(all(windows[i].end_utc<windows[j].start_utc or windows[i].start_utc>windows[j].end_utc for j in te for i in tr) for tr,te in folds))

    def test_pbo_is_exploratory_not_cpcv(self):
        records={'alpha':[.02,-.01,.03,.01,.02,-.02,.01,0],
                 'beta':[0,.01,0,.01,-.01,.02,0,.01]}
        report=cscv_pbo(records,4)
        self.assertEqual(report['split_count'],6)
        self.assertTrue(0<=report['pbo_estimate']<=1)
        with self.assertRaises(ContractError):cscv_pbo({'x':[0]*8},4)
        with self.assertRaises(ContractError):cscv_pbo({'x':[0]*8,'y':[float('nan')]*8},4)

if __name__=='__main__':unittest.main()

class BridgeTests(unittest.TestCase):
    def test_route_registry_and_compound_metric(self):
        from research_lab.provider_bridge import collect_observation
        from unittest.mock import patch
        payload={'schema_version':'1.0','status':'ok','metric':'spot.quote',
                 'instrument_id':'binance:spot:BTCUSDT',
                 'value':{'mid':'100','spread_bps':'1.0'},'unit':'quote_per_base',
                 'observation_time_utc':T,'retrieved_at_utc':T,
                 'source':{'origin':'https://data-api.binance.vision','endpoint':'/api/v3/ticker/bookTicker'}}
        with patch('quant_free_mcp.adapters.spot_ticker',return_value=payload) as mocked:
            o=collect_observation('spot_ticker',{'symbol':'BTCUSDT'},field='spread_bps',field_unit='bps')
            self.assertEqual((o.metric,o.value,o.unit),('spot.quote.spread_bps','1.0','bps'))
            mocked.assert_called_once_with(symbol='BTCUSDT')
        with self.assertRaises(ContractError):collect_observation('buy',{})
        with self.assertRaises(ContractError):normalize_envelope(payload,field='spread_bps')

    def test_archived_offset_aware_time_is_compared_chronologically(self):
        with tempfile.TemporaryDirectory() as d:
            archive=EvidenceArchive(Path(d)/'d.db')
            o=observation();o=replace(o,available_at_utc='2026-11-11T15:00:00+00:00')
            archive.ingest(o)
            self.assertEqual(archive.as_of(o.instrument_id,o.metric,T),[o])
