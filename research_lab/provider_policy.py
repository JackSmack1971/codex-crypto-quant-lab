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


CACHE_STATUSES = ('LIVE', 'FRESH_CACHE', 'STALE_CACHE')


def _cache_state(candidate: dict, require: bool):
    """Return (status, problem). A candidate that carries cache metadata must carry *valid* metadata;
    a missing or contradictory flag is never read as LIVE."""
    if 'cache' not in candidate or candidate['cache'] is None:
        return (None, 'CACHE_STATE_MISSING') if require else ('UNSPECIFIED', None)
    meta = candidate['cache']
    if not isinstance(meta, dict) or meta.get('status') not in CACHE_STATUSES:
        return None, 'INVALID_CACHE_METADATA'
    served = meta.get('served_from_cache')
    if served is not None and (not isinstance(served, bool) or served != (meta['status'] != 'LIVE')):
        return None, 'INVALID_CACHE_METADATA'
    if meta['status'] != 'LIVE' and not candidate.get('retrieved_at_utc'):
        return None, 'INVALID_CACHE_METADATA'  # cache age cannot be judged without the original retrieval time
    return meta['status'], None


def qualified_fallback(required: MetricContract, candidates: list[dict], as_of_utc: str, *,max_age_seconds: int,
                       allow_stale_cache: bool=False, require_retrieval_time: bool=False,
                       require_cache_state: bool=False) -> dict:
    """Select the first candidate with identical metric semantics. Never converts or approximates.

    Four distinct times are kept apart: ``observed_at_utc`` (when the provider says the value applies),
    ``available_at_utc`` (earliest documented time it was public), ``retrieved_at_utc`` (when this system
    fetched it; it never substitutes for availability) and ``as_of_utc`` (the evaluation time).
    A candidate qualifies only if instrument, metric, unit, venue, interval, method (and coverage, when
    required) are identical, it is OBSERVED with observation and availability times, and
    observed <= available <= as_of, observed <= retrieved <= as_of (any supplied retrieval time must be
    consistent with that order), age = as_of - observed is within the staleness budget, and its cache
    metadata is valid. A STALE_CACHE candidate is rejected unless explicitly allowed; cache metadata that
    is present but invalid or contradictory is rejected, never treated as a live observation. Absent cache
    metadata is reported as warning ``cache_state_unspecified`` (or rejected with ``require_cache_state``)."""
    cutoff=_utc(as_of_utc)
    if max_age_seconds<0:raise ContractError('invalid staleness budget')
    decisions=[]
    for c in candidates:
        identity=c.get('contract',{})
        keys=['instrument_id','metric','unit','venue','interval','method']+(['coverage'] if required.coverage is not None else [])
        if any(identity.get(k)!=getattr(required,k) for k in keys):
            decisions.append({'provider':c.get('provider'),'status':'INCOMPATIBLE'});continue
        cache_status,cache_problem=_cache_state(c,require_cache_state)
        if cache_problem:
            decisions.append({'provider':c.get('provider'),'status':cache_problem});continue
        if cache_status=='STALE_CACHE' and not allow_stale_cache:
            decisions.append({'provider':c.get('provider'),'status':'STALE_CACHE_REJECTED'});continue
        try:
            if c.get('status')!='OBSERVED' or not c.get('observed_at_utc') or not c.get('available_at_utc'):
                decisions.append({'provider':c.get('provider'),'status':'UNAVAILABLE'});continue
            observed,available=_utc(c['observed_at_utc']),_utc(c['available_at_utc'])
            age=(cutoff-observed).total_seconds()
            missing_retrieval=c.get('retrieved_at_utc') is None
            retrieved=None if missing_retrieval else _utc(c['retrieved_at_utc'])
            if missing_retrieval and require_retrieval_time:
                decisions.append({'provider':c.get('provider'),'status':'STALE_OR_FUTURE'});continue
            if available<observed or (retrieved is not None and (retrieved<observed or retrieved<available)):
                decisions.append({'provider':c.get('provider'),'status':'INVALID_TIMESTAMP'});continue
            if (retrieved is not None and retrieved>cutoff) or age<0 or age>max_age_seconds or available>cutoff:
                decisions.append({'provider':c.get('provider'),'status':'STALE_OR_FUTURE'});continue
        except (ValueError,TypeError):
            decisions.append({'provider':c.get('provider'),'status':'INVALID_TIMESTAMP'});continue
        warnings=(['retrieval_time_missing'] if missing_retrieval else [])+(['cache_state_unspecified'] if cache_status=='UNSPECIFIED' else [])
        decisions.append({'provider':c.get('provider'),'status':'QUALIFIED','warnings':warnings})
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
