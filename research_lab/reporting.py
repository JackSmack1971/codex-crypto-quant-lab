"""Evidence-bound report composition (P2-A).

The coordinator's RENDERING operation calls :func:`compose_report`. Every quantitative claim is a structured record
with lineage; derived values are recomputed here (Decimal) from eligible archived observations; interpretations are
authored input, labelled inference, and may not introduce numbers. Nothing here performs I/O, executes authored
content, or contacts a provider. Canonical content (``report_content.json``) is separated from run metadata: the
manifest carries no wall-clock value, so identical pinned inputs yield identical bytes.
"""
from __future__ import annotations

import html as html_lib
import re
import unicodedata
from decimal import Decimal, DivisionByZero, InvalidOperation
from typing import Any
from urllib.parse import urlsplit

from .charts import CHART_RENDERER_VERSION, ChartError, build_chart, chart_html, chart_markdown, md_escape, venue_of
from .core import ContractError, _utc, canonical_json, digest, instant_key

INPUT_SCHEMA = 'research-intelligence-report.v1'
CONTENT_SCHEMA = 'research-report-content.v1'
MANIFEST_SCHEMA = 'research-report-manifest.v1'
COMPOSER_VERSION = '2'
CLASSES = {'R': 'Retrieved observation', 'D': 'Deterministically derived value', 'I': 'Inference or interpretation',
           'U': 'Unavailable or unsupported'}
TRANSFORM_IDS = {'difference': '1', 'pct_change': '2', 'ratio': '1', 'mean': '1'}
STANCES = ('base', 'bull', 'bear')
INPUT_KEYS = {'schema', 'mission_id', 'as_of_utc', 'executive_thesis', 'key_findings', 'derived_claims', 'interpretations',
              'risks', 'corroborations', 'charts'}
NUMBER = re.compile(r'(?<![\w.])[-+]?\d+(?:\.\d+)?(?![\w.])')
ID = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$')


class ReportError(ContractError):
    """Report-composition failure with a stable machine code (never classified by message text)."""

    def __init__(self, code: str, message: str = ''):
        super().__init__(f'{code}: {message}' if message else code)
        self.code, self.message = code, message or code


def _fmt(value: Decimal) -> str:
    return format(value.normalize(), 'f') if value != 0 else '0'


def _norm_number(text: str) -> str | None:
    try:
        return _fmt(Decimal(text))
    except InvalidOperation:
        return None


def _text(value: Any, field: str, limit: int = 2000) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ReportError('REPORT_INPUT_INVALID', f'{field} must be a non-empty string of at most {limit} characters')
    return value.strip()


def _refs(value: Any, field: str, known: dict[str, dict]) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value) or len(set(value)) != len(value):
        raise ReportError('REPORT_INPUT_INVALID', f'{field} must be a list of unique claim ids')
    for ref in value:
        if ref not in known:
            raise ReportError('CLAIM_REFERENCE_UNRESOLVED', f'{field} cites unknown claim {ref!r}')
    return list(value)


def _freshness(age: float, limit: int) -> str:
    return 'FRESH' if age <= limit else 'STALE'


def _retrieved_claim(o: dict, as_of: str, max_age: int) -> dict:
    age = (_utc(as_of) - _utc(o['observed_at_utc'])).total_seconds()
    return {'claim_id': 'R:' + o['id'], 'classification': 'R', 'metric': o['metric'], 'instrument_id': o['instrument_id'],
            'venue': venue_of(o['instrument_id']), 'value': o['value'], 'unit': o['unit'],
            'observed_at_utc': o['observed_at_utc'], 'retrieved_at_utc': o['retrieved_at_utc'],
            'available_at_utc': o['available_at_utc'], 'evidence_ids': [o['id']], 'transform': None,
            'source': o['source_uri'], 'freshness': {'status': _freshness(age, max_age), 'age_seconds': int(age), 'max_age_seconds': max_age},
            'limitations': ['provider measurement as archived; not independently re-verified by the report layer']}


def _latest(stamps: list[str]) -> str:
    """Latest instant by value; ties between spellings resolve by text so the result never depends on input order."""
    return max(stamps, key=lambda t: (instant_key(t), t))


def _derive(item: Any, observations: dict[str, dict], eligible: set[str], as_of: str, max_age: int, taken: set[str]) -> dict:
    if not isinstance(item, dict) or not {'claim_id', 'transform', 'input_evidence_ids'} <= set(item) \
            or set(item) - {'claim_id', 'transform', 'input_evidence_ids', 'expected_value', 'description'}:
        raise ReportError('REPORT_INPUT_INVALID', 'derived claim needs claim_id, transform, input_evidence_ids')
    if not isinstance(item['claim_id'], str) or not ID.fullmatch(item['claim_id']):
        raise ReportError('REPORT_INPUT_INVALID', 'derived claim_id is malformed')
    cid = 'D:' + item['claim_id']
    if cid in taken:
        raise ReportError('REPORT_INPUT_INVALID', f'duplicate claim id {cid}')
    kind = item['transform']
    if kind not in TRANSFORM_IDS:
        raise ReportError('REPORT_TRANSFORM_UNSUPPORTED', f'transform {kind!r} is not supported (allowed: {sorted(TRANSFORM_IDS)})')
    ids = item['input_evidence_ids']
    if not isinstance(ids, list) or not all(isinstance(i, str) for i in ids) or len(set(ids)) != len(ids):
        raise ReportError('REPORT_INPUT_INVALID', 'input_evidence_ids must be unique strings')
    need = (2, 2) if kind != 'mean' else (2, 64)
    if not need[0] <= len(ids) <= need[1]:
        raise ReportError('DERIVED_INPUT_INVALID', f'{kind} needs {need[0]}..{need[1]} inputs')
    rows = []
    for oid in ids:
        if oid not in observations:
            raise ReportError('DERIVED_INPUT_UNRESOLVED', f'evidence {oid!r} is not part of the mission evidence')
        if oid not in eligible:
            raise ReportError('DERIVED_INPUT_INELIGIBLE', f'evidence {oid!r} was excluded by reconciliation')
        o = observations[oid]
        if any(_utc(o[k]) > _utc(as_of) for k in ('observed_at_utc', 'retrieved_at_utc', 'available_at_utc')):
            raise ReportError('DERIVED_INPUT_INELIGIBLE', f'evidence {oid!r} is dated after the research as-of')
        try:
            value = Decimal(str(o['value']))
        except InvalidOperation as exc:
            raise ReportError('DERIVED_INPUT_INVALID', f'evidence {oid!r} is not numeric') from exc
        if not value.is_finite():
            raise ReportError('DERIVED_INPUT_INVALID', f'evidence {oid!r} is not finite')
        rows.append((o, value))
    first = rows[0][0]
    if len({r[0]['instrument_id'] for r in rows}) != 1:
        raise ReportError('DERIVED_IDENTITY_MISMATCH', 'inputs span different instruments or venues')
    if kind != 'ratio' and len({r[0]['metric'] for r in rows}) != 1:
        raise ReportError('DERIVED_IDENTITY_MISMATCH', 'inputs have different metric definitions')
    if kind != 'ratio' and len({r[0]['unit'] for r in rows}) != 1:
        raise ReportError('DERIVED_UNIT_MISMATCH', 'inputs have different units')
    try:
        if kind == 'difference':
            a, b = sorted(rows, key=lambda r: _utc(r[0]['observed_at_utc']))
            if _utc(a[0]['observed_at_utc']) == _utc(b[0]['observed_at_utc']):
                raise ReportError('DERIVED_INPUT_INVALID', 'inputs share one observation time')
            result, unit = b[1] - a[1], first['unit']
        elif kind == 'pct_change':
            a, b = sorted(rows, key=lambda r: _utc(r[0]['observed_at_utc']))
            if _utc(a[0]['observed_at_utc']) == _utc(b[0]['observed_at_utc']):
                raise ReportError('DERIVED_INPUT_INVALID', 'inputs share one observation time')
            result, unit = Decimal(100) * (b[1] - a[1]) / abs(a[1]), 'percent'  # abs(): a rise from a negative base is positive
        elif kind == 'ratio':
            if _utc(rows[0][0]['observed_at_utc']) != _utc(rows[1][0]['observed_at_utc']):
                raise ReportError('DERIVED_TIME_MISALIGNED', 'ratio inputs must share one observation time')
            result, unit = rows[0][1] / rows[1][1], f"{rows[0][0]['unit']}/{rows[1][0]['unit']}"
        else:
            result, unit = sum(v for _, v in rows) / len(rows), first['unit']
    except (DivisionByZero, InvalidOperation) as exc:
        raise ReportError('DERIVED_DIVISION_INVALID', 'division by zero in a derived value') from exc
    if not result.is_finite():
        raise ReportError('DERIVED_INPUT_INVALID', 'derived value is not finite')
    expected = item.get('expected_value')
    if expected is not None and _norm_number(str(expected)) != _fmt(result):
        raise ReportError('DERIVED_VALUE_MISMATCH', f'authored expected_value {expected!r} differs from the recomputed {_fmt(result)}')
    oldest = min(_utc(o['observed_at_utc']) for o, _ in rows)
    age = (_utc(as_of) - oldest).total_seconds()
    return {'claim_id': cid, 'classification': 'D', 'metric': first['metric'] if kind != 'ratio' else f"{rows[0][0]['metric']}/{rows[1][0]['metric']}",
            'instrument_id': first['instrument_id'], 'venue': venue_of(first['instrument_id']), 'value': _fmt(result), 'unit': unit,
            'observed_at_utc': _latest([o['observed_at_utc'] for o, _ in rows]), 'retrieved_at_utc': _latest([o['retrieved_at_utc'] for o, _ in rows]),
            'available_at_utc': _latest([o['available_at_utc'] for o, _ in rows]), 'evidence_ids': sorted(ids),
            'transform': ({'id': kind, 'version': TRANSFORM_IDS[kind], 'numerator_id': ids[0], 'denominator_id': ids[1]} if kind == 'ratio'
                          else {'id': kind, 'version': TRANSFORM_IDS[kind]}), 'source': sorted({o['source_uri'] for o, _ in rows}),
            'freshness': {'status': _freshness(age, max_age), 'age_seconds': int(age), 'max_age_seconds': max_age},
            'limitations': [f'derived by {kind}@{TRANSFORM_IDS[kind]} from the listed observations only',
                            'inherits every limitation of its inputs']}


VALIDATION_WORDS = re.compile(r'\b(validated|proven|guaranteed)\b', re.I)
PLAIN_NUMBER = re.compile(r'[-+]?\d+(?:\.\d+)?')


def _check_interpretation(text: str, refs: list[str], claims: dict[str, dict], field: str) -> None:
    """Interpretation may not claim validation and may contain only numbers that are values of the claims it cites.
    Any token containing a digit (1e3, 100x, Q4, 10:00, fractions) that is not exactly such a value is rejected;
    spelled-out numbers ("one hundred") cannot be detected and are a documented limit."""
    if VALIDATION_WORDS.search(text):
        raise ReportError('INTERPRETATION_UNSUPPORTED', f'{field} claims validation; reports cannot designate VALIDATED')
    allowed = {_norm_number(c['value']) for r in refs for c in [claims[r]] if c['classification'] in 'RD'}
    for raw in re.findall(r'\S+', unicodedata.normalize('NFKC', text)):
        if not any(ch.isdigit() or unicodedata.numeric(ch, None) is not None for ch in raw):
            continue
        core = raw.strip('.,;:!?()[]{}"\'%')
        if PLAIN_NUMBER.fullmatch(core) and _norm_number(core) in allowed:
            continue
        raise ReportError('INTERPRETATION_INTRODUCES_NUMBER',
                          f'{field} contains {raw!r}, which is not the value of a claim it cites')


def _source_family(uri: str) -> str:
    return (urlsplit(uri).hostname or uri).lower()


def validate_report_input(raw: Any, context: dict) -> dict:
    """Validate the authored report input against the mission and return the claims-resolved form."""
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ReportError('REPORT_INPUT_INVALID', 'report input must be an object')
    if raw and raw.get('schema') != INPUT_SCHEMA:
        raise ReportError('REPORT_INPUT_INVALID', f'schema must be {INPUT_SCHEMA}')
    if set(raw) - INPUT_KEYS:
        raise ReportError('REPORT_INPUT_INVALID', f'unsupported fields: {sorted(set(raw) - INPUT_KEYS)} (no HTML or code fields exist)')
    if raw and raw.get('mission_id') != context['mission_id']:
        raise ReportError('REPORT_INPUT_INVALID', 'mission_id differs from the mission')
    if raw and raw.get('as_of_utc') != context['as_of_utc']:
        raise ReportError('REPORT_INPUT_INVALID', 'as_of_utc differs from the mission research as-of')
    return raw


def compose_report(context: dict, authored: Any, *, report_input_digest: str | None) -> dict:
    """Compose canonical content, Markdown, HTML and a manifest from reconciled evidence and authored input."""
    raw = validate_report_input(authored, context)
    for key in ('derived_claims', 'key_findings', 'interpretations', 'risks', 'corroborations', 'charts'):
        if key in raw and not isinstance(raw[key], list):
            raise ReportError('REPORT_INPUT_INVALID', f'{key} must be a list')
    as_of, max_age = context['as_of_utc'], context['max_age_seconds']
    observations, eligible = context['observations'], set(context['eligible_observation_ids'])
    claims: dict[str, dict] = {}
    for oid in sorted(eligible):
        claims['R:' + oid] = _retrieved_claim(observations[oid], as_of, max_age)
    for index, item in enumerate(raw.get('derived_claims', [])):
        claim = _derive(item, observations, eligible, as_of, max_age, set(claims))
        claims[claim['claim_id']] = claim
    for miss in context['missing_required']:
        cid = f"U:{miss['instrument_id']}|{miss['metric']}"
        claims[cid] = {'claim_id': cid, 'classification': 'U', 'metric': miss['metric'], 'instrument_id': miss['instrument_id'],
                       'venue': venue_of(miss['instrument_id']), 'value': None, 'unit': None, 'observed_at_utc': None,
                       'retrieved_at_utc': None, 'available_at_utc': None, 'evidence_ids': [], 'transform': None, 'source': None,
                       'freshness': {'status': 'UNAVAILABLE'}, 'limitations': ['no eligible observation: absent, stale, future-dated, '
                                                                                'out of scope, contradictory or unit-mismatched']}
    thesis = None
    if raw.get('executive_thesis') is not None:
        item = raw['executive_thesis']
        if not isinstance(item, dict) or set(item) != {'text', 'claim_refs'}:
            raise ReportError('REPORT_INPUT_INVALID', 'executive_thesis needs exactly text and claim_refs')
        refs = _refs(item['claim_refs'], 'executive_thesis.claim_refs', claims)
        text = _text(item['text'], 'executive_thesis.text')
        if not any(claims[r]['classification'] in 'RD' for r in refs):
            raise ReportError('INTERPRETATION_UNSUPPORTED', 'executive_thesis must cite at least one [R] or [D] claim')
        _check_interpretation(text, refs, claims, 'executive_thesis')
        thesis = {'text': text, 'claim_refs': refs, 'classification': 'I'}
    findings = raw.get('key_findings', [])
    if not isinstance(findings, list) or len(findings) > 7:
        raise ReportError('REPORT_INPUT_INVALID', 'key_findings must be a list of at most seven items')
    key_findings, seen = [], set()
    for i, item in enumerate(findings):
        if not isinstance(item, dict) or set(item) != {'id', 'text', 'claim_refs'} or not isinstance(item['id'], str) or not ID.fullmatch(item['id']) or item['id'] in seen:
            raise ReportError('REPORT_INPUT_INVALID', f'key_findings[{i}] needs unique id, text, claim_refs')
        seen.add(item['id'])
        refs = _refs(item['claim_refs'], f'key_findings[{i}].claim_refs', claims)
        text = _text(item['text'], f'key_findings[{i}].text')
        if not any(claims[r]['classification'] in 'RD' for r in refs):
            raise ReportError('INTERPRETATION_UNSUPPORTED', f'key_findings[{i}] must cite at least one [R] or [D] claim')
        _check_interpretation(text, refs, claims, f'key_findings[{i}]')
        key_findings.append({'id': item['id'], 'text': text, 'claim_refs': refs, 'classification': 'I'})
    scenarios, seen = [], set()
    for i, item in enumerate(raw.get('interpretations', [])):
        field = f'interpretations[{i}]'
        if not isinstance(item, dict) or set(item) != {'id', 'stance', 'text', 'claim_refs', 'assumptions', 'falsifiers'} \
                or not isinstance(item['id'], str) or not ID.fullmatch(item['id']) or item['id'] in seen or item['stance'] not in STANCES:
            raise ReportError('REPORT_INPUT_INVALID', f'{field} needs unique id, stance in {STANCES}, text, claim_refs, assumptions, falsifiers')
        seen.add(item['id'])
        refs = _refs(item['claim_refs'], f'{field}.claim_refs', claims)
        if not any(claims[r]['classification'] in 'RD' for r in refs):
            raise ReportError('INTERPRETATION_UNSUPPORTED', f'{field} ({item["stance"]}) cites no [R] or [D] claim; an unavailable metric cannot support it')
        text = _text(item['text'], f'{field}.text')
        lists = {}
        for key in ('assumptions', 'falsifiers'):
            if not isinstance(item[key], list) or not item[key] or not all(isinstance(x, str) and x.strip() for x in item[key]):
                raise ReportError('REPORT_INPUT_INVALID', f'{field}.{key} must be a non-empty list of strings')
            lists[key] = [x.strip() for x in item[key]]
            for x in lists[key]:
                _check_interpretation(x, refs, claims, f'{field}.{key}')
        _check_interpretation(text, refs, claims, field)
        scenarios.append({'id': item['id'], 'stance': item['stance'], 'text': text, 'claim_refs': refs, 'classification': 'I', **lists})
    risks = []
    for i, item in enumerate(raw.get('risks', [])):
        if not isinstance(item, dict) or set(item) != {'id', 'text', 'claim_refs'} or not isinstance(item['id'], str) or not ID.fullmatch(item['id']):
            raise ReportError('REPORT_INPUT_INVALID', f'risks[{i}] needs id, text, claim_refs')
        refs = _refs(item['claim_refs'], f'risks[{i}].claim_refs', claims)
        text = _text(item['text'], f'risks[{i}].text')
        _check_interpretation(text, refs, claims, f'risks[{i}]')
        risks.append({'id': item['id'], 'text': text, 'claim_refs': refs, 'classification': 'I'})
    corroborations = []
    for i, item in enumerate(raw.get('corroborations', [])):
        if not isinstance(item, dict) or set(item) != {'id', 'evidence_ids', 'assertion'} or item['assertion'] != 'INDEPENDENT' \
                or not isinstance(item['id'], str) or not ID.fullmatch(item['id']):
            raise ReportError('REPORT_INPUT_INVALID', f'corroborations[{i}] needs id, evidence_ids and assertion == INDEPENDENT')
        ids = item['evidence_ids']
        if not isinstance(ids, list) or len(ids) < 2 or not all(isinstance(x, str) and x in eligible for x in ids):
            raise ReportError('CORROBORATION_NOT_INDEPENDENT', 'corroboration needs two or more eligible evidence ids')
        if item['id'] in {c['id'] for c in corroborations}:
            raise ReportError('REPORT_INPUT_INVALID', f'duplicate corroboration id {item["id"]!r}')
        if len({(observations[x]['instrument_id'], observations[x]['metric'], observations[x]['unit'], instant_key(observations[x]['observed_at_utc'])) for x in ids}) != 1:
            raise ReportError('CORROBORATION_NOT_INDEPENDENT', 'corroborating evidence must measure one instrument, metric, unit and observation time')
        families = [_source_family(observations[x]['source_uri']) for x in ids]
        if len(set(families)) != len(families):
            raise ReportError('CORROBORATION_NOT_INDEPENDENT',
                              f'evidence for {item["id"]!r} shares an underlying source family ({sorted(set(f for f in families if families.count(f) > 1))}); '
                              'agreement is not independent corroboration')
        corroborations.append({'id': item['id'], 'evidence_ids': sorted(ids), 'source_families': sorted(families),
                               'status': 'DISTINCT_SOURCE_HOSTS_ONLY', 'limitation': 'distinct hosts do not prove distinct underlying data'})
    charts = []
    chart_specs = raw.get('charts', [])
    if not isinstance(chart_specs, list):
        raise ReportError('REPORT_INPUT_INVALID', 'charts must be a list')
    chart_ids = set()
    for spec in chart_specs:
        try:
            chart = build_chart(spec, observations, eligible, as_of)
        except ChartError as exc:
            raise ReportError(exc.code, exc.message) from exc
        cid = chart.get('chart_id') or chart['meta']['chart_id']
        if cid in chart_ids:
            raise ReportError('REPORT_INPUT_INVALID', 'duplicate chart_id')
        chart_ids.add(cid)
        charts.append(chart)
    # ------------------------------------------------------------- canonical content
    state = context['research_state']
    grade = 'OBSERVATIONS_ONLY_NOT_A_TRADE_SIGNAL' if state == 'EVALUABLE' else 'INSUFFICIENT_EVIDENCE / NO_TRADE'
    referenced = sorted({e for c in claims.values() for e in c['evidence_ids']})
    content = {
        'schema': CONTENT_SCHEMA, 'mission_id': context['mission_id'], 'as_of_utc': as_of, 'spec_digest': context['spec_digest'],
        'evidence_snapshot_id': context['evidence_snapshot_id'], 'research_state': state, 'research_grade': grade,
        'decision_question': context['decision_question'], 'research_horizon': context['research_horizon'],
        'instruments': context['instruments'], 'max_age_seconds': max_age,
        'executive_thesis': thesis, 'key_findings': key_findings, 'scenarios': scenarios, 'risks': risks, 'corroborations': corroborations,
        'claims': [claims[k] for k in sorted(claims)], 'charts': [c['meta'] if c['status'] == 'AVAILABLE' else
                                                                  {k: c[k] for k in ('chart_id', 'title', 'status', 'reason_code', 'reason')}
                                                                  for c in charts],
        'excluded': context['excluded'], 'conflicts': context['conflicts'], 'missing_required': context['missing_required'],
        'hypotheses': context['hypotheses'], 'no_hypotheses_reason': context['no_hypotheses_reason'],
        'attempts': context['attempts'], 'reviews': context['reviews'], 'operations': context['operations'],
        'network_calls_by_coordinator': context['network_calls'], 'limitations': context['limitations'], 'experiment': context['experiment'],
        'provenance_limitations': [
            'observations are archived local inputs; the report layer does not re-contact any provider',
            'reviewer handoffs are unauthenticated assertions, not independent verification',
            'authored interpretation is inference [I] and is not independently verified evidence',
            'offline composition is not production readiness and not evidence of predictive edge'],
        'referenced_evidence_ids': referenced,
    }
    content_bytes = canonical_json(content)
    md, page = _markdown(content, charts), _html(content, charts)
    manifest = {
        'schema': MANIFEST_SCHEMA, 'mission_id': context['mission_id'], 'input_spec_digest': context['spec_digest'],
        'report_input_digest': report_input_digest, 'evidence_snapshot_id': context['evidence_snapshot_id'],
        'as_of_utc': as_of, 'content_sha256': _sha(content_bytes), 'report_md_sha256': _sha(md), 'report_html_sha256': _sha(page),
        'referenced_evidence_ids': referenced, 'composer_version': COMPOSER_VERSION, 'chart_renderer_version': CHART_RENDERER_VERSION,
        'chart_dataset_digests': {c['meta']['chart_id']: c['meta']['input_data_digest'] for c in charts if c['status'] == 'AVAILABLE'},
        'claim_counts': {k: sum(1 for c in claims.values() if c['classification'] == k) for k in 'RDIU'}
                        | {'I': len(key_findings) + len(scenarios) + len(risks) + (1 if thesis else 0)},
        'run_metadata': {'creation_timestamp': 'EXCLUDED_FROM_CANONICAL_CONTENT',
                         'note': 'wall-clock times of report creation are recorded by the mission store (stages.completed_utc), '
                                 'not in canonical artifacts, so identical pinned inputs give identical bytes'},
        'provenance_limitations': content['provenance_limitations'],
    }
    return {'content': content_bytes, 'report.md': md, 'report.html': page, 'manifest': canonical_json(manifest)}


def _sha(data: bytes) -> str:
    import hashlib
    return hashlib.sha256(data).hexdigest()


def verify_companions(files: dict[str, bytes], *, spec_digest: str, mission_id: str, evidence_ids: set[str]) -> list[str]:
    """Independent FINALIZING check of the RENDERING companions; returns problem strings (empty = consistent)."""
    import json
    problems = []
    try:
        manifest, content = json.loads(files['report_manifest.json']), json.loads(files['report_content.json'])
    except (KeyError, ValueError, UnicodeError):
        return ['report companions are missing or not valid JSON']
    if manifest.get('schema') != MANIFEST_SCHEMA or content.get('schema') != CONTENT_SCHEMA:
        problems.append('companion schema mismatch')
    if _sha(files['report_content.json']) != manifest.get('content_sha256'):
        problems.append('report_content.json digest differs from the manifest')
    for name, key in (('report.md', 'report_md_sha256'), ('report.html', 'report_html_sha256')):
        if _sha(files[name]) != manifest.get(key):
            problems.append(f'{name} digest differs from the manifest')
    if manifest.get('input_spec_digest') != spec_digest or content.get('spec_digest') != spec_digest:
        problems.append('companion spec digest differs from the mission spec')
    if manifest.get('mission_id') != mission_id or content.get('mission_id') != mission_id:
        problems.append('companion mission id differs from the mission')
    if set(manifest.get('referenced_evidence_ids', [])) != set(content.get('referenced_evidence_ids', [])):
        problems.append('manifest and content disagree on referenced evidence')
    if not set(content.get('referenced_evidence_ids', [])) <= evidence_ids:
        problems.append('report cites evidence that is not in the verified evidence references')
    if not isinstance(content.get('claims'), list):
        return problems + ['claims is not a list']
    for claim in content['claims']:
        if claim['classification'] in 'RD' and (claim.get('value') is None or not claim.get('evidence_ids')):
            problems.append(f"claim {claim['claim_id']} has no value or evidence")
        if claim['classification'] == 'U' and claim.get('value') is not None:
            problems.append(f"unavailable claim {claim['claim_id']} carries a value")
    return problems


_UNSET = object()
SEMANTIC_VERIFIER_VERSION = '1'


def verify_companion_semantics(files: dict[str, bytes], *, spec_digest: str, mission_id: str, observations: dict[str, dict],
                               eligible: set[str], as_of: str, max_age: int | None = None,
                               evidence_snapshot_id: Any = _UNSET, report_input_digest: Any = _UNSET) -> list[str]:
    """Semantic verification of the RENDERING companions against *archived observations*, not merely against each
    other. A recomputed manifest hash is local-writer self-consistency; this function re-derives every [R] claim from
    its archived observation and every [D] claim from its declared transform and archived inputs, and binds the
    companions to the mission (id, spec digest, as-of, evidence snapshot, pinned report input).

    ``observations`` maps id -> archived observation fields; ``eligible`` is the verified evidence reference set.
    Returns problem strings (empty = semantically verified). Not covered: rendered chart tables and the prose of
    report.md/html beyond the presence of each recorded claim value (those remain hash-bound only)."""
    import json
    problems = verify_companions(files, spec_digest=spec_digest, mission_id=mission_id, evidence_ids=set(eligible))
    try:
        manifest, content = json.loads(files['report_manifest.json']), json.loads(files['report_content.json'])
    except (KeyError, ValueError, UnicodeError):
        return problems or ['report companions are missing or not valid JSON']
    if not isinstance(content, dict) or not isinstance(manifest, dict) or not isinstance(content.get('claims'), list):
        return problems + ['companion structure is malformed']
    if content.get('as_of_utc') != as_of or manifest.get('as_of_utc') != as_of:
        problems.append('companion as-of differs from the mission as-of')
    if evidence_snapshot_id is not _UNSET and (content.get('evidence_snapshot_id') != evidence_snapshot_id
                                               or manifest.get('evidence_snapshot_id') != evidence_snapshot_id):
        problems.append('companion evidence snapshot differs from the mission evidence snapshot')
    if report_input_digest is not _UNSET and manifest.get('report_input_digest') != report_input_digest:
        problems.append('companion report-input digest differs from the pinned mission input')
    age_limit = content.get('max_age_seconds') if max_age is None else max_age
    if max_age is not None and content.get('max_age_seconds') != max_age:
        problems.append('companion max_age_seconds differs from the mission spec')
    if isinstance(age_limit, bool) or not isinstance(age_limit, int):
        return problems + ['companion max_age_seconds is not an integer']
    try:
        as_of_ok = _utc(as_of) is not None
    except ContractError:
        as_of_ok = False
    if not as_of_ok:
        return problems + ['mission as-of is not a valid UTC timestamp']
    md = files.get('report.md', b'').decode('utf-8', 'replace')
    page = files.get('report.html', b'').decode('utf-8', 'replace')
    seen_r: set[str] = set()
    for claim in content['claims']:
        if not isinstance(claim, dict):
            problems.append('a claim is not an object')
            continue
        cid, kind = claim.get('claim_id'), claim.get('classification')
        if kind == 'R':
            oid = cid[2:] if isinstance(cid, str) and cid.startswith('R:') else None
            seen_r.add(oid)
            if oid is None or oid not in observations:
                problems.append(f'{cid}: archived observation is missing')
                continue
            if oid not in eligible:
                problems.append(f'{cid}: observation is not part of the verified eligible evidence')
                continue
            o = observations[oid]
            if o.get('status', 'OBSERVED') != 'OBSERVED' or o.get('value') is None or o.get('observed_at_utc') is None:
                problems.append(f'{cid}: archived observation is not an OBSERVED value')
                continue
            try:
                expected = _retrieved_claim(o, as_of, age_limit)
            except (ContractError, KeyError, TypeError) as exc:
                problems.append(f'{cid}: archived observation is unusable ({type(exc).__name__})')
                continue
            diff = sorted(k for k in set(expected) | set(claim) if expected.get(k) != claim.get(k))
            if diff:
                problems.append(f'{cid}: differs from the archived observation in {", ".join(diff)}')
        elif kind == 'D':
            transform = claim.get('transform')
            if not isinstance(transform, dict) or transform.get('id') not in TRANSFORM_IDS \
                    or transform.get('version') != TRANSFORM_IDS.get(transform.get('id')) or not isinstance(cid, str) or not cid.startswith('D:'):
                problems.append(f'{cid}: declared transform is unknown or has the wrong version')
                continue
            ids = claim.get('evidence_ids')
            if transform['id'] == 'ratio':
                ids = [transform.get('numerator_id'), transform.get('denominator_id')]
            try:
                recomputed = _derive({'claim_id': cid[2:], 'transform': transform['id'], 'input_evidence_ids': list(ids)},
                                     observations, set(eligible), as_of, age_limit, set())
            except (ReportError, TypeError) as exc:
                problems.append(f'{cid}: cannot be recomputed from archived inputs ({getattr(exc, "code", type(exc).__name__)})')
                continue
            diff = sorted(k for k in set(recomputed) | set(claim) if recomputed.get(k) != claim.get(k))
            if diff:
                problems.append(f'{cid}: differs from recomputation in {", ".join(diff)}')
        elif kind == 'U':
            continue
        else:
            problems.append(f'{cid}: unknown claim classification {kind!r}')
            continue
        if kind in ('R', 'D') and isinstance(claim.get('value'), str):
            if md and md_escape(claim['value']) not in md:
                problems.append(f'{cid}: recorded value is absent from report.md')
            if page and _html_escape(claim['value']) not in page:
                problems.append(f'{cid}: recorded value is absent from report.html')
    expected_r = {oid for oid in eligible}
    if seen_r != expected_r:
        problems.append('[R] claims do not match the verified eligible evidence (missing: %s; extra: %s)'
                        % (sorted(expected_r - seen_r), sorted(x for x in seen_r - expected_r if x is not None)))
    cited = sorted({e for c in content['claims'] if isinstance(c, dict) for e in (c.get('evidence_ids') or [])})
    if content.get('referenced_evidence_ids') != cited:
        problems.append('referenced_evidence_ids does not equal the evidence cited by the claims')
    for chart in content.get('charts', []):
        if isinstance(chart, dict) and chart.get('status') == 'AVAILABLE':
            bad = [x for x in chart.get('observation_ids', []) if x not in eligible or x not in observations]
            if bad:
                problems.append(f'chart {chart.get("chart_id")}: cites observations outside the verified evidence ({bad})')
    return problems


LEGACY_RENDER_CONTRACT = 'render.local_report@1'
COMPANION_FILES = ('report_content.json', 'report_manifest.json', 'report.md', 'report.html')


def verify_bundle_companions(root: Any, archive: Any = None) -> dict:
    """Single bundle-level companion verdict shared by finalization, ``scripts/verify_mission.py`` and history.

    status: ``SEMANTICALLY_VERIFIED`` (every [R]/[D] claim re-derived from the archive) | ``NOT_VERIFIED_NO_ARCHIVE``
    (hash/binding consistency only) | ``ABSENT_DECLARED_V1`` (baseline declares a render@1 legacy mission; no companions
    expected) | ``ABSENT_UNATTESTED_LEGACY`` (no companions and no declaration: a pre-P2 bundle OR a stripped one;
    nothing report-derived may be trusted) | ``FAILED``. Hashes and manifests written by the same local writer are
    never treated as authentication."""
    import json
    from dataclasses import asdict
    from pathlib import Path
    from .archive import ArchiveDatabaseError, ArchiveIntegrityError, EvidenceArchive, EvidenceMissing
    root = Path(root)

    def load(name):
        try:
            return json.loads((root / name).read_text(encoding='utf-8'))
        except (OSError, ValueError, UnicodeError):
            return None
    baseline, evidence = load('mission_baseline.json'), load('evidence.json')
    declared_legacy = isinstance(baseline, dict) and baseline.get('render_contract') == LEGACY_RENDER_CONTRACT
    present = [n for n in COMPANION_FILES[:2] if (root / n).exists()]
    if not present:
        final = load('final_verification.json')
        attested = isinstance(final, dict) and isinstance(final.get('report_companions'), dict) and \
            (final['report_companions'].get('checked') or final['report_companions'].get('verdict') == 'SEMANTICALLY_VERIFIED')
        if attested and not declared_legacy:   # the finalized bundle says companions were verified; they were removed afterwards
            return {'status': 'FAILED', 'problems': ['final_verification attests report companions that are no longer present']}
        return {'status': 'ABSENT_DECLARED_V1' if declared_legacy else 'ABSENT_UNATTESTED_LEGACY', 'problems': []}
    if declared_legacy:
        return {'status': 'FAILED', 'problems': ['baseline declares render.local_report@1 yet report companions exist']}
    if not isinstance(baseline, dict) or not isinstance(evidence, dict) or not isinstance(evidence.get('references'), list):
        return {'status': 'FAILED', 'problems': ['baseline or evidence references are unreadable']}
    files = {}
    for name in COMPANION_FILES:
        try:
            files[name] = (root / name).read_bytes()
        except OSError:
            pass
    if len(files) != 4:
        return {'status': 'FAILED', 'problems': ['report companions or reports are missing']}
    ev_ids = {r['id'] for r in evidence['references'] if isinstance(r, dict) and isinstance(r.get('id'), str)}
    try:
        return _bundle_verdict(files, baseline, ev_ids, archive)
    except (TypeError, KeyError, AttributeError, ValueError) as exc:  # malformed companion structure fails closed
        return {'status': 'FAILED', 'problems': [f'companions are structurally malformed ({type(exc).__name__})']}


def _bundle_verdict(files: dict, baseline: dict, ev_ids: set, archive: Any) -> dict:
    import json  # noqa: F401
    from dataclasses import asdict
    from pathlib import Path
    from .archive import ArchiveDatabaseError, ArchiveIntegrityError, EvidenceArchive, EvidenceMissing
    if archive is None or not Path(archive).exists():
        problems = verify_companions(files, spec_digest=baseline.get('spec_digest'), mission_id=baseline.get('run_id'), evidence_ids=ev_ids)
        return {'status': 'FAILED' if problems else 'NOT_VERIFIED_NO_ARCHIVE', 'problems': problems}
    observations = {}
    try:
        store = EvidenceArchive(archive)
        for oid in sorted(ev_ids):
            try:
                observations[oid] = asdict(store.get(oid))
            except (EvidenceMissing, ArchiveIntegrityError, ArchiveDatabaseError):
                pass  # unresolved evidence is reported by the verifier as missing
    except Exception:  # noqa: BLE001 - an unreadable archive means nothing can be re-derived
        observations = {}
    problems = verify_companion_semantics(files, spec_digest=baseline.get('spec_digest'), mission_id=baseline.get('run_id'),
                                          observations=observations, eligible=ev_ids, as_of=baseline.get('as_of_utc'))
    return {'status': 'FAILED' if problems else 'SEMANTICALLY_VERIFIED', 'problems': problems}


# --------------------------------------------------------------------------------- rendering
def _md(text: Any) -> str:
    return md_escape(text)


def _mdtable(headers: list[str], rows: list[list[Any]]) -> str:
    out = '| ' + ' | '.join(headers) + ' |\n|' + '---|' * len(headers) + '\n'
    return out + ''.join('| ' + ' | '.join(_md(c) for c in r) + ' |\n' for r in rows)


def _claim_rows(content: dict, kinds: str) -> list[list[Any]]:
    out = []
    for c in content['claims']:
        if c['classification'] in kinds:
            src = c['source'] if isinstance(c['source'], str) or c['source'] is None else ', '.join(c['source'])
            out.append([f"[{c['classification']}] {c['claim_id']}", c['instrument_id'], c['venue'], c['metric'], c['value'] if c['value'] is not None else 'UNAVAILABLE',
                        c['unit'] or '—', c['observed_at_utc'] or 'n/a', c['retrieved_at_utc'] or 'n/a', c['available_at_utc'] or 'n/a',
                        ', '.join(c['evidence_ids']) or '—', (c['transform'] and f"{c['transform']['id']}@{c['transform']['version']}") or '—',
                        src or '—', c['freshness']['status']])
    return out


CLAIM_HEAD = ['Claim', 'Instrument', 'Venue', 'Metric', 'Value', 'Unit', 'Observed UTC', 'Retrieved UTC', 'Available UTC', 'Evidence',
              'Transform', 'Source', 'Freshness']


def _sections(content: dict) -> list[tuple[str, list[tuple[str, Any]]]]:
    """Section model shared by both renderers: (heading, [(kind, payload)]), kinds: p, ul, table, charts."""
    state = content['research_state']
    ev_rows = _claim_rows(content, 'RD')
    un_rows = _claim_rows(content, 'U')
    sc = {s: [x for x in content['scenarios'] if x['stance'] == s] for s in STANCES}
    secs = []
    secs.append(('Executive thesis', [('p', ('[I] ' + content['executive_thesis']['text'] + ' (cites: ' + ', '.join(content['executive_thesis']['claim_refs']) + ')')
                                            if content['executive_thesis'] else
                                            f'No thesis is authored. Mechanical research state: {state}. [U] This report is an evidence ledger, not a forecast or trade recommendation.')]))
    secs.append(('Decision question and horizon', [('p', content['decision_question']), ('p', f"Horizon: {content['research_horizon']}. Instruments: {', '.join(content['instruments'])}.")]))
    secs.append(('Evidence quality and freshness', [
        ('p', f"Maximum permitted observation age: {content['max_age_seconds']} s. Evidence snapshot: {content['evidence_snapshot_id'] or 'none'}."),
        ('table', (CLAIM_HEAD, ev_rows or [])), ('p', 'Excluded observations:'),
        ('table', (['Observation', 'Reason'], [[e['id'], e['code']] for e in content['excluded']]))]))
    secs.append(('Key findings', [('ul', [f"[I] {f['text']} (cites: {', '.join(f['claim_refs'])})" for f in content['key_findings']])
                                  if content['key_findings'] else ('p', '[U] No key findings were authored; none are generated from the ledger.')]))
    secs.append(('Instrument and venue alignment', [('table', (['Instrument', 'Venue', 'Eligible claims'], [
        [i, venue_of(i), sum(1 for c in content['claims'] if c['instrument_id'] == i and c['classification'] in 'RD')] for i in content['instruments']]))]))
    quant: list[tuple[str, Any]] = []
    quant.append(('table', (CLAIM_HEAD, ev_rows)) if ev_rows else ('p', '[U] No qualifying quantitative claims are available.'))
    if content['hypotheses']:
        quant.append(('table', (['Hypothesis', 'Conclusion', 'Missing evidence'], [[h['hypothesis_id'], h['conclusion'], ', '.join(h['missing_evidence']) or '—'] for h in content['hypotheses']])))
    else:
        quant.append(('p', f"No hypotheses declared: {content['no_hypotheses_reason']}"))
    quant.append(('charts', None))
    secs.append(('Relevant quantitative research', quant))
    scen: list[tuple[str, Any]] = []
    for stance in STANCES:
        if sc[stance]:
            for s in sc[stance]:
                scen.append(('p', f"[I] {stance.upper()} ({s['id']}): {s['text']} (cites: {', '.join(s['claim_refs'])})"))
        else:
            scen.append(('p', f'[U] {stance.upper()} scenario: Not produced (not authored; none is generated by the report layer).'))
    if state != 'EVALUABLE':
        scen.append(('p', f'Evidence-limited: research state is {state}; scenarios, where present, are conditional inference only.'))
    secs.append(('Base, bullish and bearish scenarios', scen))
    assumptions = [a for s in content['scenarios'] for a in s['assumptions']]
    falsifiers = [f for s in content['scenarios'] for f in s['falsifiers']]
    secs.append(('Assumptions and falsifiers', [('ul', [f'[I] Assumption: {a}' for a in assumptions] + [f'[I] Falsifier: {f}' for f in falsifiers])
                                                if assumptions or falsifiers else ('p', '[U] No assumptions or falsifiers were authored.')]))
    secs.append(('Risk and failure conditions', [('ul', [f"[I] {r['text']} (cites: {', '.join(r['claim_refs'])})" for r in content['risks']])
                                                 if content['risks'] else ('p', '[U] No risk analysis was authored.')]))
    secs.append(('Methods and provider fallback disclosures', [
        ('p', f"Operations: {', '.join(content['operations'])}. Network calls by the coordinator: {content['network_calls_by_coordinator']}."),
        ('table', (['Operation', 'Status', 'Source', 'Note'], [[a['operation_id'], a['status'], a.get('source', ''), a.get('reason', a.get('error', ''))] for a in content['attempts']])),
        ('p', 'Reviews are asserted, unauthenticated reviewer handoffs:'),
        ('table', (['Reviewer', 'State', 'Note'], [[n, r['state'], r.get('reason', '')] for n, r in sorted(content['reviews'].items())])),
        ('ul', [f"Corroboration {c['id']}: {', '.join(c['evidence_ids'])} ({c['limitation']})" for c in content['corroborations']])
        if content['corroborations'] else ('p', 'No independent-corroboration claim is made.')]))
    secs.append(('Evidence ledger and bibliography', [('table', (['Observation', 'Instrument', 'Metric', 'Value', 'Unit', 'Observed UTC', 'Source'], [
        [c['evidence_ids'][0], c['instrument_id'], c['metric'], c['value'], c['unit'], c['observed_at_utc'], c['source']]
        for c in content['claims'] if c['classification'] == 'R']))]))
    gaps = [f"[U] {m['instrument_id']} / {m['metric']}: no eligible observation" for m in content['missing_required']]
    gaps += [f"conflict ({c['kind']}) {c['instrument_id']} / {c['metric']}: {', '.join(c['observation_ids'])}" for c in content['conflicts']]
    secs.append(('Missing data and unsupported conclusions', [('table', (CLAIM_HEAD, un_rows)) if un_rows else ('p', 'No required metric is missing.'),
                                                              ('ul', gaps) if gaps else ('p', 'No conflicts identified against the declared requirements.')]))
    final = (f'Research grade: {content["research_grade"]}. ' + ('Explicit NO_TRADE / INSUFFICIENT_EVIDENCE: required evidence is incomplete.'
             if state != 'EVALUABLE' else 'Observations only; this is not a trade signal and not evidence of predictive edge.'))
    secs.append(('Conclusion status', [('p', final), ('ul', content['limitations'] + content['provenance_limitations'])]))
    return secs


def _markdown(content: dict, charts: list[dict]) -> bytes:
    out = [f"# Research mission report: {_md(content['mission_id'])}\n\n**Research as-of UTC:** {content['as_of_utc']}  \n"
           f"**Research state:** `{content['research_state']}`  \n**Research grade:** `{content['research_grade']}`  \n"
           f"**Spec digest:** `{content['spec_digest']}`  \nClaim classes: [R] retrieved, [D] derived, [I] inference, [U] unavailable.\n"]
    for heading, blocks in _sections(content):
        out.append(f'\n## {heading}\n\n')
        for kind, payload in blocks:
            if kind == 'p':
                out.append(_md(payload) + '\n\n')
            elif kind == 'ul':
                out.append(''.join('- ' + _md(x) + '\n' for x in payload) + '\n')
            elif kind == 'table':
                out.append(_mdtable(*payload) + '\n')
            elif kind == 'charts':
                out.append(''.join(chart_markdown(c) + '\n' for c in charts) or '[U] No charts were requested.\n\n')
    return ''.join(out).encode('utf-8')


CSS = ('/* no scripts or remote assets */:root{color-scheme:light dark;--fg:#111;--bg:#fff;--line:#555}'
       '@media(prefers-color-scheme:dark){:root{--fg:#f1f1f1;--bg:#111;--line:#aaa}}'
       'body{font:16px/1.55 system-ui,sans-serif;max-width:72rem;margin:0 auto;padding:1rem;color:var(--fg);background:var(--bg)}'
       'a.skip{position:absolute;left:-999px}a.skip:focus{left:0;background:var(--bg);padding:.5rem}'
       'table{border-collapse:collapse;width:100%}th,td{border:1px solid var(--line);padding:.3rem .5rem;text-align:left;vertical-align:top}'
       '.scroll{overflow-x:auto}svg.chart{width:100%;height:auto;color:var(--fg)}summary{cursor:pointer}summary:focus-visible,a:focus-visible{outline:3px solid currentColor;outline-offset:2px}'
       'figure{margin:1rem 0}.note{font-size:.9rem}')


def _html(content: dict, charts: list[dict]) -> bytes:
    e = _html_escape
    parts = [f'<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
             f'<title>{e(content["mission_id"])} research report</title><style>{CSS}</style></head><body><a class="skip" href="#main">Skip to report</a><main id="main">'
             f'<h1>Research mission report: {e(content["mission_id"])}</h1><p><strong>Research as-of UTC:</strong> {e(content["as_of_utc"])}<br>'
             f'<strong>Research state:</strong> <code>{e(content["research_state"])}</code><br><strong>Research grade:</strong> <code>{e(content["research_grade"])}</code><br>'
             f'<strong>Spec digest:</strong> <code>{e(content["spec_digest"])}</code></p>'
             '<p>Claim classes: [R] retrieved observation, [D] deterministically derived, [I] inference or interpretation, [U] unavailable or unsupported.</p>']
    for n, (heading, blocks) in enumerate(_sections(content)):
        parts.append(f'<section aria-labelledby="s{n}"><h2 id="s{n}">{e(heading)}</h2>')
        for kind, payload in blocks:
            if kind == 'p':
                parts.append(f'<p>{e(payload)}</p>')
            elif kind == 'ul':
                parts.append('<ul>' + ''.join(f'<li>{e(x)}</li>' for x in payload) + '</ul>')
            elif kind == 'table':
                headers, rows = payload
                parts.append('<div class="scroll" role="region" tabindex="0" aria-label="' + e(heading) + ' table"><table><thead><tr>'
                             + ''.join(f'<th scope="col">{e(h)}</th>' for h in headers) + '</tr></thead><tbody>'
                             + ''.join('<tr>' + ''.join(f'<td>{e(c)}</td>' for c in r) + '</tr>' for r in rows) + '</tbody></table></div>')
            elif kind == 'charts':
                parts.append(''.join(chart_html(c) for c in charts) or '<p>[U] No charts were requested.</p>')
        parts.append('</section>')
    parts.append('</main></body></html>\n')
    return ''.join(parts).encode('utf-8')


def _html_escape(value: Any) -> str:
    return html_lib.escape(str(value), quote=True)
