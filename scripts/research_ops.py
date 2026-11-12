"""Read-only offline research evaluation / research intelligence CLI.

python scripts/research_ops.py eval task.json artifact.json
python scripts/research_ops.py freeze plan.json dataset.json
python scripts/research_ops.py test plan.json dataset.json frozen.json
python scripts/research_ops.py thesis-add db.sqlite revision.json
python scripts/research_ops.py thesis-search db.sqlite term
"""
import argparse
import json
import sys
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from research_lab.core import ContractError
from research_lab.evals import evaluate_task
from research_lab.experiments import ExperimentPlan, freeze_candidate, evaluate_frozen
from research_lab.validation import LabelWindow
from research_lab.intelligence import ResearchIntelligence

def read(path):return json.loads(Path(path).read_text(encoding='utf-8'))

def execute(argv=None):
    p=argparse.ArgumentParser(description='Offline research intelligence utilities (no provider requests)')
    p.add_argument('action',choices=['eval','freeze','test','thesis-add','thesis-search'])
    p.add_argument('args',nargs='+')
    opts=p.parse_args(argv)
    a=opts.args
    if opts.action=='eval' and len(a)==2:return evaluate_task(read(a[0]),read(a[1]))
    if opts.action in ('freeze','test') and len(a)==(2 if opts.action=='freeze' else 3):
        plan=ExperimentPlan(**read(a[0]));data=read(a[1]);windows=[LabelWindow(**v) for v in data['windows']]
        if opts.action=='freeze':return freeze_candidate(plan,windows,data['candidates'])
        frozen=read(a[2]);return evaluate_frozen(frozen,plan,windows,data['candidates'],registered_digest=frozen['freeze_digest'])
    if opts.action=='thesis-add' and len(a)==2:
        return {'revision_id':ResearchIntelligence(a[0]).add(read(a[1]))}
    if opts.action=='thesis-search' and len(a)==2:
        return {'revisions':ResearchIntelligence(a[0]).search(a[1])}
    p.error('invalid arguments for action')

if __name__=='__main__':
    try: print(json.dumps(execute(),sort_keys=True,indent=2))
    except (ContractError,ValueError,KeyError,OSError) as exc:raise SystemExit('research operation failed: '+str(exc))
