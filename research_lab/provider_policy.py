"""Comparable-source qualification and explicit provider diagnostic status."""
from dataclasses import dataclass
from .core import ContractError, _utc

@dataclass(frozen=True)
class MetricContract:
    instrument_id: str
    metric: str
    unit: str
    venue: str
    interval: str
    method: str


def qualified_fallback(required: MetricContract, candidates: list[dict], as_of_utc: str, *,max_age_seconds: int) -> dict:
    cutoff=_utc(as_of_utc)
    if max_age_seconds<0:raise ContractError('invalid staleness budget')
    decisions=[]
    for c in candidates:
        identity=c.get('contract',{})
        if any(identity.get(k)!=getattr(required,k) for k in ('instrument_id','metric','unit','venue','interval','method')):
            decisions.append({'provider':c.get('provider'),'status':'INCOMPATIBLE'});continue
        try:
            if c.get('status')!='OBSERVED' or not c.get('observed_at_utc') or not c.get('available_at_utc'):
                decisions.append({'provider':c.get('provider'),'status':'UNAVAILABLE'});continue
            age=(cutoff-_utc(c['observed_at_utc'])).total_seconds()
            if age<0 or age>max_age_seconds or _utc(c['available_at_utc'])>cutoff:
                decisions.append({'provider':c.get('provider'),'status':'STALE_OR_FUTURE'});continue
        except (ValueError,TypeError):
            decisions.append({'provider':c.get('provider'),'status':'INVALID_TIMESTAMP'});continue
        decisions.append({'provider':c.get('provider'),'status':'QUALIFIED'})
        return {'status':'QUALIFIED','candidate':c,'decisions':decisions}
    return {'status':'INSUFFICIENT_EVIDENCE','candidate':None,'decisions':decisions}


def provider_health(events: list[dict]) -> dict:
    """Observed diagnostics, not a license to substitute semantically distinct sources."""
    report={}
    for e in events:
        name=e['provider']; status=e['status']
        if status not in ('SUCCESS','TIMEOUT','RATE_LIMITED','ERROR'):
            raise ContractError('unknown provider event')
        item=report.setdefault(name,{'total':0,'successful':0,'rate_limited':0,'timeouts':0})
        item['total']+=1
        item['successful']+=status=='SUCCESS';item['rate_limited']+=status=='RATE_LIMITED';item['timeouts']+=status=='TIMEOUT'
    for item in report.values():
        item['observed_success_fraction']=item['successful']/item['total']
    return report
