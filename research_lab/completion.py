"""Deterministic integrity gate for locally assembled research mission bundles.

The gate assesses persisted process evidence. It does not execute or authorize
research work and does not assess whether a research conclusion is true.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath

ARTIFACTS = (
    'mission_baseline.json', 'provider_attempts.json', 'evidence.json',
    'metrics.json', 'hypothesis_assessment.json', 'evaluation_result.json',
    'thesis_revision.json', 'committee_verification.json', 'report.md',
    'report.html',
)
RESEARCH_STATES = {'NOT_EVALUABLE', 'INSUFFICIENT_EVIDENCE', 'EVALUABLE', 'VALIDATED'}
COMPLETION_STATES = {'COMPLETE', 'INCOMPLETE', 'BLOCKED'}
REQUIRED_REVIEWS = {'source_verifier', 'methodology_reviewer'}


def _diagnostic(code: str, message: str, artifact: str | None = None) -> dict:
    result = {'code': code, 'message': message}
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


def verify(root: Path) -> dict:
    """Validate a research-mission.v1 bundle and return stable machine diagnostics."""
    root = Path(root)
    diagnostics: list[dict] = []
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
    if mission.get('schema') != 'research-mission.v1':
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
    if research_state == 'VALIDATED':
        validation = evaluation.get('validation') if isinstance(evaluation, dict) else None
        cross_validation = (validation.get('walk_forward'), validation.get('cpcv')) if isinstance(validation, dict) else (None, None)
        supported = all(_valid_cross_validation(result) for result in cross_validation)
        if not (isinstance(evaluation, dict) and evaluation.get('state') == 'PASS' and supported):
            diagnostics.append(_diagnostic('VALIDATED_UNSUPPORTED', 'VALIDATED requires a passing evaluation with non-empty, disjoint walk-forward and CPCV folds using research-crossvalidation.v1', 'evaluation_result.json'))
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
    return _result(status, diagnostics)


def _result(status: str, diagnostics: list[dict]) -> dict:
    ordered = sorted(diagnostics, key=lambda d: (d['code'], d.get('artifact', ''), d['message']))
    return {'schema': 'research-mission-verification.v1', 'status': status,
            'errors': [item['message'] for item in ordered], 'diagnostics': ordered}
