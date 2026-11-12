"""Composable deterministic research-only strategies and causal scenario replay.

No portfolio holdings, orders, broker interface, or trading authorization exists.
Scores express research exposure, not calibrated confidence or future returns.
"""
from __future__ import annotations
from dataclasses import dataclass
from decimal import Decimal
from typing import Iterable
from .core import ContractError, ResearchSignal, Observation, _decimal, _utc, validate_replay


@dataclass(frozen=True)
class StrategySpec:
    id: str
    model_weights: tuple[tuple[str, str], ...]
    threshold: str = '0'
    minimum_models: int = 1

    def __post_init__(self):
        if not self.id or not self.model_weights or self.minimum_models < 1:
            raise ContractError('invalid strategy specification')
        if len({k for k,_ in self.model_weights}) != len(self.model_weights):
            raise ContractError('duplicate strategy model')
        for model, weight in self.model_weights:
            if not model or _decimal(weight) <= 0:
                raise ContractError('model weights must be positive')
        if _decimal(self.threshold)<0 or _decimal(self.threshold)>1:
            raise ContractError('invalid threshold')
        if self.minimum_models>len(self.model_weights):
            raise ContractError('minimum_models exceeds model count')


@dataclass(frozen=True)
class ClosedBar:
    instrument_id: str
    closed_at_utc: str
    available_at_utc: str
    close: str

    def __post_init__(self):
        if not self.instrument_id or _decimal(self.close)<=0:
            raise ContractError('invalid close bar')
        if _utc(self.available_at_utc) < _utc(self.closed_at_utc):
            raise ContractError('bar available before close')


def blend_signals(spec: StrategySpec, signals: Iterable[ResearchSignal],
                  observations: Iterable[Observation], cutoff_utc: str,
                  instrument_id: str) -> Decimal | None:
    """Weighted mean over available requested models, with missing coverage gate."""
    choices={}
    valid=list(signals)
    used=[s for s in valid if s.instrument_id==instrument_id and s.as_of_utc==cutoff_utc]
    obs=list(observations)
    eligible=set()
    for signal in used:
        if signal.status == 'CALCULATED' and validate_replay([signal],obs,cutoff_utc)['status']=='ELIGIBLE':
            eligible.add(signal.id)
    for signal in used:
        if signal.id in eligible:
            if signal.model_id in choices:
                raise ContractError('more than one signal per model and bar')
            choices[signal.model_id]=signal
    weights=dict(spec.model_weights)
    matched=[(weights[m],choices[m]) for m in weights if m in choices]
    if len(matched)<spec.minimum_models:
        return None
    total=sum((_decimal(w) for w,_ in matched),Decimal('0'))
    score=sum((_decimal(w)*_decimal(s.value) for w,s in matched),Decimal('0'))/total
    return Decimal('0') if abs(score)<_decimal(spec.threshold) else score


def replay_strategy(spec: StrategySpec, bars: list[ClosedBar], signals: list[ResearchSignal],
                    observations: list[Observation], *, fee_bps: str='0',
                    spread_bps: str='0', slippage_bps: str='0') -> dict:
    """Evaluate each bar's signal on the *next* available bar's close-to-close return.

    A scenario calculation, not a fill model. Costs apply to research exposure
    turnover at the next interval; price path must have verified closed bars.
    """
    costs=[_decimal(x) for x in (fee_bps,spread_bps,slippage_bps)]
    if any(x<0 for x in costs):raise ContractError('negative transaction costs')
    if len(bars)<2:raise ContractError('at least two bars required')
    if len({b.instrument_id for b in bars})!=1:
        raise ContractError('mixed instruments in scenario')
    if any(_utc(a.closed_at_utc)>=_utc(b.closed_at_utc) or
           _utc(a.available_at_utc)>_utc(b.closed_at_utc)
           for a,b in zip(bars,bars[1:])):
        raise ContractError('bars out of order or unavailable by next decision')
    result=[]; prev_position=Decimal('0'); equity=Decimal('1')
    for previous, following in zip(bars,bars[1:]):
        # Signal must exist at an explicitly supported bar as-of; late bar
        # availability cannot be used as a signal formed earlier.
        score=blend_signals(spec,signals,observations,previous.available_at_utc,previous.instrument_id)
        if score is None:
            # Do not silently treat missing evidence as a flat signal.
            result.append({'period_end':following.closed_at_utc,'status':'NO_DATA',
                           'return':None,'equity':None})
            break
        target=score  # bounded [-1,+1] research exposure, not order sizing
        gross=target*(_decimal(following.close)/_decimal(previous.close)-Decimal('1'))
        turnover=abs(target-prev_position)
        expense=turnover*(costs[0]+costs[1]/2+costs[2])/Decimal('10000')
        net=gross-expense
        equity*=Decimal('1')+net
        if equity<=0:
            raise ContractError('scenario equity exhausted')
        result.append({'period_end':following.closed_at_utc,'status':'CALCULATED',
                       'exposure':str(target),'gross_return':str(gross),
                       'cost_return':str(expense),'return':str(net),'equity':str(equity)})
        prev_position=target
    return {'schema':'research-scenario.v1','strategy_id':spec.id,
            'instrument_id':bars[0].instrument_id,'units':'decimal_return',
            'cost_assumptions_bps':{'fee':fee_bps,'spread':spread_bps,'slippage':slippage_bps},
            'periods':result,'final_equity':str(equity),
            'status':'INSUFFICIENT_EVIDENCE' if len(result)<len(bars)-1 or any(x['status']=='NO_DATA' for x in result) else 'RESEARCH_SIMULATION_ONLY',
            'limitations':['close-to-close proxy, not executable fills',
                           'missing observations create unevaluated periods',
                           'data survivorship, venue coverage and selection bias require external review']}


def cost_sensitivity(spec: StrategySpec, bars: list[ClosedBar], signals: list[ResearchSignal],
                     observations: list[Observation], scenarios: list[dict[str,str]], *, max_scenarios: int=64) -> dict:
    """Replay fixed evidence across bounded, explicitly hypothetical cost assumptions."""
    if not scenarios or len(scenarios)>max_scenarios or len({tuple(sorted(s.items())) for s in scenarios})!=len(scenarios):
        raise ContractError('invalid/duplicate cost grid or budget exceeded')
    results=[]
    for assumption in scenarios:
        if set(assumption)!={'fee_bps','spread_bps','slippage_bps'}:
            raise ContractError('each cost scenario must specify all three cost types')
        if any(_decimal(value)<0 or _decimal(value)>Decimal('1000') for value in assumption.values()):
            raise ContractError('cost outside bounded research grid')
        replay=replay_strategy(spec,bars,signals,observations,**assumption)
        results.append({'assumptions_bps':assumption,'status':replay['status'],
                        'final_equity':replay['final_equity'] if replay['status']=='RESEARCH_SIMULATION_ONLY' else None,
                        'simulation':replay})
    return {'schema':'research-cost-grid.v1','scenarios':results,
            'limitations':['hypothetical costs; no executable order-book/fill validation',
                           'missing evidence leaves final outcome unavailable']}
