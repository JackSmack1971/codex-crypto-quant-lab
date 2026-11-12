"""Deterministic, offline structural validator for shipped control-plane artifacts."""
from pathlib import Path
import os
import json
import sys
import tomllib
import re
import ast

ROOT=Path(__file__).resolve().parents[1]
AXES=['identity.json','12-axis-matrix.md','model-policy.md','authority.md','runtime.md',
      'network.md','orchestration.md','source-control.md','telemetry.md','capabilities.json','report-contract.md','research-lab.md']
SKILLS=['crypto-ohlcv-fetch','crypto-indicators','crypto-backtest','crypto-research',
        'crypto-report','crypto-strategy-lab','crypto-temporal-validation','crypto-hypothesis','crypto-research-ledger','crypto-data-quality','crypto-defi','crypto-derivatives','crypto-risk']
AGENTS=['market_researcher','onchain_researcher','methodology_reviewer','report_editor','source_verifier']

def _managed_files(root: Path) -> set[str]:
    """Return package files, excluding only repository metadata and runtime output."""
    ignored_dirs={'.git','.venv','__pycache__','.pytest_cache'}
    present=set()
    for current, dirs, files in os.walk(root):
        current_path=Path(current)
        relative_dir=current_path.relative_to(root)
        dirs[:]=[name for name in dirs if name not in ignored_dirs]
        # Reports are user-generated at runtime under reports/<run-id>/; keep
        # the package's root-level .gitkeep but do not package report contents.
        if relative_dir == Path('reports'):
            dirs.clear()
        for name in files:
            path=current_path/name
            relative=path.relative_to(root)
            if relative.as_posix()=='PACKAGE-MANIFEST.json':
                continue
            if name.endswith(('.pyc','.sqlite','.sqlite-wal','.sqlite-shm')):
                continue
            present.add(relative.as_posix())
    return present

def _manifest_errors(root: Path, manifest: dict) -> list[str]:
    import hashlib
    errors=[]
    entries=manifest['files']
    for path, expected in entries.items():
        source=root/path
        if not source.is_file():errors.append('manifest file missing '+path)
        elif hashlib.sha256(source.read_bytes()).hexdigest()!=expected:errors.append('manifest hash mismatch '+path)
    present=_managed_files(root)
    if set(entries)!=present:
        errors.append('manifest file set mismatch: '+str(sorted(present^set(entries))))
    return errors

def main() -> int:
    errors=[]
    for name in AXES:
        if not (ROOT/'control-plane'/name).is_file():errors.append('missing control-plane/'+name)
    for name in SKILLS:
        path=ROOT/'.agents'/'skills'/name/'SKILL.md'
        if not path.is_file():errors.append('missing skill '+name);continue
        raw=path.read_text(encoding='utf-8')
        if not re.search(r'^name:\s*'+re.escape(name)+r'\s*$',raw,re.M):errors.append('invalid skill name '+name)
        if not re.search(r'^description:\s*\S+',raw,re.M):errors.append('missing skill description '+name)
    for name in ("market_data.py", "indicators.py", "backtest.py", "edge_forensics.py"):
        source=ROOT/"src"/name
        if not source.is_file(): errors.append("missing src/"+name); continue
        try: ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
        except (SyntaxError, UnicodeError) as exc: errors.append("invalid src/"+name+": "+str(exc))
    for name in ("conftest.py", "test_market_data.py", "test_indicators.py", "test_backtest.py"):
        if not (ROOT/"tests"/name).is_file(): errors.append("missing tests/"+name)
    for name in ('__init__.py', 'core.py', 'normalization.py', 'provider_bridge.py', 'archive.py', 'ledger_sqlite.py', 'models.py', 'strategies.py', 'validation.py', 'workflows.py'):
        module=ROOT/'research_lab'/name
        if not module.is_file():errors.append('missing research_lab/'+name);continue
        try:ast.parse(module.read_text(encoding='utf-8'),filename=str(module))
        except (SyntaxError,UnicodeError) as exc:errors.append('invalid research_lab/'+name+': '+str(exc))
    for name in ('test_research_lab.py','test_research_expansion.py','test_research_workflows.py'):
        if not (ROOT/'tests'/name).is_file():errors.append('missing research lab test '+name)
    if not (ROOT/'scripts'/'run_research_lab.py').is_file():errors.append('missing research run CLI')
    for name in ('evals.py','experiments.py','intelligence.py','jobs.py','provider_policy.py','factors.py','risk.py'):
        module=ROOT/'research_lab'/name
        if not module.is_file():errors.append('missing research extension '+name);continue
        try:ast.parse(module.read_text(encoding='utf-8'),filename=str(module))
        except (SyntaxError,UnicodeError) as exc:errors.append('invalid extension '+name+': '+str(exc))
    for name in ('research_ops.py','research_explorer.py'):
        script=ROOT/'scripts'/name
        if not script.is_file():errors.append('missing research script '+name);continue
        try:ast.parse(script.read_text(encoding='utf-8'),filename=str(script))
        except (SyntaxError,UnicodeError) as exc:errors.append('invalid script '+name+': '+str(exc))
    for name in ('test_research_v24.py','test_research_explorer_v24.py'):
        if not (ROOT/'tests'/name).is_file():errors.append('missing new test '+name)
    for rel in ('quant_free_mcp/acquisition.py','research_lab/completion.py','research_lab/mission_integrity.py','scripts/check_dependencies.py',
                'research_lab/mission_spec.py','research_lab/mission_coordinator.py','research_lab/experiment_registry.py','scripts/mission.py'):
        module=ROOT/rel
        if not module.is_file():errors.append('missing P0 module '+rel);continue
        try:ast.parse(module.read_text(encoding='utf-8'),filename=str(module))
        except (SyntaxError,UnicodeError) as exc:errors.append('invalid P0 module '+rel+': '+str(exc))
    for rel in ('.github/workflows/ci.yml','.github/workflows/live-providers.yml','constraints-ci.txt','tests/test_acquisition.py','tests/test_mission_integrity.py','tests/live/test_live_providers.py',
                'tests/test_p0_regressions.py','tests/test_mission_coordinator.py','tests/test_mission_recovery.py',
                'tests/test_experiment_registry.py','tests/test_mission_e2e.py','tests/p1_support.py'):
        if not (ROOT/rel).is_file():errors.append('missing P0 qualification file '+rel)
    try:
        manifest=json.loads((ROOT/'PACKAGE-MANIFEST.json').read_text(encoding='utf8'))
        errors.extend(_manifest_errors(ROOT,manifest))
    except (ValueError,OSError,KeyError,TypeError) as exc:
        errors.append('manifest integrity failure: '+str(exc))

    for name in AGENTS:
        path=ROOT/'.codex'/'agents'/(name+'.toml')
        if not path.is_file():errors.append('missing agent '+name);continue
        parsed=tomllib.loads(path.read_text(encoding='utf-8'))
        if parsed.get('name')!=name or not parsed.get('description') or not parsed.get('developer_instructions'):
            errors.append('invalid agent '+name)
    try:
        cfg=tomllib.loads((ROOT/'.codex'/'config.toml').read_text())
        assert cfg['approval_policy']=='on-request'
        assert cfg['default_permissions']==':workspace'
        assert 'mcp_servers' not in cfg
        global_cfg=tomllib.loads((ROOT/'docs'/'global-config-original.toml').read_text())
        assert 'coingecko' in global_cfg['mcp_servers']
        assert global_cfg['mcp_servers']['jupiter']['enabled'] is False
        assert global_cfg['mcp_servers']['lifi']['enabled'] is False
        json.loads((ROOT/'control-plane'/'identity.json').read_text())
        registry=json.loads((ROOT/'control-plane'/'capabilities.json').read_text())
        assert registry['research_only'] is True
    except (OSError, ValueError, AssertionError, KeyError) as exc:
        errors.append('config/JSON validation failure: '+str(exc))
    if errors:
        print(json.dumps({'status':'failed','errors':errors},indent=2));return 1
    print(json.dumps({'status':'passed','axes':12,'skills':len(SKILLS),'agents':len(AGENTS),'configuration':'parsed','scope':'offline structural validation'}));return 0

if __name__=='__main__':sys.exit(main())
