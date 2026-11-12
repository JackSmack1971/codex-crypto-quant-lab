"""Evidence-resolution, metric-lineage and cross-validation chronology checks.

These checks separate five claims that a JSON bundle must not conflate:
artifact integrity (bytes match recorded hashes), source provenance (cited ids
resolve to archived observations that existed before the mission as-of time),
review provenance (a review assertion exists; it is NOT authenticated), quantitative
verification (metric values are recomputed from archived inputs through a declared
deterministic transform) and scientific validity (never established here).

Nothing here authenticates an author or proves a provider told the truth: the
local SQLite archive and the bundle can be rewritten consistently by anyone who
controls them. A separately trusted anchor would be required for that.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict
from decimal import Decimal, InvalidOperation, DivisionByZero
from pathlib import Path
import sqlite3
from typing import Any

from .archive import ArchiveDatabaseError, ArchiveIntegrityError, EvidenceMissing
from .core import ContractError, _utc, digest

# Diagnostic/layer states. They are deliberately distinct:
#   UNKNOWN      - cannot be determined from what was supplied (e.g. no archive)
#   INCOMPLETE   - required material is absent
#   INVALID      - supplied material is malformed, contradictory, future-dated or fails recomputation
#   UNSUPPORTED  - a claim this verifier has no way to check (legacy/undeclared lineage)
UNKNOWN, INCOMPLETE, INVALID, UNSUPPORTED = 'UNKNOWN', 'INCOMPLETE', 'INVALID', 'UNSUPPORTED'
TRANSFORMS = {'identity': (1, 1), 'sum': (1, None), 'mean': (1, None), 'difference': (2, 2), 'ratio': (2, 2)}
SAME_SERIES_TRANSFORMS = {'identity', 'sum', 'mean', 'difference'}


def _d(code, message, artifact, state):
    return {'code': code, 'message': message, 'artifact': artifact, 'state': state}


def _open_owned(archive: Any):
    """Return (archive, owned). ``owned`` is the object this module created and must close; a
    caller-supplied archive object is never owned."""
    if archive is None:
        return None, None
    if hasattr(archive, 'get'):
        return archive, None
    path = Path(archive)
    if not path.is_file():
        raise ContractError('evidence archive not found')
    from .archive import EvidenceArchive
    try:
        opened = EvidenceArchive(path)
    except sqlite3.Error as exc:
        raise ArchiveDatabaseError(f'archive database error: {type(exc).__name__}') from exc
    return opened, opened


def open_archive(archive: Any):
    """Return an object with ``get(id)`` or None. A path is opened only if it already exists
    (never created as a side effect of verification). The caller then owns any returned object
    opened from a path; prefer :func:`archive_session`, which closes it deterministically."""
    return _open_owned(archive)[0]


@contextmanager
def archive_session(archive: Any):
    """Yield ``(archive, open_error)``. An archive opened here from a path is closed on every exit
    path (success, failure, exception); an archive object supplied by the caller is never closed."""
    owned = None
    try:
        opened, owned = _open_owned(archive)
        error = None
    except ContractError as exc:
        opened, error = None, exc
    try:
        yield opened, error
    finally:
        close = getattr(owned, 'close', None)
        if callable(close):
            close()


def reference_id(item: Any) -> str | None:
    if isinstance(item, str):
        return item.strip() or None
    if isinstance(item, dict) and isinstance(item.get('id'), str):
        return item['id'].strip() or None
    return None


def resolve_evidence(references: list, archive, as_of_utc: str | None, *, strict: bool):
    """Resolve each evidence id against the archive. Returns (diagnostics, state, resolved)."""
    out: list[dict] = []
    ids = [reference_id(r) for r in references]
    seen: set[str] = set()
    for index, rid in enumerate(ids):
        if rid is None:  # validate before duplicate tracking: malformed ids never enter ``seen``
            out.append(_d('EVIDENCE_REFERENCE_MALFORMED', f'evidence reference {index} has no usable string id', 'evidence.json', INVALID))
        elif rid in seen:
            out.append(_d('EVIDENCE_REFERENCE_DUPLICATE', f'duplicate evidence reference: {rid}', 'evidence.json', INVALID))
        else:
            seen.add(rid)
    if archive is None:
        if strict:
            out.append(_d('EVIDENCE_UNRESOLVED_NO_ARCHIVE',
                          'research-mission.v2 requires an evidence archive to resolve references', 'evidence.json', UNKNOWN))
        return out, UNKNOWN, {}
    cutoff = None
    if as_of_utc is not None:
        try:
            cutoff = _utc(as_of_utc)
        except ContractError:
            out.append(_d('BASELINE_AS_OF_INVALID', 'baseline as_of_utc must be an ISO-8601 UTC timestamp', 'mission_baseline.json', INVALID))
    elif strict:
        out.append(_d('BASELINE_AS_OF_MISSING', 'research-mission.v2 baseline requires as_of_utc', 'mission_baseline.json', INCOMPLETE))
    resolved: dict[str, Any] = {}
    for item, rid in zip(references, ids):
        if rid is None:
            continue
        try:
            obs = archive.get(rid)
        except EvidenceMissing:  # classification is by exception type, never by message text
            out.append(_d('EVIDENCE_NOT_IN_ARCHIVE', f'{rid}: not present in the archive', 'evidence.json', INVALID))
            continue
        except ArchiveIntegrityError as exc:
            out.append(_d('EVIDENCE_ARCHIVE_INTEGRITY', f'{rid}: {exc}', 'evidence.json', INVALID))
            continue
        except (ArchiveDatabaseError, sqlite3.Error) as exc:
            out.append(_d('EVIDENCE_ARCHIVE_DATABASE_ERROR', f'{rid}: {type(exc).__name__}', 'evidence.json', INVALID))
            continue
        except Exception as exc:  # noqa: BLE001 - unclassified archive failure must fail closed, not crash
            out.append(_d('EVIDENCE_ARCHIVE_INTEGRITY', f'{rid}: {type(exc).__name__}', 'evidence.json', INVALID))
            continue
        if obs.status != 'OBSERVED':
            out.append(_d('EVIDENCE_NOT_OBSERVED', f'{rid} is an UNAVAILABLE record, not a measurement', 'evidence.json', INVALID))
            continue
        if isinstance(item, dict):
            if item.get('sha256') is not None and item['sha256'] != digest(asdict(obs)):
                out.append(_d('EVIDENCE_REFERENCE_CONTRADICTS_ARCHIVE', f'{rid}: sha256 differs from archived observation', 'evidence.json', INVALID))
                continue
            mismatch = next((f for f in ('instrument_id', 'metric', 'unit') if f in item and item[f] != getattr(obs, f)), None)
            if mismatch:
                out.append(_d('EVIDENCE_REFERENCE_CONTRADICTS_ARCHIVE', f'{rid}: {mismatch} differs from archived observation', 'evidence.json', INVALID))
                continue
        if cutoff is not None:
            if obs.available_at_utc is None:
                out.append(_d('EVIDENCE_AVAILABILITY_UNKNOWN', f'{rid}: no documented availability time; not eligible as of mission baseline', 'evidence.json', UNKNOWN))
                continue
            if any(_utc(t) > cutoff for t in (obs.available_at_utc, obs.observed_at_utc, obs.retrieved_at_utc)):
                out.append(_d('EVIDENCE_FUTURE_DATED', f'{rid}: observation, retrieval or availability is after the mission as_of_utc', 'evidence.json', INVALID))
                continue
        resolved[rid] = obs
    if any(d['state'] == INVALID for d in out):
        state = INVALID
    elif any(d['state'] in (UNKNOWN, INCOMPLETE) for d in out):
        state = UNKNOWN
    else:
        state = 'ARCHIVE_RESOLVED'
    return out, state, resolved


def _num(text: Any) -> Decimal:
    if isinstance(text, bool) or not isinstance(text, (str, int)):
        raise ValueError
    value = Decimal(str(text))
    if not value.is_finite():
        raise ValueError
    return value


def _obs_value(value: Any) -> Decimal:
    """Archived observation value as a finite Decimal; NaN/Infinity/non-numeric are rejected."""
    return _num(value)


def verify_metrics(metrics_obj: Any, evidence_ids: set[str], resolved: dict, *, strict: bool, archive_present: bool):
    """Recompute each metric from archived inputs via a declared deterministic transform."""
    metrics = metrics_obj.get('metrics') if isinstance(metrics_obj, dict) else None
    if not isinstance(metrics, list):
        return [_d('METRICS_INVALID', 'metrics.json must contain a metrics list', 'metrics.json', INVALID)], INVALID
    if not metrics:
        return [], 'NOT_APPLICABLE'
    out: list[dict] = []
    legacy = verified = 0
    seen: set[str] = set()
    for index, metric in enumerate(metrics):
        label = f'metric {index}'
        if not isinstance(metric, dict) or not isinstance(metric.get('id'), str) or not metric['id'].strip():
            out.append(_d('METRIC_RECORD_INVALID', f'{label} needs an id', 'metrics.json', INVALID))
            continue
        label = f'metric {metric["id"]}'
        if metric['id'] in seen:
            out.append(_d('METRIC_ID_DUPLICATE', f'{label} is declared twice', 'metrics.json', INVALID))
            continue
        seen.add(metric['id'])
        transform, inputs = metric.get('transform'), metric.get('inputs')
        if transform is None and inputs is None:
            legacy += 1
            if strict:
                out.append(_d('METRIC_LINEAGE_MISSING', f'{label} lacks inputs and a declared transform', 'metrics.json', INCOMPLETE))
            continue
        name = transform.get('name') if isinstance(transform, dict) else None
        if not isinstance(inputs, list) or not inputs or any(not isinstance(i, str) for i in inputs) or len(set(inputs)) != len(inputs):
            out.append(_d('METRIC_INPUTS_INVALID', f'{label} inputs must be a non-empty list of unique observation ids', 'metrics.json', INVALID))
            continue
        if name not in TRANSFORMS:
            out.append(_d('METRIC_TRANSFORM_UNSUPPORTED', f'{label} declares transform {name!r}; supported: {sorted(TRANSFORMS)}', 'metrics.json', UNSUPPORTED))
            legacy += 1
            continue
        low, high = TRANSFORMS[name]
        if len(inputs) < low or (high is not None and len(inputs) > high):
            out.append(_d('METRIC_INPUTS_INVALID', f'{label}: {name} takes {low}..{high or "n"} inputs', 'metrics.json', INVALID))
            continue
        if any(i not in evidence_ids for i in inputs):
            out.append(_d('METRIC_INPUT_NOT_DECLARED_EVIDENCE', f'{label} cites inputs absent from evidence.json', 'metrics.json', INVALID))
            continue
        try:
            claimed = _num(metric.get('value'))
            tolerance = _num(transform.get('tolerance', '0'))
            if tolerance < 0:
                raise ValueError
        except (ArithmeticError, ValueError, TypeError):
            out.append(_d('METRIC_VALUE_INVALID', f'{label} value/tolerance must be finite decimals', 'metrics.json', INVALID))
            continue
        if not isinstance(metric.get('unit'), str) or not metric['unit'].strip():
            out.append(_d('METRIC_UNIT_MISSING', f'{label} requires a unit', 'metrics.json', INVALID))
            continue
        if not archive_present:
            continue  # lineage shape is checked; recomputation is UNKNOWN without archived inputs
        observations = [resolved.get(i) for i in inputs]
        if any(o is None for o in observations):
            out.append(_d('METRIC_INPUT_UNRESOLVED', f'{label} has inputs that did not resolve to valid archived observations', 'metrics.json', INVALID))
            continue
        if name in SAME_SERIES_TRANSFORMS:
            incompatible = (len({o.unit for o in observations}) > 1 or len({o.instrument_id for o in observations}) > 1
                            or (name != 'difference' and len({o.metric for o in observations}) > 1))
            if incompatible:
                out.append(_d('METRIC_INPUT_INCOMPATIBLE', f'{label}: {name} requires inputs with one instrument and unit'
                              + ('' if name == 'difference' else ' and one metric'), 'metrics.json', INVALID))
                continue
            if metric['unit'] != observations[0].unit:
                out.append(_d('METRIC_UNIT_MISMATCH', f'{label}: unit {metric["unit"]!r} != input unit {observations[0].unit!r}', 'metrics.json', INVALID))
                continue
        try:
            values = [_obs_value(o.value) for o in observations]
            if name == 'identity':
                expected = values[0]
            elif name == 'sum':
                expected = sum(values, Decimal(0))
            elif name == 'mean':
                expected = sum(values, Decimal(0)) / len(values)
            elif name == 'difference':
                expected = values[0] - values[1]
            else:
                expected = values[0] / values[1]
            if not expected.is_finite():
                raise ValueError('non-finite result')
            mismatch = abs(expected - claimed) > tolerance
        except (ArithmeticError, ValueError, TypeError):  # DecimalException is an ArithmeticError
            out.append(_d('METRIC_TRANSFORM_UNDEFINED', f'{label}: {name} is undefined for its inputs', 'metrics.json', INVALID))
            continue
        if mismatch:
            out.append(_d('METRIC_VALUE_MISMATCH', f'{label}: claimed {claimed} != recomputed {expected}', 'metrics.json', INVALID))
            continue
        verified += 1
    if any(d['state'] == INVALID for d in out):
        state = INVALID
    elif any(d['state'] == INCOMPLETE for d in out):
        state = INCOMPLETE
    elif legacy:
        state = UNSUPPORTED
    elif not archive_present:
        state = UNKNOWN
    else:
        state = 'VERIFIED' if verified == len(metrics) else UNSUPPORTED
    return out, state


def check_windows(raw: Any):
    """Parse label windows -> list[(start, end)] or raise ContractError."""
    if not isinstance(raw, list) or not raw:
        raise ContractError('label_windows must be a non-empty list')
    parsed = []
    for item in raw:
        if not isinstance(item, dict):
            raise ContractError('label window must be an object')
        start, end = _utc(item.get('start_utc')), _utc(item.get('end_utc'))
        if end < start:
            raise ContractError('negative label interval')
        parsed.append((start, end))
    if any(a[0] >= b[0] for a, b in zip(parsed, parsed[1:])):
        raise ContractError('label windows must have strictly increasing starts')
    return parsed


def chronology_problems(result: dict, windows_raw: Any, *, walk_forward: bool) -> list[tuple[str, str, str]]:
    """Check folds against real label windows. Returns (code, message, state) problems.

    Leakage rule: no training label interval may overlap any test label interval.
    Walk-forward additionally requires every training label to end before the first
    test label starts. Embargo width cannot be verified from windows and is not claimed."""
    if windows_raw is None:
        return [('CV_LABEL_WINDOWS_MISSING', 'cross-validation cannot be chronologically checked without label_windows', INCOMPLETE)]
    try:
        windows = check_windows(windows_raw)
    except ContractError as exc:
        return [('CV_LABEL_WINDOWS_INVALID', str(exc), INVALID)]
    problems = []
    folds = result.get('folds') if isinstance(result, dict) else None
    if not isinstance(folds, list) or not folds:
        return [('CV_FOLDS_MALFORMED', 'folds must be a non-empty list', INVALID)]
    for number, fold in enumerate(folds):
        structural = _fold_problem(fold, len(windows))
        if structural:  # chronology/leakage checks run only on structurally valid folds
            problems.append((structural[0], f'fold {number}: {structural[1]}', INVALID))
            continue
        train, test = fold['train_indices'], fold['test_indices']
        first_test_start = min(windows[j][0] for j in test)
        for i in train:
            overlap = any(windows[i][0] <= windows[j][1] and windows[j][0] <= windows[i][1] for j in test)
            if overlap:
                problems.append(('CV_LABEL_OVERLAP_LEAKAGE', f'fold {number}: train label {i} overlaps a test label interval', INVALID))
                break
            if walk_forward and windows[i][1] >= first_test_start:
                problems.append(('CV_TRAIN_NOT_BEFORE_TEST', f'fold {number}: walk-forward train label {i} does not end before the first test label', INVALID))
                break
    return problems


def _fold_problem(fold: Any, n_windows: int) -> tuple[str, str] | None:
    """Structural validation of one fold: (code, reason) or None. Never raises. Indices must be real
    ints (not bool), within [0, n_windows), unique within a list, and disjoint between train and test;
    negative values are rejected so Python's negative indexing can never select the wrong window."""
    if not isinstance(fold, dict):
        return 'CV_FOLD_MALFORMED', 'fold must be an object'
    lists = {}
    for key in ('train_indices', 'test_indices'):
        value = fold.get(key)
        if not isinstance(value, list) or not value:
            return 'CV_FOLD_MALFORMED', f'{key} must be a non-empty list'
        lists[key] = value
    for key, value in lists.items():
        for index in value:
            if isinstance(index, bool) or not isinstance(index, int):
                return 'CV_INDEX_INVALID', f'{key} contains a non-integer index'
            if index < 0:
                return 'CV_INDEX_INVALID', f'{key} contains a negative index'
            if index >= n_windows:
                return 'CV_INDEX_OUT_OF_RANGE', f'{key} indexes beyond {n_windows} label windows'
        if len(set(value)) != len(value):
            return 'CV_INDEX_DUPLICATE', f'{key} contains duplicate indices'
    if set(lists['train_indices']) & set(lists['test_indices']):
        return 'CV_TRAIN_TEST_INDEX_SHARED', 'train and test share an index'
    return None
