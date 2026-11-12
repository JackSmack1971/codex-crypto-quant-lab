"""Typed, bounded provider tools. Raw HTTP responses never count as verified values."""
from .core import fetch, envelope, unavailable, valid_symbol, valid_slug, valid_address, decimal, utc_epoch
from .math import book_metrics, trade_metrics

def _run(metric, instrument, fn):
    try:return fn()
    except (ValueError, TypeError, KeyError, IndexError, RuntimeError, OverflowError) as exc:
        return unavailable(metric,instrument,exc)

def spot_book(symbol: str, depth: int = 100):
    instrument=f'binance:spot:{symbol}'
    def run():
        valid_symbol(symbol)
        if depth not in (5,10,20,50,100,500,1000):raise ValueError('invalid_depth')
        body,src=fetch('spot_book',params={'symbol':symbol,'limit':depth})
        if not isinstance(body,dict) or not isinstance(body.get('lastUpdateId'),int): raise ValueError('schema_changed')
        vals=book_metrics(body['bids'],body['asks'])
        src['response_identifier']=f"lastUpdateId:{body['lastUpdateId']}"
        return envelope('spot.book.metrics',instrument,vals,'mixed_explicit_units',src,method='book_top_n_v1',coverage=f'{depth}_levels_rest_snapshot')
    return _run('spot.book.metrics',instrument,run)

def spot_ticker(symbol: str):
    instrument=f'binance:spot:{symbol}'
    def run():
        valid_symbol(symbol);b,s=fetch('spot_ticker',params={'symbol':symbol})
        p=decimal(b['bidPrice']); a=decimal(b['askPrice'])
        if p<=0 or a<=p:raise ValueError('invalid_quote')
        mid=(p+a)/2
        return envelope('spot.quote',instrument,{'bid':str(p),'ask':str(a),'mid':str(mid),'spread_bps':str(10000*(a-p)/mid)},'quote_per_base',s)
    return _run('spot.quote',instrument,run)

def spot_trades(symbol: str, limit: int = 250):
    instrument=f'binance:spot:{symbol}'
    def run():
        valid_symbol(symbol)
        if not 1<=limit<=1000:raise ValueError('invalid_limit')
        b,s=fetch('spot_trades',params={'symbol':symbol,'limit':limit})
        if not isinstance(b,list) or not b or len(b)>limit:raise ValueError('schema_changed')
        v=trade_metrics(b)
        return envelope('spot.trades.sample',instrument,v,'quote_currency',s,utc_epoch(v['end_ms']),method='bounded_recent_trade_sample_v1',coverage=f'{len(b)}_recent_trades_not_complete_market_window')
    return _run('spot.trades.sample',instrument,run)

def spot_candles(symbol: str, interval: str = '1h', limit: int = 100):
    instrument=f'binance:spot:{symbol}'
    def run():
        valid_symbol(symbol)
        if interval not in ('1m','5m','15m','30m','1h','2h','4h','1d') or not 1<=limit<=500:raise ValueError('invalid_candle_request')
        b,s=fetch('spot_candles',params={'symbol':symbol,'interval':interval,'limit':limit+1})
        if not isinstance(b,list):raise ValueError('schema_changed')
        from datetime import datetime,timezone
        now=int(datetime.now(timezone.utc).timestamp()*1000)
        rows=[]
        for r in b:
            if not isinstance(r,list) or len(r)<7:raise ValueError('schema_changed')
            if int(r[6])>=now:continue
            op,hi,lo,cl,v=(decimal(x) for x in r[1:6])
            if min(op,hi,lo,cl)<=0 or v<0 or hi<max(op,cl,lo) or lo>min(op,cl,hi):raise ValueError('invalid_ohlcv')
            rows.append({'open_time_utc':utc_epoch(r[0]),'open':str(op),'high':str(hi),'low':str(lo),'close':str(cl),'base_volume':str(v)})
        rows=rows[-limit:]
        if not rows or any(rows[i]['open_time_utc']>=rows[i+1]['open_time_utc'] for i in range(len(rows)-1)):
            raise ValueError('schema_changed')
        return envelope('spot.ohlcv.closed',instrument,rows,'quote_per_base_and_base_volume',s,rows[-1]['open_time_utc'],method='closed_candles_v1',coverage=f'{len(rows)}_most_recent_closed_bars_{interval}')
    return _run('spot.ohlcv.closed',instrument,run)

def futures_snapshot(symbol: str):
    instrument=f'binance:usdm:{symbol}:perp'
    def run():
        valid_symbol(symbol)
        b,s=fetch('futures_mark',params={'symbol':symbol})
        if not isinstance(b,dict) or b.get('symbol')!=symbol:raise ValueError('schema_changed')
        mark=decimal(b['markPrice']);idx=decimal(b['indexPrice']);fund=decimal(b['lastFundingRate'])
        if mark<=0 or idx<=0:raise ValueError('invalid_price')
        vals={'mark':str(mark),'index':str(idx),'basis_bps':str(10000*(mark-idx)/idx),
              'last_funding_rate':str(fund),'next_funding_time_utc':utc_epoch(b['nextFundingTime']) if b.get('nextFundingTime') else None}
        return envelope('usdm.mark_funding',instrument,vals,'quote_per_base_and_ratio',s,utc_epoch(b['time']) if b.get('time') else None,method='same_venue_mark_index_v1')
    return _run('usdm.mark_funding',instrument,run)

def futures_oi(symbol: str):
    instrument=f'binance:usdm:{symbol}:perp'
    def run():
        valid_symbol(symbol);b,s=fetch('futures_oi',params={'symbol':symbol})
        if not isinstance(b,dict) or b.get('symbol')!=symbol:raise ValueError('schema_changed')
        oi=decimal(b['openInterest'])
        if oi<0:raise ValueError('invalid_oi')
        return envelope('usdm.open_interest',instrument,str(oi),'contract_or_base_units_not_usd',s,utc_epoch(b['time']) if b.get('time') else None,method='exchange_reported_oi_v1')
    return _run('usdm.open_interest',instrument,run)

def futures_funding_history(symbol: str, limit: int=50):
    instrument=f'binance:usdm:{symbol}:perp'
    def run():
        valid_symbol(symbol)
        if not 1<=limit<=200:raise ValueError('invalid_limit')
        b,s=fetch('futures_funding',params={'symbol':symbol,'limit':limit})
        if not isinstance(b,list) or len(b)>limit:raise ValueError('schema_changed')
        out=[]
        for r in b:
            if r.get('symbol')!=symbol:raise ValueError('schema_changed')
            out.append({'time_utc':utc_epoch(r['fundingTime']),'rate':str(decimal(r['fundingRate']))})
        return envelope('usdm.funding.history',instrument,out,'ratio_per_funding_event',s,out[-1]['time_utc'] if out else None,coverage=f'{len(out)}_reported_funding_events')
    return _run('usdm.funding.history',instrument,run)

def defi_protocol(slug: str):
    instrument=f'defillama:protocol:{slug}'
    def run():
        valid_slug(slug);b,s=fetch('defi_protocol',parts={'slug':slug})
        if not isinstance(b,dict) or not isinstance(b.get('tvl'),list):raise ValueError('schema_changed')
        tvl=b['tvl']; last=tvl[-1] if tvl else None
        if not isinstance(last,dict):raise ValueError('schema_changed')
        value=decimal(last['totalLiquidityUSD'])
        if value<0:raise ValueError('invalid_tvl')
        when=datetime_from_seconds(last['date'])
        return envelope('defi.protocol.tvl',instrument,{'tvl_usd':str(value),'name':b.get('name'),'chain_names':b.get('chains',[])},'USD',s,when,coverage='protocol_aggregated_tvl_possible_double_counting')
    return _run('defi.protocol.tvl',instrument,run)

def datetime_from_seconds(value):
    from datetime import datetime,timezone
    return datetime.fromtimestamp(int(value),timezone.utc).isoformat(timespec='seconds').replace('+00:00','Z')

def defi_chain(slug: str, limit: int=100):
    instrument=f'defillama:chain:{slug}'
    def run():
        valid_slug(slug)
        if not 1<=limit<=500:raise ValueError('invalid_limit')
        b,s=fetch('defi_chain',parts={'slug':slug})
        if not isinstance(b,list):raise ValueError('schema_changed')
        out=[]
        for r in b[-limit:]:
            val=decimal(r['tvl'])
            if val<0:raise ValueError('invalid_tvl')
            out.append({'time_utc':datetime_from_seconds(r['date']),'tvl_usd':str(val)})
        return envelope('defi.chain.tvl_history',instrument,out,'USD',s,out[-1]['time_utc'] if out else None,coverage=f'{len(out)}_published_daily_samples')
    return _run('defi.chain.tvl_history',instrument,run)

def dex_pair(chain: str, pair: str):
    instrument=f'dexscreener:{chain}:{pair}'
    def run():
        valid_slug(chain);valid_address(pair,chain)
        b,s=fetch('dex_pair',parts={'chain':chain,'pair':pair})
        matches=b.get('pairs') if isinstance(b,dict) else None
        if not isinstance(matches,list):raise ValueError('schema_changed')
        rows=[r for r in matches if r.get('chainId')==chain and r.get('pairAddress','').lower()==pair.lower()]
        if len(rows)!=1:raise ValueError('schema_changed')
        r=rows[0]
        liquidity=decimal(r['liquidity']['usd']); price=decimal(r['priceUsd'])
        if liquidity<0 or price<=0:raise ValueError('invalid_pool_metrics')
        vals={'price_usd':str(price),'liquidity_usd':str(liquidity),'volume_h24_usd':str(decimal(r.get('volume',{}).get('h24'))) if r.get('volume',{}).get('h24') is not None else None,
              'fdv_usd':str(decimal(r['fdv'])) if r.get('fdv') is not None else None,
              'base_token_address':r['baseToken']['address'],'quote_token_address':r['quoteToken']['address'],
              'pair_created_at_utc':utc_epoch(r['pairCreatedAt']) if r.get('pairCreatedAt') else None}
        return envelope('dex.pair.snapshot',instrument,vals,'USD',s,method='exact_chain_pair_v1',coverage='single_pool_source_rolling_window')
    return _run('dex.pair.snapshot',instrument,run)

def dex_token_pairs(chain: str, token: str, limit: int=25):
    instrument=f'dexscreener:{chain}:token:{token}'
    def run():
        valid_slug(chain);valid_address(token,chain)
        if not 1<=limit<=50:raise ValueError('invalid_limit')
        b,s=fetch('dex_token',parts={'chain':chain,'pair':token})
        if not isinstance(b,list):raise ValueError('schema_changed')
        items=[]
        for r in b:
            if r.get('chainId')!=chain:continue
            base=r.get('baseToken',{}).get('address','').lower()
            quote=r.get('quoteToken',{}).get('address','').lower()
            if token.lower() not in (base,quote):continue
            items.append({'pair':r.get('pairAddress'),'dex':r.get('dexId'),
                          'liquidity_usd':str(decimal(r['liquidity']['usd'])) if r.get('liquidity',{}).get('usd') is not None else None})
        return envelope('dex.token.pairs',instrument,items[:limit],'USD',s,coverage=f'{len(items[:limit])}_pairs_api_list_not_exhaustive')
    return _run('dex.token.pairs',instrument,run)

def btc_mempool():
    def run():
        b,s=fetch('btc_mempool')
        if not isinstance(b,dict):raise ValueError('schema_changed')
        vals={'count':int(b['count']),'vsize':int(b['vsize']),'total_fee_sats':int(b['total_fee'])}
        if min(vals.values())<0:raise ValueError('invalid_mempool')
        return envelope('btc.mempool.snapshot','bitcoin:mainnet',vals,'count_vbyte_satoshi',s)
    return _run('btc.mempool.snapshot','bitcoin:mainnet',run)

def btc_fees():
    def run():
        b,s=fetch('btc_fees')
        if not isinstance(b,dict):raise ValueError('schema_changed')
        vals={k:int(b[k]) for k in ('fastestFee','halfHourFee','hourFee','economyFee','minimumFee')}
        if any(v<=0 for v in vals.values()):raise ValueError('invalid_fee')
        return envelope('btc.fees.recommended','bitcoin:mainnet',vals,'sat/vB_estimate_not_guarantee',s)
    return _run('btc.fees.recommended','bitcoin:mainnet',run)

def btc_tip():
    def run():
        b,s=fetch('btc_tip')
        if not isinstance(b,int) or b<0:raise ValueError('schema_changed')
        return envelope('btc.block_tip','bitcoin:mainnet',b,'blocks',s)
    return _run('btc.block_tip','bitcoin:mainnet',run)

def btc_hashrate(period: str='1m'):
    def run():
        b,s=fetch('btc_hashrate',parts={'period':period})
        if not isinstance(b,dict):raise ValueError('schema_changed')
        rows=b.get('hashrates',[])
        if not isinstance(rows,list) or not rows:raise ValueError('schema_changed')
        sample=rows[-1]
        value=decimal(sample['avgHashrate'])
        if value<=0:raise ValueError('invalid_hashrate')
        return envelope('btc.mining.average_hashrate','bitcoin:mainnet',str(value),'H/s_period_average',s,datetime_from_seconds(sample['timestamp']),coverage=f'provider_average_{period}_not_instantaneous')
    return _run('btc.mining.average_hashrate','bitcoin:mainnet',run)
