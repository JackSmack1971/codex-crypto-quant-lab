"""stdio MCP server entry point. No network probes on import or initialization."""
import sys
from . import adapters

def main():
    if len(sys.argv)>1:
        if len(sys.argv)==3 and sys.argv[1]=='probe':
            if sys.argv[2]!='binance_spot': raise SystemExit('supported probe: binance_spot')
            import json
            result=adapters.spot_ticker('BTCUSDT')
            print(json.dumps(result))
            raise SystemExit(0 if result['status']=='ok' else 1)
        raise SystemExit('usage: python -m quant_free_mcp [probe binance_spot]')
    try:
        from mcp.server import MCPServer
    except ImportError as exc:
        raise SystemExit('Install pinned project dependency: python -m pip install -e .') from exc
    mcp=MCPServer('quant_free_local',version='0.1.0',instructions='Public read-only crypto research. Every result has provenance or an explicit unavailable status.')
    for function in (adapters.spot_book, adapters.spot_ticker, adapters.spot_trades, adapters.spot_candles,
                     adapters.futures_snapshot, adapters.futures_oi, adapters.futures_funding_history,
                     adapters.defi_protocol, adapters.defi_chain, adapters.dex_pair, adapters.dex_token_pairs,
                     adapters.btc_mempool, adapters.btc_fees, adapters.btc_tip, adapters.btc_hashrate):
        mcp.tool()(function)
    @mcp.tool()
    def quant_capabilities() -> dict:
        """Return supported read-only tools, coverage notes, and limitations without outbound traffic."""
        from .core import ROUTES
        return {'schema_version':'1.0','status':'ok','providers':sorted({p for p,_ in ROUTES.values()}),
                'tools':[f.__name__ for f in (adapters.spot_book, adapters.spot_ticker, adapters.spot_trades,
                adapters.spot_candles, adapters.futures_snapshot, adapters.futures_oi,
                adapters.futures_funding_history, adapters.defi_protocol, adapters.defi_chain,
                adapters.dex_pair, adapters.dex_token_pairs, adapters.btc_mempool,
                adapters.btc_fees, adapters.btc_tip, adapters.btc_hashrate)],
                'limitations':['bounded REST reads','no complete tick history','no guaranteed live depth','no order/trade/wallet tools','no retained history']}
    mcp.run(transport='stdio')

if __name__=='__main__': main()
