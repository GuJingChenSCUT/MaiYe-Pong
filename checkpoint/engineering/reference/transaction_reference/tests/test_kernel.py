import dataclasses
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
import copy
from contextlib import contextmanager

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from kernel import Kernel, Rejected, TestPrincipal, digest
from local_psp import LocalPSP, dispatch_once, reconcile_only
from runtime_adapter import ExecutionWorkflowAdapter


class KernelTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)/"kernel.sqlite"
        self.psp_path = Path(self.temp.name)/"psp.sqlite"
        self.now = 1000
        self.kernel = Kernel(self.path, clock=lambda: self.now)
        self.psp = LocalPSP(self.psp_path)
        base = dict(tenant_id="tenant-a", owner_id="buyer-a", plan_ids=frozenset({"p1", "p2"}), expires_at=9000)
        self.setup = TestPrincipal("fixture", kind="service", role="fixture", scopes=frozenset({"fixture_setup"}), **base)
        self.human = TestPrincipal("buyer-a", kind="human", role="buyer", scopes=frozenset({"approve", "stop", "inspect", "approved_settlement_refund"}), **base)
        self.agent = TestPrincipal("agent-a", kind="agent", role="buyer_planner", scopes=frozenset({"request_execution", "inspect"}), **base)
        self.worker = TestPrincipal("worker-a", kind="service", role="payment_worker", scopes=frozenset({"dispatch", "reconcile", "inspect"}), **base)
        self.kernel.seed_budget(self.setup, "HKD", 1500)
        self.make_plan("p1", 1000)

    def tearDown(self):
        self.kernel.close(); self.psp.close(); self.temp.cleanup()

    def make_plan(self, pid, amount, *, fees=0, approve=True):
        fingerprint = self.kernel.seed_plan(self.setup, plan_id=pid, merchant="synthetic-merchant",
                                            currency="HKD", goods=amount, fees=fees, quote_expires=4000)
        if approve:
            self.kernel.approve(self.human, pid, expected_digest=fingerprint, expected_version=1,
                                merchant="synthetic-merchant", currency="HKD", cash_cap=amount+fees,
                                expires=3000)
        return fingerprint

    def execute(self, pid="p1", key="logical-p1"):
        return self.kernel.request_execution(self.agent, pid, command_key=key, authorization_version=1)["data"]["operation_id"]

    def state(self, pid="p1"):
        return self.kernel.inspect(self.human, pid)

    def count(self, table):
        return self.kernel.db.execute("SELECT count(*) FROM "+table).fetchone()[0]

    def test_success_reserves_then_settles_exact_cash(self):
        oid = self.execute()
        self.assertEqual(self.state()["budget"], {"cap": 1500, "reserved": 1000, "spent": 0})
        self.assertEqual(dispatch_once(self.kernel, self.psp, self.worker, oid, synthetic_reward_units=7), "applied")
        self.assertEqual(self.state()["budget"], {"cap": 1500, "reserved": 0, "spent": 1000})
        self.assertEqual(self.state()["operation"]["reward_state"], "posted")
        self.assertEqual(self.count("audit"), self.count("outbox"))

    def test_human_approval_cannot_be_given_by_agent(self):
        fingerprint = self.make_plan("p2", 300, approve=False)
        agent = dataclasses.replace(self.agent, scopes=self.agent.scopes | {"approve"})
        with self.assertRaisesRegex(Rejected, "FORBIDDEN"):
            self.kernel.approve(agent, "p2", expected_digest=fingerprint, expected_version=1,
                                merchant="synthetic-merchant", currency="HKD", cash_cap=300, expires=3000)

    def test_approval_binds_merchant_digest_version_currency(self):
        fingerprint = self.make_plan("p2", 300, approve=False)
        good = dict(expected_digest=fingerprint, expected_version=1, merchant="synthetic-merchant", currency="HKD", cash_cap=300, expires=3000)
        for field, wrong in [("expected_digest", "forged"), ("expected_version", 2), ("merchant", "other"), ("currency", "USD")]:
            with self.subTest(field=field), self.assertRaisesRegex(Rejected, "APPROVAL_BINDING_MISMATCH"):
                self.kernel.approve(self.human, "p2", **(good | {field: wrong}))

    def test_immutable_plan_database_trigger(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.kernel.db.execute("UPDATE plans SET amount=2 WHERE id='p1'")

    def test_total_cash_includes_fixture_additional_amount(self):
        fingerprint = self.make_plan("p2", 400, fees=100, approve=False)
        with self.assertRaisesRegex(Rejected, "BUDGET_EXCEEDED"):
            self.kernel.approve(self.human, "p2", expected_digest=fingerprint, expected_version=1,
                                merchant="synthetic-merchant", currency="HKD", cash_cap=400, expires=3000)

    def test_no_approval_no_execution(self):
        self.make_plan("p2", 300, approve=False)
        with self.assertRaisesRegex(Rejected, "HUMAN_APPROVAL_REQUIRED"):
            self.execute("p2", "logical-p2")

    def test_cross_tenant_and_out_of_scope_denied(self):
        for bad in [dataclasses.replace(self.agent, tenant_id="other"), dataclasses.replace(self.agent, plan_ids=frozenset())]:
            with self.assertRaisesRegex(Rejected, "RESOURCE_FORBIDDEN"):
                self.kernel.request_execution(bad, "p1", command_key="x", authorization_version=1)

    def test_wrong_role_and_human_cannot_call_agent_port(self):
        for bad in [dataclasses.replace(self.agent, role="seller_assistant"), dataclasses.replace(self.human, scopes=frozenset({"request_execution"}))]:
            with self.assertRaisesRegex(Rejected, "FORBIDDEN"):
                self.kernel.request_execution(bad, "p1", command_key="x", authorization_version=1)

    def test_same_command_key_idempotent(self):
        oid = self.execute()
        self.assertEqual(self.execute(), oid)
        self.assertEqual(self.count("operations"), 1)
        self.assertEqual(self.state()["budget"]["reserved"], 1000)

    def test_same_key_different_plan_rejected(self):
        self.execute(); self.make_plan("p2", 300)
        with self.assertRaisesRegex(Rejected, "IDEMPOTENCY_CONFLICT"):
            self.execute("p2", "logical-p1")

    def test_new_key_same_plan_rejected(self):
        self.execute()
        with self.assertRaisesRegex(Rejected, "USE_ORIGINAL_OPERATION_KEY"):
            self.execute(key="new-key")

    def test_expired_mandate_and_stale_authorization_version(self):
        with self.assertRaisesRegex(Rejected, "STALE_VERSION"):
            self.kernel.request_execution(self.agent, "p1", command_key="x", authorization_version=0)
        self.now = 3001
        with self.assertRaisesRegex(Rejected, "AUTH_EXPIRED"):
            self.execute()
        self.assertEqual(self.count("operations"), 0)

    def test_stop_before_dispatch_releases_budget(self):
        oid = self.execute()
        result = self.kernel.stop(self.human, "p1", stale_view_version=0)
        self.assertEqual(result["stop_state"], "stopped_before_dispatch")
        self.assertEqual(dispatch_once(self.kernel, self.psp, self.worker, oid), "not_dispatched")
        self.assertEqual(self.state()["budget"]["reserved"], 0)
        self.assertIsNone(self.psp.query(oid))

    def test_stop_before_enqueue_prevents_first_debit(self):
        self.kernel.stop(self.human, "p1")
        with self.assertRaisesRegex(Rejected, "REVOKED"):
            self.execute()

    def test_after_claim_stop_reports_inflight_payment_can_complete(self):
        oid = self.execute()
        command = self.kernel.claim_dispatch(self.worker, oid)
        self.assertEqual(self.kernel.stop(self.human, "p1")["stop_state"], "dispatch_already_started")
        event, signature = self.psp.pay(command)
        self.assertEqual(self.kernel.apply_payment_event(self.worker, event, signature), "applied")
        self.assertEqual(self.state()["operation"]["state"], "SUCCEEDED")
        self.assertIsNone(self.kernel.claim_dispatch(self.worker, oid))

    def test_expiry_between_enqueue_and_dispatch_blocks(self):
        oid = self.execute(); self.now = 3001
        self.assertEqual(dispatch_once(self.kernel, self.psp, self.worker, oid), "not_dispatched")
        self.assertEqual(self.state()["budget"]["reserved"], 0)

    def test_unknown_keeps_reservation_no_new_key_and_reconcile_after_revoke(self):
        oid = self.execute()
        self.assertEqual(dispatch_once(self.kernel, self.psp, self.worker, oid, mode="accept_then_timeout"), "unknown")
        self.assertEqual(self.state()["budget"]["reserved"], 1000)
        self.assertEqual(self.kernel.stop(self.human, "p1")["stop_state"], "payment_state_unknown")
        with self.assertRaisesRegex(Rejected, "USE_ORIGINAL_OPERATION_KEY"):
            self.execute(key="retry-new-key")
        self.assertEqual(reconcile_only(self.kernel, self.psp, self.worker, oid), "applied")
        self.assertEqual(self.state()["budget"]["spent"], 1000)

    def test_missing_provider_record_does_not_release_or_resubmit(self):
        oid = self.execute()
        self.kernel.claim_dispatch(self.worker, oid)
        self.assertEqual(reconcile_only(self.kernel, self.psp, self.worker, oid), "unknown")
        self.assertEqual(self.state()["budget"]["reserved"], 1000)
        self.assertIsNone(self.kernel.claim_dispatch(self.worker, oid))
        self.assertEqual(self.psp.db.execute("SELECT count(*) FROM payments").fetchone()[0], 0)

    def test_restart_new_process_queries_original_payment(self):
        oid = self.execute()
        dispatch_once(self.kernel, self.psp, self.worker, oid, mode="accept_then_timeout")
        code = """
import sys
from kernel import Kernel, TestPrincipal
from local_psp import LocalPSP, reconcile_only
k=Kernel(sys.argv[1],clock=lambda:1000); p=LocalPSP(sys.argv[2])
w=TestPrincipal('worker-a','tenant-a','buyer-a','service','payment_worker',frozenset({'dispatch','reconcile','inspect'}),frozenset({'p1'}),9000)
assert reconcile_only(k,p,w,sys.argv[3]) == 'applied'
assert k.inspect(w,'p1')['operation']['state']=='SUCCEEDED'
assert p.db.execute('SELECT count(*) FROM payments').fetchone()[0]==1
k.close(); p.close()
"""
        child = subprocess.run([sys.executable, "-c", code, str(self.path), str(self.psp_path), oid], cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(child.returncode, 0, child.stderr)
        self.assertEqual(self.state()["budget"]["spent"], 1000)

    def test_two_connections_race_budget_only_one_succeeds(self):
        self.make_plan("p2", 1000)
        barrier = threading.Barrier(2); outcomes = []
        def contender(pid):
            k = Kernel(self.path, clock=lambda: 1000)
            try:
                barrier.wait()
                k.request_execution(self.agent, pid, command_key="cmd-"+pid, authorization_version=1)
                outcomes.append("accepted")
            except Rejected as error:
                outcomes.append(str(error))
            finally:
                k.close()
        threads = [threading.Thread(target=contender, args=(pid,)) for pid in ("p1", "p2")]
        for thread in threads: thread.start()
        for thread in threads: thread.join(timeout=10)
        self.assertTrue(all(not t.is_alive() for t in threads))
        self.assertCountEqual(outcomes, ["accepted", "BUDGET_EXCEEDED"])
        self.assertEqual(self.state()["budget"]["reserved"], 1000)

    def test_atomic_rollback_when_outbox_write_fails(self):
        self.kernel.db.execute("CREATE TRIGGER fail_outbox BEFORE INSERT ON outbox WHEN NEW.event='execution_queued' BEGIN SELECT RAISE(ABORT,'simulated_disk_failure'); END")
        audits = self.count("audit")
        with self.assertRaises(sqlite3.IntegrityError):
            self.execute()
        self.assertEqual(self.count("operations"), 0)
        self.assertEqual(self.state()["budget"]["reserved"], 0)
        self.assertEqual(self.count("audit"), audits)

    def test_two_connections_stop_and_dispatch_are_serialized(self):
        oid = self.execute(); barrier = threading.Barrier(2); results = {}; errors = []
        def compete(action):
            k = Kernel(self.path, clock=lambda: 1000)
            try:
                barrier.wait()
                if action == "stop":
                    results[action] = k.stop(self.human, "p1", stale_view_version=0)
                else:
                    results[action] = k.claim_dispatch(self.worker, oid)
            except BaseException as error:
                errors.append(repr(error))
            finally:
                k.close()
        threads = [threading.Thread(target=compete, args=(action,)) for action in ("stop", "claim")]
        for thread in threads: thread.start()
        for thread in threads: thread.join(timeout=10)
        self.assertTrue(all(not t.is_alive() for t in threads)); self.assertEqual(errors, [])
        if results["claim"] is None:
            self.assertEqual(results["stop"]["stop_state"], "stopped_before_dispatch")
            self.assertEqual(self.state()["operation"]["state"], "STOPPED")
            self.assertEqual(self.state()["budget"]["reserved"], 0)
        else:
            self.assertEqual(results["stop"]["stop_state"], "dispatch_already_started")
            self.assertEqual(self.state()["operation"]["state"], "DISPATCH_COMMITTED")
            self.assertEqual(self.state()["budget"]["reserved"], 1000)
        self.assertIsNone(self.kernel.claim_dispatch(self.worker, oid))

    def test_duplicate_callback_and_new_id_duplicate_fact_no_double_spend(self):
        oid = self.execute(); dispatch_once(self.kernel, self.psp, self.worker, oid)
        event, signature = self.psp.query(oid)
        self.assertEqual(self.kernel.apply_payment_event(self.worker, event, signature), "duplicate")
        event["event_id"] = "different-delivery-id"
        self.assertEqual(self.kernel.apply_payment_event(self.worker, event, self.psp.sign(event)), "duplicate_fact")
        self.assertEqual(self.state()["budget"]["spent"], 1000)

    def test_conflicting_callback_alerts_and_cannot_change_money(self):
        oid = self.execute(); dispatch_once(self.kernel, self.psp, self.worker, oid)
        event, _ = self.psp.query(oid)
        event["status"] = "FAILED"
        self.assertEqual(self.kernel.apply_payment_event(self.worker, event, self.psp.sign(event)), "conflict")
        event["event_id"] = "second-conflict"
        self.assertEqual(self.kernel.apply_payment_event(self.worker, event, self.psp.sign(event)), "conflict")
        self.assertEqual(self.state()["operation"]["state"], "SUCCEEDED")
        self.assertEqual(self.state()["budget"]["spent"], 1000)
        self.assertEqual(self.state()["alerts"], 2)

    def test_wrong_callback_signature_amount_and_payee_rejected(self):
        oid = self.execute(); command = self.kernel.claim_dispatch(self.worker, oid)
        event, signature = self.psp.pay(command)
        with self.assertRaisesRegex(Rejected, "CALLBACK_SIGNATURE_INVALID"):
            self.kernel.apply_payment_event(self.worker, event, "fake")
        for field, value in [("amount", 999), ("merchant", "wrong-payee"), ("currency", "USD")]:
            wrong = event | {field: value, "event_id": "bad-"+field}
            self.assertEqual(self.kernel.apply_payment_event(self.worker, wrong, self.psp.sign(wrong)), "conflict")
        self.assertEqual(self.state()["budget"]["reserved"], 1000)

    def test_decline_releases_budget(self):
        oid = self.execute(); dispatch_once(self.kernel, self.psp, self.worker, oid, mode="decline")
        self.assertEqual(self.state()["budget"], {"cap": 1500, "reserved": 0, "spent": 0})
        self.assertEqual(self.state()["operation"]["state"], "FAILED")

    def test_refund_original_payment_and_reward_reversal_idempotent(self):
        oid = self.execute(); dispatch_once(self.kernel, self.psp, self.worker, oid, synthetic_reward_units=7)
        refund = self.kernel.request_refund(self.human, oid)
        result = self.psp.refund_full(refund_id=refund["id"], original_ref=refund["original_ref"], amount=1000)
        args = dict(confirmed_original_ref=result["original_ref"], confirmed_amount=result["amount"])
        self.assertEqual(self.kernel.finish_refund(self.worker, refund["id"], **args), "applied")
        self.assertEqual(self.kernel.finish_refund(self.worker, refund["id"], **args), "duplicate")
        self.assertEqual(self.state()["budget"]["spent"], 0)
        self.assertEqual(self.state()["operation"]["reward_state"], "reversed")
        self.assertEqual(self.state()["operation"]["reward_units"], 7)

    def test_refund_cannot_change_original_ref_or_amount(self):
        oid = self.execute(); dispatch_once(self.kernel, self.psp, self.worker, oid)
        refund = self.kernel.request_refund(self.human, oid)
        with self.assertRaisesRegex(Rejected, "REFUND_ORIGINAL_PAYMENT_MISMATCH"):
            self.kernel.finish_refund(self.worker, refund["id"], confirmed_original_ref="attacker", confirmed_amount=1000)
        with self.assertRaisesRegex(Rejected, "FULL_ORIGINAL_PAYMENT_REQUIRED"):
            self.psp.refund_full(refund_id=refund["id"], original_ref=refund["original_ref"], amount=500)
        with self.assertRaisesRegex(Rejected, "FORBIDDEN"):
            self.kernel.request_refund(self.agent, oid)

    def test_money_rejects_float_and_boolean(self):
        for value in [1.5, True, -1]:
            with self.assertRaisesRegex(Rejected, "INVALID_INTEGER"):
                self.kernel.seed_budget(self.setup, "USD", value)

    def test_principal_expiry_rejects_nonfinite_and_wrong_types(self):
        for value in [float("nan"), float("inf"), float("-inf"), True, None, "9000", 9000.0, 10**400]:
            with self.subTest(value=repr(value)), self.assertRaisesRegex(Rejected, "IDENTITY_EXPIRY_INVALID"):
                self.kernel.request_execution(dataclasses.replace(self.agent, expires_at=value),
                                              "p1", command_key="bad-expiry", authorization_version=1)
        self.assertEqual(self.count("operations"), 0)
        self.assertEqual(self.state()["budget"]["reserved"], 0)

    def test_adapter_context_expiry_rejects_nonfinite_and_wrong_types(self):
        from types import SimpleNamespace
        adapter = ExecutionWorkflowAdapter(self.kernel, lambda _: self.agent)
        for value in [float("nan"), float("inf"), float("-inf"), True, None, "9000", 10**400]:
            ctx = SimpleNamespace(actor_id="agent-a", tenant_id="tenant-a", role="buyer_planner",
                                  operation_key="bad-expiry", authorization_version=1, expires_at_epoch=value)
            with self.subTest(value=repr(value)), self.assertRaisesRegex(Rejected, "TRUSTED_CONTEXT_EXPIRY_INVALID"):
                adapter.enqueue_once(context=ctx, tool_name="request_execution", arguments={"plan_id": "p1"},
                                     operation_key="bad-expiry")
        self.assertEqual(self.count("operations"), 0)
        self.assertEqual(self.state()["budget"]["reserved"], 0)

    def test_adapter_accepts_finite_float_epoch_and_rejects_expired(self):
        from types import SimpleNamespace
        adapter = ExecutionWorkflowAdapter(self.kernel, lambda _: self.agent)
        ctx = SimpleNamespace(actor_id="agent-a", tenant_id="tenant-a", role="buyer_planner",
                              operation_key="logical-p1", authorization_version=1, expires_at_epoch=999.5)
        with self.assertRaisesRegex(Rejected, "TRUSTED_CONTEXT_MISMATCH"):
            adapter.enqueue_once(context=ctx, tool_name="request_execution", arguments={"plan_id": "p1"},
                                 operation_key="logical-p1")
        ctx.expires_at_epoch = 9000.5
        result = adapter.enqueue_once(context=ctx, tool_name="request_execution", arguments={"plan_id": "p1"},
                                      operation_key="logical-p1")
        self.assertEqual(result["data"]["state"], "queued")

    def test_minimal_runtime_adapter_uses_only_plan_id(self):
        @dataclasses.dataclass
        class Context:
            actor_id: str = "agent-a"
            tenant_id: str = "tenant-a"
            role: str = "buyer_planner"
            operation_key: str = "logical-p1"
            authorization_version: int = 1
            expires_at_epoch: int = 9000
        adapter = ExecutionWorkflowAdapter(self.kernel, lambda _: self.agent)
        args = dict(context=Context(), tool_name="request_execution", arguments={"plan_id": "p1"}, operation_key="logical-p1")
        receipt = adapter.enqueue_once(**args)
        self.assertEqual(receipt["data"]["state"], "queued")
        with self.assertRaisesRegex(Rejected, "UNIMPLEMENTED_OR_INVALID_TOOL"):
            adapter.enqueue_once(**(args | {"arguments": {"plan_id": "p1", "amount": 1}}))
        with self.assertRaisesRegex(Rejected, "UNTRUSTED_OPERATION_KEY"):
            adapter.enqueue_once(**(args | {"operation_key": "model-call-id"}))


class StageOneKernelTests(unittest.TestCase):
    """New V1 behavior, not a relabelling of the legacy fixture test count."""

    def setUp(self):
        sys.path.insert(0, str(ROOT.parents[1]))
        from kernel import StageOneKernel
        from slice04.domain import catalog_v1, freeze_v1
        from slice04.fixtures import V1_PRODUCT
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "stage1.sqlite"
        self.now = 2_000_000_000
        self.conn = sqlite3.connect(self.path, isolation_level=None)
        StageOneKernel.install(self.conn)
        self.k = StageOneKernel(self.conn, clock=lambda: self.now)
        self.actor = {"tenant_id": "tenant-one", "actor_id": "buyer-one", "role": "buyer"}
        self.draft = {"text": "two soap packages", "product": copy.deepcopy(V1_PRODUCT),
                      "purchase_quantity": 2, "cash_cap_minor": 15000, "destination_ref": "fixture-address",
                      "preference": "lowest_cost", "requires_change_of_mind_return": False,
                      "latest_delivery_epoch": None}
        self.quote = catalog_v1(self.draft, now=self.now)["quotes"][0]
        self.snapshot = freeze_v1("task-one", 1, self.draft, self.quote, now=self.now)
        with self.tx():
            self.challenge = self.k.register_snapshot(self.snapshot, self.actor)

    def tearDown(self):
        self.conn.close(); self.temp.cleanup()

    @contextmanager
    def tx(self):
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self.conn.rollback(); raise
        else:
            self.conn.commit()

    def approve(self):
        with self.tx():
            return self.k.approve("task-one", self.snapshot["snapshot_id"], self.challenge["challenge_id"], self.actor)

    def test_v1_requires_shared_outer_transaction_and_install_never_commits_it(self):
        from kernel import StageOneKernel
        with self.assertRaisesRegex(Rejected, "CALLER_TRANSACTION_REQUIRED"):
            self.k.stop("task-one", self.actor)
        with self.tx():
            with self.assertRaisesRegex(Rejected, "INSTALL_REQUIRES_NO_TRANSACTION"):
                StageOneKernel.install(self.conn)
            self.assertTrue(self.conn.in_transaction)

    def test_v1_approval_is_explicit_idempotent_and_preserves_amount_components(self):
        self.assertIsNone(self.k.inspect("task-one", self.actor)["operation"])
        first = self.approve(); second = self.approve()
        self.assertEqual(first["operation_id"], second["operation_id"])
        self.assertEqual(first["state"], "QUEUED")
        self.assertEqual((first["purchase_quantity"], first["unit_price_minor"], first["goods_minor"]), (2, 5000, 10000))
        self.assertEqual((first["fees_minor"], first["discount_minor"], first["cash_minor"]), (0, 0, 10000))
        self.assertEqual(first["order_status"], "NOT_CREATED")
        for table in ("s1_mandates", "s1_operations", "s1_dispatch_commands"):
            self.assertEqual(self.conn.execute("SELECT count(*) FROM " + table).fetchone()[0], 1)
        self.assertNotIn("provider_idempotency_key", json.dumps(self.k.inspect("task-one", self.actor)))

    def test_v1_outer_rollback_undoes_challenge_budget_command_and_audit(self):
        audit_count = self.conn.execute("SELECT count(*) FROM s1_event_audit").fetchone()[0]
        with self.assertRaisesRegex(RuntimeError, "abort application transaction"):
            with self.tx():
                self.k.approve("task-one", self.snapshot["snapshot_id"], self.challenge["challenge_id"], self.actor)
                raise RuntimeError("abort application transaction")
        view = self.k.inspect("task-one", self.actor)
        self.assertIsNone(view["operation"])
        self.assertEqual(view["challenge"]["state"], "PENDING")
        self.assertEqual(view["budget"]["reserved"], 0)
        for table in ("s1_mandates", "s1_operations", "s1_dispatch_commands"):
            self.assertEqual(self.conn.execute("SELECT count(*) FROM " + table).fetchone()[0], 0)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM s1_event_audit").fetchone()[0], audit_count)
        self.approve()

    def test_v1_claim_rolls_back_with_application_and_does_not_resend_after_commit(self):
        operation = self.approve()
        with self.assertRaises(RuntimeError):
            with self.tx():
                self.assertIsNotNone(self.k.claim(operation["operation_id"]))
                raise RuntimeError("abort claim")
        self.assertEqual(self.k.inspect("task-one", self.actor)["operation"]["state"], "QUEUED")
        with self.tx():
            self.assertIsNotNone(self.k.claim(operation["operation_id"]))
        with self.tx():
            self.assertIsNone(self.k.claim(operation["operation_id"]))
        self.assertEqual(self.k.recover(operation["operation_id"])["action"], "query_original_only")

    def test_v1_wrong_actor_tenant_and_roles_cannot_approve_replay_inspect_or_stop(self):
        self.approve()
        for changes in ({"actor_id": "another"}, {"tenant_id": "another"}, {"role": "merchant"}, {"role": "operator"}):
            actor = self.actor | changes
            with self.subTest(changes=changes):
                with self.assertRaises(Rejected):
                    self.k.inspect("task-one", actor)
                with self.assertRaises(Rejected), self.tx():
                    self.k.approve("task-one", self.snapshot["snapshot_id"], self.challenge["challenge_id"], actor)
                with self.assertRaises(Rejected), self.tx():
                    self.k.stop("task-one", actor)

    def test_v1_confirmation_does_not_accept_another_snapshot(self):
        with self.assertRaisesRegex(Rejected, "CHALLENGE_BINDING_MISMATCH"), self.tx():
            self.k.approve("task-one", "snapshot-from-another-task", self.challenge["challenge_id"], self.actor)

    def test_v1_every_current_binding_change_blocks_queued_dispatch(self):
        for field, bad in (("constraints_version", 2), ("fact_version", 2), ("quote_id", "other-quote"),
                           ("quote_digest", "changed"), ("payee_ref", "other-account"), ("payee_mapping_version", 2)):
            with self.subTest(field=field):
                self.conn.execute("BEGIN IMMEDIATE")
                try:
                    op = self.k.approve("task-one", self.snapshot["snapshot_id"], self.challenge["challenge_id"], self.actor)
                    self.conn.execute("UPDATE s1_current_bindings SET " + field + "=? WHERE task_id='task-one'", (bad,))
                    self.assertIsNone(self.k.claim(op["operation_id"]))
                    view = self.k.inspect("task-one", self.actor)
                    self.assertEqual(view["operation"]["state"], "STOPPED")
                    self.assertEqual(view["budget"]["reserved"], 0)
                finally:
                    self.conn.rollback()

    def test_v1_changed_bindings_before_confirmation_reject_without_operation(self):
        with self.tx():
            self.k.update_current_bindings("task-one", self.actor, constraints_version=1, fact_version=2,
                                          payee_ref=self.quote["payee_ref"], payee_mapping_version=1,
                                          quote_id=self.quote["quote_id"], quote_digest=digest(self.quote))
        with self.assertRaisesRegex(Rejected, "CURRENT_BINDING_CHANGED"):
            self.approve()
        self.assertIsNone(self.k.inspect("task-one", self.actor)["operation"])

    def test_v1_answer_invalidates_old_pending_or_queued_authority_and_fences_late_run(self):
        self.approve()
        with self.tx():
            self.k.invalidate_task("task-one", self.actor, 2)
        self.assertEqual(self.conn.execute("SELECT state FROM s1_operations").fetchone()[0], "STOPPED")
        self.assertEqual(self.conn.execute("SELECT state FROM s1_dispatch_commands").fetchone()[0], "CANCELLED")
        self.assertEqual(self.k.inspect("task-one", self.actor)["budget"]["reserved"], 0)
        with self.assertRaisesRegex(Rejected, "CONSTRAINTS_VERSION_MISMATCH"), self.tx():
            self.k.register_snapshot(self.snapshot, self.actor)
        from slice04.domain import freeze_v1
        newer = freeze_v1("task-one", 2, self.draft, self.quote, now=self.now)
        with self.tx():
            self.k.register_snapshot(newer, self.actor)

    def test_v1_user_stop_without_operation_persists_until_explicit_new_constraint_version(self):
        with self.tx():
            self.k.stop("task-one", self.actor)
        with self.assertRaisesRegex(Rejected, "TASK_STOPPED"), self.tx():
            self.k.register_snapshot(self.snapshot, self.actor)
        with self.tx():
            self.k.invalidate_task("task-one", self.actor, 2)
        self.assertFalse(self.k.inspect("task-one", self.actor)["stopped"])

    def test_v1_expired_confirmation_and_expired_claim(self):
        self.now += 301
        with self.assertRaisesRegex(Rejected, "CHALLENGE_EXPIRED_OR_REVOKED"):
            self.approve()
        self.now -= 301
        operation = self.approve()
        self.now += 301
        with self.tx():
            self.assertIsNone(self.k.claim(operation["operation_id"]))
        self.assertEqual(self.k.inspect("task-one", self.actor)["operation"]["state"], "STOPPED")

    def test_v1_initial_zero_fact_binding_can_be_stopped_before_proposal(self):
        with self.tx():
            self.k.update_current_bindings("empty-task", self.actor, constraints_version=1, fact_version=0)
            result = self.k.stop("empty-task", self.actor)
        self.assertTrue(result["stopped"])
        self.assertIsNone(result["operation"])

    def test_v1_rule_stop_is_not_recorded_as_user_revocation(self):
        self.approve()
        with self.tx():
            result = self.k.stop("task-one", self.actor, origin="rule")
        self.assertEqual(result["operation"]["stop_reason"], "RULE_STOP")
        self.assertEqual(result["events"][-1]["event"], "rule_stop")
        self.assertEqual(result["events"][-1]["details"]["origin"], "rule")
        with self.assertRaisesRegex(Rejected, "INVALID_STOP_ORIGIN"), self.tx():
            self.k.stop("task-one", self.actor, origin="model-approved-user")

    def test_v1_corrupt_calculation_quantity_or_duplicate_coupon_cannot_be_registered(self):
        variants = []
        bad = copy.deepcopy(self.snapshot); bad["calculation"]["cash_minor"] -= 1; variants.append(bad)
        for quantity in (True, 2.0, 0):
            bad = copy.deepcopy(self.snapshot); bad["quote"]["purchase_quantity"] = quantity; variants.append(bad)
        bad = copy.deepcopy(self.snapshot); bad["quote"]["line_subtotal_minor"] -= 1; variants.append(bad)
        bad = copy.deepcopy(self.snapshot)
        coupon = {"coupon_instance_id": "coupon-one", "subject_ref": "fixture-subject", "order_scope_ref": "fixture-A-order-scope",
                  "rule_id": "rule", "amount_minor": 100, "eligibility": "eligible", "expires_at": self.now + 100,
                  "stackable_with": ["rule"], "exclusive_group": None}
        bad["quote"]["discounts"] = [coupon, copy.deepcopy(coupon)]; variants.append(bad)
        for index, bad in enumerate(variants):
            # This is not merely a digest-tamper test: recompute an internally
            # consistent digest, then require semantic validation to reject it.
            body = {k: v for k, v in bad.items() if k not in ("snapshot_id", "digest")}
            bad["digest"] = digest(body); bad["snapshot_id"] = "snap_" + bad["digest"]
            with self.subTest(index=index), self.assertRaises(Rejected), self.tx():
                self.k.register_snapshot(bad, self.actor)


if __name__ == "__main__":
    unittest.main()
