"""Deterministic, offline structural validator for shipped control-plane artifacts."""
from pathlib import Path
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
    try:
        import hashlib
        manifest=json.loads((ROOT/'PACKAGE-MANIFEST.json').read_text(encoding='utf8'))
        for path, expected in manifest['files'].items():
            source=ROOT/path
            if not source.is_file():errors.append('manifest file missing '+path)
            elif hashlib.sha256(source.read_bytes()).hexdigest()!=expected:errors.append('manifest hash mismatch '+path)
        present={str(path.relative_to(ROOT)).replace('\\','/') for path in ROOT.rglob('*')
                 if path.is_file() and '__pycache__' not in path.parts and path.name!='PACKAGE-MANIFEST.json'}
        if set(manifest['files'])!=present:
            errors.append('manifest file set mismatch: '+str(sorted(present^set(manifest['files']))))
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
