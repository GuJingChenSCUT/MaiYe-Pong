"""Independent on-disk SYNTHETIC PSP simulator. No network and no real money.

Rewards are arbitrary fixture ledger units, not an earning rate or currency value.
The signature format is for local tests, not a real provider callback protocol.
"""
from __future__ import annotations
import hashlib
import hmac
import json
import sqlite3
if __package__:
    from .kernel import Rejected, canonical, digest, integer
else:  # The unchanged legacy CLI/tests load these files as top-level modules.
    from kernel import Rejected, canonical, digest, integer


class LocalPSP:
    def __init__(self, path, *, callback_key=b"synthetic-test-key-not-production"):
        self.key = callback_key
        self.db = sqlite3.connect(str(path), timeout=10, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS payments(
          operation_id TEXT PRIMARY KEY, command_digest TEXT NOT NULL,
          payment_ref TEXT UNIQUE NOT NULL, event TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS refunds(
          refund_id TEXT PRIMARY KEY, original_ref TEXT UNIQUE NOT NULL,
          amount INTEGER NOT NULL, result TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS s1_local_payments(
          operation_id TEXT PRIMARY KEY, idempotency_key TEXT UNIQUE NOT NULL,
          command_digest TEXT NOT NULL, payment_ref TEXT UNIQUE NOT NULL,
          event TEXT NOT NULL);
        """)

    def close(self):
        self.db.close()

    def sign(self, event):
        return hmac.new(self.key, canonical(event).encode(), hashlib.sha256).hexdigest()

    def pay(self, command, *, mode="success", synthetic_reward_units=0):
        if set(command) != {"operation_id", "amount", "currency", "merchant"}:
            raise Rejected("SYNTHETIC_COMMAND_SCHEMA")
        if mode not in {"success", "decline", "accept_then_timeout"}:
            raise Rejected("SYNTHETIC_MODE_INVALID")
        integer(command["amount"], 1); integer(synthetic_reward_units)
        key = command["operation_id"]; fingerprint = digest(command)
        self.db.execute("BEGIN IMMEDIATE")
        try:
            prior = self.db.execute("SELECT * FROM payments WHERE operation_id=?", (key,)).fetchone()
            if prior:
                if prior["command_digest"] != fingerprint:
                    raise Rejected("SYNTHETIC_IDEMPOTENCY_CONFLICT")
                event = json.loads(prior["event"])
            else:
                event = {"event_id": "synthetic-event-"+key, "operation_id": key,
                         "status": "FAILED" if mode == "decline" else "SUCCEEDED",
                         "payment_ref": "synthetic-payment-"+key,
                         "amount": command["amount"], "currency": command["currency"],
                         "merchant": command["merchant"],
                         "reward_units": 0 if mode == "decline" else synthetic_reward_units}
                self.db.execute("INSERT INTO payments VALUES(?,?,?,?)",
                                (key, fingerprint, event["payment_ref"], canonical(event)))
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise
        if mode == "accept_then_timeout":
            raise TimeoutError("SYNTHETIC_ACCEPTED_RESPONSE_LOST")
        return event, self.sign(event)

    def query(self, operation_id):
        row = self.db.execute("SELECT event FROM payments WHERE operation_id=?", (operation_id,)).fetchone()
        if row is None:
            return None
        event = json.loads(row["event"])
        return event, self.sign(event)

    def pay_v1(self, command, *, mode="success"):
        """Synthetic V1 payment only; never creates a merchant order or uses HTTP.

        A production adapter is deliberately absent. It would have to bind a
        provider-enforced fixed quote/SKU/quantity/total before any real dispatch.
        """
        fields = {"operation_id", "provider_idempotency_key", "task_id", "snapshot_id", "quote_id",
                  "quote_version", "quote_digest", "merchant_id", "merchant_sku", "purchase_quantity",
                  "unit_price_minor", "goods_minor", "fees_minor", "discount_minor", "cash_minor",
                  "currency", "payee_ref", "payee_mapping_version"}
        if set(command) != fields or command["currency"] != "HKD":
            raise Rejected("SYNTHETIC_V1_COMMAND_SCHEMA")
        if mode not in {"success", "decline", "accept_then_timeout"}:
            raise Rejected("SYNTHETIC_MODE_INVALID")
        for name in ("purchase_quantity", "quote_version", "payee_mapping_version", "cash_minor"):
            integer(command[name], 1)
        for name in ("unit_price_minor", "goods_minor", "fees_minor", "discount_minor"):
            integer(command[name])
        if (command["goods_minor"] != command["unit_price_minor"] * command["purchase_quantity"] or
                command["cash_minor"] != command["goods_minor"] + command["fees_minor"] - command["discount_minor"]):
            raise Rejected("SYNTHETIC_V1_AMOUNT_MISMATCH")
        for name in fields - {"purchase_quantity", "quote_version", "payee_mapping_version", "cash_minor",
                              "unit_price_minor", "goods_minor", "fees_minor", "discount_minor"}:
            if not isinstance(command[name], str) or not command[name]:
                raise Rejected("SYNTHETIC_V1_COMMAND_SCHEMA")
        key = command["operation_id"]; fingerprint = digest(command)
        self.db.execute("BEGIN IMMEDIATE")
        try:
            prior = self.db.execute("SELECT * FROM s1_local_payments WHERE operation_id=? OR idempotency_key=?",
                                    (key, command["provider_idempotency_key"])).fetchone()
            if prior:
                if prior["command_digest"] != fingerprint:
                    raise Rejected("SYNTHETIC_IDEMPOTENCY_CONFLICT")
                event = json.loads(prior["event"])
            else:
                event = {"event_id": "local_event_" + key, "operation_id": key,
                         "payment_ref": "local_payment_" + key, "status": "FAILED" if mode == "decline" else "SUCCEEDED",
                         "command": command, "environment": "local_simulator"}
                self.db.execute("INSERT INTO s1_local_payments VALUES(?,?,?,?,?)",
                                (key, command["provider_idempotency_key"], fingerprint, event["payment_ref"], canonical(event)))
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise
        if mode == "accept_then_timeout":
            raise TimeoutError("SYNTHETIC_ACCEPTED_RESPONSE_LOST")
        return event, self.sign(event)

    def query_v1(self, operation_id):
        row = self.db.execute("SELECT event FROM s1_local_payments WHERE operation_id=?", (operation_id,)).fetchone()
        if row is None:
            return None
        event = json.loads(row["event"])
        return event, self.sign(event)

    def refund_full(self, *, refund_id, original_ref, amount):
        integer(amount, 1)
        self.db.execute("BEGIN IMMEDIATE")
        try:
            payment = self.db.execute("SELECT event FROM payments WHERE payment_ref=?", (original_ref,)).fetchone()
            if payment is None:
                raise Rejected("ORIGINAL_PAYMENT_NOT_FOUND")
            event = json.loads(payment["event"])
            if event["status"] != "SUCCEEDED" or event["amount"] != amount:
                raise Rejected("FULL_ORIGINAL_PAYMENT_REQUIRED")
            prior = self.db.execute("SELECT * FROM refunds WHERE refund_id=?", (refund_id,)).fetchone()
            if prior:
                if prior["original_ref"] != original_ref or prior["amount"] != amount:
                    raise Rejected("REFUND_KEY_CONFLICT")
                result = json.loads(prior["result"])
            else:
                if self.db.execute("SELECT 1 FROM refunds WHERE original_ref=?", (original_ref,)).fetchone():
                    raise Rejected("ORIGINAL_PAYMENT_ALREADY_REFUNDED")
                result = {"refund_id": refund_id, "original_ref": original_ref,
                          "amount": amount, "status": "SUCCEEDED",
                          "source": "synthetic_fixture"}
                self.db.execute("INSERT INTO refunds VALUES(?,?,?,?)",
                                (refund_id, original_ref, amount, canonical(result)))
            self.db.commit()
            return result
        except BaseException:
            self.db.rollback()
            raise


def dispatch_once(kernel, psp, worker, operation_id, **simulation):
    """One caller wins claim; reconcilers never call this to re-send a claimed debit."""
    command = kernel.claim_dispatch(worker, operation_id)
    if command is None:
        return "not_dispatched"
    try:
        event, signature = psp.pay(command, **simulation)
    except TimeoutError:
        kernel.mark_unknown(worker, operation_id)
        return "unknown"
    return kernel.apply_payment_event(worker, event, signature)


def reconcile_only(kernel, psp, worker, operation_id):
    """Safe after restart or revocation: query original id, never initiate payment."""
    o = kernel.row("operations", operation_id)
    p = kernel.row("plans", o["plan_id"])
    kernel.check(worker, "reconcile", kind="service", plan=p)
    result = psp.query(operation_id)
    if result is None:
        # Absence may be temporary in a real provider. Keep the budget reservation.
        kernel.mark_unknown(worker, operation_id)
        return "unknown"
    return kernel.apply_payment_event(worker, *result)


def _stage1_write(kernel, action):
    """Composition helper owns an outer transaction; the kernel never commits."""
    if kernel.db.in_transaction:
        raise Rejected("LOCAL_DISPATCH_REQUIRES_NO_TRANSACTION")
    kernel.db.execute("BEGIN IMMEDIATE")
    try:
        result = action()
        kernel.db.commit()
        return result
    except BaseException:
        kernel.db.rollback()
        raise


def dispatch_stage1(kernel, psp, operation_id, *, mode="success"):
    """Explicit local-only exercise. Commit claim BEFORE touching the simulator.

    Re-entry after any committed claim never calls pay again, even if no provider
    row exists. A lost process between commit and send needs reconciliation.
    """
    if type(psp) is not LocalPSP:
        raise Rejected("LOCAL_SIMULATOR_ONLY")
    if mode not in {"success", "decline", "accept_then_timeout"}:
        raise Rejected("SYNTHETIC_MODE_INVALID")
    command = _stage1_write(kernel, lambda: kernel.claim(operation_id))
    if command is None:
        return "not_dispatched"
    try:
        event, signature = psp.pay_v1(command, mode=mode)
    except (TimeoutError, ConnectionError):
        _stage1_write(kernel, lambda: kernel.mark_unknown(operation_id))
        return "unknown"
    return _stage1_write(kernel, lambda: kernel.apply_local_result(event, signature, callback_key=psp.key))


def reconcile_stage1(kernel, psp, operation_id):
    if type(psp) is not LocalPSP:
        raise Rejected("LOCAL_SIMULATOR_ONLY")
    if kernel.db.in_transaction:
        raise Rejected("LOCAL_DISPATCH_REQUIRES_NO_TRANSACTION")
    descriptor = kernel.recover(operation_id)
    if descriptor["action"] != "query_original_only":
        return "not_reconciled"
    result = psp.query_v1(operation_id)
    if result is None:
        _stage1_write(kernel, lambda: kernel.mark_unknown(operation_id))
        return "unknown"
    return _stage1_write(kernel, lambda: kernel.apply_local_result(*result, callback_key=psp.key))
