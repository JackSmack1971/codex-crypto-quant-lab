import unittest
from decimal import Decimal
from unittest.mock import patch
from quant_free_mcp.core import decimal, valid_symbol, valid_slug, valid_address, envelope, unavailable, ROUTES
from quant_free_mcp.math import book_metrics, trade_metrics
from quant_free_mcp import adapters

class MathTests(unittest.TestCase):
    def test_spread_depth_and_invariant(self):
        b=[['99','2'],['98','3']];a=[['101','1'],['102','4']]
        m=book_metrics(b,a)
        self.assertEqual(Decimal(m['mid']),Decimal('100'))
        self.assertEqual(Decimal(m['spread_bps']),Decimal('200'))
        self.assertEqual(Decimal(m['imbalance_base_qty']),Decimal('0'))
        scaled=book_metrics([[p,str(int(q)*10)] for p,q in b],[[p,str(int(q)*10)] for p,q in a])
        self.assertEqual(m['imbalance_base_qty'],scaled['imbalance_base_qty'])
    def test_crossed_zero_and_nan(self):
        for b,a in [([['101','1']],[['100','1']]),([['99','0']],[['100','1']]),([], [['100','1']])]:
            with self.assertRaises(ValueError):book_metrics(b,a)
        for bad in ('NaN','Infinity',None,True):
            with self.assertRaises(ValueError):decimal(bad)
    def test_taker_side(self):
        v=trade_metrics([{'price':'100','qty':'2','isBuyerMaker':False,'time':10},
                         {'price':'100','qty':'1','isBuyerMaker':True,'time':11}])
        self.assertEqual(v['trade_delta_quote'],'100')
        self.assertEqual(v['vwap_quote_per_base'],'100')
        self.assertEqual(v['sample_count'],2)
    def test_validation(self):
        with self.assertRaises(ValueError):valid_symbol('BTCUSDT/../../')
        with self.assertRaises(ValueError):valid_slug('../thing')
        with self.assertRaises(ValueError):valid_address('localhost','solana')
    def test_no_arbitrary_routes(self):
        self.assertNotIn('withdraw',ROUTES)
        self.assertNotIn('order',ROUTES)
        from quant_free_mcp.core import fetch
        with self.assertRaises(ValueError):fetch('https://evil.invalid')

class AdapterTests(unittest.TestCase):
    @patch('quant_free_mcp.adapters.fetch')
    def test_book_provenance(self, get):
        get.return_value=({'lastUpdateId':1,'bids':[['99','2']],'asks':[['101','1']]},
                          {'provider':'binance_spot','retrieved_at_utc':'2026-11-11T00:00:00Z','endpoint':'/api/v3/depth'})
        r=adapters.spot_book('BTCUSDT',5)
        self.assertEqual(r['status'],'ok')
        self.assertIn('source_time_unavailable',r['quality_flags'])
        self.assertIsNone(r['observation_time_utc'])
    @patch('quant_free_mcp.adapters.fetch')
    def test_schema_change_fails_closed(self,get):
        get.return_value=({'foo':'bar'}, {'provider':'binance_spot','retrieved_at_utc':'2026-11-11T00:00:00Z'})
        r=adapters.spot_book('BTCUSDT',5)
        self.assertNotEqual(r['status'],'ok')
        self.assertIsNone(r['value'])
    @patch('quant_free_mcp.adapters.fetch')
    def test_wrong_pair_rejected(self,get):
        get.return_value=({'pairs':[{'chainId':'ethereum','pairAddress':'0x'+'1'*40}]},
                          {'provider':'dexscreener','retrieved_at_utc':'2026-11-11T00:00:00Z'})
        r=adapters.dex_pair('ethereum','0x'+'2'*40)
        self.assertNotEqual(r['status'],'ok')
    def test_unavailable_never_zero(self):
        r=unavailable('test','bitcoin:mainnet',RuntimeError('network_unavailable'))
        self.assertIsNone(r['value']);self.assertIsNone(r['source'])

if __name__=='__main__':unittest.main()
