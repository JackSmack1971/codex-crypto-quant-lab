"""Descriptive equal-period portfolio and correlation diagnostics only."""
from math import isfinite,sqrt
from statistics import mean
from .core import ContractError

def portfolio_risk(returns: dict[str,list[float]], weights: dict[str,float]) -> dict:
    if not returns or set(weights)!=set(returns) or len({len(v) for v in returns.values()})!=1:
        raise ContractError('unmatched asset returns/weights')
    n=len(next(iter(returns.values())))
    if n<3 or any(type(w) not in (float,int) or not isfinite(w) for w in weights.values()) or abs(sum(weights.values())-1)>1e-9:
        raise ContractError('invalid portfolio weights/sample')
    if any(type(x) not in (float,int) or not isfinite(x) for seq in returns.values() for x in seq):
        raise ContractError('non-finite returns')
    names=sorted(returns)
    centered={k:[x-mean(returns[k]) for x in returns[k]] for k in names}
    cov={(a,b):sum(x*y for x,y in zip(centered[a],centered[b]))/(n-1) for a in names for b in names}
    var=sum(weights[a]*weights[b]*cov[(a,b)] for a in names for b in names)
    if var< -1e-12:raise ContractError('invalid covariance')
    var=max(0,var); contributions={a:weights[a]*sum(weights[b]*cov[(a,b)] for b in names) for a in names}
    return {'schema':'descriptive-portfolio-risk.v1','sample_periods':n,'portfolio_period_variance':var,
            'portfolio_period_volatility':sqrt(var),'variance_contributions':contributions,
            'correlations':{a:{b:(cov[(a,b)]/sqrt(cov[(a,a)]*cov[(b,b)]) if cov[(a,a)]>0 and cov[(b,b)]>0 else None) for b in names} for a in names},
            'limitations':['aligned same-frequency observed return periods required','historical covariance not a forward risk forecast']}
