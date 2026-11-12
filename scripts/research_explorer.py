"""Static accessible comparative research explorer from verified local numerical series.

Input requires exact provenance for every series; no fetches or inferred data.
"""
import json
import math
import sys
from urllib.parse import urlsplit
from datetime import datetime
from pathlib import Path
from html import escape
from hashlib import sha256

def render(input_path:Path,output_path:Path):
    payload=json.loads(input_path.read_text(encoding='utf8'))
    if payload.get('schema')!='research-explorer-input.v1' or not isinstance(payload.get('series'),list) or not payload['series']:
        raise ValueError('invalid explorer schema/series')
    if output_path.exists():raise ValueError('output already exists')
    plots=[]; rows=[]
    for series in payload['series']:
        for key in ('name','units','source_uri','observation_ids','values','timestamps_utc','method'):
            if not series.get(key):raise ValueError('incomplete series provenance')
        if urlsplit(series['source_uri']).scheme not in ('https','http') or not urlsplit(series['source_uri']).netloc:
            raise ValueError('source must be HTTP(S) provenance URL')
        values=series['values'];times=series['timestamps_utc']
        if len(values)!=len(times) or len(values)<2 or len(set(times))!=len(times) or times!=sorted(times):
            raise ValueError('unaligned timestamps or insufficient samples')
        if not all(type(v) in (int,float) and math.isfinite(v) for v in values):raise ValueError('invalid value')
        for timestamp in times:
            try:
                parsed=datetime.fromisoformat(timestamp.replace('Z','+00:00'))
                if parsed.utcoffset() is None or parsed.utcoffset().total_seconds()!=0:raise ValueError()
            except (ValueError,AttributeError):raise ValueError('timestamps must be ISO-8601 UTC')
        if len(series['observation_ids'])!=len(values):raise ValueError('each sample requires evidence ID')
        low=min(values);high=max(values);span=high-low or 1
        points=' '.join(f'{35+i*730/(len(values)-1):.2f},{175-(v-low)/span*145:.2f}' for i,v in enumerate(values))
        name=escape(series['name']);unit=escape(series['units']);src=escape(series['source_uri'],quote=True)
        plots.append(f'<section><h2>{name}</h2><p>Units: {unit}. Method: {escape(series["method"])}. <a href="{src}" rel="noopener noreferrer">Evidence source</a>.</p>'
                     f'<svg viewBox="0 0 800 210" role="img" aria-label="{name} series from {escape(times[0])} to {escape(times[-1])}, minimum {low:g}, maximum {high:g}">'
                     f'<rect x="0" y="0" width="800" height="210" fill="none" stroke="currentColor"/><polyline points="{points}" fill="none" stroke="currentColor" stroke-width="2"/></svg>'
                     '<details><summary>Underlying recorded values</summary><div class="scroll"><table><thead><tr><th>UTC</th><th>Value</th><th>Evidence ID</th></tr></thead><tbody>'+
                     ''.join(f'<tr><td>{escape(t)}</td><td>{v:g}</td><td>{escape(e)}</td></tr>' for t,v,e in zip(times,values,series['observation_ids']))+'</tbody></table></div></details></section>')
        rows.append(f'<tr><th scope="row">{name}</th><td>{len(values)}</td><td>{values[0]:g}</td><td>{values[-1]:g}</td><td>{unit}</td></tr>')
    html='<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Research history explorer</title><style>body{font:16px/1.55 system-ui;max-width:1100px;margin:auto;padding:1.5rem}svg{width:100%;height:auto}table{border-collapse:collapse;width:100%}td,th{padding:.5rem;border:1px solid #888;text-align:left}.scroll{overflow-x:auto}a{color:inherit;text-decoration:underline}</style></head><body><main><h1>Research history explorer</h1><p>Offline recorded series only; charts are not live. Comparison is descriptive, not a predictive trading signal.</p><h2>Series comparison</h2><div class="scroll"><table><thead><tr><th>Series</th><th>Samples</th><th>First</th><th>Last</th><th>Unit</th></tr></thead><tbody>'+''.join(rows)+'</tbody></table></div>'+''.join(plots)+'</main></body></html>'
    output_path.parent.mkdir(parents=True,exist_ok=True);output_path.write_text(html,encoding='utf8')
    return {'output':str(output_path),'sha256':sha256(output_path.read_bytes()).hexdigest(),'series':len(plots)}

if __name__=='__main__':
    if len(sys.argv)!=3:raise SystemExit('usage: python scripts/research_explorer.py input.json output.html')
    try:print(json.dumps(render(Path(sys.argv[1]),Path(sys.argv[2]))))
    except (ValueError,KeyError,OSError,TypeError) as e:raise SystemExit(str(e))
