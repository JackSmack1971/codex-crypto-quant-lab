"""Typed, deterministic, read-only research evaluation and provenance utilities.

This module does not fetch market data, issue orders, infer likelihoods or rate
trade attractiveness. Callers must supply measured values and availability times.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from pathlib import Path
from typing import Any
import json
import math


class ContractError(ValueError):
    """A research contract or point-in-time invariant has been violated."""


def _utc(value: str) -> datetime:
    if not isinstance(value, str):
        raise ContractError('expected ISO-8601 UTC timestamp')
    try:
        date = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError as exc:
        raise ContractError('invalid timestamp') from exc
    if date.tzinfo is None or date.utcoffset() != timezone.utc.utcoffset(date):
        raise ContractError('timestamp must specify UTC')
    return date


def instant_key(value: str) -> str:
    """Canonical identity of a UTC instant: ``Z`` and ``+00:00`` spellings and equivalent fractional seconds map
    to one fixed-width string, so equal text order == equal time order. Fails closed (ContractError) on missing,
    malformed or non-UTC input. The original spelling is never rewritten in stored evidence; this key is used only
    to *compare*, group and order instants."""
    return _utc(value).astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%fZ')


def same_instant(a: str, b: str) -> bool:
    return _utc(a) == _utc(b)


def _decimal(value: str) -> Decimal:
    if not isinstance(value, str):
        raise ContractError('numeric values must be decimal strings')
    try:
        number = Decimal(value)
    except InvalidOperation as exc:
        raise ContractError('invalid decimal value') from exc
    if not number.is_finite():
        raise ContractError('non-finite value')
    return number


def _required(value: str, label: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ContractError(f'{label} required')


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False,
                      allow_nan=False).encode('utf-8')


def digest(value: Any) -> str:
    return sha256(canonical_json(value)).hexdigest()


@dataclass(frozen=True)
class Observation:
    id: str
    instrument_id: str  # e.g. exchange:binance:spot:BTCUSDT, not BTC alone
    metric: str
    value: str | None
    unit: str
    source_uri: str
    observed_at_utc: str | None
    retrieved_at_utc: str
    available_at_utc: str | None  # point-in-time eligibility; unknown is not eligible
    status: str = 'OBSERVED'  # OBSERVED | UNAVAILABLE
    method: str = 'provider-reported'

    def __post_init__(self) -> None:
        for key in ('id', 'instrument_id', 'metric', 'unit', 'source_uri', 'method'):
            _required(getattr(self, key), key)
        _utc(self.retrieved_at_utc)
        for field_name in ('observed_at_utc', 'available_at_utc'):
            value = getattr(self, field_name)
            if value is not None:
                _utc(value)
        if self.status not in ('OBSERVED', 'UNAVAILABLE'):
            raise ContractError('unknown observation status')
        if self.status == 'OBSERVED':
            if self.value is None or self.observed_at_utc is None:
                raise ContractError('observed data needs value and source time')
            _decimal(self.value)
            if _utc(self.observed_at_utc) > _utc(self.retrieved_at_utc):
                raise ContractError('observation is after retrieval')
            if self.available_at_utc is not None and _utc(self.available_at_utc) > _utc(self.retrieved_at_utc):
                raise ContractError('availability is after retrieval')
        elif self.value is not None:
            raise ContractError('unavailable data must not supply a value')


@dataclass(frozen=True)
class ResearchSignal:
    id: str
    model_id: str
    instrument_id: str
    horizon: str
    as_of_utc: str
    value: str | None  # optional normalized signed score, NOT a probability
    observation_ids: tuple[str, ...]
    method_version: str
    interpretation: str
    status: str = 'CALCULATED'  # CALCULATED | INSUFFICIENT_EVIDENCE

    def __post_init__(self) -> None:
        for name in ('id', 'model_id', 'instrument_id', 'horizon', 'method_version', 'interpretation'):
            _required(getattr(self, name), name)
        _utc(self.as_of_utc)
        if self.status not in ('CALCULATED', 'INSUFFICIENT_EVIDENCE'):
            raise ContractError('unknown signal status')
        if self.status == 'CALCULATED':
            if self.value is None or not self.observation_ids:
                raise ContractError('calculated signal needs score and evidence references')
            score = _decimal(self.value)
            if score < -1 or score > 1:
                raise ContractError('signal score must be in [-1,1]')
        elif self.value is not None:
            raise ContractError('insufficient evidence cannot have a score')
        if len(set(self.observation_ids)) != len(self.observation_ids):
            raise ContractError('duplicate observation references')


@dataclass(frozen=True)
class HypothesisAssessment:
    hypothesis_id: str
    as_of_utc: str
    supporting_signal_ids: tuple[str, ...]
    contradicting_signal_ids: tuple[str, ...]
    missing_evidence: tuple[str, ...]
    conclusion: str  # SUPPORTED | CONTRADICTED | MIXED | INSUFFICIENT_EVIDENCE
    rationale: str
    overlap_observation_ids: tuple[str, ...] = ()


def evaluate_hypothesis(hypothesis_id: str, as_of_utc: str, signals: list[ResearchSignal],
                        observations: list[Observation], supporting: set[str],
                        contradicting: set[str], required_metrics: set[str]) -> HypothesisAssessment:
    """Classify *declared* evidence direction; never count LLM votes as proof.

    A signal is ineligible if its method-time evidence is missing, unreleased,
    stale relative to its own as-of time, or mismatched to the investigation.
    Caller is responsible for declaring which signals support each hypothesis;
    this code neither invents directions nor calculates probabilities.
    """
    _required(hypothesis_id, 'hypothesis_id')
    cutoff = _utc(as_of_utc)
    by_obs = {o.id: o for o in observations}
    if len(by_obs) != len(observations):
        raise ContractError('duplicate observation id')
    by_sig = {s.id: s for s in signals}
    if len(by_sig) != len(signals):
        raise ContractError('duplicate signal id')
    if supporting & contradicting or (supporting | contradicting) - by_sig.keys():
        raise ContractError('signal assignment collision or unknown reference')
    usable: dict[str, set[str]] = {}
    for signal_id in supporting | contradicting:
        signal = by_sig[signal_id]
        if signal.status != 'CALCULATED' or _utc(signal.as_of_utc) > cutoff:
            continue
        obs_ids = set(signal.observation_ids)
        if not obs_ids or not obs_ids.issubset(by_obs):
            continue
        if any(by_obs[oid].status != 'OBSERVED' or by_obs[oid].available_at_utc is None
               or _utc(by_obs[oid].available_at_utc) > _utc(signal.as_of_utc)
               or _utc(by_obs[oid].observed_at_utc) > _utc(signal.as_of_utc)
               or by_obs[oid].instrument_id != signal.instrument_id
               for oid in obs_ids):
            continue
        usable[signal_id] = obs_ids
    available_metrics = {by_obs[oid].metric for obs_ids in usable.values() for oid in obs_ids}
    missing = sorted(required_metrics - available_metrics)
    positive = sorted(supporting & usable.keys())
    negative = sorted(contradicting & usable.keys())
    overlaps = sorted(set().union(*(usable[x] for x in positive)) &
                      set().union(*(usable[x] for x in negative))) if positive and negative else []
    if missing or not (positive or negative):
        outcome = 'INSUFFICIENT_EVIDENCE'
    elif positive and negative:
        outcome = 'MIXED'
    elif positive:
        outcome = 'SUPPORTED'
    else:
        outcome = 'CONTRADICTED'
    rationale = ('Declared directions and availability-gated evidence; no probability, '
                 'majority vote or independence inferred. Overlap is disclosed.')
    return HypothesisAssessment(hypothesis_id, as_of_utc, tuple(positive), tuple(negative),
                                tuple(missing), outcome, rationale, tuple(overlaps))


def validate_replay(signals: list[ResearchSignal], observations: list[Observation],
                    historical_cutoff_utc: str) -> dict[str, Any]:
    """Fail-closed eligibility for historical replay, not a backtest or edge claim."""
    cutoff = _utc(historical_cutoff_utc)
    by_id = {o.id: o for o in observations}
    if len(by_id) != len(observations):
        raise ContractError('duplicate observation id')
    rejected: list[str] = []
    for signal in signals:
        if _utc(signal.as_of_utc) > cutoff or signal.status != 'CALCULATED':
            rejected.append(signal.id)
            continue
        if any(oid not in by_id or by_id[oid].status != 'OBSERVED' or
               by_id[oid].available_at_utc is None or
               _utc(by_id[oid].available_at_utc) > _utc(signal.as_of_utc) or
               _utc(by_id[oid].observed_at_utc) > _utc(signal.as_of_utc) or
               by_id[oid].instrument_id != signal.instrument_id
               for oid in signal.observation_ids):
            rejected.append(signal.id)
    return {'status': 'ELIGIBLE' if not rejected else 'INELIGIBLE',
            'cutoff_utc': historical_cutoff_utc, 'rejected_signal_ids': sorted(rejected),
            'validated_signal_count': len(signals) - len(rejected)}


class ResearchLedger:
    """Append-only local hash chain; does not supply independently anchored integrity."""

    def __init__(self, directory: str | Path):
        self.directory = Path(directory)

    def verify(self) -> list[dict[str, Any]]:
        if not self.directory.exists():
            return []
        entries = sorted(self.directory.glob('*.json'))
        records: list[dict[str, Any]] = []
        previous = None
        for index, path in enumerate(entries):
            if path.name != f'{index:08d}.json':
                raise ContractError('ledger sequence gap or unexpected filename')
            record = json.loads(path.read_text(encoding='utf-8'))
            if record.get('sequence') != index or record.get('previous_hash') != previous:
                raise ContractError('ledger sequence or parent mismatch')
            entry_hash = record.get('hash')
            payload = {k: v for k, v in record.items() if k != 'hash'}
            if digest(payload) != entry_hash:
                raise ContractError('ledger record hash mismatch')
            records.append(record)
            previous = entry_hash
        return records

    def append(self, run_id: str, as_of_utc: str, payload: dict[str, Any]) -> Path:
        _required(run_id, 'run_id')
        _utc(as_of_utc)
        if not isinstance(payload, dict):
            raise ContractError('payload must be a mapping')
        self.directory.mkdir(parents=True, exist_ok=True)
        records = self.verify()  # fail closed on tamper before appending
        if any(record['run_id'] == run_id for record in records):
            raise ContractError('run_id already in ledger')
        index = len(records)
        record = {'sequence': index, 'run_id': run_id, 'as_of_utc': as_of_utc,
                  'previous_hash': records[-1]['hash'] if records else None,
                  'payload': payload}
        record['hash'] = digest(record)
        path = self.directory / f'{index:08d}.json'
        with path.open('x', encoding='utf-8') as stream:
            stream.write(canonical_json(record).decode('utf-8') + '\n')
        return path
