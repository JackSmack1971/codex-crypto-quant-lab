import tempfile
import unittest
from pathlib import Path
from research_lab.core import ContractError
from research_lab.validation import LabelWindow
from research_lab.experiments import ExperimentPlan, freeze_candidate, evaluate_frozen
from research_lab.evals import evaluate_task
from research_lab.intelligence import ResearchIntelligence
from research_lab.jobs import JobStore
from research_lab.provider_policy import MetricContract, qualified_fallback, provider_health
from research_lab.factors import calculate_factor
from research_lab.risk import portfolio_risk

T='2026-01-01T00:00:00Z'

def windows(n):
    return [LabelWindow(f'2026-01-{i+1:02d}T00:00:00Z', f'2026-01-{i+1:02d}T12:00:00Z') for i in range(n)]

class V24Tests(unittest.TestCase):
    def test_train_only_freeze_and_holdout(self):
        plan=ExperimentPlan('x',4,7)
        rows=windows(10)
        base={'a':[0.1]*4+[0.0]*6,'b':[-0.1]*4+[1.0]*6}
        freeze=freeze_candidate(plan,rows,base)
        self.assertEqual(freeze['frozen']['selected'],'a')
        result=evaluate_frozen(freeze,plan,rows,base,registered_digest=freeze['freeze_digest'])
        self.assertEqual(result['selected'],'a')
        mutated={'a':[0.1]*4+[9.0]*6,'b':[-0.1]*4+[10.0]*6}
        self.assertEqual(freeze_candidate(plan,rows,mutated)['freeze_digest'],freeze['freeze_digest'])
        self.assertEqual(evaluate_frozen(freeze,plan,rows,mutated,registered_digest=freeze['freeze_digest'])['selected'],'a')
        with self.assertRaises(ContractError):
            evaluate_frozen(freeze,plan,rows,base,registered_digest='tampered')
    def test_purge_and_invalid(self):
        rows=windows(9); rows[3]=LabelWindow(rows[3].start_utc,rows[5].end_utc)
        with self.assertRaises(ContractError):freeze_candidate(ExperimentPlan('x',4,6,embargo=3),rows,{'a':[0.1]*9})
    def test_agent_eval_missing_and_leak(self):
        spec={'schema':'research-task-eval.v1','task_id':'t','allowed_capabilities':['read'], 'required_metrics':['funding']}
        artifact={'run_id':'x','as_of_utc':T,'evidence_snapshot_id':'s','used_capabilities':['read']}
        self.assertEqual(evaluate_task(spec,artifact)['state'],'NOT_EVALUABLE')
        artifact['observations']=[{'metric':'funding','status':'OBSERVED','observed_at_utc':T,'available_at_utc':'2026-01-02T00:00:00Z'}]
        self.assertEqual(evaluate_task(spec,artifact)['state'],'FAIL')
        artifact['observations'][0]['available_at_utc']=T
        self.assertEqual(evaluate_task(spec,artifact)['state'],'PASS')
        artifact['used_capabilities']=['trade']
        self.assertEqual(evaluate_task(spec,artifact)['state'],'FAIL')
    def test_immutable_thesis(self):
        with tempfile.TemporaryDirectory() as temp:
            store=ResearchIntelligence(Path(temp)/'i.sqlite')
            first={'thesis_id':'btc','state':'UNVERIFIED','claim':'possible association','falsifier':'no association','created_at_utc':T,'evidence_ids':['o1']}
            a=store.add(first)
            self.assertEqual(store.add(first),a)
            later={**first,'parent_id':a,'state':'SUPPORTED','claim':'observed association','created_at_utc':'2026-01-02T00:00:00Z'}
            b=store.add(later)
            self.assertEqual(store.compare(a,b)['validity'],'NOT_REASSESSED')
            self.assertEqual(len(store.search(thesis_id='btc')),2)
            with self.assertRaises(ContractError):store.add({**later,'parent_id':'nope'})
    def test_job_reconciliation(self):
        with tempfile.TemporaryDirectory() as temp:
            db=JobStore(Path(temp)/'jobs.db')
            db.create('x',{'input':1},T)
            db.move('x','NEW','RUNNING',T)
            self.assertEqual(db.reconcile_candidates(),['x'])
            with self.assertRaises(ContractError):db.move('x','NEW','COMPLETED',T,result={})
            db.move('x','RUNNING','INTERRUPTED',T)
            db.move('x','INTERRUPTED','RECONCILING',T)
            db.move('x','RECONCILING','RUNNING',T)
            db.move('x','RUNNING','COMPLETED',T,result={'ok':True})
            self.assertEqual(db.get('x')['state'],'COMPLETED')
            # This valid-shaped transition reaches the SQL transaction and fails
            # on stale state; TemporaryDirectory cleanup verifies the connection
            # was closed on the exceptional path as well.
            with self.assertRaises(ContractError):db.move('x','RUNNING','FAILED',T)
    def test_source_compatibility(self):
        c=MetricContract('venue:btc','funding','rate','venue','8h','provider')
        candidate={'provider':'p','contract':c.__dict__,'status':'OBSERVED','observed_at_utc':T,'available_at_utc':T}
        other={**candidate,'contract':{**c.__dict__,'venue':'other'}}
        out=qualified_fallback(c,[other,candidate],T,max_age_seconds=120)
        self.assertEqual([x['status'] for x in out['decisions']],['INCOMPATIBLE','QUALIFIED'])
        self.assertEqual(provider_health([{'provider':'p','status':'SUCCESS'}])['p']['observed_success_fraction'],1)
    def test_factors_risk(self):
        self.assertEqual(calculate_factor('simple_momentum',{'past_close':'100','current_close':'110'}),'0.1')
        self.assertEqual(calculate_factor('basis',{'futures_price':'102','spot_price':'100'}),'0.02')
        risk=portfolio_risk({'a':[0.1,0,-0.1],'b':[-0.1,0,0.1]},{'a':0.5,'b':0.5})
        self.assertAlmostEqual(risk['portfolio_period_variance'],0)

if __name__=='__main__':unittest.main()
