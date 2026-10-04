"""Persisted application flow tests using the real scripted tool runtime.

These exercise local synthetic offers and queued LocalPSP commands, not an online
model, a merchant order, an official payment sandbox, or a deployed service.
"""
from __future__ import annotations
import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.auth import APIError
from app.task_store import TaskStore
from app.task_service import TaskService
from app.agent_worker import AgentWorker
from slice04.agent import run_task_v1
from slice04.fixtures import V1_PRODUCT


class PersistedTaskFlowTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'tasks.sqlite3'
        self.clock = [0]  # offset; adapters and store share wall time plus explicit lease advances
        self.store = TaskStore(self.path, clock=lambda: int(time.time()) + self.clock[0])
        self.service = TaskService(self.store)
        self.actor = {'actor_id': 'buyer-real-input-test', 'tenant_id': 'hk-test', 'role': 'buyer'}
        self.sequence = 0

    def fields(self, **changes):
        result = {'product': copy.deepcopy(V1_PRODUCT), 'purchase_quantity': 1,
                  'cash_cap_minor': 6000, 'destination_ref': 'HK-DEMO-KOWLOON',
                  'preference': 'lowest_cost'}
        result.update(changes)
        return result

    def mutate(self, action, task_id=None, body=None, *, key=None, actor=None):
        self.sequence += 1
        return self.service.mutation(actor or self.actor, action, task_id,
                                     key or 'request-%04d' % self.sequence, body or {})

    def create(self, *, fields=None, mode='scripted', scenario='normal', text='帮我补购同规格日用品，缺项时问我。'):
        return self.mutate('create', body={'text': text, 'fields': fields or {},
                                          'mode': mode, 'scenario': scenario})

    def process(self, task_id):
        self.assertTrue(AgentWorker(self.service).once())
        return self.service.get(self.actor, task_id)

    def proposed(self, **field_changes):
        task = self.create(fields=self.fields(**field_changes))
        result = self.process(task['task_id'])
        self.assertEqual(result['status'], 'AWAITING_APPROVAL', result['model_result'])
        return result

    def approve(self, proposal, *, key=None):
        body = {'expected_state_version': proposal['state_version'],
                'snapshot_id': proposal['proposal']['snapshot_id'],
                'challenge_id': proposal['proposal']['challenge_id']}
        return self.mutate('approve', proposal['task_id'], body, key=key), body

    def counts(self):
        tables = ('app_tasks', 'app_runs', 'app_http_idempotency', 'app_events',
                  's1_current_bindings', 's1_snapshots', 's1_challenges',
                  's1_mandates', 's1_operations', 's1_dispatch_commands', 's1_event_audit')
        with self.store.connection() as conn:
            return {name: conn.execute('SELECT count(*) FROM ' + name).fetchone()[0] for name in tables}

    def result_for(self, run):
        return run_task_v1(run['draft'], mode='scripted', scenario=run['scenario'],
                           task_id=run['task_id'], constraints_version=run['constraints_version'],
                           run_id=run['run_id'])

    def test_real_input_clarification_answer_approval_stop_share_one_snapshot(self):
        initial = self.create(text='先帮我核对两家同规格日用品，预算和数量我补充。')
        task_id = initial['task_id']
        self.assertEqual(initial['draft']['text'], '先帮我核对两家同规格日用品，预算和数量我补充。')
        clarified = self.process(task_id)
        self.assertEqual(clarified['status'], 'CLARIFYING')
        self.assertIn('purchase_quantity', clarified['missing_fields'])
        self.assertTrue(clarified['questions'])
        self.assertIsNone(clarified['proposal'])
        answered = self.mutate('answers', task_id, {
            'expected_state_version': clarified['state_version'],
            'base_constraints_version': clarified['constraints_version'], 'answers': self.fields()})
        self.assertEqual(answered['constraints_version'], 2)
        proposal = self.process(task_id)
        self.assertEqual(proposal['status'], 'AWAITING_APPROVAL', proposal['model_result'])
        snap = proposal['proposal']['snapshot']
        self.assertEqual(snap['task_id'], task_id)
        self.assertEqual(snap['constraints_version'], 2)
        self.assertEqual(len(proposal['comparison']['candidates']), 2)
        self.assertTrue(proposal['model_result']['trace'])
        queued, _ = self.approve(proposal)
        self.assertEqual(queued['operation']['state'], 'QUEUED')
        self.assertEqual(queued['operation']['snapshot_id'], snap['snapshot_id'])
        with self.store.connection() as conn:
            operation = conn.execute('SELECT * FROM s1_operations WHERE task_id=?', (task_id,)).fetchone()
            mandate = conn.execute('SELECT * FROM s1_mandates WHERE mandate_id=?', (operation['mandate_id'],)).fetchone()
            command = conn.execute('SELECT * FROM s1_dispatch_commands WHERE operation_id=?', (operation['operation_id'],)).fetchone()
            payload = json.loads(command['payload'])
            self.assertEqual(mandate['snapshot_digest'], snap['digest'])
            self.assertEqual(mandate['snapshot_id'], snap['snapshot_id'])
            for key in ('snapshot_id', 'task_id'):
                self.assertEqual(payload[key], snap[key])
            for key in ('merchant_sku', 'purchase_quantity', 'unit_price_minor', 'payee_ref', 'payee_mapping_version'):
                self.assertEqual(payload[key], snap['quote'][key])
            for key in ('cash_minor', 'goods_minor', 'fees_minor', 'discount_minor'):
                self.assertEqual(payload[key], snap['calculation'][key])
            self.assertEqual(command['state'], 'PENDING')
        stopped = self.mutate('stop', task_id)
        self.assertEqual(stopped['status'], 'STOPPED')
        self.assertEqual(stopped['operation']['operation_id'], queued['operation']['operation_id'])
        with self.store.transaction() as conn:
            self.assertIsNone(self.service.kernel(conn).claim(queued['operation']['operation_id']))
            state = self.service.kernel(conn).inspect(task_id, self.actor)
            self.assertEqual(state['budget']['reserved'], 0)
        self.assertEqual(stopped['metrics']['input_count'], 1)
        self.assertEqual(stopped['metrics']['answer_count'], 1)
        self.assertEqual(stopped['metrics']['approval_count'], 1)
        self.assertFalse(stopped['metrics']['user_value_validated'])
        self.assertEqual(stopped['capabilities']['model'], 'scripted')
        self.assertEqual(stopped['capabilities']['goods'], 'synthetic_fixture')
        self.assertEqual(stopped['capabilities']['payment'], 'local_simulator')
        self.assertFalse(stopped['capabilities']['model_online_verified'])

    def test_fastest_preference_selects_more_expensive_b(self):
        proposal = self.proposed(preference='fastest_delivery')
        snap = proposal['proposal']['snapshot']
        self.assertEqual(snap['quote']['quote_id'], 'fixture-B')
        self.assertEqual(snap['draft']['preference'], 'fastest_delivery')
        self.assertEqual(snap['calculation']['cash_minor'], 5500)
        self.assertFalse(proposal['model_result']['replanning_demonstrated'])

    def test_no_live_key_blocks_with_no_scripted_fallback_or_proposal(self):
        with patch.dict(os.environ, {'DEEPSEEK_API_KEY': ''}):
            initial = self.create(fields=self.fields(), mode='live')
            result = self.process(initial['task_id'])
        self.assertEqual(result['status'], 'BLOCKED')
        self.assertEqual(result['capabilities']['model'], 'blocked')
        self.assertFalse(result['capabilities']['model_online_verified'])
        self.assertIsNone(result['proposal'])
        self.assertIsNone(result['operation'])
        self.assertEqual(result['model_result']['model_requests'], [])
        self.assertEqual(self.counts()['s1_dispatch_commands'], 0)

    def test_unknown_merchandise_stops_instead_of_approving_sample_soap(self):
        fields = self.fields()
        fields['product']['variant'] = 'unknown-user-toothpaste'
        initial = self.create(fields=fields)
        result = self.process(initial['task_id'])
        self.assertEqual(result['status'], 'STOPPED', result['model_result'])
        self.assertIsNone(result['proposal'])
        self.assertEqual(self.counts()['s1_snapshots'], 0)
        with self.store.connection() as conn:
            events = conn.execute('SELECT event,actor_id FROM s1_event_audit WHERE task_id=?', (initial['task_id'],)).fetchall()
            self.assertIn(('rule_stop', 'rule_engine'), [(e['event'], e['actor_id']) for e in events])
            self.assertNotIn('user_stop', [e['event'] for e in events])

    def test_shipping_update_uses_revised_quote_and_cannot_reuse_old_choice(self):
        initial = self.create(fields=self.fields(), scenario='shipping_increase')
        result = self.process(initial['task_id'])
        self.assertEqual(result['status'], 'AWAITING_APPROVAL', result['model_result'])
        self.assertEqual(result['proposal']['snapshot']['quote']['quote_id'], 'fixture-B')
        self.assertTrue(result['model_result']['replanning_demonstrated'])
        self.assertIn('BUDGET_EXCEEDED', result['comparison']['candidates'][0]['blockers'])

    def test_double_confirm_replay_keeps_one_operation_and_one_command(self):
        proposal = self.proposed()
        first, body = self.approve(proposal, key='confirm-one-same-click')
        replay = self.mutate('approve', proposal['task_id'], body, key='confirm-one-same-click')
        self.assertEqual(replay, first)
        self.assertEqual(self.counts()['s1_operations'], 1)
        self.assertEqual(self.counts()['s1_mandates'], 1)
        self.assertEqual(self.counts()['s1_dispatch_commands'], 1)
        with self.assertRaises(APIError) as raised:
            self.mutate('approve', proposal['task_id'], body, key='confirm-second-new-click')
        self.assertEqual(raised.exception.code, 'STATE_VERSION_CONFLICT')
        self.assertEqual(self.counts()['s1_operations'], 1)

    def test_new_store_and_service_recover_queued_then_stopped_state(self):
        queued, _ = self.approve(self.proposed())
        task_id = queued['task_id']
        reopened_store = TaskStore(self.path, clock=lambda: int(time.time()) + self.clock[0])
        reopened = TaskService(reopened_store)
        loaded = reopened.get(self.actor, task_id)
        self.assertEqual(loaded['operation'], queued['operation'])
        self.assertEqual(loaded['proposal'], queued['proposal'])
        self.assertFalse(AgentWorker(reopened).once())
        stopped = reopened.mutation(self.actor, 'stop', task_id, 'after-restart-stop', {})
        reloaded = TaskService(TaskStore(self.path)).get(self.actor, task_id)
        self.assertEqual(reloaded['status'], 'STOPPED')
        self.assertEqual(reloaded['operation']['operation_id'], stopped['operation']['operation_id'])
        with reopened_store.connection() as conn:
            command = conn.execute('SELECT state FROM s1_dispatch_commands').fetchone()
            self.assertEqual(command['state'], 'CANCELLED')

    def test_pending_research_survives_restart_without_losing_user_text(self):
        initial = self.create(fields=self.fields(), text='这个任务重启后仍须按我输入办理。')
        reopened = TaskService(TaskStore(self.path, clock=lambda: int(time.time()) + self.clock[0]))
        self.assertTrue(AgentWorker(reopened).once())
        loaded = reopened.get(self.actor, initial['task_id'])
        self.assertEqual(loaded['status'], 'AWAITING_APPROVAL', loaded['model_result'])
        self.assertEqual(loaded['draft']['text'], '这个任务重启后仍须按我输入办理。')
        self.assertEqual(loaded['run']['attempts'], 1)

    def test_expired_model_lease_old_fence_cannot_overwrite_new_claim(self):
        initial = self.create(fields=self.fields())
        first = self.service.claim_run('worker-one', lease_seconds=1)
        old_result = self.result_for(first)
        self.clock[0] += 2
        second = self.service.claim_run('worker-two', lease_seconds=30)
        self.assertEqual(first['run_id'], second['run_id'])
        self.assertGreater(second['fence'], first['fence'])
        self.assertFalse(self.service.is_current_run(first))
        self.assertFalse(self.service.finish_run(first, old_result))
        still_waiting = self.service.get(self.actor, initial['task_id'])
        self.assertEqual(still_waiting['status'], 'PLANNING')
        self.assertIsNone(still_waiting['proposal'])
        self.assertTrue(self.service.finish_run(second, self.result_for(second)))
        result = self.service.get(self.actor, initial['task_id'])
        self.assertEqual(result['status'], 'AWAITING_APPROVAL')
        self.assertEqual(self.counts()['s1_snapshots'], 1)
        self.assertEqual(result['run']['attempts'], 2)
        self.assertIn('stale_model_result_discarded', [e['kind'] for e in result['events']])

    def test_expired_model_lease_cannot_finish_before_any_reclaim(self):
        initial = self.create(fields=self.fields())
        original = self.service.claim_run('lease-expired-worker', lease_seconds=1)
        result = self.result_for(original)
        self.clock[0] += 2
        self.assertFalse(self.service.is_current_run(original))
        self.assertFalse(self.service.finish_run(original, result))
        loaded = self.service.get(self.actor, initial['task_id'])
        self.assertEqual(loaded['status'], 'PLANNING')
        self.assertIsNone(loaded['proposal'])
        self.assertEqual(self.counts()['s1_snapshots'], 0)
        replacement = self.service.claim_run('replacement-worker', lease_seconds=30)
        self.assertGreater(replacement['fence'], original['fence'])
        self.assertTrue(self.service.finish_run(replacement, self.result_for(replacement)))
        self.assertEqual(self.service.get(self.actor, initial['task_id'])['status'], 'AWAITING_APPROVAL')

    def test_answer_cancels_running_result_and_new_constraints_win(self):
        initial = self.create(fields=self.fields())
        first = self.service.claim_run('worker-before-answer')
        old_result = self.result_for(first)
        updated = self.mutate('answers', initial['task_id'], {
            'expected_state_version': initial['state_version'],
            'base_constraints_version': initial['constraints_version'],
            'answers': {'preference': 'fastest_delivery'}})
        self.assertEqual(updated['constraints_version'], 2)
        self.assertFalse(self.service.finish_run(first, old_result))
        proposal = self.process(initial['task_id'])
        self.assertEqual(proposal['proposal']['snapshot']['quote']['quote_id'], 'fixture-B')
        self.assertEqual(proposal['proposal']['snapshot']['constraints_version'], 2)
        with self.store.connection() as conn:
            self.assertEqual(conn.execute('SELECT status FROM app_runs WHERE run_id=?', (first['run_id'],)).fetchone()[0], 'CANCELLED')
            self.assertEqual(conn.execute('SELECT count(*) FROM s1_snapshots WHERE constraints_version=1').fetchone()[0], 0)

    def test_stop_during_research_discards_late_result_without_snapshot(self):
        initial = self.create(fields=self.fields())
        run = self.service.claim_run('slow-worker')
        old_result = self.result_for(run)
        stopped = self.mutate('stop', initial['task_id'])
        self.assertEqual(stopped['status'], 'STOPPED')
        self.assertFalse(self.service.finish_run(run, old_result))
        self.assertEqual(self.counts()['s1_snapshots'], 0)
        self.assertFalse(AgentWorker(self.service).once())

    def test_changed_conditions_revoke_existing_challenge(self):
        proposal = self.proposed()
        old_challenge = proposal['proposal']['challenge_id']
        changed = self.mutate('answers', proposal['task_id'], {
            'expected_state_version': proposal['state_version'],
            'base_constraints_version': proposal['constraints_version'],
            'answers': {'purchase_quantity': 2, 'cash_cap_minor': 12000}})
        self.assertIsNone(changed['proposal'])
        with self.store.connection() as conn:
            self.assertEqual(conn.execute('SELECT state FROM s1_challenges WHERE challenge_id=?', (old_challenge,)).fetchone()[0], 'REVOKED')
        newer = self.process(proposal['task_id'])
        self.assertNotEqual(newer['proposal']['snapshot_id'], proposal['proposal']['snapshot_id'])
        self.assertEqual(newer['proposal']['snapshot']['quote']['purchase_quantity'], 2)
        self.assertEqual(newer['proposal']['snapshot']['calculation']['goods_minor'], 10000)
        self.assertEqual(newer['proposal']['snapshot']['constraints_version'], 2)
        with self.assertRaises(APIError) as raised:
            self.mutate('approve', proposal['task_id'], {
                'expected_state_version': newer['state_version'],
                'snapshot_id': proposal['proposal']['snapshot_id'], 'challenge_id': old_challenge})
        self.assertEqual(raised.exception.code, 'PROPOSAL_BINDING_CONFLICT')

    def test_approval_failure_rolls_back_app_and_kernel_in_one_transaction(self):
        proposal = self.proposed()
        before = self.counts()
        original_event = self.store.event
        def fail_after_kernel(conn, task_id, kind, details=None):
            if kind == 'user_approved':
                raise RuntimeError('injected application persistence fault')
            return original_event(conn, task_id, kind, details)
        with patch.object(self.store, 'event', side_effect=fail_after_kernel):
            with self.assertRaisesRegex(RuntimeError, 'persistence fault'):
                self.approve(proposal)
        self.assertEqual(self.counts(), before)
        loaded = self.service.get(self.actor, proposal['task_id'])
        self.assertEqual(loaded['state_version'], proposal['state_version'])
        self.assertEqual(loaded['status'], 'AWAITING_APPROVAL')
        with self.store.connection() as conn:
            state = self.service.kernel(conn).inspect(proposal['task_id'], self.actor)
            self.assertEqual(state['challenge']['state'], 'PENDING')
            self.assertEqual(state['budget']['reserved'], 0)
        recovered, _ = self.approve(proposal)
        self.assertEqual(recovered['status'], 'QUEUED')

    def test_answer_failure_rolls_back_kernel_revocation_and_task_version(self):
        proposal = self.proposed()
        before = self.counts()
        original_event = self.store.event
        def fail_after_invalidate(conn, task_id, kind, details=None):
            if kind == 'user_answer':
                raise RuntimeError('injected answer persistence fault')
            return original_event(conn, task_id, kind, details)
        with patch.object(self.store, 'event', side_effect=fail_after_invalidate):
            with self.assertRaisesRegex(RuntimeError, 'answer persistence fault'):
                self.mutate('answers', proposal['task_id'], {
                    'expected_state_version': proposal['state_version'],
                    'base_constraints_version': proposal['constraints_version'],
                    'answers': {'preference': 'easiest_returns'}})
        self.assertEqual(self.counts(), before)
        loaded = self.service.get(self.actor, proposal['task_id'])
        self.assertEqual(loaded['proposal'], proposal['proposal'])
        self.assertEqual(loaded['constraints_version'], proposal['constraints_version'])
        self.assertEqual(loaded['draft']['preference'], 'lowest_cost')
        with self.store.connection() as conn:
            state = self.service.kernel(conn).inspect(proposal['task_id'], self.actor)
            self.assertEqual(state['challenge']['state'], 'PENDING')

    def test_task_creation_failure_does_not_leave_kernel_orphan(self):
        original_event = self.store.event
        def fail_queue(conn, task_id, kind, details=None):
            if kind == 'model_run_queued':
                raise RuntimeError('injected queue persistence fault')
            return original_event(conn, task_id, kind, details)
        with patch.object(self.store, 'event', side_effect=fail_queue):
            with self.assertRaisesRegex(RuntimeError, 'queue persistence fault'):
                self.create(fields=self.fields())
        self.assertTrue(all(value == 0 for value in self.counts().values()), self.counts())

    def test_worker_cannot_change_user_budget_or_create_authority_on_error(self):
        initial = self.create(fields=self.fields())
        def bad_runner(draft, **kwargs):
            changed = copy.deepcopy(draft)
            changed['cash_cap_minor'] = 100000
            return {'status': 'blocked', 'draft': changed, 'model_status': 'test_transport'}
        self.assertTrue(AgentWorker(self.service, runner=bad_runner).once())
        result = self.service.get(self.actor, initial['task_id'])
        self.assertEqual(result['status'], 'BLOCKED')
        self.assertEqual(result['draft']['cash_cap_minor'], 6000)
        self.assertEqual(result['model_result']['reason'], 'WORKER_RESULT_REJECTED')
        self.assertEqual(self.counts()['s1_dispatch_commands'], 0)


if __name__ == '__main__':
    unittest.main(verbosity=2)
