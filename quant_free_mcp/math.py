"""Finite, unit-aware spot microstructure calculations."""
from .core import decimal

def book_metrics(bids, asks):
    if not bids or not asks:
        raise ValueError('empty_book')
    b = [(decimal(p),decimal(q)) for p,q in bids]
    a = [(decimal(p),decimal(q)) for p,q in asks]
    if any(p <= 0 or q <= 0 for p,q in b+a):
        raise ValueError('nonpositive_book_value')
    if any(b[i][0] < b[i+1][0] for i in range(len(b)-1)) or any(a[i][0]>a[i+1][0] for i in range(len(a)-1)):
        raise ValueError('unsorted_book')
    if b[0][0] >= a[0][0]:
        raise ValueError('crossed_book')
    mid = (b[0][0]+a[0][0])/2
    bq=sum((q for _,q in b), decimal('0'))
    aq=sum((q for _,q in a), decimal('0'))
    if bq+aq <= 0: raise ValueError('zero_depth')
    imbalance=(bq-aq)/(bq+aq)
    return {'mid':str(mid),'spread_bps':str(10000*(a[0][0]-b[0][0])/mid),
            'imbalance_base_qty':str(imbalance),'bid_base_qty':str(bq),'ask_base_qty':str(aq),
            'levels_bid':len(b),'levels_ask':len(a)}

def trade_metrics(trades):
    if not trades: raise ValueError('empty_trade_sample')
    buy=sell=notional=quantity=decimal('0')
    times=[]
    for t in trades:
        price=decimal(t['price']); qty=decimal(t['qty'])
        if price <= 0 or qty <= 0 or not isinstance(t['isBuyerMaker'],bool):
            raise ValueError('invalid_trade')
        q=price*qty
        if t['isBuyerMaker']: sell+=q
        else: buy+=q
        notional+=q;quantity+=qty
        times.append(int(t['time']))
    if quantity<=0:raise ValueError('zero_quantity')
    return {'vwap_quote_per_base':str(notional/quantity),'taker_buy_quote':str(buy),
            'taker_sell_quote':str(sell),'trade_delta_quote':str(buy-sell),
            'sample_count':len(trades),'start_ms':min(times),'end_ms':max(times)}
