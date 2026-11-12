"""Offline research-task replay grading with explicit missing-evidence outcomes."""
from .core import ContractError, _decimal, _utc

def evaluate_task(spec: dict, artifact: dict) -> dict:
    if spec.get('schema')!='research-task-eval.v1':raise ContractError('unknown evaluation contract')
    checks=[]
    def mark(name,state,why=''): checks.append({'name':name,'state':state,'reason':why})
    for key in ('run_id','as_of_utc','evidence_snapshot_id'):
        mark('required:'+key,'PASS' if artifact.get(key) else 'FAIL')
    used=set(artifact.get('used_capabilities',[])); allowed=set(spec.get('allowed_capabilities',[]))
    mark('capabilities','FAIL' if used-allowed else 'PASS',','.join(sorted(used-allowed)))
    for metric in spec.get('required_metrics',[]):
        matches=[o for o in artifact.get('observations',[]) if o.get('metric')==metric and o.get('status')=='OBSERVED']
        if not matches:mark('metric:'+metric,'NOT_EVALUABLE','missing observations');continue
        result='PASS'
        for o in matches:
            try:
                if not o.get('observed_at_utc') or not o.get('available_at_utc'):
                    result='NOT_EVALUABLE';break
                if _utc(o['observed_at_utc'])>_utc(artifact['as_of_utc']) or _utc(o['available_at_utc'])>_utc(artifact['as_of_utc']):
                    result='FAIL';break
            except (ValueError,KeyError,TypeError):result='NOT_EVALUABLE';break
        mark('metric:'+metric,result)
    for e in spec.get('numeric_expectations',[]):
        key=e['key'];actual=artifact.get('metrics',{}).get(key)
        if actual is None:mark('numeric:'+key,'NOT_EVALUABLE');continue
        try:
            delta=abs(_decimal(str(actual))-_decimal(str(e['expected'])))
            mark('numeric:'+key,'PASS' if delta<=_decimal(str(e.get('absolute_tolerance','0'))) else 'FAIL')
        except (ValueError,TypeError):mark('numeric:'+key,'FAIL','invalid numerical value')
    for disclosure in spec.get('required_disclosures',[]):
        mark('disclosure:'+disclosure,'PASS' if disclosure in artifact.get('disclosures',[]) else 'FAIL')
    states=[c['state'] for c in checks]
    return {'schema':'research-task-eval-result.v1','task_id':spec.get('task_id'),
            'state':'FAIL' if 'FAIL' in states else 'NOT_EVALUABLE' if 'NOT_EVALUABLE' in states else 'PASS',
            'checks':checks}
