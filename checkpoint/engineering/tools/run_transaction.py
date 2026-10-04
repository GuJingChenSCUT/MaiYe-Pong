import argparse,json,subprocess,sys,tempfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from slice04.transaction import start,recover

def main():
    p=argparse.ArgumentParser();p.add_argument('--recover');p.add_argument('--out');a=p.parse_args()
    if a.recover:
        print(json.dumps(recover(a.recover)));return
    if not a.out:p.error('--out required')
    with tempfile.TemporaryDirectory() as temp:
        base=Path(temp)
        success=start(base/'success','success');stop=start(base/'stop','stop');unknown=start(base/'unknown','unknown')
        recovered=json.loads(subprocess.check_output([sys.executable,__file__,'--recover',str(base/'unknown')],text=True))
        assert success['view']['operation']['state']=='SUCCEEDED' and success['payment_rows']==1
        assert stop['view']['operation']['state']=='STOPPED' and stop['payment_rows']==0
        assert unknown['view']['operation']['state']=='UNKNOWN' and unknown['view']['budget']['reserved']==5500
        assert recovered['operation_id']==unknown['operation_id'] and recovered['payment_rows']==1
        assert recovered['view']['operation']['state']=='SUCCEEDED' and recovered['view']['budget']['spent']==5500
        report={'status':'passed','environment':'local_simulator','official_psp_verified':False,
                'success':success,'stop':stop,'unknown_before_restart':unknown,'recovered_in_new_process':recovered}
        Path(a.out).parent.mkdir(parents=True,exist_ok=True);Path(a.out).write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
        print(json.dumps({'status':'passed','scenarios':3,'recovery':'new_process_original_operation','official_psp_verified':False}))
if __name__=='__main__':main()
