"""Reproduce the public-evidence comparison. It cannot create a checkout quote."""
import argparse,json,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from slice04.domain import digest
def audit(data):
    assert data['environment']=='public_research'
    offers=data['offers'];assert len(offers)==2
    for q in offers:
        assert type(q['goods_minor']) is int and type(q['standard_shipping_minor']) is int
        assert q['checkout_verified'] is False and q['can_execute'] is False
        assert all(s in data['sources'] for s in q['source_refs'])
    conflicts=[k for k in ('brand','variant','net_content','unit','pack_count','origin')
               if offers[0]['product'][k]!=offers[1]['product'][k]]
    return {'status':'research_compared_execution_blocked','environment':'public_research',
            'input_sha256':digest(data),'matched_listed_fields':[k for k in ('brand','variant','net_content','unit','pack_count') if k not in conflicts],
            'conflicting_fields':conflicts,
            'conditional_subtotals_minor':{q['merchant_id']:q['goods_minor']+q['standard_shipping_minor'] for q in offers},
            'conditional_subtotal_is_executable_total':False,
            'blockers':['PRODUCT_ORIGIN_CONFLICT','CURRENT_BATCH_AND_GTIN_UNCONFIRMED','CHECKOUT_FEES_AND_STOCK_UNVERIFIED','MERCHANT_EXECUTION_PERMISSION_MISSING','RETURN_LOGISTICS_UNCONFIRMED'],
            'correct_next_action':'Obtain current merchant confirmation for batch identity, checkout and return logistics; no payment.'}
def main():
    p=argparse.ArgumentParser();p.add_argument('--out',required=True);a=p.parse_args()
    result=audit(json.loads((ROOT/'data/public_fact_slice.json').read_text()))
    Path(a.out).write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n');print(json.dumps(result,ensure_ascii=False))
if __name__=='__main__':main()
