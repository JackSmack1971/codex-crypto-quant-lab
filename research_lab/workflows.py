"""Offline end-to-end orchestrator for research snapshots and scenario reports.

Caller supplies provider-grounded evidence and historical bars. This module
makes no network or brokerage calls and no claim about model calibration.
"""
from __future__ import annotations
from dataclasses import asdict
from pathlib import Path
from typing import Any
from .core import Observation, ResearchSignal, ContractError, _utc, canonical_json, digest, validate_replay
from .archive import EvidenceArchive
from .ledger_sqlite import TransactionalResearchLedger
from .strategies import StrategySpec, ClosedBar, replay_strategy
from .models import ModelSpec, calculate_model
from .validation import LabelWindow, walk_forward_splits, cpcv_splits, cscv_pbo, select_and_evaluate_folds


def execute_research_run(payload: dict[str,Any], *, archive_path: str|Path,
                         ledger_path: str|Path) -> dict[str, Any]:
    """Validate and replay a user-supplied immutable research dataset.

    Writes occur only after deterministic validation. Archive and ledger are
    separate SQLite transactions: interrupted runs can leave archived evidence
    but no research ledger record; rerun is safe if run_id not yet recorded.
    """
    if not isinstance(payload,dict) or payload.get('schema')!='research-run-input.v1':
        raise ContractError('invalid research run schema')
    run_id=payload.get('run_id'); ts=payload.get('as_of_utc')
    if not isinstance(run_id,str) or not run_id.strip():raise ContractError('run_id missing')
    _utc(ts)
    observations=[Observation(**row) for row in payload.get('observations',[])]
    signals=[ResearchSignal(**{**row,'observation_ids':tuple(row['observation_ids'])})
             for row in payload.get('signals',[])]
    spec_data=payload.get('strategy',{})
    spec=StrategySpec(id=spec_data['id'],
                      model_weights=tuple(tuple(pair) for pair in spec_data['model_weights']),
                      threshold=spec_data.get('threshold','0'),
                      minimum_models=spec_data.get('minimum_models',1))
    bars=[ClosedBar(**row) for row in payload.get('bars',[])]
    if payload.get('models'):
        models=[ModelSpec(**model) for model in payload['models']]
        if len({m.id for m in models})!=len(models):
            raise ContractError('duplicate model id')
        if signals:
            raise ContractError('choose provided signals or deterministic models, not both')
        for bar in bars[:-1]:
            for model in models:
                signals.append(calculate_model(model,observations,bar.instrument_id,bar.available_at_utc))
    if len({o.id for o in observations})!=len(observations):
        raise ContractError('duplicate observation ids')
    if len({s.id for s in signals})!=len(signals):
        raise ContractError('duplicate signal ids')
    if any(_utc(s.as_of_utc)>_utc(ts) for s in signals):
        raise ContractError('signal is after research as-of')
    if any(_utc(b.closed_at_utc)>_utc(ts) for b in bars):
        raise ContractError('bar is after research as-of')
    causal=validate_replay(signals,observations,ts)
    rejected=set(causal['rejected_signal_ids'])
    if any(s.status=='CALCULATED' and s.id in rejected for s in signals):
        raise ContractError('historical calculated signal uses future/incompatible evidence')
    # Explicitly insufficient models are retained as NO_DATA, never imputed.
    # validate_replay still reports INELIGIBLE so reports cannot claim full coverage.
    simulation=replay_strategy(spec,bars,signals,observations,
        **payload.get('costs_bps',{}))
    cv_result=None
    if payload.get('validation'):
        cv=payload['validation']
        label_windows=[LabelWindow(**w) for w in cv['label_windows']]
        series=cv['candidate_period_returns']
        if any(len(returns)!=len(label_windows) for returns in series.values()):
            raise ContractError('validation series and label windows differ')
        wf=walk_forward_splits(label_windows,cv['train_size'],cv['test_size'],
                                embargo=cv.get('embargo',0))
        cpcv=cpcv_splits(label_windows,cv['n_groups'],cv['n_test_groups'],
                         embargo=cv.get('embargo',0))
        cv_result={'walk_forward':select_and_evaluate_folds(series,wf),
                   'cpcv':select_and_evaluate_folds(series,cpcv),
                   'cscv_pbo':cscv_pbo(series,cv.get('pbo_groups',8))}
    evidence=EvidenceArchive(archive_path)
    for item in observations:
        evidence.ingest(item)
    snapshot_id=evidence.freeze([o.id for o in observations],ts)
    result={'schema':'research-run-result.v1','run_id':run_id,'as_of_utc':ts,
            'input_digest':digest(payload),'evidence_snapshot_id':snapshot_id,
            'strategy':asdict(spec),'simulation':simulation,
            'generated_signals':[asdict(s) for s in signals] if payload.get('models') else [],
            'validation':cv_result,
            'replay_eligibility':causal,
            'limitations':['local deterministic research; not brokerage or live execution',
                           'timestamp provenance is supplied by caller and must be independently verified']}
    ledger=TransactionalResearchLedger(ledger_path)
    tip=ledger.append(run_id,ts,result)
    return {'result':result,'ledger_tip_hash':tip,'ledger_anchor_receipt':ledger.anchor()}
