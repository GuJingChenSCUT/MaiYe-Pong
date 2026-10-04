"""Analyze actual, de-identified three-arm trials; never fill missing measurements."""
from collections import defaultdict
from statistics import median
import math
from .domain import Rejected
ARMS=('manual','comparison_plus_ai','agent')
CHECKS=('specification','delivery','cash_total','eligibility','returns')

def summarize(records):
    if not records:
        return {'status':'not_measured','participants':0,'trials':0,'arms':{},'paired':[]}
    groups=defaultdict(dict);arms=defaultdict(list)
    for r in records:
        if r.get('record_kind')!='observed' or r.get('arm') not in ARMS:
            raise Rejected('ONLY_OBSERVED_TRIALS_ALLOWED')
        if not all(isinstance(r.get(k),str) and r[k] for k in ('participant_id','task_family','evidence_version')):
            raise Rejected('MATCH_KEYS_REQUIRED')
        if type(r.get('elapsed_seconds')) not in (int,float) or not math.isfinite(r['elapsed_seconds']) or r['elapsed_seconds']<0:
            raise Rejected('INVALID_ELAPSED_TIME')
        if r.get('outcome') not in ('completed','stopped_correctly','failed','abandoned'):
            raise Rejected('OUTCOME_REQUIRED')
        if set(r.get('correctness',{}))!=set(CHECKS) or any(type(v) is not bool for v in r['correctness'].values()):
            raise Rejected('FIVE_BLINDED_CHECKS_REQUIRED')
        key=tuple(r[k] for k in ('participant_id','task_family','evidence_version'))
        if r['arm'] in groups[key]:raise Rejected('DUPLICATE_TRIAL_ARM')
        groups[key][r['arm']]=r;arms[r['arm']].append(r)
    result={}
    for arm in ARMS:
        rows=arms[arm]
        processing=[r.get('merchant_seconds') for r in rows]
        rates=[r.get('merchant_hkd_per_hour') for r in rows]
        cost=None
        if rows and all(type(v) in (int,float) and math.isfinite(v) and v>=0 for v in processing+rates):
            cost=sum(sec*rate/3600 for sec,rate in zip(processing,rates))
        result[arm]={'attempts':len(rows),'completed':sum(r['outcome']=='completed' for r in rows),
                     'all_five_correct':sum(all(r['correctness'].values()) for r in rows),
                     'median_elapsed_all_attempts':median(r['elapsed_seconds'] for r in rows) if rows else None,
                     'failed_or_abandoned':sum(r['outcome'] in ('failed','abandoned') for r in rows),
                     'merchant_labor_hkd':round(cost,2) if cost is not None else None,
                     'merchant_cost_coverage':sum(type(v) in (int,float) for v in processing)}
    paired=[]
    for base in ('manual','comparison_plus_ai'):
        deltas=[]
        complete=0
        for rows in groups.values():
            if base not in rows or 'agent' not in rows:continue
            complete+=1
            a,b=rows['agent'],rows[base]
            if all(a['correctness'].values()) and all(b['correctness'].values()) and a['outcome']=='completed' and b['outcome']=='completed':
                deltas.append(b['elapsed_seconds']-a['elapsed_seconds'])
        paired.append({'baseline':base,'matched_pairs':complete,'both_correct_completed_pairs':len(deltas),
                       'median_seconds_saved_conditional':median(deltas) if deltas else None,
                       'interpretation':'Conditional on both arms completing correctly; failures remain in arm totals.'})
    return {'status':'observed_descriptive_only','participants':len({r['participant_id'] for r in records}),
            'trials':len(records),'arms':result,'paired':paired}
