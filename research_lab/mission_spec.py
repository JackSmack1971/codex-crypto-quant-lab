"""Versioned, immutable input contract for coordinator-executed research missions.

``research-mission-execution.v1`` describes *what* to research, never *how to run
code*: there is no command, import path, URL or shell field. Operations are named by
identifier and resolved against a fixed in-process allowlist. Validation is pure
(no filesystem access); path *safety against the real filesystem* is enforced when a
path is used (see :func:`resolve_under`).
"""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from pathlib import Path, PurePosixPath
from typing import Any

from .core import ContractError, _utc, digest

SCHEMA = 'research-mission-execution.v1'
STAGES = ('PLANNED', 'ACQUIRING', 'RECONCILING', 'ANALYZING', 'VERIFYING', 'RENDERING', 'FINALIZING')
# Fixed allowlist: stage -> (operation_id, operation_version). Mission payloads select among these
# by id only; nothing in a spec can add an operation, import a module or run a command.
STAGE_OPERATIONS = {
    'PLANNED': ('plan.freeze_spec', '1'),
    'ACQUIRING': ('acquire.local_inputs', '1'),
    'RECONCILING': ('reconcile.sources', '1'),
    'ANALYZING': ('analyze.hypotheses', '1'),
    'VERIFYING': ('verify.stage_artifacts', '1'),
    'RENDERING': ('render.local_report', '2'),
    'FINALIZING': ('finalize.bundle', '2'),
}
KNOWN_OPERATIONS = frozenset(op for op, _ in STAGE_OPERATIONS.values())
REVIEWERS = ('methodology_reviewer', 'source_verifier')
ACCEPTABLE_RESEARCH_STATES = frozenset({'NOT_EVALUABLE', 'INSUFFICIENT_EVIDENCE', 'EVALUABLE'})
MISSION_ID = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$')

REQUIRED_KEYS = {'schema', 'mission_id', 'decision_question', 'as_of_utc', 'research_horizon', 'instruments',
                 'required_metrics', 'max_age_seconds', 'provider_operations', 'hypotheses',
                 'permitted_operations', 'inputs', 'output_dir', 'completion_requirements'}
OPTIONAL_KEYS = {'conflict_tolerance', 'no_hypotheses_reason', 'experiment', 'analysis', 'costs', 'supersedes'}


class MissionError(ContractError):
    """Coordinator failure with a stable machine code. Never classify these by message text."""

    def __init__(self, code: str, message: str = '', *, diagnostics: list[dict] | None = None):
        super().__init__(f'{code}: {message}' if message else code)
        self.code, self.message, self.diagnostics = code, message or code, diagnostics or []


def _s(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def safe_relative(raw: Any) -> bool:
    """Syntactic check: non-empty POSIX relative path with no '.', '..', empty or backslash parts."""
    if not isinstance(raw, str) or not raw or '\\' in raw or '\x00' in raw:
        return False
    path = PurePosixPath(raw)
    return (not path.is_absolute() and path.as_posix() == raw  # reject 'a//b', './a', 'a/' (non-canonical spellings)
            and all(part not in ('', '.', '..') for part in path.parts))


def resolve_under(root: Path, raw: str) -> Path:
    """Resolve ``raw`` below ``root``; reject traversal and any symlink component."""
    if not safe_relative(raw):
        raise MissionError('INVALID_MISSION_SPEC', f'unsafe relative path: {raw!r}')
    current = root
    for part in PurePosixPath(raw).parts:
        current = current / part
        if current.is_symlink():
            raise MissionError('INVALID_MISSION_SPEC', f'symlink path component rejected: {raw!r}')
    resolved = current.resolve(strict=False)
    try:
        resolved.relative_to(root.resolve())
    except ValueError as exc:
        raise MissionError('INVALID_MISSION_SPEC', f'path escapes the workspace: {raw!r}') from exc
    return current


def _file_ref(value: Any) -> bool:
    return (isinstance(value, dict) and set(value) == {'path', 'sha256'} and safe_relative(value['path'])
            and isinstance(value['sha256'], str) and re.fullmatch(r'[0-9a-f]{64}', value['sha256']) is not None)


def validate_spec(spec: Any) -> dict:
    try:
        return _validate(spec)
    except (TypeError, AttributeError, KeyError, ValueError) as exc:  # pragma: no cover - defence in depth for odd JSON shapes
        if isinstance(exc, MissionError):
            raise
        raise MissionError('INVALID_MISSION_SPEC', f'malformed specification ({type(exc).__name__})',
                           diagnostics=[{'code': 'SPEC_MALFORMED', 'field': '', 'message': type(exc).__name__}]) from exc


def _validate(spec: Any) -> dict:
    """Return ``spec`` unchanged when valid; otherwise raise ``MissionError('INVALID_MISSION_SPEC')``
    carrying one ``{code, field, message}`` diagnostic per problem."""
    problems: list[dict] = []

    def bad(code: str, field: str, message: str) -> None:
        problems.append({'code': code, 'field': field, 'message': message})

    if not isinstance(spec, dict):
        raise MissionError('INVALID_MISSION_SPEC', 'specification must be an object',
                           diagnostics=[{'code': 'SPEC_NOT_OBJECT', 'field': '', 'message': 'must be an object'}])
    if spec.get('schema') != SCHEMA:
        bad('SPEC_SCHEMA_UNSUPPORTED', 'schema', f'schema must be {SCHEMA}')
    for key in sorted(REQUIRED_KEYS - spec.keys()):
        bad('SPEC_FIELD_MISSING', key, 'required field missing')
    for key in sorted(spec.keys() - REQUIRED_KEYS - OPTIONAL_KEYS):
        bad('SPEC_FIELD_UNSUPPORTED', key, 'unsupported field')
    if problems:
        raise MissionError('INVALID_MISSION_SPEC', f'{len(problems)} problem(s)', diagnostics=problems)

    if not isinstance(spec['mission_id'], str) or not MISSION_ID.fullmatch(spec['mission_id']):
        bad('SPEC_FIELD_INVALID', 'mission_id', 'must match [A-Za-z0-9][A-Za-z0-9._-]{0,63}')
    for key in ('decision_question', 'research_horizon'):
        if not _s(spec[key]):
            bad('SPEC_FIELD_INVALID', key, 'must be a non-empty string')
    try:
        _utc(spec['as_of_utc'])
    except ContractError:
        bad('SPEC_FIELD_INVALID', 'as_of_utc', 'must be an ISO-8601 UTC timestamp')
    instruments = spec['instruments']
    if not isinstance(instruments, list) or not instruments or not all(_s(i) for i in instruments) \
            or len(set(instruments)) != len(instruments):
        bad('SPEC_FIELD_INVALID', 'instruments', 'must be a non-empty list of unique instrument ids')
        instruments = []
    metrics = spec['required_metrics']
    metric_names: set[str] = set()
    if not isinstance(metrics, list) or not metrics:
        bad('SPEC_FIELD_INVALID', 'required_metrics', 'must be a non-empty list')
    else:
        seen = set()
        for index, item in enumerate(metrics):
            if not (isinstance(item, dict) and set(item) == {'instrument_id', 'metric', 'unit'}
                    and all(_s(v) for v in item.values())):
                bad('SPEC_FIELD_INVALID', f'required_metrics[{index}]', 'needs exactly instrument_id, metric, unit')
                continue
            if item['instrument_id'] not in instruments:
                bad('SPEC_FIELD_INVALID', f'required_metrics[{index}]', 'instrument_id not in instruments')
            key = (item['instrument_id'], item['metric'])
            if key in seen:
                bad('SPEC_FIELD_INVALID', f'required_metrics[{index}]', 'duplicate instrument/metric')
            seen.add(key)
            metric_names.add(item['metric'])
    age = spec['max_age_seconds']
    if isinstance(age, bool) or not isinstance(age, int) or age < 0:
        bad('SPEC_FIELD_INVALID', 'max_age_seconds', 'must be a non-negative integer')
    tolerance = spec.get('conflict_tolerance', '0')
    try:
        if not isinstance(tolerance, str) or not (Decimal(tolerance).is_finite() and Decimal(tolerance) >= 0):
            raise InvalidOperation
    except InvalidOperation:
        bad('SPEC_FIELD_INVALID', 'conflict_tolerance', 'must be a finite non-negative decimal string')
    operations = spec['provider_operations']
    if not isinstance(operations, list) or any(
            not (isinstance(o, dict) and set(o) == {'operation_id', 'required'} and _s(o['operation_id'])
                 and isinstance(o['required'], bool)) for o in operations) \
            or len({o.get('operation_id') for o in operations if isinstance(o, dict)}) != len(operations):
        bad('SPEC_FIELD_INVALID', 'provider_operations', 'must be a list of unique {operation_id, required:bool}')
    hypotheses = spec['hypotheses']
    if not isinstance(hypotheses, list):
        bad('SPEC_FIELD_INVALID', 'hypotheses', 'must be a list')
    else:
        ids = []
        for index, h in enumerate(hypotheses):
            field = f'hypotheses[{index}]'
            if not (isinstance(h, dict) and set(h) == {'id', 'supporting_signal_ids', 'contradicting_signal_ids',
                                                       'required_metrics'} and _s(h['id'])
                    and all(isinstance(h[k], list) and all(_s(x) for x in h[k])
                            for k in ('supporting_signal_ids', 'contradicting_signal_ids', 'required_metrics'))):
                bad('SPEC_FIELD_INVALID', field, 'needs id, supporting_signal_ids, contradicting_signal_ids, required_metrics')
                continue
            ids.append(h['id'])
            if set(h['supporting_signal_ids']) & set(h['contradicting_signal_ids']):
                bad('SPEC_FIELD_INVALID', field, 'a signal cannot both support and contradict')
            if set(h['required_metrics']) - metric_names:
                bad('SPEC_FIELD_INVALID', field, 'required_metrics must be declared in required_metrics')
        if len(set(ids)) != len(ids):
            bad('SPEC_FIELD_INVALID', 'hypotheses', 'duplicate hypothesis id')
        reason = spec.get('no_hypotheses_reason')
        if not hypotheses and not _s(reason):
            bad('SPEC_FIELD_MISSING', 'no_hypotheses_reason', 'explicit absence of hypotheses needs a reason')
        if hypotheses and reason is not None:
            bad('SPEC_FIELD_INVALID', 'no_hypotheses_reason', 'only valid when hypotheses is empty')
    permitted = spec['permitted_operations']
    if not isinstance(permitted, list) or not permitted or not all(isinstance(p, str) for p in permitted):
        bad('SPEC_FIELD_INVALID', 'permitted_operations', 'must be a non-empty list of operation ids')
    else:
        for op in sorted(set(permitted) - KNOWN_OPERATIONS):
            bad('SPEC_OPERATION_UNKNOWN', 'permitted_operations', f'unknown operation id: {op!r}')
    inputs = spec['inputs']
    if not isinstance(inputs, dict) or not _file_ref(inputs.get('research_run')) or \
            set(inputs) - {'research_run', 'provider_attempts', 'reviewer_handoffs', 'report_input'}:
        bad('SPEC_FIELD_INVALID', 'inputs', 'needs research_run {path, sha256}; optional provider_attempts, reviewer_handoffs, report_input')
    else:
        if 'provider_attempts' in inputs and not _file_ref(inputs['provider_attempts']):
            bad('SPEC_FIELD_INVALID', 'inputs.provider_attempts', 'needs exactly path and sha256')
        if 'report_input' in inputs and not _file_ref(inputs['report_input']):
            bad('SPEC_FIELD_INVALID', 'inputs.report_input', 'needs exactly path and sha256')
        handoffs = inputs.get('reviewer_handoffs', {})
        if not isinstance(handoffs, dict) or set(handoffs) - set(REVIEWERS) or not all(_file_ref(v) for v in handoffs.values()):
            bad('SPEC_FIELD_INVALID', 'inputs.reviewer_handoffs', f'keys must be within {list(REVIEWERS)} with {{path, sha256}}')
    if not safe_relative(spec['output_dir']):
        bad('SPEC_PATH_UNSAFE', 'output_dir', 'must be a safe relative path')
    cr = spec['completion_requirements']
    if not (isinstance(cr, dict) and set(cr) == {'accepted_research_states', 'required_reviews'}
            and isinstance(cr['accepted_research_states'], list) and cr['accepted_research_states']
            and all(isinstance(x, str) for x in cr['accepted_research_states'])
            and set(cr['accepted_research_states']) <= ACCEPTABLE_RESEARCH_STATES
            and cr['required_reviews'] == list(REVIEWERS)):
        bad('SPEC_FIELD_INVALID', 'completion_requirements',
            f'needs accepted_research_states (subset of {sorted(ACCEPTABLE_RESEARCH_STATES)}; the coordinator '
            f'never designates VALIDATED) and required_reviews == {list(REVIEWERS)}')
    experiment = spec.get('experiment')
    if experiment is not None and not (
            isinstance(experiment, dict) and {'experiment_id', 'registration_digest'} <= set(experiment) <= {'experiment_id', 'registration_digest', 'result_digest'}
            and _s(experiment['experiment_id'])
            and all(isinstance(experiment[k], str) and re.fullmatch(r'[0-9a-f]{64}', experiment[k]) for k in experiment if k != 'experiment_id')):
        bad('SPEC_FIELD_INVALID', 'experiment', 'needs experiment_id and a pinned registration_digest (lowercase SHA-256); optional result_digest')
    analysis = spec.get('analysis')
    if analysis is not None and not (isinstance(analysis, dict) and set(analysis) == {'evaluation_task'}
                                     and isinstance(analysis['evaluation_task'], dict)):
        bad('SPEC_FIELD_INVALID', 'analysis', 'needs exactly evaluation_task (research-task-eval.v1 object)')
    if 'costs' in spec and not isinstance(spec['costs'], dict):
        bad('SPEC_FIELD_INVALID', 'costs', 'must be an object')
    if 'supersedes' in spec and not _s(spec['supersedes']):
        bad('SPEC_FIELD_INVALID', 'supersedes', 'must be a non-empty mission id')
    try:
        digest(spec)  # canonical JSON: rejects NaN/Infinity and non-serialisable values
    except (ValueError, TypeError):
        bad('SPEC_NOT_CANONICAL', '', 'must be canonical JSON (no NaN, Infinity or non-JSON types)')
    if problems:
        raise MissionError('INVALID_MISSION_SPEC', f'{len(problems)} problem(s)', diagnostics=problems)
    return spec


def spec_digest(spec: dict) -> str:
    return digest(spec)
