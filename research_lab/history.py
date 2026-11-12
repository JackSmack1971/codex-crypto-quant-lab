"""Read-only historical thesis and mission comparison (P2-C).

``ResearchIntelligence`` remains the only authoritative store of thesis revisions. This service never writes to it,
never copies its records, and never lets a mission bundle create or modify a revision. Every output states the
original research time of each side, the source record digests, and keeps three things apart: what was known at the
earlier research time, what a later revision recorded, and what has never been independently verified.
A historical ``SUPPORTED`` is reported as supported *as of its own time*; validity is always ``NOT_REASSESSED`` here.
"""
from __future__ import annotations

import json
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from . import completion, reporting
from .core import ContractError, _utc, digest
from .intelligence import ResearchIntelligence

HISTORY_SCHEMA = 'research-history-comparison.v1'
SERVICE_VERSION = '1'
NOTICE = ('historical records keep their original research time; this comparison does not reassess any conclusion '
          'and later evidence does not rewrite an earlier one')


class HistoryError(ContractError):
    """Stable machine code for unusable, tampered, incompatible or contradictory historical records."""

    def __init__(self, code: str, message: str = ''):
        super().__init__(f'{code}: {message}' if message else code)
        self.code, self.message = code, message or code


def _d(value: Any) -> Decimal | None:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return number if number.is_finite() else None


def _fmt(value: Decimal) -> str:
    return format(value.normalize(), 'f') if value != 0 else '0'


class ThesisHistory:
    def __init__(self, intelligence: ResearchIntelligence):
        self._store = intelligence  # used only through get()/search(); no write path exists in this class

    # ------------------------------------------------------------------ revisions
    def _get(self, revision_id: str) -> dict:
        try:
            return self._store.get(revision_id)
        except ContractError as exc:  # ResearchIntelligence raises a bare ContractError; its message is the only discriminator
            code = 'REVISION_MISSING' if 'missing' in str(exc) else 'REVISION_TAMPERED'
            raise HistoryError(code, str(exc)) from exc
        except (ValueError, TypeError, KeyError) as exc:
            raise HistoryError('REVISION_TAMPERED', f'unreadable revision record ({type(exc).__name__})') from exc

    def timeline(self, thesis_id: str) -> dict:
        """Ordered, lineage-verified revisions of one thesis. Parent links are re-checked against stored records,
        so a record inserted outside ``ResearchIntelligence.add`` cannot present contradictory ancestry."""
        try:
            records = self._store.search('', thesis_id)
        except ContractError as exc:
            raise HistoryError('REVISION_TAMPERED', str(exc)) from exc
        except (ValueError, TypeError, KeyError) as exc:
            raise HistoryError('REVISION_TAMPERED', f'unreadable revision record ({type(exc).__name__})') from exc
        if not records:
            raise HistoryError('THESIS_UNKNOWN', f'no revisions for thesis {thesis_id!r}')
        by_id = {r['revision_id']: r for r in records}
        children: dict[str, list[str]] = {}
        for r in records:
            parent = r.get('parent_id')
            if not parent:
                continue
            if parent not in by_id:
                other = self._get(parent)  # resolves or raises REVISION_MISSING / REVISION_TAMPERED
                raise HistoryError('CROSS_THESIS_PARENT', f'{r["revision_id"]} has a parent of thesis {other["thesis_id"]!r}')
            if _utc(by_id[parent]['created_at_utc']) >= _utc(r['created_at_utc']):
                raise HistoryError('ANCESTRY_CONTRADICTORY', f'{r["revision_id"]} is not later than its parent')
            children.setdefault(parent, []).append(r['revision_id'])
        for r in records:  # cycle guard (unreachable via add(); possible via direct database edits)
            seen, cursor = set(), r
            while cursor.get('parent_id'):
                if cursor['revision_id'] in seen:
                    raise HistoryError('ANCESTRY_CONTRADICTORY', 'ancestry cycle')
                seen.add(cursor['revision_id'])
                cursor = by_id[cursor['parent_id']]
        ordered = sorted(records, key=lambda r: (_utc(r['created_at_utc']), r['revision_id']))
        roots = [r['revision_id'] for r in ordered if not r.get('parent_id')]
        entries = [{'revision_id': r['revision_id'], 'research_as_of_utc': r['created_at_utc'], 'state': r['state'],
                    'parent_id': r.get('parent_id'), 'claim': r['claim'], 'evidence_count': len(r['evidence_ids']),
                    'validity_note': self._validity_note(r)} for r in ordered]
        return {'schema': HISTORY_SCHEMA, 'kind': 'timeline', 'thesis_id': thesis_id, 'revisions': entries,
                'roots': roots, 'branches': {p: c for p, c in sorted(children.items()) if len(c) > 1},
                'lineage_digest': digest([e['revision_id'] for e in entries]), 'current_validity': 'NOT_REASSESSED',
                'notice': NOTICE, 'service_version': SERVICE_VERSION}

    @staticmethod
    def _validity_note(record: dict) -> str:
        if record['state'] == 'SUPPORTED':
            return f"supported as of {record['created_at_utc']} only; not a current validity finding (NOT_REASSESSED)"
        return f"state {record['state']} as of {record['created_at_utc']}"

    def _ancestors(self, revision_id: str) -> list[str]:
        out, cursor = [], self._get(revision_id)
        while cursor.get('parent_id'):
            cursor = self._get(cursor['parent_id'])
            out.append(cursor['revision_id'])
            if len(out) > 10_000:
                raise HistoryError('ANCESTRY_CONTRADICTORY', 'ancestry too deep or cyclic')
        return out

    def compare(self, old_id: str, new_id: str) -> dict:
        old, new = self._get(old_id), self._get(new_id)
        if old['thesis_id'] != new['thesis_id']:
            raise HistoryError('DIFFERENT_THESES', 'revisions belong to different theses')
        if old_id == new_id:
            relation = 'SAME'
        elif old_id in self._ancestors(new_id):
            relation = 'NEW_DESCENDS_FROM_OLD'
        elif new_id in self._ancestors(old_id):
            raise HistoryError('REVISION_ORDER_INVALID', 'the "old" revision is a descendant of the "new" one')
        else:
            relation = 'UNRELATED_BRANCHES'
        if _utc(old['created_at_utc']) > _utc(new['created_at_utc']):
            raise HistoryError('REVISION_ORDER_INVALID', 'the "old" revision was recorded after the "new" one')
        old_ev, new_ev = set(old['evidence_ids']), set(new['evidence_ids'])
        added, removed = sorted(new_ev - old_ev), sorted(old_ev - new_ev)
        scalar = {k: {'old': old.get(k), 'new': new.get(k)} for k in ('claim', 'state', 'falsifier') if old.get(k) != new.get(k)}
        lists = {}
        for key in ('assumptions', 'methods'):
            a, b = old.get(key), new.get(key)
            if a is None and b is None:
                lists[key] = {'status': 'NOT_RECORDED'}
            elif not isinstance(a or [], list) or not isinstance(b or [], list):
                lists[key] = {'status': 'NON_COMPARABLE', 'reason': 'not a list in at least one revision'}
            else:
                a, b = a or [], b or []
                lists[key] = {'status': 'COMPARED', 'added': sorted(set(map(str, b)) - set(map(str, a))),
                              'removed': sorted(set(map(str, a)) - set(map(str, b)))}
        metrics = self._compare_metrics(old.get('metrics'), new.get('metrics'))
        narrowed = bool(removed) and new['state'] == 'SUPPORTED'
        result = {
            'schema': HISTORY_SCHEMA, 'kind': 'revision_comparison', 'thesis_id': old['thesis_id'], 'relation': relation,
            'sources': {'old': {'revision_id': old_id, 'record_digest': old_id.split(':', 1)[1], 'research_as_of_utc': old['created_at_utc']},
                        'new': {'revision_id': new_id, 'record_digest': new_id.split(':', 1)[1], 'research_as_of_utc': new['created_at_utc']}},
            'changes': scalar, 'falsifier_changed': 'falsifier' in scalar,
            'evidence_coverage': {'known_at_old_time': sorted(old_ev), 'recorded_in_new_revision_only': added,
                                  'no_longer_cited': removed, 'unchanged': sorted(old_ev & new_ev)},
            'assumptions': lists['assumptions'], 'methods': lists['methods'], 'metrics': metrics,
            'temporal_context': {'known_at_original_research_time': 'revision "old" and its cited evidence ids',
                                 'recorded_later': 'differences listed above were recorded in revision "new" at its own time',
                                 'newly_inferred': sorted(k for k in scalar if k in ('claim', 'state')),
                                 'independently_verified': 'NOT_ASSESSED: neither revision is verified by this service'},
            'validity': 'NOT_REASSESSED',
            'coverage_assessment': 'INSUFFICIENT_EVIDENCE' if narrowed else 'NOT_REASSESSED',
            'coverage_note': ('evidence coverage narrowed while the state stays SUPPORTED: retained citations do not demonstrate continued support'
                              if narrowed else 'no coverage reduction under a SUPPORTED state was detected; coverage is not a validity finding'),
            'notice': NOTICE, 'service_version': SERVICE_VERSION}
        result['comparison_digest'] = digest(result)
        return result

    @staticmethod
    def _compare_metrics(old: Any, new: Any) -> dict:
        if old is None and new is None:
            return {'status': 'NOT_RECORDED', 'items': []}
        items = []
        index = lambda rows: {m.get('name'): m for m in rows if isinstance(m, dict)}  # noqa: E731
        if not isinstance(old or [], list) or not isinstance(new or [], list):
            return {'status': 'NON_COMPARABLE', 'items': [], 'reason': 'metrics must be a list of {name, definition, unit, value}'}
        a, b = index(old or []), index(new or [])
        for name in sorted(set(a) | set(b), key=str):
            if name not in a or name not in b:
                items.append({'name': name, 'status': 'ADDED' if name in b else 'REMOVED'})
                continue
            x, y = a[name], b[name]
            reasons = [f'{k} differs' for k in ('definition', 'unit', 'instrument_id') if x.get(k) != y.get(k)]
            vx, vy = _d(x.get('value')), _d(y.get('value'))
            if vx is None or vy is None:
                reasons.append('value missing or not a finite decimal')
            if reasons:
                items.append({'name': name, 'status': 'NON_COMPARABLE', 'reasons': reasons})  # no synthetic change figure
            else:
                items.append({'name': name, 'status': 'COMPARED', 'unit': x.get('unit'), 'old': _fmt(vx), 'new': _fmt(vy), 'change': _fmt(vy - vx)})
        return {'status': 'COMPARED', 'items': items}

    # ------------------------------------------------------------------ mission bundles
    @staticmethod
    def _load_bundle(bundle_dir: Path | str, archive: Path | str | None) -> dict:
        root = Path(bundle_dir)

        def read(name: str, required: bool = True):
            try:
                return json.loads((root / name).read_text(encoding='utf-8'))
            except FileNotFoundError:
                if required:
                    raise HistoryError('BUNDLE_UNREADABLE', f'{name} is missing') from None
                return None
            except (OSError, ValueError, UnicodeError) as exc:
                raise HistoryError('BUNDLE_UNREADABLE', f'{name}: {type(exc).__name__}') from exc
        mission, baseline, evidence = read('mission.json'), read('mission_baseline.json'), read('evidence.json')
        if not (isinstance(mission, dict) and isinstance(baseline, dict) and isinstance(evidence, dict)
                and isinstance(evidence.get('references'), list)):
            raise HistoryError('BUNDLE_UNREADABLE', 'bundle files do not have the expected shape')
        if archive is None:
            verification = {'status': 'UNKNOWN', 'reason': 'no evidence archive supplied; the bundle was not verified'}
        else:
            result = completion.verify(root, archive=archive)
            # completion.verify reports INCOMPLETE for both declared remaining work and for integrity damage;
            # only diagnostics whose own state is INVALID/UNSUPPORTED/UNKNOWN make the bundle untrustworthy here
            damaged = sorted({d['code'] for d in result['diagnostics'] if d.get('state') in ('INVALID', 'UNSUPPORTED', 'UNKNOWN')})
            verification = {'status': 'INVALID' if damaged else result['status'], 'authenticity': result['authenticity'],
                            'diagnostic_codes': sorted({d['code'] for d in result['diagnostics']}), 'integrity_problem_codes': damaged}
        content = read('report_content.json', False)
        manifest = read('report_manifest.json', False)
        if content is not None or manifest is not None:  # digest-consistency check of the companions (not recomputation)
            files = {}
            for name in ('report_content.json', 'report_manifest.json', 'report.md', 'report.html'):
                try:
                    files[name] = (root / name).read_bytes()
                except OSError:
                    pass
            problems = reporting.verify_companions(
                files, spec_digest=(manifest or {}).get('input_spec_digest') if isinstance(manifest, dict) else None,
                mission_id=baseline.get('run_id'), evidence_ids={r['id'] for r in evidence['references'] if isinstance(r, dict) and 'id' in r}) \
                if len(files) == 4 else ['report companions or reports are missing']
            verification['companion_problems'] = problems
            if problems:
                verification['status'] = 'INVALID'
        bundle_digest = digest({n: (root / n).read_bytes().hex() for n in ('mission.json', 'evidence.json', 'mission_baseline.json')})
        return {'root': root, 'mission': mission, 'baseline': baseline, 'evidence': evidence['references'], 'verification': verification,
                'content': content if isinstance(content, dict) else None, 'manifest': manifest if isinstance(manifest, dict) else None,
                'digest': bundle_digest}

    def link_mission(self, revision_id: str, bundle_dir: Path | str, archive: Path | str | None = None) -> dict:
        """Check (never create) the relation between a stored thesis revision and a mission bundle."""
        revision = self._get(revision_id)
        bundle = self._load_bundle(bundle_dir, archive)
        bundle_ids = {r['id'] for r in bundle['evidence'] if isinstance(r, dict) and 'id' in r}
        cited = set(revision['evidence_ids'])
        missing = sorted(cited - bundle_ids)
        as_of = bundle['baseline'].get('as_of_utc')
        verified = bundle['verification']['status'] in ('COMPLETE', 'INCOMPLETE')
        if not verified:
            state = 'BUNDLE_NOT_VERIFIED'
        elif as_of is None or _utc(as_of) > _utc(revision['created_at_utc']):
            state = 'TEMPORAL_CONFLICT'
        elif missing:
            state = 'UNRESOLVED'
        else:
            state = 'LINKED'
        return {'schema': HISTORY_SCHEMA, 'kind': 'mission_link', 'link_state': state, 'authoritative': False,
                'revision': {'revision_id': revision_id, 'thesis_id': revision['thesis_id'], 'research_as_of_utc': revision['created_at_utc'],
                            'state': revision['state'], 'cited_evidence_ids': sorted(cited)},
                'mission': {'mission_id': bundle['baseline'].get('run_id'), 'research_as_of_utc': as_of,
                            'research_state': bundle['mission'].get('research_state'), 'completion_state': bundle['mission'].get('completion_state'),
                            'evidence_ids': sorted(bundle_ids), 'bundle_digest': bundle['digest'], 'verification': bundle['verification']},
                'cited_but_absent_from_bundle': missing, 'validity': 'NOT_REASSESSED',
                'note': 'a mission bundle is research-execution evidence; it does not create, change or validate a thesis revision',
                'notice': NOTICE, 'service_version': SERVICE_VERSION}

    def compare_missions(self, older: Path | str, newer: Path | str, *, archives: tuple[Path | str | None, Path | str | None] = (None, None)) -> dict:
        try:
            return self._compare_missions(older, newer, archives)
        except (TypeError, KeyError, AttributeError, ValueError) as exc:
            if isinstance(exc, ContractError):
                raise
            raise HistoryError('BUNDLE_UNREADABLE', f'malformed bundle content ({type(exc).__name__})') from exc

    def _compare_missions(self, older, newer, archives) -> dict:
        a, b = self._load_bundle(older, archives[0]), self._load_bundle(newer, archives[1])
        ta, tb = a['baseline'].get('as_of_utc'), b['baseline'].get('as_of_utc')
        if not ta or not tb or _utc(ta) > _utc(tb):
            raise HistoryError('MISSION_ORDER_INVALID', 'the older mission must have the earlier research as-of')

        def coverage(bundle):  # key -> ids (several observations may share one key)
            return {f"{r.get('instrument_id')}|{r.get('metric')}|{r.get('unit')}": r['id'] for r in bundle['evidence'] if isinstance(r, dict)}
        ca, cb = coverage(a), coverage(b)

        def claims(bundle):
            """Latest recorded R claim per instrument|metric|unit (by observation time, never by claim id order)."""
            if not bundle['content'] or not isinstance(bundle['content'].get('claims'), list):
                return None
            latest: dict[str, dict] = {}
            for c in bundle['content']['claims']:
                if isinstance(c, dict) and c.get('classification') == 'R':
                    key = f"{c['instrument_id']}|{c['metric']}|{c['unit']}"
                    if key not in latest or _utc(c['observed_at_utc']) > _utc(latest[key]['observed_at_utc']):
                        latest[key] = c
            return latest
        ra, rb = claims(a), claims(b)
        metrics = []
        if ra is None or rb is None:
            metrics_status = 'NOT_AVAILABLE'
        else:
            metrics_status = 'COMPARED'
            for key in sorted(set(ra) & set(rb)):
                va, vb = _d(ra[key]['value']), _d(rb[key]['value'])
                if va is None or vb is None:
                    metrics.append({'key': key, 'status': 'NON_COMPARABLE', 'reasons': ['value not a finite decimal']})
                else:
                    metrics.append({'key': key, 'status': 'COMPARED', 'older_value': _fmt(va), 'newer_value': _fmt(vb), 'change': _fmt(vb - va),
                                    'older_observed_at_utc': ra[key]['observed_at_utc'], 'newer_observed_at_utc': rb[key]['observed_at_utc']})
            # same instrument and metric, different unit => definitions are not comparable; never a change figure
            names = lambda r: {tuple(k.split('|')[:2]): k for k in r}  # noqa: E731
            na, nb = names(ra), names(rb)
            for pair in sorted(set(na) & set(nb)):
                if na[pair] != nb[pair]:
                    metrics.append({'key': '|'.join(pair), 'status': 'NON_COMPARABLE', 'reasons': ['unit differs between missions']})

        def listed(bundle, field):
            if not bundle['content']:
                return None
            if field == 'falsifiers':
                return sorted({f for s in bundle['content']['scenarios'] for f in s['falsifiers']})
            if field == 'assumptions':
                return sorted({x for s in bundle['content']['scenarios'] for x in s['assumptions']})
            return sorted(bundle['content']['operations'])

        def delta(field):
            x, y = listed(a, field), listed(b, field)
            if x is None or y is None:
                return {'status': 'NOT_AVAILABLE', 'reason': 'report companions absent from at least one bundle'}
            return {'status': 'COMPARED', 'added': sorted(set(y) - set(x)), 'removed': sorted(set(x) - set(y))}
        result = {
            'schema': HISTORY_SCHEMA, 'kind': 'mission_comparison',
            'sources': {'older': {'mission_id': a['baseline'].get('run_id'), 'research_as_of_utc': ta, 'bundle_digest': a['digest'], 'verification': a['verification']},
                        'newer': {'mission_id': b['baseline'].get('run_id'), 'research_as_of_utc': tb, 'bundle_digest': b['digest'], 'verification': b['verification']}},
            'same_decision_question': a['baseline'].get('decision_question') == b['baseline'].get('decision_question'),
            'research_state': {'older': a['mission'].get('research_state'), 'newer': b['mission'].get('research_state')},
            'completion_state': {'older': a['mission'].get('completion_state'), 'newer': b['mission'].get('completion_state')},
            'evidence_coverage': {'newly_present': sorted(set(cb) - set(ca)), 'missing_now': sorted(set(ca) - set(cb)), 'common': sorted(set(ca) & set(cb))},
            'metrics': {'status': metrics_status, 'items': metrics}, 'falsifiers': delta('falsifiers'), 'assumptions': delta('assumptions'),
            'methods': delta('methods'), 'validity': 'NOT_REASSESSED',
            'coverage_assessment': 'INSUFFICIENT_EVIDENCE' if set(ca) - set(cb) else 'NOT_REASSESSED',
            'temporal_context': {'older_known_at': ta, 'newer_known_at': tb,
                                 'note': 'the newer mission does not rewrite the older mission; each keeps its own as-of'},
            'notice': NOTICE, 'service_version': SERVICE_VERSION}
        result['comparison_digest'] = digest(result)
        return result
