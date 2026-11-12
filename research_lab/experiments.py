"""Train-only selection, frozen experiment identity, and final holdout evaluation.

Fixed candidate return streams only; no fitted estimators or PIT certification.
"""
from dataclasses import dataclass, asdict
from math import isfinite
from statistics import mean
from .core import ContractError, digest, _utc
from .validation import LabelWindow

@dataclass(frozen=True)
class ExperimentPlan:
    experiment_id: str
    train_end: int
    holdout_start: int
    embargo: int = 0


def freeze_candidate(plan: ExperimentPlan, windows: list[LabelWindow], candidates: dict[str,list[float]]) -> dict:
    n=len(windows)
    if not plan.experiment_id or not (2<=plan.train_end<plan.holdout_start<n) or plan.embargo<0:
        raise ContractError('invalid experiment partitions')
    if not candidates or any(len(v)!=n or any(type(x) not in (int,float) or not isfinite(x) for x in v) for v in candidates.values()):
        raise ContractError('unaligned or invalid candidate streams')
    if any(_utc(a.start_utc)>=_utc(b.start_utc) for a,b in zip(windows,windows[1:])):
        raise ContractError('unordered windows')
    # Purge training labels crossing selection cut and enforce sample-count embargo.
    train=[i for i in range(max(0,plan.train_end-plan.embargo)) if _utc(windows[i].end_utc)<_utc(windows[plan.train_end].start_utc)]
    if len(train)<2:raise ContractError('insufficient purged training coverage')
    scores={name:mean(values[i] for i in train) for name,values in candidates.items()}
    selected=sorted(scores,key=lambda name:(-scores[name],name))[0]
    frozen={'schema':'frozen-candidate.v1','plan':asdict(plan),'selected':selected,'training_scores':scores,
            'train_indices':train,'windows_digest':digest([asdict(w) for w in windows]),
            'candidate_training_digest':digest({k:v[:plan.train_end] for k,v in sorted(candidates.items())})}
    return {'freeze_digest':digest(frozen),'frozen':frozen}


def evaluate_frozen(freeze: dict, plan: ExperimentPlan, windows: list[LabelWindow],
                    candidates: dict[str,list[float]], *, registered_digest: str) -> dict:
    """Require externally registered freeze identity before consuming holdout data."""
    recomputed=freeze_candidate(plan,windows,candidates)
    if registered_digest!=recomputed['freeze_digest'] or freeze!=recomputed:
        raise ContractError('freeze mismatch (possible selection leakage)')
    n=len(windows); selected=freeze['frozen']['selected']
    if any(len(v)!=n for v in candidates.values()):raise ContractError('series mismatch')
    validation=[i for i in range(plan.train_end,max(plan.train_end,plan.holdout_start-plan.embargo))
                if _utc(windows[i].end_utc)<_utc(windows[plan.holdout_start].start_utc)]
    if not validation:raise ContractError('no valid validation periods')
    return {'schema':'frozen-holdout-evaluation.v1','freeze_digest':registered_digest,
            'selected':selected,'validation_mean_return':mean(candidates[selected][i] for i in validation),
            'holdout_mean_return':mean(candidates[selected][i] for i in range(plan.holdout_start,n)),
            'holdout_indices':list(range(plan.holdout_start,n)),
            'limitations':['fixed candidate streams; independent PIT verification not included',
                           'freeze registry and no-reuse holdout policy must be enforced outside this function',
                           'mean returns are not realizable trading returns']}
