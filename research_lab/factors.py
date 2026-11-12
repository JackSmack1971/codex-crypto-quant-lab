"""Explicitly defined elementary crypto research factors; no alpha assertions."""
from decimal import Decimal
from .core import ContractError, _decimal

FACTOR_CONTRACTS={
 'simple_momentum':{'inputs':['past_close','current_close'],'unit':'decimal_return','availability':'both prices must be available at decision'},
 'funding_divergence':{'inputs':['funding_rate','reference_funding_rate'],'unit':'decimal_rate_difference','availability':'same contract/venue and effective interval'},
 'open_interest_change':{'inputs':['past_oi','current_oi'],'unit':'fraction_change','availability':'same venue/contract and quantity denomination'},
 'basis':{'inputs':['futures_price','spot_price'],'unit':'fraction_spot','availability':'matched underlying/quote and sampled interval'},
 'relative_spread':{'inputs':['best_ask','best_bid'],'unit':'fraction_mid','availability':'same venue/order book timestamp'},
}

def calculate_factor(name: str, inputs: dict[str,str]) -> str:
    if name not in FACTOR_CONTRACTS:raise ContractError('unknown factor')
    expected=set(FACTOR_CONTRACTS[name]['inputs'])
    if set(inputs)!=expected:raise ContractError('factor input mismatch')
    d={k:_decimal(v) for k,v in inputs.items()}
    if name=='simple_momentum':
        if d['past_close']<=0:raise ContractError('nonpositive reference price')
        value=d['current_close']/d['past_close']-1
    elif name=='funding_divergence':value=d['funding_rate']-d['reference_funding_rate']
    elif name=='open_interest_change':
        if d['past_oi']<=0:raise ContractError('nonpositive past OI')
        value=d['current_oi']/d['past_oi']-1
    elif name=='basis':
        if d['spot_price']<=0:raise ContractError('nonpositive spot')
        value=d['futures_price']/d['spot_price']-1
    else:
        if d['best_ask']<d['best_bid'] or d['best_bid']<=0:raise ContractError('crossed/invalid book')
        value=2*(d['best_ask']-d['best_bid'])/(d['best_ask']+d['best_bid'])
    return str(value)
