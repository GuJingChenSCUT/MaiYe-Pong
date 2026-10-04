"""One offline command; no model API, merchant API or payment provider calls."""
import json,re,subprocess,sys
from datetime import datetime,timezone
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
E=ROOT/'evidence';E.mkdir(exist_ok=True)
def call(name,args):
    r=subprocess.run([sys.executable,*args],cwd=ROOT,text=True,capture_output=True)
    (E/(name+'.log')).write_text(r.stdout+'\n'+r.stderr)
    match=re.search(r'Ran (\d+) tests?',r.stderr)
    return {'check':name,'exit_code':r.returncode,'unit_tests':int(match.group(1)) if match else 0,'log':name+'.log'}
def main():
    jobs=[('original_runtime',['-m','unittest','discover','-s','reference/runtime_reference/tests','-v']),
          ('original_capabilities',['-m','unittest','discover','-s','reference/capability_runtime/tests','-v']),
          ('original_transactions',['-m','unittest','discover','-s','reference/transaction_reference/tests','-v']),
          ('original_composition',['-m','unittest','discover','-s','reference/qa','-p','test_reference_composition.py','-v']),
          ('original_contracts',['reference/qa/validate_contracts.py']),
          ('slice04_tests',['-m','unittest','discover','-s','tests','-v']),
          ('public_fact_audit',['tools/audit_public_facts.py','--out','evidence/public_fact_audit.json']),
          ('transaction_scenarios',['tools/run_transaction.py','--out','evidence/transaction_run.json']),
          ('benchmark_empty',['tools/summarize_benchmark.py','--input','data/benchmark_observations.jsonl','--out','evidence/benchmark_summary.json'])]
    jobs += [('offline_'+s,['tools/run_agent.py','--mode','offline','--scenario',s,'--out','evidence/agent_offline_'+s+'.json']) for s in ('shipping_increase','both_unavailable','no_change','injection')]
    results=[call(name,args) for name,args in jobs]
    report={'checked_at_utc':datetime.now(timezone.utc).isoformat(),'status':'passed' if all(r['exit_code']==0 for r in results) else 'failed',
            'unit_tests':sum(r['unit_tests'] for r in results),'checks':results,
            'live_model_calls_in_this_command':0,'real_payments':0,'official_psp_verified':False,
            'production_identity_verified':False,'public_application_deployed':False,
            'scope':'Local deterministic/reference tests and synthetic scenarios. Offline model behavior is scripted. Public facts remain non-executable. No value trial was conducted.'}
    (E/'verification.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(report,ensure_ascii=False));return 0 if report['status']=='passed' else 1
if __name__=='__main__':raise SystemExit(main())
