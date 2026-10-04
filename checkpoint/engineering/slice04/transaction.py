"""Versioned transaction composition; all payment execution is local simulation.

The app imports StageOneKernel and uses its own authenticated session and shared
SQLite transaction. Its human approval only enqueues; dispatch_stage1 is an
explicit local test/CLI helper. Legacy prepare/start/recover below remain isolated
v0.4 fixture demos: their TestPrincipal and automatic approval are not app auth.
"""
import json
from pathlib import Path
import sys
import time
from .domain import ROOT, canonical, assert_snapshot, freeze_decision, Rejected
from .fixtures import make_fixture
sys.path.insert(0,str(ROOT/'reference/transaction_reference'))
from kernel import Kernel,TestPrincipal
from reference.transaction_reference.kernel import StageOneKernel,Rejected as TransactionRejected
from reference.transaction_reference.local_psp import LocalPSP,dispatch_once,reconcile_only,dispatch_stage1,reconcile_stage1


def principals(plan_id, expires):
    base=dict(tenant_id='synthetic-tenant',owner_id='synthetic-buyer',plan_ids=frozenset({plan_id}),expires_at=expires)
    return {
        'setup':TestPrincipal('fixture',kind='service',role='fixture',scopes=frozenset({'fixture_setup'}),**base),
        'human':TestPrincipal('synthetic-buyer',kind='human',role='buyer',scopes=frozenset({'approve','stop','inspect'}),**base),
        'agent':TestPrincipal('synthetic-agent',kind='agent',role='buyer_planner',scopes=frozenset({'request_execution'}),**base),
        'worker':TestPrincipal('synthetic-worker',kind='service',role='payment_worker',scopes=frozenset({'dispatch','reconcile','inspect'}),**base)}


def prepare(directory):
    """Legacy synthetic fixture only; never called by the V1 approval service."""
    directory=Path(directory);directory.mkdir(parents=True,exist_ok=True)
    now=int(time.time());task,offers,evidence=make_fixture(now)
    quote=offers['B'];snapshot=freeze_decision(quote,task,evidence,now)
    plan_id='v04-'+snapshot['snapshot_id']
    actors=principals(plan_id,now+600)
    kernel=Kernel(directory/'kernel.sqlite');psp=LocalPSP(directory/'simulated_psp.sqlite')
    kernel.seed_budget(actors['setup'],'HKD',task['cash_cap_minor'])
    # The legacy schema has no discount column. Do not disguise total payable as
    # goods: discounted orders must use the V1 ledger with explicit components.
    if quote['discounts']:
        raise TransactionRejected('LEGACY_DISCOUNTS_REQUIRE_V1')
    fingerprint=kernel.seed_plan(actors['setup'],plan_id=plan_id,merchant=quote['merchant_id'],currency='HKD',
                                 goods=quote['goods_minor'],fees=sum(f['amount_minor'] for f in quote['fee_lines']),
                                 quote_expires=quote['expires_at'])
    # This is fixture approval, not an implementation of login/challenge verification.
    kernel.approve(actors['human'],plan_id,expected_digest=fingerprint,expected_version=1,
                   merchant=quote['merchant_id'],currency='HKD',cash_cap=task['cash_cap_minor'],expires=now+600)
    assert_snapshot(snapshot,quote,task,evidence,now)
    operation=kernel.request_execution(actors['agent'],plan_id,command_key='fixture-purchase-'+snapshot['snapshot_id'],authorization_version=1)
    state={'snapshot':snapshot,'quote':quote,'task':task,'evidence':evidence,'plan_id':plan_id,
           'operation_id':operation['data']['operation_id'],'principal_expiry':now+600}
    (directory/'checkpoint.json').write_text(canonical(state))
    return kernel,psp,actors,state


def summary(kernel,psp,human,state):
    view=kernel.inspect(human,state['plan_id'])
    return {'environment':'local_simulator','identity':'TestPrincipal','operation_id':state['operation_id'],
            'plan_id':state['plan_id'],'snapshot_id':state['snapshot']['snapshot_id'],
            'payment_rows':psp.db.execute('select count(*) from payments').fetchone()[0],
            'view':view,'audit':[dict(r) for r in kernel.db.execute('select seq,event,object_id,details from audit order by seq')]}


def start(directory,scenario):
    kernel,psp,actors,state=prepare(directory)
    try:
        # A production adapter must reload these from its trusted registry and
        # atomically bind their versions to the dispatch claim. This local fixture
        # has no external refresh service and does not demonstrate that DB gate.
        assert_snapshot(state['snapshot'],state['quote'],state['task'],state['evidence'],int(time.time()))
        if scenario=='stop':
            kernel.stop(actors['human'],state['plan_id'])
        result=dispatch_once(kernel,psp,actors['worker'],state['operation_id'],
                             mode='accept_then_timeout' if scenario=='unknown' else 'success')
        return {'dispatch_result':result,**summary(kernel,psp,actors['human'],state)}
    finally:
        kernel.close();psp.close()


def recover(directory):
    directory=Path(directory);state=json.loads((directory/'checkpoint.json').read_text())
    kernel=Kernel(directory/'kernel.sqlite');psp=LocalPSP(directory/'simulated_psp.sqlite')
    actors=principals(state['plan_id'],int(time.time())+600) # new server-issued TEST principal
    try:
        kernel.stop(actors['human'],state['plan_id'])
        result=reconcile_only(kernel,psp,actors['worker'],state['operation_id'])
        return {'recovery_method':'query_original_only','recovery_result':result,**summary(kernel,psp,actors['human'],state)}
    finally:
        kernel.close();psp.close()
