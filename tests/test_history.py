"""P2-C: read-only thesis / mission comparison that preserves original research time and immutable revisions."""
import hashlib
import json
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from p1_support import INSTRUMENT, METRIC, Fixture, run_to_end  # noqa: E402
from p1_support import obs_row as _obs  # noqa: E402

from research_lab.core import canonical_json, digest  # noqa: E402
from research_lab.history import HistoryError, ThesisHistory  # noqa: E402
from research_lab.intelligence import ResearchIntelligence  # noqa: E402


def rev(thesis='T1', created='2026-01-02T00:00:00Z', state='SUPPORTED', evidence=('o1', 'o2'), **extra):
    return {'thesis_id': thesis, 'claim': f'claim at {created}', 'falsifier': 'fell below recorded floor', 'created_at_utc': created,
            'state': state, 'evidence_ids': list(evidence), **extra}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / 'intel.sqlite'
        self.store = ResearchIntelligence(self.db)
        self.history = ThesisHistory(self.store)

    def sha(self):
        return hashlib.sha256(self.db.read_bytes()).hexdigest()

    def raw_insert(self, record):
        content, h = canonical_json(record).decode(), digest(record)
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute('INSERT OR IGNORE INTO revisions VALUES(?,?,?,?,?,?)', ('revision:' + h, record['thesis_id'], record['state'],
                                                                                 record['created_at_utc'], content, h))
        return 'revision:' + h


class RevisionTests(Base):
    def setUp(self):
        super().setUp()
        self.r1 = self.store.add(rev(created='2026-01-02T00:00:00Z', metrics=[
            {'name': 'mid', 'definition': 'book mid', 'unit': 'USDT', 'value': '100', 'instrument_id': INSTRUMENT}], assumptions=['a1']))
        self.r2 = self.store.add(rev(created='2026-01-05T00:00:00Z', evidence=('o2', 'o3'), parent_id=self.r1, metrics=[
            {'name': 'mid', 'definition': 'book mid', 'unit': 'USDT', 'value': '112.5', 'instrument_id': INSTRUMENT}], assumptions=['a1', 'a2']))

    def test_timeline_is_ordered_and_never_calls_supported_current(self):
        t = self.history.timeline('T1')
        self.assertEqual([e['revision_id'] for e in t['revisions']], [self.r1, self.r2])
        self.assertEqual(t['current_validity'], 'NOT_REASSESSED')
        self.assertIn('not a current validity finding', t['revisions'][0]['validity_note'])
        self.assertEqual(t['lineage_digest'], self.history.timeline('T1')['lineage_digest'])

    def test_comparison_keeps_original_as_of_and_is_read_only(self):
        before = self.sha()
        c = self.history.compare(self.r1, self.r2)
        self.assertEqual(self.sha(), before)                                       # G10: no write of any kind
        self.assertEqual((c['relation'], c['validity']), ('NEW_DESCENDS_FROM_OLD', 'NOT_REASSESSED'))
        self.assertEqual(c['sources']['old']['research_as_of_utc'], '2026-01-02T00:00:00Z')
        self.assertEqual(c['sources']['new']['research_as_of_utc'], '2026-01-05T00:00:00Z')
        cov = c['evidence_coverage']
        self.assertEqual((cov['known_at_old_time'], cov['recorded_in_new_revision_only'], cov['no_longer_cited']), (['o1', 'o2'], ['o3'], ['o1']))
        self.assertEqual(c['coverage_assessment'], 'INSUFFICIENT_EVIDENCE')       # coverage narrowed under a SUPPORTED state
        self.assertEqual(c['assumptions'], {'status': 'COMPARED', 'added': ['a2'], 'removed': []})
        self.assertEqual(c['methods'], {'status': 'NOT_RECORDED'})
        self.assertIn('NOT_ASSESSED', c['temporal_context']['independently_verified'])
        self.assertEqual(c['sources']['old']['record_digest'], self.r1.split(':')[1])
        self.assertEqual(self.history.compare(self.r1, self.r2)['comparison_digest'], c['comparison_digest'])

    def test_compatible_metrics_compare_and_incompatible_do_not(self):
        m = self.history.compare(self.r1, self.r2)['metrics']['items'][0]
        self.assertEqual((m['status'], m['change']), ('COMPARED', '12.5'))
        r3 = self.store.add(rev(created='2026-01-09T00:00:00Z', parent_id=self.r2, metrics=[
            {'name': 'mid', 'definition': 'volume-weighted mid', 'unit': 'USDT', 'value': '113', 'instrument_id': INSTRUMENT}]))
        m = self.history.compare(self.r2, r3)['metrics']['items'][0]
        self.assertEqual(m['status'], 'NON_COMPARABLE')
        self.assertNotIn('change', m)                                              # no synthetic change figure
        self.assertIn('definition differs', m['reasons'])
        r4 = self.store.add(rev(created='2026-01-10T00:00:00Z', parent_id=r3, metrics=[
            {'name': 'mid', 'definition': 'volume-weighted mid', 'unit': 'USD', 'value': 'NaN', 'instrument_id': INSTRUMENT}]))
        m = self.history.compare(r3, r4)['metrics']['items'][0]
        self.assertEqual(m['status'], 'NON_COMPARABLE')
        self.assertEqual(len(m['reasons']), 2)

    def test_invalid_comparisons_are_rejected(self):
        other = self.store.add(rev(thesis='T2'))
        for args, code in (((self.r1, other), 'DIFFERENT_THESES'), ((self.r2, self.r1), 'REVISION_ORDER_INVALID'),
                           ((self.r1, 'revision:' + '0' * 64), 'REVISION_MISSING')):
            with self.assertRaises(HistoryError, msg=code) as ctx:
                self.history.compare(*args)
            self.assertEqual(ctx.exception.code, code)
        with self.assertRaises(HistoryError) as ctx:
            self.history.timeline('nope')
        self.assertEqual(ctx.exception.code, 'THESIS_UNKNOWN')

    def test_tampered_revision_is_detected(self):
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute("UPDATE revisions SET content=replace(content,'fell below','rose above') WHERE id=?", (self.r1,))
        with self.assertRaises(HistoryError) as ctx:
            self.history.compare(self.r1, self.r2)
        self.assertEqual(ctx.exception.code, 'REVISION_TAMPERED')
        with self.assertRaises(HistoryError) as ctx:
            self.history.timeline('T1')
        self.assertEqual(ctx.exception.code, 'REVISION_TAMPERED')

    def test_contradictory_ancestry_inserted_outside_add_is_rejected(self):
        other = self.store.add(rev(thesis='T2', created='2026-01-01T00:00:00Z'))
        self.raw_insert(rev(created='2026-01-20T00:00:00Z', parent_id=other))      # cross-thesis parent
        with self.assertRaises(HistoryError) as ctx:
            self.history.timeline('T1')
        self.assertEqual(ctx.exception.code, 'CROSS_THESIS_PARENT')

    def test_parent_not_earlier_than_child_is_rejected(self):
        self.raw_insert(rev(created='2026-01-01T00:00:00Z', parent_id=self.r2))
        with self.assertRaises(HistoryError) as ctx:
            self.history.timeline('T1')
        self.assertEqual(ctx.exception.code, 'ANCESTRY_CONTRADICTORY')

    def test_branches_are_reported_not_merged(self):
        self.store.add(rev(created='2026-01-07T00:00:00Z', evidence=('o9',), parent_id=self.r1))
        t = self.history.timeline('T1')
        self.assertEqual(list(t['branches']), [self.r1])
        side = [e['revision_id'] for e in t['revisions'] if e['revision_id'] not in (self.r1, self.r2)][0]
        self.assertEqual(self.history.compare(self.r2, side)['relation'], 'UNRELATED_BRANCHES')

    def test_history_has_no_write_path(self):
        public = [n for n in dir(ThesisHistory) if not n.startswith('_')]
        self.assertEqual(sorted(public), ['compare', 'compare_missions', 'link_mission', 'timeline'])


def day2_rows(value='120', unit='USDT', ident='p'):
    return [_obs(ident + '1', value, unit=unit, observed='2026-01-02T11:00:00Z', retrieved='2026-01-02T11:01:00Z', available='2026-01-02T11:01:00Z')]


class MissionLinkTests(Base):
    def setUp(self):
        super().setUp()
        self.fx = Fixture()
        self.addCleanup(self.fx.close)
        self.c = self.fx.coordinator()

    def run_mission(self, mission_id, as_of=None, rows=None, unit='USDT'):
        kw = {}
        if as_of:
            kw['as_of_utc'] = as_of
            kw['hypotheses'] = []
        spec = self.fx.spec(mission_id, observations=rows, required_metrics=[{'instrument_id': INSTRUMENT, 'metric': METRIC, 'unit': unit}], **kw)
        self.c.create_mission(spec)
        run_to_end(self.c, mission_id)
        view = self.c.inspect_mission(mission_id)
        return self.fx.ws / view['final']['bundle_dir'], self.fx.ws / spec['output_dir'] / 'evidence.sqlite'

    def test_link_states(self):
        bundle, archive = self.run_mission('m1')
        ok = self.store.add(rev(created='2026-01-03T00:00:00Z', evidence=('o1',)))
        absent = self.store.add(rev(thesis='T3', created='2026-01-03T00:00:00Z', evidence=('o1', 'zzz')))
        early = self.store.add(rev(thesis='T4', created='2025-12-31T00:00:00Z', evidence=('o1',)))
        before = self.sha()
        link = self.history.link_mission(ok, bundle, archive)
        self.assertEqual((link['link_state'], link['authoritative']), ('LINKED', False))
        self.assertIn('does not create', link['note'])
        self.assertEqual(self.history.link_mission(ok, bundle)['link_state'], 'BUNDLE_NOT_VERIFIED')
        link = self.history.link_mission(absent, bundle, archive)
        self.assertEqual((link['link_state'], link['cited_but_absent_from_bundle']), ('UNRESOLVED', ['zzz']))
        self.assertEqual(self.history.link_mission(early, bundle, archive)['link_state'], 'TEMPORAL_CONFLICT')
        self.assertEqual(self.sha(), before)                                         # G12: linking wrote nothing

    def test_tampered_bundle_is_not_verified(self):
        bundle, archive = self.run_mission('m1')
        (bundle / 'report.md').write_text('tampered')
        rid = self.store.add(rev(created='2026-01-03T00:00:00Z', evidence=('o1',)))
        link = self.history.link_mission(rid, bundle, archive)
        self.assertEqual((link['mission']['verification']['status'], link['link_state']), ('INVALID', 'BUNDLE_NOT_VERIFIED'))
        (bundle / 'mission.json').write_text('{')
        with self.assertRaises(HistoryError) as ctx:
            self.history.link_mission(rid, bundle, archive)
        self.assertEqual(ctx.exception.code, 'BUNDLE_UNREADABLE')

    def test_tampered_companion_is_detected_in_a_bundle(self):
        bundle, archive = self.run_mission('m1')
        content = json.loads((bundle / 'report_content.json').read_text())
        next(c for c in content['claims'] if c['classification'] == 'R')['value'] = '9999'
        (bundle / 'report_content.json').write_text(json.dumps(content))
        rid = self.store.add(rev(created='2026-01-03T00:00:00Z', evidence=('o1',)))
        link = self.history.link_mission(rid, bundle, archive)
        self.assertEqual((link['mission']['verification']['status'], link['link_state']), ('INVALID', 'BUNDLE_NOT_VERIFIED'))
        self.assertTrue(link['mission']['verification']['companion_problems'])

    def test_latest_sample_per_key_is_compared_not_the_last_claim_id(self):
        rows = [_obs('zz', '100', observed='2026-01-02T10:00:00Z', retrieved='2026-01-02T10:01:00Z', available='2026-01-02T10:01:00Z'),
                _obs('aa', '150', observed='2026-01-02T11:00:00Z', retrieved='2026-01-02T11:01:00Z', available='2026-01-02T11:01:00Z')]
        old, oa = self.run_mission('m1', as_of='2026-01-02T12:00:00Z', rows=rows)
        new, na = self.run_mission('m2', as_of='2026-01-03T12:00:00Z', rows=[_obs('n1', '160', observed='2026-01-03T11:00:00Z', retrieved='2026-01-03T11:01:00Z', available='2026-01-03T11:01:00Z')])
        item = self.history.compare_missions(old, new, archives=(oa, na))['metrics']['items'][0]
        self.assertEqual((item['older_value'], item['change'], item['older_observed_at_utc']), ('150', '10', '2026-01-02T11:00:00Z'))

    def test_malformed_records_raise_typed_errors(self):
        bundle, archive = self.run_mission('m1')
        content = json.loads((bundle / 'report_content.json').read_text())
        content['claims'] = 5
        (bundle / 'report_content.json').write_text(json.dumps(content))
        new, _ = self.run_mission('m2', as_of='2026-01-02T12:00:00Z', rows=day2_rows())
        with self.assertRaises(HistoryError) as ctx:   # a tampered companion makes the bundle INVALID: refused, not compared
            self.history.compare_missions(bundle, new)
        self.assertEqual(ctx.exception.code, 'BUNDLE_INVALID')

    def test_mission_comparison_preserves_each_as_of_and_flags_units(self):
        old, old_archive = self.run_mission('m1')
        new, new_archive = self.run_mission('m2', as_of='2026-01-02T12:00:00Z', rows=day2_rows())
        diff, diff_archive = self.run_mission('m3', as_of='2026-01-02T12:00:00Z', rows=day2_rows('5', 'USD', 'q'), unit='USD')
        c = self.history.compare_missions(old, new, archives=(old_archive, new_archive))
        self.assertEqual((c['sources']['older']['research_as_of_utc'], c['sources']['newer']['research_as_of_utc']),
                         ('2026-01-01T12:00:00Z', '2026-01-02T12:00:00Z'))
        item = c['metrics']['items'][0]
        self.assertEqual((item['status'], item['older_value'], item['newer_value'], item['change']), ('COMPARED', '100', '120', '20'))
        self.assertEqual((c['evidence_coverage']['newly_present'], c['evidence_coverage']['missing_now']), ([], []))   # same instrument/metric/unit key
        self.assertEqual(c['coverage_assessment'], 'NOT_REASSESSED')
        self.assertEqual(c['falsifiers']['status'], 'COMPARED')
        d = self.history.compare_missions(old, diff, archives=(old_archive, diff_archive))
        kinds = {(i['status']) for i in d['metrics']['items']}
        self.assertEqual(kinds, {'NON_COMPARABLE'})
        self.assertEqual(d['coverage_assessment'], 'INSUFFICIENT_EVIDENCE')           # the older unit-keyed series is no longer covered
        with self.assertRaises(HistoryError) as ctx:
            self.history.compare_missions(new, old)
        self.assertEqual(ctx.exception.code, 'MISSION_ORDER_INVALID')
        self.assertEqual(json.loads(json.dumps(c))['validity'], 'NOT_REASSESSED')


if __name__ == '__main__':
    unittest.main()
