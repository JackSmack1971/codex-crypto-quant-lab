"""Fixture-side mutations used by benchmark cases B29/B30 (scratch workspace only).

These helpers *construct the adverse situation* a case asserts against: a mission rendered under the superseded
``render.local_report@1`` contract, and a report companion forged the way a local writer can (content edited, every
digest recomputed). They write only inside the temporary workspace handed in by the runner, never case files, expected
outcomes or repository sources; the verdicts they record come from the production verifiers, not from this module.
"""
from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from unittest import mock

from . import history, reporting
from . import mission_coordinator as _mc
from .intelligence import ResearchIntelligence
from .mission_coordinator import MissionCoordinator


def render_under_v1(c: MissionCoordinator, mission_id: str) -> None:
    """Drive RENDERING under the superseded render.local_report@1 contract to reproduce a pre-P2 mission."""
    legacy = lambda ctx: {k: v for k, v in _mc.op_render(ctx).items() if k in _mc.LEGACY_RENDER_OUTPUTS}  # noqa: E731
    with mock.patch.dict(_mc.STAGE_OPERATIONS, {'RENDERING': ('render.local_report', _mc.LEGACY_RENDER_VERSION)}), \
            mock.patch.dict(_mc.OPERATIONS, {'RENDERING': legacy}), \
            mock.patch.dict(_mc.STAGE_OUTPUTS, {'RENDERING': _mc.LEGACY_RENDER_OUTPUTS}):
        for _ in range(8):
            if {r['stage_id']: r['status'] for r in c.inspect_mission(mission_id)['stages']}['RENDERING'] in ('SUCCEEDED', 'FAILED'):
                break
            c.advance_mission(mission_id)


def forge_companion(c: MissionCoordinator, ws: Path, spec: dict, step: dict, companions: dict) -> None:
    """Forge a claim the way a local writer can (edit content, recompute every digest) and record the verdicts."""
    bundle = ws / c.inspect_mission(spec['mission_id'])['final']['bundle_dir']
    archive = ws / spec['output_dir'] / 'evidence.sqlite'
    companions.setdefault('clean', reporting.verify_bundle_companions(bundle, archive)['status'])
    forged = ws / ('forged_' + step['label'])
    shutil.copytree(bundle, forged)
    content = json.loads((forged / 'report_content.json').read_text(encoding='utf-8'))
    next(x for x in content['claims'] if x['claim_id'] == step['claim_id'])[step['field']] = step['value']
    raw = json.dumps(content, sort_keys=True, separators=(',', ':')).encode()
    md = (forged / 'report.md').read_text(encoding='utf-8')
    for old, new in step.get('rewrite', {}).items():
        md = md.replace(old, new)
    manifest = json.loads((forged / 'report_manifest.json').read_text(encoding='utf-8'))
    manifest.update(content_sha256=hashlib.sha256(raw).hexdigest(), report_md_sha256=hashlib.sha256(md.encode()).hexdigest())
    (forged / 'report_content.json').write_bytes(raw)
    (forged / 'report.md').write_text(md, encoding='utf-8')
    (forged / 'report_manifest.json').write_text(json.dumps(manifest, sort_keys=True), encoding='utf-8')
    companions['forged_' + step['label']] = reporting.verify_bundle_companions(forged, archive)['status']
    try:
        history.ThesisHistory(ResearchIntelligence(ws / 'bench-history.sqlite')).compare_missions(forged, bundle, archives=(archive, archive))
        companions['history_' + step['label']] = 'COMPARED'
    except history.HistoryError as exc:
        companions['history_' + step['label']] = exc.code
