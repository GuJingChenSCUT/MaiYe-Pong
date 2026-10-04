"""V1 local handoff/financial regressions. No live model or external provider."""
import copy
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from contextlib import contextmanager
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "reference/transaction_reference"))
from kernel import StageOneKernel, Rejected
from local_psp import LocalPSP, dispatch_stage1, reconcile_stage1
from slice04.domain import catalog_v1, freeze_v1, rank_v1
from slice04.fixtures import V1_PRODUCT, refresh_evidence_v1


class TransactionHandoffTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "application.sqlite"
        self.psp_path = Path(self.temp.name) / "synthetic-psp.sqlite"
        self.now = 2_000_000_000
        self.conn = sqlite3.connect(self.path, isolation_level=None)
        StageOneKernel.install(self.conn)
        self.kernel = StageOneKernel(self.conn, clock=lambda: self.now)
        self.psp = LocalPSP(self.psp_path)
        self.actor = {"actor_id": "buyer-one", "tenant_id": "tenant-one", "role": "buyer"}
        self.draft = {"text": "buy soap", "product": copy.deepcopy(V1_PRODUCT), "purchase_quantity": 1,
                      "cash_cap_minor": 6000, "destination_ref": "fixture-address", "preference": "lowest_cost",
                      "requires_change_of_mind_return": False, "latest_delivery_epoch": None}

    def tearDown(self):
        self.conn.close(); self.psp.close(); self.temp.cleanup()

    @contextmanager
    def tx(self):
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self.conn.rollback(); raise
        else:
            self.conn.commit()

    def propose(self, scenario="normal", quote=None):
        if quote is None:
            quotes = catalog_v1(self.draft, scenario=scenario, now=self.now)["quotes"]
            ranking = rank_v1(quotes, self.draft, now=self.now)
            quote = next(q for q in quotes if q["quote_id"] == ranking["selected_quote_id"])
        self.snapshot = freeze_v1("task-local", 1, self.draft, quote, now=self.now)
        with self.tx():
            self.challenge = self.kernel.register_snapshot(self.snapshot, self.actor)
        return self.snapshot

    def approve(self):
        with self.tx():
            operation = self.kernel.approve("task-local", self.snapshot["snapshot_id"],
                                            self.challenge["challenge_id"], self.actor)
        return operation["operation_id"]

    def view(self):
        return self.kernel.inspect("task-local", self.actor)

    def test_selected_quote_snapshot_is_the_exact_human_authorized_payment(self):
        snapshot = self.propose("shipping_increase")
        self.assertEqual(snapshot["quote"]["quote_id"], "fixture-B")
        self.assertIsNone(self.view()["operation"])
        oid = self.approve()
        self.assertEqual(self.view()["operation"]["snapshot_id"], snapshot["snapshot_id"])
        self.assertEqual(self.psp.db.execute("SELECT count(*) FROM s1_local_payments").fetchone()[0], 0)
        self.assertEqual(dispatch_stage1(self.kernel, self.psp, oid), "applied")
        view = self.view()
        self.assertEqual(view["operation"]["merchant_id"], "fixture-merchant-B")
        self.assertEqual(view["operation"]["payee_ref"], "fixture-payee-B")
        self.assertEqual(view["budget"], {"cap": 6000, "reserved": 0, "spent": 5500})
        self.assertEqual(view["operation"]["order_status"], "NOT_CREATED")
        self.assertEqual(view["operation"]["environment"], "local_simulator")

    def test_namespace_imports_share_v1_exception_type_without_path_injection(self):
        script = """
from reference.transaction_reference import kernel,local_psp
from slice04 import transaction
assert kernel.Rejected is local_psp.Rejected
assert kernel.Rejected is transaction.TransactionRejected
assert kernel.StageOneKernel is transaction.StageOneKernel
assert local_psp.LocalPSP is transaction.LocalPSP
"""
        subprocess.run([sys.executable, "-B", "-c", script], cwd=ROOT, check=True,
                       capture_output=True, text=True)

    def test_quantity_goods_fees_discount_stay_separate_in_ledger_and_receipt(self):
        self.draft.update(purchase_quantity=2, cash_cap_minor=15000)
        quote = catalog_v1(self.draft, now=self.now)["quotes"][0]
        quote["fee_lines"][0]["amount_minor"] = 500
        quote["discounts"] = [{"coupon_instance_id": "coupon-one", "subject_ref": "fixture-subject",
                               "order_scope_ref": "fixture-A-order-scope", "rule_id": "one-rule", "amount_minor": 600,
                               "eligibility": "eligible", "expires_at": self.now + 300,
                               "stackable_with": [], "exclusive_group": None}]
        refresh_evidence_v1(quote)
        self.propose(quote=quote); oid = self.approve()
        self.assertEqual(dispatch_stage1(self.kernel, self.psp, oid), "applied")
        operation = self.view()["operation"]
        expected = {"purchase_quantity": 2, "unit_price_minor": 5000, "goods_minor": 10000,
                    "fees_minor": 500, "discount_minor": 600, "cash_minor": 9900}
        for field, value in expected.items():
            self.assertEqual(operation[field], value)
            self.assertEqual(self.psp.query_v1(oid)[0]["command"][field], value)

    def test_stop_before_claim_causes_no_simulator_payment(self):
        self.propose(); oid = self.approve()
        with self.tx():
            self.kernel.stop("task-local", self.actor)
        with patch.object(self.psp, "pay_v1", side_effect=AssertionError("stop must prevent pay")):
            self.assertEqual(dispatch_stage1(self.kernel, self.psp, oid), "not_dispatched")
        self.assertEqual(self.view()["operation"]["state"], "STOPPED")
        self.assertEqual(self.view()["budget"]["reserved"], 0)

    def test_stop_after_claim_reports_inflight_and_valid_result_can_settle(self):
        self.propose(); oid = self.approve()
        with self.tx():
            command = self.kernel.claim(oid)
        with self.tx():
            stopped = self.kernel.stop("task-local", self.actor)
        self.assertTrue(stopped["stopped"])
        self.assertEqual(stopped["operation"]["state"], "DISPATCH_COMMITTED")
        with self.assertRaisesRegex(Rejected, "EXECUTION_ALREADY_STARTED"), self.tx():
            self.kernel.invalidate_task("task-local", self.actor, 2)
        event, signature = self.psp.pay_v1(command)
        with self.tx():
            self.assertEqual(self.kernel.apply_local_result(event, signature), "applied")
        self.assertEqual(self.view()["operation"]["state"], "SUCCEEDED")

    def test_committed_claim_before_send_crash_is_query_only_even_when_not_found(self):
        self.propose(); oid = self.approve()
        with self.tx():
            self.kernel.claim(oid)
        self.conn.close()
        self.conn = sqlite3.connect(self.path, isolation_level=None)
        self.kernel = StageOneKernel(self.conn, clock=lambda: self.now)
        with patch.object(self.psp, "pay_v1", side_effect=AssertionError("crash recovery must never send")):
            self.assertEqual(reconcile_stage1(self.kernel, self.psp, oid), "unknown")
            self.assertEqual(dispatch_stage1(self.kernel, self.psp, oid), "not_dispatched")
        self.assertEqual(self.view()["operation"]["state"], "UNKNOWN")
        self.assertEqual(self.view()["budget"]["reserved"], 5000)
        self.assertEqual(self.psp.db.execute("SELECT count(*) FROM s1_local_payments").fetchone()[0], 0)

    def test_accepted_unknown_is_recovered_in_new_process_by_original_query_after_stop(self):
        self.propose(); oid = self.approve()
        self.assertEqual(dispatch_stage1(self.kernel, self.psp, oid, mode="accept_then_timeout"), "unknown")
        with self.tx():
            self.kernel.stop("task-local", self.actor)
        script = """
import json,sqlite3,sys
sys.path.insert(0,sys.argv[1]+'/reference/transaction_reference')
from kernel import StageOneKernel
from local_psp import LocalPSP,reconcile_stage1
c=sqlite3.connect(sys.argv[2],isolation_level=None)
k=StageOneKernel(c); p=LocalPSP(sys.argv[3])
def forbidden(*args,**kwargs): raise AssertionError('recovery attempted payment')
p.pay_v1=forbidden
print(json.dumps({'result':reconcile_stage1(k,p,sys.argv[4])}))
p.close();c.close()
"""
        result = subprocess.run([sys.executable, "-c", script, str(ROOT), str(self.path), str(self.psp_path), oid],
                                capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(result.stdout)["result"], "applied")
        self.assertEqual(self.view()["operation"]["state"], "SUCCEEDED")
        self.assertEqual(self.view()["budget"], {"cap": 6000, "reserved": 0, "spent": 5000})
        self.assertEqual(self.psp.db.execute("SELECT count(*) FROM s1_local_payments").fetchone()[0], 1)

    def test_signed_receipt_must_match_real_payee_mapping_sku_quantity_and_amount(self):
        self.propose(); oid = self.approve()
        with self.tx():
            command = self.kernel.claim(oid)
        event, signature = self.psp.pay_v1(command)
        for field, wrong in (("payee_ref", "another-account"), ("payee_mapping_version", 2),
                              ("merchant_sku", "another-sku"), ("purchase_quantity", 2),
                              ("cash_minor", 1), ("currency", "USD")):
            bad = copy.deepcopy(event); bad["command"][field] = wrong
            with self.subTest(field=field), self.assertRaisesRegex(Rejected, "CALLBACK_BINDING_MISMATCH"), self.tx():
                self.kernel.apply_local_result(bad, self.psp.sign(bad))
        with self.assertRaisesRegex(Rejected, "LOCAL_CALLBACK_SIGNATURE_INVALID"), self.tx():
            self.kernel.apply_local_result(event, "incorrect")
        with self.tx():
            self.assertEqual(self.kernel.apply_local_result(event, signature), "applied")
            self.assertEqual(self.kernel.apply_local_result(event, signature), "already_applied")
        self.assertEqual(self.view()["budget"]["spent"], 5000)

    def test_same_provider_payment_cannot_settle_two_operations(self):
        self.propose(); first_id = self.approve()
        self.assertEqual(dispatch_stage1(self.kernel, self.psp, first_id), "applied")
        first_event = self.psp.query_v1(first_id)[0]
        second_snapshot = freeze_v1("task-two", 1, self.draft, self.snapshot["quote"], now=self.now)
        with self.tx():
            challenge = self.kernel.register_snapshot(second_snapshot, self.actor)
            second = self.kernel.approve("task-two", second_snapshot["snapshot_id"], challenge["challenge_id"], self.actor)
            command = self.kernel.claim(second["operation_id"])
        event, signature = self.psp.pay_v1(command)
        bad = copy.deepcopy(event); bad["payment_ref"] = first_event["payment_ref"]
        with self.assertRaisesRegex(Rejected, "PROVIDER_PAYMENT_ALREADY_BOUND"), self.tx():
            self.kernel.apply_local_result(bad, self.psp.sign(bad))
        bad = copy.deepcopy(event); bad["event_id"] = first_event["event_id"]
        with self.assertRaisesRegex(Rejected, "PROVIDER_EVENT_ALREADY_BOUND"), self.tx():
            self.kernel.apply_local_result(bad, self.psp.sign(bad))
        view = self.kernel.inspect("task-two", self.actor)
        self.assertEqual(view["operation"]["state"], "DISPATCH_COMMITTED")
        self.assertEqual(view["budget"], {"cap": 6000, "reserved": 5000, "spent": 0})
        with self.tx():
            self.assertEqual(self.kernel.apply_local_result(event, signature), "applied")

    def test_decline_releases_reservation_without_claiming_an_order_exists(self):
        self.propose(); oid = self.approve()
        self.assertEqual(dispatch_stage1(self.kernel, self.psp, oid, mode="decline"), "applied")
        self.assertEqual(self.view()["operation"]["state"], "FAILED")
        self.assertEqual(self.view()["operation"]["order_status"], "NOT_CREATED")
        self.assertEqual(self.view()["budget"], {"cap": 6000, "reserved": 0, "spent": 0})

    def test_bad_simulation_mode_is_rejected_before_claim(self):
        self.propose(); oid = self.approve()
        with self.assertRaisesRegex(Rejected, "SYNTHETIC_MODE_INVALID"):
            dispatch_stage1(self.kernel, self.psp, oid, mode="unsupported")
        self.assertEqual(self.view()["operation"]["state"], "QUEUED")

    def test_local_dispatch_refuses_to_run_inside_uncommitted_application_transaction(self):
        self.propose(); oid = self.approve()
        with patch.object(self.psp, "pay_v1", side_effect=AssertionError("must not touch provider")):
            with self.assertRaisesRegex(Rejected, "REQUIRES_NO_TRANSACTION"), self.tx():
                dispatch_stage1(self.kernel, self.psp, oid)
        self.assertEqual(self.view()["operation"]["state"], "QUEUED")

    def _parallel(self, actions):
        barrier = threading.Barrier(len(actions)); results = []; errors = []
        def work(action):
            connection = sqlite3.connect(self.path, isolation_level=None, timeout=10)
            kernel = StageOneKernel(connection, clock=lambda: self.now)
            try:
                barrier.wait(timeout=5)
                connection.execute("BEGIN IMMEDIATE")
                result = action(kernel)
                connection.commit(); results.append(result)
            except BaseException as exc:
                connection.rollback(); errors.append(exc)
            finally:
                connection.close()
        threads = [threading.Thread(target=work, args=(action,)) for action in actions]
        for thread in threads: thread.start()
        for thread in threads: thread.join(timeout=15)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertFalse(errors, errors)
        return results

    def test_two_connections_confirm_one_challenge_create_one_payment_command(self):
        self.propose()
        def approve(kernel):
            return kernel.approve("task-local", self.snapshot["snapshot_id"], self.challenge["challenge_id"], self.actor)["operation_id"]
        results = self._parallel([approve, approve])
        self.assertEqual(len(set(results)), 1)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM s1_dispatch_commands").fetchone()[0], 1)
        self.assertEqual(self.view()["budget"]["reserved"], 5000)

    def test_two_connections_stop_and_claim_have_one_serializable_outcome(self):
        self.propose(); oid = self.approve()
        results = self._parallel([lambda k: ("claim", k.claim(oid)),
                                  lambda k: ("stop", k.stop("task-local", self.actor))])
        claim = next(value for name, value in results if name == "claim")
        view = self.view()
        self.assertTrue(view["stopped"])
        if claim is None:
            self.assertEqual(view["operation"]["state"], "STOPPED")
            self.assertEqual(view["budget"]["reserved"], 0)
        else:
            self.assertEqual(view["operation"]["state"], "DISPATCH_COMMITTED")
            self.assertEqual(view["budget"]["reserved"], 5000)
        with self.tx():
            self.assertIsNone(self.kernel.claim(oid))
        self.assertEqual(self.psp.db.execute("SELECT count(*) FROM s1_local_payments").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
