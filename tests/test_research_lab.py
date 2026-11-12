import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from research_lab import (ContractError, Observation, ResearchSignal,
                          evaluate_hypothesis, validate_replay, ResearchLedger)

T='2026-11-11T15:00:00Z'

def obs(id='o1', available=T, metric='funding', instrument='exchange:future:BTCUSDT'):
    return Observation(id, instrument, metric, '0.012', 'percent', 'https://example.org/data',
                       '2026-11-11T14:00:00Z', T, available)

def sig(id='s1', ids=('o1',), time=T):
    return ResearchSignal(id,'funding-v1','exchange:future:BTCUSDT','1h',time,'0.2',ids,'1.0',
                          'conditional interpretation')

class ResearchLabTests(unittest.TestCase):
    def test_observation_rejects_unknown_as_zero(self):
        with self.assertRaises(ContractError): replace(obs(), status='UNAVAILABLE')
        with self.assertRaises(ContractError): replace(obs(), value='NaN')
        with self.assertRaises(ContractError): replace(obs(), retrieved_at_utc='2026-11-11T10:00:00Z')

    def test_signal_rejects_invalid_score(self):
        with self.assertRaises(ContractError): replace(sig(), value='1.2')
        with self.assertRaises(ContractError): replace(sig(), value=None)

    def test_hypothesis_availability_and_no_vote(self):
        o=obs()
        p=sig()
        q=sig('s2')
        result=evaluate_hypothesis('h',T,[p,q],[o],{'s1'},{'s2'},{'funding'})
        self.assertEqual(result.conclusion,'MIXED')
        self.assertEqual(result.overlap_observation_ids,('o1',))
        late=replace(o,available_at_utc=None)
        result=evaluate_hypothesis('h',T,[p],[late],{'s1'},set(),{'funding'})
        self.assertEqual(result.conclusion,'INSUFFICIENT_EVIDENCE')

    def test_historical_replay_rejects_late_release(self):
        late=replace(obs(),available_at_utc='2026-11-11T16:00:00Z',
                     retrieved_at_utc='2026-11-11T17:00:00Z')
        self.assertEqual(validate_replay([sig()],[late],T)['status'],'INELIGIBLE')
        self.assertEqual(validate_replay([sig()],[obs()],T)['status'],'ELIGIBLE')

    def test_ledger_replay_and_tamper(self):
        with tempfile.TemporaryDirectory() as root:
            ledger=ResearchLedger(root)
            a=ledger.append('run-a',T,{'question':'q'})
            ledger.append('run-b',T,{'result':'INSUFFICIENT_EVIDENCE'})
            self.assertEqual(len(ledger.verify()),2)
            with self.assertRaises(ContractError):ledger.append('run-a',T,{})
            value=json.loads(a.read_text())
            value['payload']['question']='changed'
            a.write_text(json.dumps(value))
            with self.assertRaises(ContractError):ledger.verify()
            with self.assertRaises(ContractError):ledger.append('run-c',T,{})

if __name__=='__main__':unittest.main()
