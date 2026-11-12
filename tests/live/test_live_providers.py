"""Opt-in live provider probes. Never part of offline CI.

Run only with QUANT_LIVE_TESTS=1 (see .github/workflows/live-providers.yml). Public provider
availability and jurisdiction are not guaranteed; a regional block is a valid, explicit
outcome and is asserted as such rather than hidden. No credentials are used.
"""
import os
import unittest

LIVE = os.environ.get('QUANT_LIVE_TESTS') == '1'


@unittest.skipUnless(LIVE, 'live provider tests are opt-in: set QUANT_LIVE_TESTS=1')
class LiveProviderTests(unittest.TestCase):
    def test_spot_ticker_is_ok_or_explicitly_unavailable(self):
        from quant_free_mcp import adapters, core
        core.set_acquirer(None)  # fresh default acquirer: retries/breaker on, cache off
        out = adapters.spot_ticker('BTCUSDT')
        self.assertIn(out['status'], {'ok', 'unavailable', 'rate_limited', 'schema_changed'})
        if out['status'] == 'ok':
            self.assertEqual(out['source']['cache']['status'], 'LIVE')
            self.assertEqual(out['source']['origin'], 'https://data-api.binance.vision')
            self.assertFalse(out['fallback_used'])
        else:
            self.assertIsNone(out['value'])
        self.assertLessEqual(core.get_acquirer().health()['binance_spot']['requests'], 3)


if __name__ == '__main__':
    unittest.main()
