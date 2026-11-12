"""Local Markdown + accessible HTML report from verified observation envelopes.

Usage: python scripts/render_report.py findings.json reports/my-run
No model-driven facts or synthetic charts. External publication is never attempted.
"""
from __future__ import annotations
from datetime import datetime, timezone
from hashlib import sha256
from html import escape
from pathlib import Path
import json
import re
import sys


def render(source: Path, target: Path) -> tuple[Path, Path, Path]:
    if target.exists() and any(target.iterdir()):
        raise ValueError('report_directory_not_empty_no_overwrite')
    data=json.loads(source.read_text(encoding='utf-8'))
    if not isinstance(data,dict) or not isinstance(data.get('observations'),list):
        raise ValueError('expected_observations_list')
    title=data.get('title')
    if not isinstance(title,str) or not 4<=len(title)<=180:raise ValueError('invalid_title')
    observations=data['observations']
    if not observations:raise ValueError('empty_report')
    validated=[]
    for n,obs in enumerate(observations):
        if not isinstance(obs,dict) or obs.get('schema_version')!='1.0' or obs.get('status') not in ('ok','partial','stale','unavailable','invalid_request','rate_limited','schema_changed','unsupported','incompatible_sources'):
            raise ValueError(f'invalid_observation_{n}')
        for key in ('metric','instrument_id','retrieved_at_utc','quality_flags'):
            if key not in obs:raise ValueError(f'missing_{key}_{n}')
        if obs['status']=='ok' and (obs.get('value') is None or not isinstance(obs.get('source'),dict) or not obs.get('unit')):
            raise ValueError(f'unsupported_success_{n}')
        if obs['status']!='ok' and obs.get('value') is not None:
            raise ValueError(f'unsuccessful_observation_with_value_{n}')
        validated.append(obs)
    if not target.parent.exists():target.parent.mkdir(parents=True)
    target.mkdir(exist_ok=True)
    asof=datetime.now(timezone.utc).isoformat(timespec='seconds').replace('+00:00','Z')
    unknown=sum(o['status']!='ok' for o in validated)
    verdict='INSUFFICIENT_EVIDENCE' if unknown else 'OBSERVATIONS_ONLY_NOT_A_TRADE_SIGNAL'
    source_sha=sha256(source.read_bytes()).hexdigest()
    rows=[];html_rows=[]
    for o in validated:
        val=json.dumps(o.get('value'),ensure_ascii=False,sort_keys=True,separators=(',',':')) if o.get('value') is not None else 'UNAVAILABLE'
        src=o.get('source') or {}
        locator=str(src.get('origin','unknown'))+str(src.get('endpoint',''))
        cells=[o['status'],o['metric'],o['instrument_id'],val,o.get('unit') or '—',o.get('observation_time_utc') or 'SOURCE TIME UNAVAILABLE',o['retrieved_at_utc'],locator]
        rows.append('| '+' | '.join(str(c).replace('|','\\|').replace('\n',' ') for c in cells)+' |')
        html_rows.append('<tr>'+''.join('<td><code>'+escape(str(c))+'</code></td>' for c in cells)+'</tr>')
    intro=f'As of {asof}. {len(validated)} reported observations; {unknown} unavailable, stale or failed. These are source observations, not a forecast or verified trade edge.'
    md=(f'# {title}\n\n**As-of UTC:** {asof}  \n**Research grade:** `{verdict}`\n\n## Executive summary\n\n{intro}\n\n'
        '## Evidence / freshness ledger\n\n| Status | Metric | Instrument | Value | Unit | Observation UTC | Retrieved UTC | Source |\n'
        '|---|---|---|---|---|---|---|---|\n'+'\n'.join(rows)+'\n\n'
        '## Interpretation boundaries\n\n- This output reports only supplied observation envelopes. An unavailable source is not evidence of zero.\n'
        '- Source observation time may be absent even if retrieval time is known.\n'
        '- Raw numeric data alone do not establish predictive edge, causal explanation, or executable market liquidity.\n'
        '- No scenarios, trade recommendations, or independent research conclusions are synthesized. Add separately verified narrative to support those claims.\n\n'
        f'## Input provenance\n\nInput SHA-256: `{source_sha}`\n')
    ths=''.join(f'<th scope="col">{escape(x)}</th>' for x in ('Status','Metric','Instrument','Value','Unit','Observation UTC','Retrieved UTC','Source'))
    html=('<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
          '<title>'+escape(title)+'</title><style>:root{color-scheme:light dark}body{font:16px/1.6 system-ui,sans-serif;max-width:1100px;margin:auto;padding:1.5rem}table{border-collapse:collapse;width:100%}th,td{padding:.7rem;border-bottom:1px solid #888;text-align:left;vertical-align:top}code{white-space:pre-wrap;overflow-wrap:anywhere}caption{text-align:left;font-weight:bold;padding:.6rem} .scroll{overflow-x:auto} a{color:inherit}</style></head><body><main><h1>'
          +escape(title)+'</h1><p><strong>As-of UTC:</strong> '+escape(asof)+'</p><p><strong>Research grade:</strong> '+escape(verdict)+'</p><h2>Executive summary</h2><p>'+escape(intro)+'</p>'
          '<h2>Evidence and freshness ledger</h2><div class="scroll" role="region" aria-label="Market data evidence" tabindex="0"><table><caption>Verified input envelopes (not predictions)</caption><thead><tr>'+ths+'</tr></thead><tbody>'+''.join(html_rows)+'</tbody></table></div>'
          '<h2>Interpretation boundaries</h2><p>Missing measurements are not zero. Source timestamps may be missing. These observations do not validate predictive edge, trading signals or executable liquidity.</p>'
          '<h2>Input provenance</h2><p>SHA-256: <code>'+source_sha+'</code></p></main></body></html>')
    manifest={'schema':'quant-report-manifest.v1','title':title,'as_of_utc':asof,'verdict':verdict,'observations':len(validated),
              'non_ok_observations':unknown,'input_sha256':source_sha,'source_path':source.name,
              'checks':{'source_envelope_validation':'passed','external_source_reverification':'not_run','backtest_validation':'not_run'}}
    (target/'report.md').write_text(md,encoding='utf-8')
    (target/'report.html').write_text(html,encoding='utf-8')
    for name in ('report.md','report.html'):
        manifest[name+'_sha256']=sha256((target/name).read_bytes()).hexdigest()
    (target/'manifest.json').write_text(json.dumps(manifest,indent=2)+'\n',encoding='utf-8')
    return target/'report.md',target/'report.html',target/'manifest.json'

if __name__=='__main__':
    if len(sys.argv)!=3:raise SystemExit('usage: python scripts/render_report.py findings.json reports/run-id')
    try:
        outputs=render(Path(sys.argv[1]),Path(sys.argv[2]))
    except (ValueError,OSError,json.JSONDecodeError) as exc:
        raise SystemExit(str(exc)) from exc
    print(json.dumps({'created':[str(x) for x in outputs]}))
