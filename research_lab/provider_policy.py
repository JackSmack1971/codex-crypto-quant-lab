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
    coverage: str | None = None  # when set, a candidate must declare exactly this coverage


def qualified_fallback(required: MetricContract, candidates: list[dict], as_of_utc: str, *,max_age_seconds: int,
                       allow_stale_cache: bool=False, require_retrieval_time: bool=False) -> dict:
    """Select the first candidate with identical metric semantics. Never converts or approximates.

    A candidate qualifies only if instrument, metric, unit, venue, interval, method (and coverage,
    when required) are identical, it is OBSERVED with observation/availability/retrieval times,
    observed<=available<=retrieved<=as_of is not required for retrieval but no timestamp may
    post-date as_of, and it is within the staleness budget. Candidates served from STALE_CACHE
    are rejected unless explicitly allowed."""
    cutoff=_utc(as_of_utc)
    if max_age_seconds<0:raise ContractError('invalid staleness budget')
    decisions=[]
    for c in candidates:
        identity=c.get('contract',{})
        keys=['instrument_id','metric','unit','venue','interval','method']+(['coverage'] if required.coverage is not None else [])
        if any(identity.get(k)!=getattr(required,k) for k in keys):
            decisions.append({'provider':c.get('provider'),'status':'INCOMPATIBLE'});continue
        if (c.get('cache') or {}).get('status')=='STALE_CACHE' and not allow_stale_cache:
            decisions.append({'provider':c.get('provider'),'status':'STALE_CACHE_REJECTED'});continue
        try:
            if c.get('status')!='OBSERVED' or not c.get('observed_at_utc') or not c.get('available_at_utc'):
                decisions.append({'provider':c.get('provider'),'status':'UNAVAILABLE'});continue
            age=(cutoff-_utc(c['observed_at_utc'])).total_seconds()
            missing_retrieval=c.get('retrieved_at_utc') is None
            if (missing_retrieval and require_retrieval_time) or (not missing_retrieval and _utc(c['retrieved_at_utc'])>cutoff):
                decisions.append({'provider':c.get('provider'),'status':'STALE_OR_FUTURE'});continue
            if _utc(c['available_at_utc'])<_utc(c['observed_at_utc']):
                decisions.append({'provider':c.get('provider'),'status':'INVALID_TIMESTAMP'});continue
            if age<0 or age>max_age_seconds or _utc(c['available_at_utc'])>cutoff:
                decisions.append({'provider':c.get('provider'),'status':'STALE_OR_FUTURE'});continue
        except (ValueError,TypeError):
            decisions.append({'provider':c.get('provider'),'status':'INVALID_TIMESTAMP'});continue
        decisions.append({'provider':c.get('provider'),'status':'QUALIFIED',
                          'warnings':['retrieval_time_missing'] if missing_retrieval else []})
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
