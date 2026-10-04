"""Offline component composition. No live model, real login, or payment sandbox.

All identities and money are synthetic fixtures. This verifies wiring through
the actual tool registry, capability loader and persistent transaction port.
"""
from pathlib import Path
import hashlib
import json
import sys
import tempfile
import time
import unittest

BASE=Path(__file__).resolve().parents[1]
for directory in ('capability_runtime','runtime_reference','transaction_reference'):
    sys.path.insert(0,str(BASE/directory))
from loader import CapabilityLoader, TrustedCapabilityContext
from agent_runtime import Binding, ToolRegistry, ToolRuntime, TrustedContext, RuntimeRejected
from kernel import Kernel, TestPrincipal
from local_psp import LocalPSP, dispatch_once, reconcile_only
from runtime_adapter import ExecutionWorkflowAdapter


class FakeModel:
    """One preselected tool call; this is not an AI decision-quality test."""
    def __init__(self): self.payloads=[]
    def complete(self,payload):
        self.payloads.append(payload)
        return {'choices':[{'finish_reason':'tool_calls','message':{
            'role':'assistant','content':None,'tool_calls':[{
                'id':'synthetic-call','type':'function','function':{
                    'name':'request_execution','arguments':'{"plan_id":"synthetic-plan"}'}}]}}]}


class FixtureAuthorizer:
    def __init__(self,context): self.context=context
    def check(self,context,tool,arguments):
        if context != self.context or arguments != {'plan_id':'synthetic-plan'}:
            raise RuntimeRejected('fixture_scope_denied')
        if tool['definition']['function']['name'] != 'request_execution':
            raise RuntimeRejected('fixture_tool_denied')
    def check_output(self,context,tool,arguments,result):
        self.check(context,tool,arguments)
        if result['data']['plan_id'] != 'synthetic-plan':
            raise RuntimeRejected('fixture_output_scope_denied')


class ReferenceCompositionTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.kernel_path=Path(self.temp.name)/'kernel.sqlite'
        self.psp_path=Path(self.temp.name)/'external-simulation.sqlite'
        self.kernel=Kernel(self.kernel_path)
        self.psp=LocalPSP(self.psp_path)
        self.deadline=int(time.time())+600
        base=dict(tenant_id='synthetic-tenant',owner_id='synthetic-buyer',
                  plan_ids=frozenset({'synthetic-plan'}),expires_at=self.deadline)
        self.fixture=TestPrincipal('fixture',kind='service',role='fixture',scopes=frozenset({'fixture_setup'}),**base)
        self.human=TestPrincipal('synthetic-buyer',kind='human',role='buyer',scopes=frozenset({'approve','stop','inspect'}),**base)
        self.agent=TestPrincipal('synthetic-agent',kind='agent',role='buyer_planner',scopes=frozenset({'request_execution'}),**base)
        self.worker=TestPrincipal('synthetic-worker',kind='service',role='payment_worker',scopes=frozenset({'dispatch','reconcile','inspect'}),**base)
        self.kernel.seed_budget(self.fixture,'HKD',2000)
        fingerprint=self.kernel.seed_plan(self.fixture,plan_id='synthetic-plan',merchant='synthetic-merchant',
            currency='HKD',goods=1000,fees=0,quote_expires=self.deadline)
        self.kernel.approve(self.human,'synthetic-plan',expected_digest=fingerprint,expected_version=1,
            merchant='synthetic-merchant',currency='HKD',cash_cap=1000,expires=self.deadline)
        self.runtime_context=TrustedContext('synthetic-agent','synthetic-tenant','synthetic-task','buyer_planner',1,'synthetic-logical-step',self.deadline)
        self.capability_context=TrustedCapabilityContext('synthetic-task','synthetic-run','buyer_planner','dispatch',
            'request_authorized_execution','phase_1',frozenset({'request_execution'}),self.deadline)

    def tearDown(self):
        self.kernel.close(); self.psp.close(); self.temp.cleanup()

    def prepare_runtime(self):
        # Both contexts are built from one server-owned fixture snapshot.
        self.assertEqual(self.capability_context.task_id,self.runtime_context.task_id)
        self.assertEqual(self.capability_context.role,self.runtime_context.role)
        self.assertEqual(self.capability_context.deadline_epoch,self.runtime_context.expires_at_epoch)
        caproot=BASE/'capabilities'
        registry=json.loads((BASE/'contracts/tool_registry.json').read_text())
        policy=json.loads((BASE/'capability_runtime/stage_tool_policy.reference.json').read_text())['stages']
        loader=CapabilityLoader(caproot,
            expected_manifest_sha256=hashlib.sha256((caproot/'manifest.json').read_bytes()).hexdigest(),
            expected_format_version='1.0.0',expected_pack_version='0.3.0',tool_registry=registry,
            deployed_handler_allowlist=frozenset({'request_execution'}),
            stage_tool_policy={k:frozenset(v) for k,v in policy.items()},offline_test_mode=True)
        projection={'plan_ref':{'plan_id':'synthetic-plan','version':1},
                    'mandate_status_projection':{'status':'active','version':1,'source':'synthetic_fixture'},
                    'execution_checkpoint':None}
        prepared=loader.prepare(self.capability_context,'authorized_execute',projection)
        self.assertEqual(prepared.allowed_tools,('request_execution',))
        model=FakeModel()
        runtime=ToolRuntime(ToolRegistry(list(prepared.filtered_registry)),model,FixtureAuthorizer(self.runtime_context),
            {'request_execution':Binding()},workflow=ExecutionWorkflowAdapter(self.kernel,lambda context:self.agent))
        return prepared,model,runtime

    def enqueue(self):
        prepared,model,runtime=self.prepare_runtime()
        result=runtime.run(system_prompt=prepared.system_prompt,user_text=prepared.task_message,context=self.runtime_context)
        self.assertEqual(result.status,'workflow_handoff')
        self.assertEqual(len(model.payloads),1)
        self.assertEqual([t['function']['name'] for t in model.payloads[0]['tools']],['request_execution'])
        return result.workflow_receipt['data']['operation_id']

    def test_loaded_capability_tool_call_reaches_persistent_queue_and_settlement(self):
        operation_id=self.enqueue()
        self.assertEqual(self.kernel.inspect(self.human,'synthetic-plan')['operation']['state'],'QUEUED')
        self.assertEqual(dispatch_once(self.kernel,self.psp,self.worker,operation_id),'applied')
        self.assertEqual(self.kernel.inspect(self.human,'synthetic-plan')['budget'],{'cap':2000,'reserved':0,'spent':1000})

    def test_direct_human_stop_after_agent_handoff_prevents_simulated_debit(self):
        operation_id=self.enqueue()
        self.kernel.stop(self.human,'synthetic-plan')
        self.assertEqual(dispatch_once(self.kernel,self.psp,self.worker,operation_id),'not_dispatched')
        self.assertIsNone(self.psp.query(operation_id))
        self.assertEqual(self.kernel.inspect(self.human,'synthetic-plan')['budget']['reserved'],0)

    def test_unknown_result_reopens_both_databases_and_only_queries_original(self):
        operation_id=self.enqueue()
        self.assertEqual(dispatch_once(self.kernel,self.psp,self.worker,operation_id,mode='accept_then_timeout'),'unknown')
        self.kernel.close(); self.psp.close()
        self.kernel=Kernel(self.kernel_path); self.psp=LocalPSP(self.psp_path)
        self.kernel.stop(self.human,'synthetic-plan')
        self.assertEqual(reconcile_only(self.kernel,self.psp,self.worker,operation_id),'applied')
        self.assertEqual(self.psp.db.execute('SELECT count(*) FROM payments').fetchone()[0],1)
        self.assertEqual(self.kernel.inspect(self.human,'synthetic-plan')['budget']['spent'],1000)

if __name__=='__main__': unittest.main(verbosity=2)
