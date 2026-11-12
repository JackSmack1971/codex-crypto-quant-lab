"""Small closed registry of deterministic research models over archived observations.

These models normalize evidence, not returns; they cannot infer market edge.
"""
from __future__ import annotations
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from .core import Observation, ResearchSignal, ContractError, _decimal, _utc, digest

MODEL_KINDS=('signed_scalar','difference_scaled')


@dataclass(frozen=True)
class ModelSpec:
    id: str
    kind: str
    metric: str
    unit: str
    scale: str = '1'
    max_age_seconds: int = 3600
    horizon: str = '1h'

    def __post_init__(self):
        if not all(isinstance(s,str) and s.strip() for s in (self.id,self.metric,self.unit,self.horizon)):
            raise ContractError('invalid model identity')
        if self.kind not in MODEL_KINDS or _decimal(self.scale)<=0 or self.max_age_seconds<=0:
            raise ContractError('unsupported model or invalid scale/age')
        if self.kind=='signed_scalar' and self.unit!='dimensionless':
            raise ContractError('signed_scalar requires dimensionless normalized input')


def calculate_model(model: ModelSpec, observations: list[Observation],
                    instrument_id: str, as_of_utc: str) -> ResearchSignal:
    cutoff=_utc(as_of_utc)
    qualified=[o for o in observations if o.instrument_id==instrument_id and
               o.metric==model.metric and o.unit==model.unit and o.status=='OBSERVED' and
               o.available_at_utc and o.observed_at_utc and
               _utc(o.available_at_utc)<=cutoff and _utc(o.observed_at_utc)<=cutoff and
               timedelta(0)<=cutoff-_utc(o.observed_at_utc)<=timedelta(seconds=model.max_age_seconds)]
    qualified.sort(key=lambda o:(_utc(o.observed_at_utc),o.id))
    n=1 if model.kind=='signed_scalar' else 2
    selected=qualified[-n:]
    sufficient=len(selected)==n and len({o.observed_at_utc for o in selected})==n
    if sufficient:
        if model.kind=='signed_scalar':
            value=_decimal(selected[-1].value)/_decimal(model.scale)
        else:
            value=(_decimal(selected[-1].value)-_decimal(selected[-2].value))/_decimal(model.scale)
        value=max(Decimal('-1'),min(Decimal('1'),value))
        score=str(value); status='CALCULATED'
    else:
        score=None; status='INSUFFICIENT_EVIDENCE'
    selected_ids=tuple(o.id for o in selected) if sufficient else ()
    fingerprint={'model':model.__dict__,'instrument_id':instrument_id,
                 'as_of_utc':as_of_utc,'observation_ids':selected_ids,'score':score}
    return ResearchSignal('model:'+digest(fingerprint),model.id,instrument_id,model.horizon,
                          as_of_utc,score,selected_ids,'research-model.v1',
                          'Deterministic evidence transform; uncalibrated, not a trade signal',status)
