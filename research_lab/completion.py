"""Deterministic integrity gate for locally assembled research mission bundles.

The gate assesses persisted process evidence. It does not execute or authorize
research work and does not assess whether a research conclusion is true.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath

from . import mission_integrity as integrity
from .core import ContractError

ARTIFACTS = (
    'mission_baseline.json', 'provider_attempts.json', 'evidence.json',
    'metrics.json', 'hypothesis_assessment.json', 'evaluation_result.json',
    'thesis_revision.json', 'committee_verification.json', 'report.md',
    'report.html',
)
SCHEMAS = {'research-mission.v1', 'research-mission.v2'}  # v2 = strict: archive-resolved evidence, metric lineage
RESEARCH_STATES = {'NOT_EVALUABLE', 'INSUFFICIENT_EVIDENCE', 'EVALUABLE', 'VALIDATED'}
COMPLETION_STATES = {'COMPLETE', 'INCOMPLETE', 'BLOCKED'}
REQUIRED_REVIEWS = {'source_verifier', 'methodology_reviewer'}


def _classify(code: str) -> str:
    """Diagnostic state for legacy codes: UNKNOWN / INCOMPLETE / INVALID / UNSUPPORTED."""
    if 'UNSUPPORTED' in code:
        return 'UNSUPPORTED'
    if any(word in code for word in ('MISSING', 'UNRECORDED', 'EMPTY', 'REMAINING_WORK', 'HAS_UNSUCCESSFUL', 'NOT_PASSED')):
        return 'INCOMPLETE'
    return 'INVALID'


def _diagnostic(code: str, message: str, artifact: str | None = None, state: str | None = None) -> dict:
    result = {'code': code, 'message': message, 'state': state or _classify(code)}
    if artifact is not None:
        result['artifact'] = artifact
    return result


def _safe_artifact_path(root: Path, raw: object) -> Path | None:
    if not isinstance(raw, str) or not raw or '\\' in raw:
        return None
    relative = PurePosixPath(raw)
    if relative.is_absolute() or any(part in ('', '.', '..') for part in relative.parts):
        return None
    candidate = root.joinpath(*relative.parts)
    try:
        candidate.resolve(strict=False).relative_to(root.resolve())
    except (OSError, ValueError):
        return None
    return candidate


def _valid_cross_validation(result: object) -> bool:
    if not isinstance(result, dict) or result.get('schema') != 'research-crossvalidation.v1':
        return False
    folds = result.get('folds')
    if not isinstance(folds, list) or not folds:
        return False
    for fold in folds:
        if not isinstance(fold, dict):
            return False
        train, test = fold.get('train_indices'), fold.get('test_indices')
        if not isinstance(train, list) or not train or not isinstance(test, list) or not test:
            return False
        if any(not isinstance(index, int) or isinstance(index, bool) or index < 0 for index in train + test):
            return False
        if len(set(train)) != len(train) or len(set(test)) != len(test):
            return False
        if set(train) & set(test):
            return False
    return True


def _valid_evidence_references(value: object) -> bool:
    return isinstance(value, list) and bool(value) and all(
        isinstance(item, str) and item.strip() or
        isinstance(item, dict) and isinstance(item.get('id'), str) and item['id'].strip()
        for item in value)


def verify(root: Path, archive=None) -> dict:
    """See :func:`_verify`. An archive opened here from a path is closed on every exit path; an
    archive object supplied by the caller is never closed (the caller keeps ownership)."""
    with integrity.archive_session(archive) as (opened, open_error):
        return _verify(root, opened, open_error)


def _verify(root: Path, archive, open_error) -> dict:
    """Validate a research-mission bundle and return stable machine diagnostics.

    ``archive`` (an EvidenceArchive or an existing SQLite path) enables resolution of evidence
    ids and recomputation of metrics. Without it those layers are reported UNKNOWN, and
    research-mission.v2 bundles and VALIDATED claims fail closed."""
    root = Path(root)
    diagnostics: list[dict] = []
    layers: dict = {}
    if open_error is not None:
        diagnostics.append(_diagnostic('EVIDENCE_ARCHIVE_UNAVAILABLE', str(open_error), 'evidence.json', 'INVALID'))
    mission_path = root / 'mission.json'
    try:
        mission = json.loads(mission_path.read_text(encoding='utf-8'))
    except (OSError, ValueError, UnicodeError) as exc:
        diagnostics.append(_diagnostic('MISSION_JSON_INVALID', f'{type(exc).__name__}: cannot read mission.json', 'mission.json'))
        return _result('INCOMPLETE', diagnostics)
    if not isinstance(mission, dict):
        return _result('INCOMPLETE', [_diagnostic('MISSION_FIELDS_INVALID', 'mission.json must be an object', 'mission.json')])

    required_fields = {'schema', 'completion_state', 'research_state', 'artifacts',
                       'missing_artifacts', 'blockers', 'remaining_work'}
    for field in sorted(required_fields - mission.keys()):
        diagnostics.append(_diagnostic('MISSION_FIELD_MISSING', f'missing required field: {field}', 'mission.json'))
    strict = mission.get('schema') == 'research-mission.v2'
    if mission.get('schema') not in SCHEMAS:
        diagnostics.append(_diagnostic('MISSION_SCHEMA_INVALID', 'unsupported or missing schema', 'mission.json'))
    declared = mission.get('completion_state')
    if not isinstance(declared, str) or declared not in COMPLETION_STATES:
        diagnostics.append(_diagnostic('COMPLETION_STATE_INVALID', 'completion_state must be COMPLETE, INCOMPLETE, or BLOCKED', 'mission.json'))
    research_state = mission.get('research_state')
    if not isinstance(research_state, str) or research_state not in RESEARCH_STATES:
        diagnostics.append(_diagnostic('RESEARCH_STATE_INVALID', 'research_state must be a declared research outcome', 'mission.json'))

    refs = mission.get('artifacts')
    if not isinstance(refs, dict):
        diagnostics.append(_diagnostic('ARTIFACTS_INVALID', 'artifacts must be an object', 'mission.json'))
        refs = {}
    for extra in sorted(set(refs) - set(ARTIFACTS)):
        diagnostics.append(_diagnostic('ARTIFACT_KEY_UNSUPPORTED', f'unsupported artifact key: {extra}', extra))

    missing_records = mission.get('missing_artifacts', [])
    missing_names: set[str] = set()
    if not isinstance(missing_records, list):
        diagnostics.append(_diagnostic('MISSING_ARTIFACTS_INVALID', 'missing_artifacts must be a list', 'mission.json'))
    else:
        for record in missing_records:
            if not isinstance(record, dict) or not isinstance(record.get('artifact'), str) or not record['artifact'].strip() or not isinstance(record.get('reason'), str) or not record['reason'].strip():
                diagnostics.append(_diagnostic('MISSING_ARTIFACT_RECORD_INVALID', 'each missing artifact record needs artifact and reason', 'mission.json'))
            else:
                missing_names.add(record['artifact'])
                if record['artifact'] in refs:
                    diagnostics.append(_diagnostic('MISSING_ARTIFACT_RECORD_CONTRADICTS_REFERENCE', f'{record["artifact"]} is declared missing and referenced', record['artifact']))
                if record['artifact'] not in ARTIFACTS:
                    diagnostics.append(_diagnostic('MISSING_ARTIFACT_UNSUPPORTED', f'unknown required artifact: {record["artifact"]}', record['artifact']))

    loaded: dict[str, object] = {}
    for name in ARTIFACTS:
        ref = refs.get(name)
        if not isinstance(ref, dict):
            if name not in missing_names:
                diagnostics.append(_diagnostic('ARTIFACT_REFERENCE_MISSING', 'missing required artifact reference', name))
            continue
        if set(ref) != {'path', 'sha256'}:
            diagnostics.append(_diagnostic('ARTIFACT_REFERENCE_FIELDS_INVALID', 'reference must contain exactly path and sha256', name))
        path = _safe_artifact_path(root, ref.get('path'))
        if path is None:
            diagnostics.append(_diagnostic('ARTIFACT_PATH_INVALID', 'artifact path must be a safe relative path inside the mission directory', name))
            continue
        expected = ref.get('sha256')
        if not isinstance(expected, str) or len(expected) != 64 or any(c not in '0123456789abcdef' for c in expected):
            diagnostics.append(_diagnostic('ARTIFACT_DIGEST_INVALID', 'sha256 must be 64 lowercase hexadecimal characters', name))
        if path.resolve() == mission_path.resolve():
            diagnostics.append(_diagnostic('ARTIFACT_SELF_REFERENCE', 'mission.json cannot be an artifact; its digest would be self-referential', name))
            continue
        try:
            data = path.read_bytes()
        except OSError:
            if name not in missing_names:
                diagnostics.append(_diagnostic('ARTIFACT_MISSING', f'artifact file does not exist: {ref.get("path")}', name))
            continue
        actual = hashlib.sha256(data).hexdigest()
        if actual != expected:
            diagnostics.append(_diagnostic('ARTIFACT_HASH_MISMATCH', 'SHA-256 does not match recorded digest', name))
        if name.endswith('.json'):
            try:
                loaded[name] = json.loads(data.decode('utf-8'))
            except (ValueError, UnicodeError):
                diagnostics.append(_diagnostic('ARTIFACT_JSON_INVALID', 'artifact is not valid UTF-8 JSON', name))

    baseline = loaded.get('mission_baseline.json')
    if (not isinstance(baseline, dict) or not baseline) and 'mission_baseline.json' not in missing_names:
        diagnostics.append(_diagnostic('BASELINE_INVALID', 'mission baseline must be a non-empty JSON object', 'mission_baseline.json'))
    required_operations = baseline.get('required_provider_operations') if isinstance(baseline, dict) else None
    if not isinstance(required_operations, list) or any(not isinstance(item, str) or not item.strip() for item in required_operations) or len(set(required_operations or [])) != len(required_operations or []):
        if 'mission_baseline.json' not in missing_names:
            diagnostics.append(_diagnostic('BASELINE_OPERATIONS_INVALID', 'baseline requires a unique required_provider_operations list of operation ids', 'mission_baseline.json'))
        required_operations = []

    evidence = loaded.get('evidence.json')
    if not isinstance(evidence, dict) or not isinstance(evidence.get('references'), list) or not evidence['references']:
        if 'evidence.json' not in missing_names:
            diagnostics.append(_diagnostic('EVIDENCE_REFERENCES_INVALID', 'evidence.json must contain a non-empty references list', 'evidence.json'))
    elif not _valid_evidence_references(evidence['references']):
        diagnostics.append(_diagnostic('EVIDENCE_REFERENCE_INVALID', 'each evidence reference must identify a non-empty id', 'evidence.json'))

    as_of = baseline.get('as_of_utc') if isinstance(baseline, dict) else None
    evidence_refs = evidence.get('references') if isinstance(evidence, dict) and _valid_evidence_references(evidence.get('references')) else []
    evidence_ids = {integrity.reference_id(item) for item in evidence_refs}
    if evidence_refs:
        found, source_state, resolved = integrity.resolve_evidence(evidence_refs, archive, as_of, strict=strict)
        diagnostics.extend(found)
    else:
        source_state, resolved = 'INCOMPLETE', {}
    if 'metrics.json' in loaded:
        found, quant_state = integrity.verify_metrics(loaded['metrics.json'], evidence_ids, resolved, strict=strict,
                                                      archive_present=archive is not None and source_state != 'INVALID')
        diagnostics.extend(found)
    else:
        quant_state = 'INCOMPLETE'

    attempts_obj = loaded.get('provider_attempts.json')
    attempts = attempts_obj.get('attempts') if isinstance(attempts_obj, dict) else None
    if not isinstance(attempts, list):
        if 'provider_attempts.json' not in missing_names:
            diagnostics.append(_diagnostic('PROVIDER_ATTEMPTS_INVALID', 'provider_attempts.json must contain an attempts list', 'provider_attempts.json'))
    else:
        if not attempts:
            diagnostics.append(_diagnostic('PROVIDER_ATTEMPTS_EMPTY', 'record at least one successful, failed, or unattempted operation', 'provider_attempts.json'))
        attempt_ids = []
        for index, attempt in enumerate(attempts):
            if not isinstance(attempt, dict) or not isinstance(attempt.get('status'), str) or attempt.get('status') not in {'SUCCEEDED', 'FAILED', 'UNATTEMPTED'}:
                diagnostics.append(_diagnostic('PROVIDER_ATTEMPT_INVALID', f'attempt {index} needs status SUCCEEDED, FAILED, or UNATTEMPTED', 'provider_attempts.json'))
            elif not isinstance(attempt.get('operation_id'), str) or not attempt['operation_id'].strip():
                diagnostics.append(_diagnostic('PROVIDER_OPERATION_ID_MISSING', f'attempt {index} needs an operation_id', 'provider_attempts.json'))
            else:
                attempt_ids.append(attempt['operation_id'])
                if attempt['status'] in {'FAILED', 'UNATTEMPTED'} and not (attempt.get('reason') or attempt.get('error')):
                    diagnostics.append(_diagnostic('PROVIDER_OUTCOME_UNRECORDED', f'attempt {index} must record reason/error', 'provider_attempts.json'))
                if attempt['status'] == 'UNATTEMPTED' and attempt.get('error'):
                    diagnostics.append(_diagnostic('UNATTEMPTED_HAS_ERROR', f'attempt {index} is unattempted and cannot record a request error', 'provider_attempts.json'))
        if len(set(attempt_ids)) != len(attempt_ids):
            diagnostics.append(_diagnostic('PROVIDER_OPERATION_DUPLICATE', 'provider operation ids must be unique', 'provider_attempts.json'))
        if set(required_operations) - set(attempt_ids):
            diagnostics.append(_diagnostic('PROVIDER_OPERATION_UNRECORDED', 'required provider operations lack an attempt record: ' + ', '.join(sorted(set(required_operations) - set(attempt_ids))), 'provider_attempts.json'))

    assessment = loaded.get('hypothesis_assessment.json')
    if not isinstance(assessment, dict) or not isinstance(assessment.get('state'), str) or assessment.get('state') not in RESEARCH_STATES:
        if 'hypothesis_assessment.json' not in missing_names:
            diagnostics.append(_diagnostic('HYPOTHESIS_STATE_INVALID', 'hypothesis assessment requires a valid research state', 'hypothesis_assessment.json'))
    elif isinstance(research_state, str) and research_state in RESEARCH_STATES and assessment['state'] != research_state:
        diagnostics.append(_diagnostic('RESEARCH_STATE_MISMATCH', 'mission and hypothesis assessment research states differ', 'hypothesis_assessment.json'))

    evaluation = loaded.get('evaluation_result.json')
    if not isinstance(evaluation, dict) or not isinstance(evaluation.get('state'), str) or evaluation.get('state') not in {'PASS', 'FAIL', 'NOT_EVALUABLE'}:
        if 'evaluation_result.json' not in missing_names:
            diagnostics.append(_diagnostic('EVALUATION_STATE_INVALID', 'evaluation result requires PASS, FAIL, or NOT_EVALUABLE state', 'evaluation_result.json'))

    thesis = loaded.get('thesis_revision.json')
    if not isinstance(thesis, dict) or not (isinstance(thesis.get('revision_id'), str) and thesis['revision_id'].strip()) and not (isinstance(thesis.get('non_revision_reason'), str) and thesis['non_revision_reason'].strip()):
        if 'thesis_revision.json' not in missing_names:
            diagnostics.append(_diagnostic('THESIS_RECORD_INVALID', 'thesis artifact requires revision_id or non_revision_reason', 'thesis_revision.json'))

    committee = loaded.get('committee_verification.json')
    reviews = committee.get('reviews') if isinstance(committee, dict) else None
    if not isinstance(reviews, dict):
        if 'committee_verification.json' not in missing_names:
            diagnostics.append(_diagnostic('COMMITTEE_VERIFICATION_MISSING', 'committee verification requires a reviews object', 'committee_verification.json'))
    else:
        for reviewer in sorted(REQUIRED_REVIEWS):
            if reviewer not in reviews:
                diagnostics.append(_diagnostic('COMMITTEE_REVIEW_MISSING', f'required review missing: {reviewer}', 'committee_verification.json'))
            elif not isinstance(reviews[reviewer], dict) or not isinstance(reviews[reviewer].get('state'), str) or reviews[reviewer].get('state') not in {'PASS', 'FAIL', 'NOT_PERFORMED'}:
                diagnostics.append(_diagnostic('COMMITTEE_REVIEW_INVALID', f'{reviewer} review state must be PASS, FAIL, or NOT_PERFORMED', 'committee_verification.json'))
            elif reviews[reviewer]['state'] != 'PASS':
                diagnostics.append(_diagnostic('COMMITTEE_REVIEW_NOT_PASSED', f'{reviewer} review is {reviews[reviewer]["state"]}', 'committee_verification.json'))
            elif not _valid_evidence_references(reviews[reviewer].get('evidence')):
                diagnostics.append(_diagnostic('COMMITTEE_REVIEW_EVIDENCE_MISSING', f'{reviewer} PASS requires attributable evidence references', 'committee_verification.json'))
            elif strict and {integrity.reference_id(i) for i in reviews[reviewer]['evidence']} - evidence_ids:
                diagnostics.append(_diagnostic('REVIEW_EVIDENCE_UNRESOLVED', f'{reviewer} cites evidence not listed in evidence.json', 'committee_verification.json', 'INVALID'))

    blockers = mission.get('blockers', [])
    if not isinstance(blockers, list):
        diagnostics.append(_diagnostic('BLOCKERS_INVALID', 'blockers must be a list', 'mission.json'))
        blockers = []
    blocker_ids = []
    for blocker in blockers:
        if not isinstance(blocker, dict) or not isinstance(blocker.get('id'), str) or not blocker['id'].strip() or not isinstance(blocker.get('reason'), str) or not blocker['reason'].strip() or not _valid_evidence_references(blocker.get('evidence')):
            diagnostics.append(_diagnostic('BLOCKER_EVIDENCE_INVALID', 'each blocker needs an id, reason, and attributable evidence list', 'mission.json'))
        else:
            blocker_ids.append(blocker['id'])
    if len(set(blocker_ids)) != len(blocker_ids):
        diagnostics.append(_diagnostic('BLOCKER_ID_DUPLICATE', 'blocker ids must be unique', 'mission.json'))
    if declared == 'BLOCKED' and isinstance(missing_records, list):
        for record in missing_records:
            if isinstance(record, dict) and record.get('artifact') in ARTIFACTS and record.get('blocker_id') not in blocker_ids:
                diagnostics.append(_diagnostic('MISSING_ARTIFACT_BLOCKER_LINK_INVALID', 'blocked missing-artifact record must reference an existing blocker id', record['artifact']))
    if declared == 'BLOCKED' and not blockers:
        diagnostics.append(_diagnostic('BLOCKED_WITHOUT_EVIDENCE', 'BLOCKED requires at least one attributable blocker record', 'mission.json'))
    if declared != 'BLOCKED' and blockers:
        diagnostics.append(_diagnostic('BLOCKER_STATE_MISMATCH', 'blocker records require completion_state BLOCKED', 'mission.json'))

    remaining = mission.get('remaining_work')
    if not isinstance(remaining, list) or any(not isinstance(item, str) or not item.strip() for item in remaining):
        diagnostics.append(_diagnostic('REMAINING_WORK_INVALID', 'remaining_work must be a list of non-empty descriptions', 'mission.json'))
        remaining = []
    if declared in {'INCOMPLETE', 'BLOCKED'} and not remaining:
        diagnostics.append(_diagnostic('REMAINING_WORK_MISSING', f'{declared} requires recorded remaining work', 'mission.json'))
    if declared == 'COMPLETE' and remaining:
        diagnostics.append(_diagnostic('COMPLETE_HAS_REMAINING_WORK', 'COMPLETE cannot declare unfinished work', 'mission.json'))

    if declared == 'COMPLETE':
        if not isinstance(missing_records, list) or missing_records:
            diagnostics.append(_diagnostic('COMPLETE_HAS_MISSING_ARTIFACTS', 'COMPLETE cannot declare missing artifacts', 'mission.json'))
        if attempts and any(isinstance(a, dict) and a.get('status') != 'SUCCEEDED' for a in attempts):
            diagnostics.append(_diagnostic('COMPLETE_HAS_UNSUCCESSFUL_PROVIDER_ATTEMPT', 'COMPLETE requires all attempted mandatory provider operations to succeed', 'provider_attempts.json'))
        if isinstance(evaluation, dict) and evaluation.get('state') == 'FAIL':
            diagnostics.append(_diagnostic('COMPLETE_HAS_FAILED_EVALUATION', 'COMPLETE cannot contain a failed evaluation', 'evaluation_result.json'))
    validated_state = 'NOT_CLAIMED'
    if research_state == 'VALIDATED':
        validation = evaluation.get('validation') if isinstance(evaluation, dict) else None
        cross_validation = (validation.get('walk_forward'), validation.get('cpcv')) if isinstance(validation, dict) else (None, None)
        supported = all(_valid_cross_validation(result) for result in cross_validation)
        before = len(diagnostics)
        if not (isinstance(evaluation, dict) and evaluation.get('state') == 'PASS' and supported):
            diagnostics.append(_diagnostic('VALIDATED_UNSUPPORTED', 'VALIDATED requires a passing evaluation with non-empty, disjoint walk-forward and CPCV folds using research-crossvalidation.v1', 'evaluation_result.json'))
        else:
            for kind, result in zip(('walk_forward', 'cpcv'), cross_validation):
                windows = result.get('label_windows', validation.get('label_windows'))
                for code, message, state in integrity.chronology_problems(result, windows, walk_forward=kind == 'walk_forward'):
                    diagnostics.append(_diagnostic(code, f'{kind}: {message}', 'evaluation_result.json', state))
        if source_state != 'ARCHIVE_RESOLVED':
            diagnostics.append(_diagnostic('VALIDATED_EVIDENCE_UNVERIFIED', f'VALIDATED requires every evidence reference to resolve in an archive (source provenance is {source_state})', 'evidence.json', 'UNKNOWN' if source_state == 'UNKNOWN' else 'INVALID'))
        if quant_state != 'VERIFIED':
            diagnostics.append(_diagnostic('VALIDATED_QUANTITATIVE_UNVERIFIED', f'VALIDATED requires metrics recomputed from archived inputs (quantitative verification is {quant_state})', 'metrics.json', quant_state if quant_state in ('UNKNOWN', 'INCOMPLETE', 'UNSUPPORTED', 'INVALID') else 'UNSUPPORTED'))
        validated_state = 'UNSUPPORTED' if len(diagnostics) > before else 'CLAIM_GATES_PASSED'
    blocked_expected_diagnostics = {'ARTIFACT_REFERENCE_MISSING', 'ARTIFACT_MISSING', 'EVIDENCE_REFERENCES_INVALID',
                                    'PROVIDER_ATTEMPTS_INVALID', 'HYPOTHESIS_STATE_INVALID',
                                    'EVALUATION_STATE_INVALID', 'THESIS_RECORD_INVALID',
                                    'COMMITTEE_VERIFICATION_MISSING'}
    blocked_missing_only = bool(missing_names) and all(
        item['code'] in blocked_expected_diagnostics and item.get('artifact') in missing_names
        for item in diagnostics)
    if not isinstance(declared, str) or declared not in COMPLETION_STATES or diagnostics and not (declared == 'BLOCKED' and blockers and blocked_missing_only):
        status = 'INCOMPLETE'
    else:
        status = declared
    artifact_codes = [d for d in diagnostics if d['code'].startswith('ARTIFACT_') or d['code'] in ('BASELINE_INVALID', 'MISSION_FIELD_MISSING')]
    review_bad = [d for d in diagnostics if d['code'].startswith(('COMMITTEE_', 'REVIEW_'))]
    layers = {
        'artifact_integrity': {'state': ('INVALID' if any(d['state'] == 'INVALID' for d in artifact_codes) else
                                         'INCOMPLETE' if artifact_codes else 'VERIFIED'),
                               'scope': 'recorded SHA-256 digests only; not authenticity'},
        'source_provenance': {'state': source_state,
                              'scope': 'evidence ids resolved against a local archive; not proof the provider was truthful'},
        'review_provenance': {'state': ('INVALID' if any(d['state'] == 'INVALID' for d in review_bad) else
                                        'INCOMPLETE' if review_bad else 'ASSERTED_UNAUTHENTICATED'),
                              'authenticated': False,
                              'scope': 'a PASS record exists; reviewer identity and substance are not attested'},
        'quantitative_verification': {'state': quant_state,
                                      'scope': 'Decimal recomputation from archived inputs through a declared transform'},
        'scientific_validity': {'state': validated_state, 'established': False,
                                'scope': 'CLAIM_GATES_PASSED means preconditions held; it is not evidence of predictive edge'},
    }
    return _result(status, diagnostics, layers, schema=mission.get('schema'))


def _result(status: str, diagnostics: list[dict], layers: dict | None = None, schema: object = None) -> dict:
    ordered = sorted(diagnostics, key=lambda d: (d['code'], d.get('artifact', ''), d['message']))
    return {'schema': 'research-mission-verification.v1', 'status': status, 'mission_schema': schema,
            'authenticity': 'NOT_AUTHENTICATED', 'layers': layers or {},
            'errors': [item['message'] for item in ordered], 'diagnostics': ordered}
