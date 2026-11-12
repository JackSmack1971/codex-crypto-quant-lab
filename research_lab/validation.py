"""Leakage-aware temporal splits and exploratory overfitting diagnostics.

CPCV folds and CSCV/PBO are different tools; do not call this a fitted model
or interpret a point-estimate PBO as calibrated future profitability.
"""
from __future__ import annotations
from dataclasses import dataclass
from itertools import combinations
from math import comb, isfinite, log
from statistics import mean
from .core import ContractError, _utc


@dataclass(frozen=True)
class LabelWindow:
    start_utc: str
    end_utc: str

    def __post_init__(self):
        if _utc(self.end_utc)<_utc(self.start_utc):
            raise ContractError('negative label interval')


def _windows(windows: list[LabelWindow]):
    if not windows or any(_utc(a.start_utc)>=_utc(b.start_utc) for a,b in zip(windows,windows[1:])):
        raise ContractError('windows must have strictly increasing start dates')


def walk_forward_splits(windows: list[LabelWindow], train_size: int,
                        test_size: int, *, step: int|None=None,
                        embargo: int=0) -> list[tuple[tuple[int,...],tuple[int,...]]]:
    """Expanding? No: fixed rolling window, embargo by sample count and label overlap."""
    _windows(windows)
    if min(train_size,test_size)<=0 or embargo<0 or (step is not None and step<=0):
        raise ContractError('invalid temporal split parameters')
    n=len(windows); step=step or test_size
    splits=[]
    for start in range(train_size+embargo,n-test_size+1,step):
        test=tuple(range(start,start+test_size))
        train=tuple(i for i in range(start-embargo-train_size,start-embargo)
                    if _utc(windows[i].end_utc) < _utc(windows[test[0]].start_utc))
        if train:
            splits.append((train,test))
    return splits


def cpcv_splits(windows: list[LabelWindow], n_groups: int,
                n_test_groups: int, *, embargo: int=0,
                max_splits: int=10000) -> list[tuple[tuple[int,...],tuple[int,...]]]:
    """Contiguous groups with purged overlapping train labels and post-test embargo.

    The method returns indices only; independent model fitting/scoring and
    construction of non-overlapping OOS paths are deliberately separate.
    """
    _windows(windows)
    n=len(windows)
    if n_groups<3 or not 1<=n_test_groups<n_groups or n<n_groups or embargo<0:
        raise ContractError('invalid CPCV group parameters')
    if comb(n_groups,n_test_groups)>max_splits:
        raise ContractError('split count exceeds budget')
    groups=[]
    for g in range(n_groups):
        lo=g*n//n_groups; hi=(g+1)*n//n_groups
        groups.append(set(range(lo,hi)))
    splits=[]
    for chosen in combinations(range(n_groups),n_test_groups):
        test=set().union(*(groups[g] for g in chosen))
        train=[]
        for i in range(n):
            if i in test:continue
            wi=windows[i]
            # A training label must not intersect any test label interval.
            overlap=any(_utc(wi.start_utc)<=_utc(windows[j].end_utc) and
                        _utc(wi.end_utc)>=_utc(windows[j].start_utc) for j in test)
            after_test=any(j<i<=j+embargo for j in test)
            if not overlap and not after_test:
                train.append(i)
        if train:
            splits.append((tuple(train),tuple(sorted(test))))
    return splits


def cscv_pbo(returns_by_candidate: dict[str,list[float]], n_groups: int=8,
             *, max_splits: int=10000) -> dict:
    """CSCV PBO: rank IS-selected candidate out of sample across symmetric halves.

    Score is mean period return, not Sharpe or calibrated model performance.
    Each row must refer to the same period for every candidate. No lookahead
    is prevented by this scoring function alone; data preparation must do so.
    """
    if len(returns_by_candidate)<2 or n_groups<4 or n_groups%2 or not returns_by_candidate:
        raise ContractError('PBO requires >=2 predeclared candidates and even >=4 groups')
    sizes={len(x) for x in returns_by_candidate.values()}
    if len(sizes)!=1 or next(iter(sizes))<n_groups:
        raise ContractError('candidate series length mismatch or insufficient samples')
    for values in returns_by_candidate.values():
        if any(not isinstance(v,(int,float)) or isinstance(v,bool) or not isfinite(v) for v in values):
            raise ContractError('invalid return series')
    if comb(n_groups,n_groups//2)>max_splits:
        raise ContractError('PBO split count exceeds budget')
    n=next(iter(sizes)); names=sorted(returns_by_candidate)
    groups=[list(range(g*n//n_groups,(g+1)*n//n_groups)) for g in range(n_groups)]
    logits=[]; failures=0
    for c in combinations(range(n_groups),n_groups//2):
        ins=[i for g in c for i in groups[g]]
        outs=[i for g in range(n_groups) if g not in c for i in groups[g]]
        is_scores={name:mean(returns_by_candidate[name][i] for i in ins) for name in names}
        os_scores={name:mean(returns_by_candidate[name][i] for i in outs) for name in names}
        winner=sorted(names,key=lambda s:(-is_scores[s],s))[0]
        # Midrank ties, normalized on (0,1), increasing with OOS performance.
        below=sum(os_scores[name]<os_scores[winner] for name in names)
        tied=sum(os_scores[name]==os_scores[winner] for name in names)
        relative_rank=(below+(tied+1)/2)/(len(names)+1)
        logits.append(log(relative_rank/(1-relative_rank)))
        failures+=relative_rank<.5
    return {'schema':'exploratory-cscv-pbo.v1','candidate_ids':names,
            'period_count':n,'group_count':n_groups,'split_count':len(logits),
            'pbo_estimate':failures/len(logits),
            'rank_logits':logits,'score':'mean_period_return',
            'limitations':['CSCV PBO, not CPCV folds or an OOS return forecast',
                           'candidate preselection, regime changes, dependencies and costs may bias estimate',
                           'ties are ranked at midrank and cannot prove absence of overfitting']}


def select_and_evaluate_folds(returns_by_candidate: dict[str,list[float]],
                              folds: list[tuple[tuple[int,...],tuple[int,...]]]) -> dict:
    """Evaluate predeclared *fixed* candidate return streams on temporal folds.

    IS selection uses only training indices; OOS is never used to select.
    Any fitted models must instead be trained afresh inside each training fold.
    """
    if not returns_by_candidate or len({len(v) for v in returns_by_candidate.values()})!=1:
        raise ContractError('incompatible candidate return streams')
    n=len(next(iter(returns_by_candidate.values())))
    if not folds:raise ContractError('empty folds')
    results=[]
    for train,test in folds:
        if not train or not test or set(train)&set(test) or min((*train,*test))<0 or max((*train,*test))>=n:
            raise ContractError('invalid fold')
        if any(not isfinite(v) for seq in returns_by_candidate.values() for v in seq):
            raise ContractError('non-finite return')
        is_scores={name:mean(returns_by_candidate[name][i] for i in train) for name in returns_by_candidate}
        best=sorted(is_scores,key=lambda name:(-is_scores[name],name))[0]
        oos=mean(returns_by_candidate[best][i] for i in test)
        results.append({'train_indices':list(train),'test_indices':list(test),
                        'selected_candidate':best,'train_mean_return':is_scores[best],
                        'test_mean_return':oos})
    return {'schema':'research-crossvalidation.v1','folds':results,'mean_test_return_across_folds':mean(x['test_mean_return'] for x in results),
            'limitations':['OOS test windows may overlap across CPCV folds; mean is not a tradable return',
                           'fixed candidate returns only: no model fitting or leakage check within features',
                           'candidate search universe must be predeclared']}
