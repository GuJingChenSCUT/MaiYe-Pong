import argparse
import json
import sys
from pathlib import Path
from datetime import datetime,timezone
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from slice04.agent import run,run_task_v1,RuntimeRejected
from slice04.domain import Rejected

def main():
    p=argparse.ArgumentParser()
    p.add_argument('--mode',choices=['offline','live','scripted'],default='offline')
    p.add_argument('--scenario',choices=['normal','shipping_increase','both_unavailable','no_change','injection'],default='shipping_increase')
    p.add_argument('--task-id',help='Read an existing persistent V1 task; does not approve or persist a result.')
    p.add_argument('--task-db',help='Path to application SQLite, used read-only with --task-id.')
    p.add_argument('--draft-file',help='Explicit V1 local draft JSON for a nonpersistent protocol exercise.')
    p.add_argument('--thinking',action='store_true')
    p.add_argument('--allow-provider-charge',action='store_true')
    p.add_argument('--out',required=True)
    a=p.parse_args()
    if a.mode=='live' and not a.allow_provider_charge:
        p.error('Live requests may incur API fees: include --allow-provider-charge')
    if a.task_id and not a.task_db:
        p.error('--task-id requires --task-db')
    if a.task_id and a.draft_file:
        p.error('Choose --task-id or --draft-file')
    try:
        if a.task_id or a.draft_file:
            if a.mode=='offline':
                p.error('V1 uses explicit --mode scripted or live; offline is the legacy fixture.')
            task_id,version=a.task_id or 'cli-draft',1
            if a.task_id:
                import sqlite3
                # A local maintainer command, not an HTTP authority boundary.
                con=sqlite3.connect(Path(a.task_db).resolve().as_uri()+'?mode=ro',uri=True)
                con.row_factory=sqlite3.Row
                try:
                    row=con.execute('SELECT * FROM app_tasks WHERE task_id=?',(a.task_id,)).fetchone()
                    if row is None:raise Rejected('TASK_NOT_FOUND')
                    record=dict(row)
                    draft_raw=record.get('draft_json',record.get('draft'))
                    if not isinstance(draft_raw,str):raise Rejected('TASK_STORAGE_SCHEMA_UNSUPPORTED')
                    draft=json.loads(draft_raw)
                    version=record['constraints_version']
                finally:con.close()
            else:
                draft=json.loads(Path(a.draft_file).read_text(encoding='utf-8'))
            report=run_task_v1(draft,mode=a.mode,scenario='normal' if a.scenario=='no_change' else a.scenario,
                               task_id=task_id,constraints_version=version,run_id='cli-readonly')
            report['persistence']='not_persisted_use_authenticated_application_for_mutations'
            exit_code=0 if report['status'] in ('proposed','clarifying','stopped') else 2
        else:
            if a.mode=='scripted' or a.scenario=='normal':p.error('V1 mode requires --task-id or --draft-file')
            report=run(a.mode,a.scenario,a.thinking);exit_code=0
    except (Rejected,RuntimeRejected) as e:
        report={**getattr(e,'safe_report',{}),'status':'blocked' if str(e)=='DEEPSEEK_API_KEY_MISSING' else 'failed',
                'code':str(e),'model_mode':a.mode,'commerce_mode':'synthetic_fixture',
                'model_online_verified':False,'real_merchant_verified':False};exit_code=2
    report['checked_at_utc']=datetime.now(timezone.utc).isoformat()
    target=Path(a.out);target.parent.mkdir(parents=True,exist_ok=True)
    target.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n', encoding='utf-8')
    print(json.dumps({k:report[k] for k in ('status','code','model_mode','model_online_verified','real_merchant_verified') if k in report}))
    return exit_code
if __name__=='__main__':raise SystemExit(main())
