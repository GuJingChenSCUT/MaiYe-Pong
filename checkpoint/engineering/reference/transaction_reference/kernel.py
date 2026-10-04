"""Local reference ONLY: SQLite transactions, test identities, synthetic payment facts.

No web authentication, real bank API, card data, FX or production payment integration.
Public methods here are internal server ports, not endpoints exposed to a model.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Callable
from pathlib import Path


class Rejected(Exception):
    pass


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def integer(value, minimum=0):
    if type(value) is not int or value < minimum or value > 10**12:
        raise Rejected("INVALID_INTEGER")
    return value


@dataclass(frozen=True)
class TestPrincipal:
    """Trusted fixture issued by the server test harness; NOT a JWT verifier.

    Never construct this from a model's JSON. The production composition root must
    verify identity, audience, issuer, session, tenant, role and delegated resources.
    """
    actor_id: str
    tenant_id: str
    owner_id: str
    kind: str
    role: str
    scopes: frozenset[str]
    plan_ids: frozenset[str]
    expires_at: int


class StageOneKernel:
    """V1 internal ports on the application's connection; never commits for it.

    Actors are authenticated server context, never model/request JSON. Public
    callers use buyer ports; claim/result/recovery are backend-only worker ports.
    This release only emits commands for the explicitly synthetic LocalPSP.
    """

    @staticmethod
    def install(conn):
        # sqlite3.executescript commits implicitly: never use it inside TaskStore.
        if conn.in_transaction:
            raise Rejected("INSTALL_REQUIRES_NO_TRANSACTION")
        conn.execute("PRAGMA foreign_keys=ON")
        sql = Path(__file__).with_name("migrations") / "001_stage1_bindings.sql"
        conn.executescript(sql.read_text(encoding="utf-8"))

    def __init__(self, conn, *, clock=None):
        self.db = conn
        self.db.row_factory = sqlite3.Row
        self.clock = clock or (lambda: int(time.time()))

    def _write(self):
        if not self.db.in_transaction:
            raise Rejected("CALLER_TRANSACTION_REQUIRED")

    @staticmethod
    def _actor(actor):
        if (not isinstance(actor, dict) or actor.get("role") != "buyer" or
                any(not isinstance(actor.get(k), str) or not actor[k]
                    for k in ("actor_id", "tenant_id"))):
            raise Rejected("BUYER_IDENTITY_REQUIRED")

    def _authorize(self, row, actor):
        self._actor(actor)
        if row["tenant_id"] != actor["tenant_id"] or row["owner_id"] != actor["actor_id"]:
            raise Rejected("RESOURCE_FORBIDDEN")

    def _get(self, table, key, value):
        allowed = {"s1_current_bindings": "task_id", "s1_snapshots": "snapshot_id",
                   "s1_challenges": "challenge_id", "s1_mandates": "mandate_id",
                   "s1_operations": "operation_id", "s1_dispatch_commands": "operation_id"}
        if allowed.get(table) != key:
            raise Rejected("INVALID_TABLE")
        row = self.db.execute(f"SELECT * FROM {table} WHERE {key}=?", (value,)).fetchone()
        if row is None:
            raise Rejected("NOT_FOUND")
        return row

    def _audit(self, task_id, actor_id, event, object_id, details):
        self.db.execute("INSERT INTO s1_event_audit(at,task_id,actor_id,event,object_id,details) "
                        "VALUES(?,?,?,?,?,?)",
                        (self.clock(), task_id, actor_id, event, object_id, canonical(details)))

    def update_current_bindings(self, task_id, actor, *, constraints_version, fact_version=None,
                                payee_ref=None, payee_mapping_version=None,
                                quote_id=None, quote_digest=None):
        """Trusted current-fact publication; constraint edits use invalidate_task.

        quote_id and quote_digest disambiguate quotes with equal vendor versions.
        The host must fence late model results before calling this internal port.
        """
        self._write(); self._actor(actor); integer(constraints_version, 1)
        if not isinstance(task_id, str) or not task_id:
            raise Rejected("INVALID_TASK_ID")
        if fact_version is not None:
            integer(fact_version)
        if payee_mapping_version is not None:
            integer(payee_mapping_version, 1)
        for value in (payee_ref, quote_id, quote_digest):
            if value is not None and (not isinstance(value, str) or not value):
                raise Rejected("INVALID_BINDING")
        row = self.db.execute("SELECT * FROM s1_current_bindings WHERE task_id=?", (task_id,)).fetchone()
        if row is None:
            self.db.execute("INSERT INTO s1_current_bindings(task_id,tenant_id,owner_id,constraints_version,"
                            "fact_version,payee_ref,payee_mapping_version,quote_id,quote_digest) VALUES(?,?,?,?,?,?,?,?,?)",
                            (task_id, actor["tenant_id"], actor["actor_id"], constraints_version,
                             fact_version, payee_ref, payee_mapping_version, quote_id, quote_digest))
        else:
            self._authorize(row, actor)
            if row["constraints_version"] != constraints_version:
                raise Rejected("CONSTRAINTS_VERSION_MISMATCH")
            if row["stopped"]:
                raise Rejected("TASK_STOPPED")
            self.db.execute("UPDATE s1_current_bindings SET fact_version=?,payee_ref=?,payee_mapping_version=?,"
                            "quote_id=?,quote_digest=? WHERE task_id=?",
                            (fact_version, payee_ref, payee_mapping_version, quote_id, quote_digest, task_id))
        self._audit(task_id, actor["actor_id"], "current_bindings_updated", task_id,
                    {"constraints_version": constraints_version, "fact_version": fact_version,
                     "quote_id": quote_id, "payee_mapping_version": payee_mapping_version})
        return dict(self._get("s1_current_bindings", "task_id", task_id))

    def _validate_snapshot(self, snapshot):
        try:
            from slice04.domain import evaluate_v1
            q, draft = snapshot["quote"], snapshot["draft"]
            body = {k: v for k, v in snapshot.items() if k not in {"snapshot_id", "digest"}}
            fingerprint = digest(body)
            if snapshot["digest"] != fingerprint or snapshot["snapshot_id"] != "snap_" + fingerprint:
                raise Rejected("SNAPSHOT_DIGEST_MISMATCH")
            for value in (snapshot["constraints_version"], snapshot["fact_version"], q["version"],
                          q["purchase_quantity"], q["payee_mapping_version"], snapshot["expires_at"]):
                integer(value, 1)
            for value in (q["unit_price_minor"], q["line_subtotal_minor"], draft["cash_cap_minor"]):
                integer(value)
            if (q["environment"] != "local_simulator" or q["provenance"] != "synthetic_fixture"
                    or q["currency"] != "HKD"):
                raise Rejected("LOCAL_SIMULATOR_ONLY")
            if (snapshot["fact_version"] != q["version"] or snapshot["payee_ref"] != q["payee_ref"]
                    or snapshot["payee_mapping_version"] != q["payee_mapping_version"]
                    or snapshot["expires_at"] > q["expires_at"]):
                raise Rejected("SNAPSHOT_BINDING_MISMATCH")
            if snapshot["expires_at"] <= self.clock():
                raise Rejected("SNAPSHOT_EXPIRED")
            if (q["purchase_quantity"] != draft["purchase_quantity"] or
                    q["line_subtotal_minor"] != q["unit_price_minor"] * q["purchase_quantity"]):
                raise Rejected("QUANTITY_OR_SUBTOTAL_MISMATCH")
            for name in ("quote_id", "merchant_id", "merchant_sku", "payee_ref"):
                if not isinstance(q[name], str) or not q[name]:
                    raise Rejected("INVALID_QUOTE_BINDING")
            seen = set()
            for discount in q["discounts"]:
                key = (discount["subject_ref"], discount["coupon_instance_id"])
                if key in seen:
                    raise Rejected("DUPLICATE_DISCOUNT_INSTANCE")
                seen.add(key)
            calculation = evaluate_v1(q, draft, now=self.clock())
            if not calculation["eligible"]:
                raise Rejected("QUOTE_INELIGIBLE:" + ",".join(calculation["blockers"]))
            for field in ("cash_minor", "goods_minor", "fees_minor", "discount_minor"):
                integer(calculation[field])
                integer(snapshot["calculation"][field])
                if calculation[field] != snapshot["calculation"][field]:
                    raise Rejected("CALCULATION_MISMATCH")
            if (calculation["goods_minor"] != q["line_subtotal_minor"] or
                    calculation["cash_minor"] != calculation["goods_minor"] + calculation["fees_minor"]
                    - calculation["discount_minor"] or not 0 < calculation["cash_minor"] <= draft["cash_cap_minor"]):
                raise Rejected("CASH_BINDING_MISMATCH")
            return q, draft, calculation
        except Rejected:
            raise
        except (KeyError, TypeError, ValueError) as exc:
            raise Rejected("INVALID_SNAPSHOT:" + str(exc)) from exc

    @staticmethod
    def _binding_matches(current, snapshot):
        expected = {"constraints_version": snapshot["constraints_version"],
                    "fact_version": snapshot["fact_version"], "quote_id": snapshot["quote"]["quote_id"],
                    "quote_digest": digest(snapshot["quote"]), "payee_ref": snapshot["payee_ref"],
                    "payee_mapping_version": snapshot["payee_mapping_version"]}
        if any(current[k] != v for k, v in expected.items()):
            raise Rejected("CURRENT_BINDING_CHANGED")
        if current["stopped"]:
            raise Rejected("TASK_STOPPED")

    def register_snapshot(self, snapshot, actor):
        self._write(); self._actor(actor)
        task_id = snapshot.get("task_id") if isinstance(snapshot, dict) else None
        current = self.db.execute("SELECT * FROM s1_current_bindings WHERE task_id=?", (task_id,)).fetchone()
        if current is not None:
            self._authorize(current, actor)
            if current["stopped"]:
                raise Rejected("TASK_STOPPED")
            if current["operation_id"]:
                op = self._get("s1_operations", "operation_id", current["operation_id"])
                if op["state"] != "STOPPED":
                    raise Rejected("OPERATION_ALREADY_EXISTS")
        q, draft, calculation = self._validate_snapshot(snapshot)
        if current is None:
            self.update_current_bindings(task_id, actor, constraints_version=snapshot["constraints_version"],
                                         fact_version=snapshot["fact_version"], quote_id=q["quote_id"],
                                         quote_digest=digest(q), payee_ref=q["payee_ref"],
                                         payee_mapping_version=q["payee_mapping_version"])
        else:
            # Fill only an as-yet-unpublished binding, never silently replace a
            # current fact with a stale run's chosen snapshot.
            expected = {"fact_version": snapshot["fact_version"], "quote_id": q["quote_id"],
                        "quote_digest": digest(q), "payee_ref": q["payee_ref"],
                        "payee_mapping_version": q["payee_mapping_version"]}
            if current["constraints_version"] != snapshot["constraints_version"]:
                raise Rejected("CONSTRAINTS_VERSION_MISMATCH")
            if any(current[k] is not None and current[k] != v for k, v in expected.items()):
                raise Rejected("CURRENT_BINDING_CHANGED")
            self.db.execute("UPDATE s1_current_bindings SET fact_version=?,quote_id=?,quote_digest=?,"
                            "payee_ref=?,payee_mapping_version=? WHERE task_id=?",
                            (snapshot["fact_version"], q["quote_id"], digest(q), q["payee_ref"],
                             q["payee_mapping_version"], task_id))
        sid = snapshot["snapshot_id"]
        values = {"snapshot_id": sid, "task_id": task_id, "tenant_id": actor["tenant_id"],
                  "owner_id": actor["actor_id"], "constraints_version": snapshot["constraints_version"],
                  "fact_version": snapshot["fact_version"], "quote_id": q["quote_id"],
                  "quote_version": q["version"], "quote_digest": digest(q), "merchant_id": q["merchant_id"],
                  "merchant_sku": q["merchant_sku"], "purchase_quantity": q["purchase_quantity"],
                  "unit_price_minor": q["unit_price_minor"],
                  **{k: calculation[k] for k in ("goods_minor", "fees_minor", "discount_minor", "cash_minor")},
                  "currency": q["currency"], "cash_cap_minor": draft["cash_cap_minor"],
                  "payee_ref": q["payee_ref"], "payee_mapping_version": q["payee_mapping_version"],
                  "digest": snapshot["digest"], "payload": canonical(snapshot),
                  "expires_at": snapshot["expires_at"], "created_at": self.clock()}
        prior = self.db.execute("SELECT * FROM s1_snapshots WHERE snapshot_id=?", (sid,)).fetchone()
        if prior is None:
            self.db.execute("INSERT INTO s1_snapshots(" + ",".join(values) + ") VALUES(" +
                            ",".join("?" for _ in values) + ")", tuple(values.values()))
        elif prior["payload"] != canonical(snapshot):
            raise Rejected("SNAPSHOT_ID_CONFLICT")
        self.db.execute("UPDATE s1_challenges SET state='REVOKED' WHERE task_id=? AND state='PENDING' "
                        "AND (snapshot_id!=? OR expires_at<=?)", (task_id, sid, self.clock()))
        challenge = self.db.execute("SELECT * FROM s1_challenges WHERE snapshot_id=? AND state='PENDING'", (sid,)).fetchone()
        if challenge is None:
            cid = "challenge_" + uuid.uuid4().hex
            expiry = min(snapshot["expires_at"], self.clock() + 300)
            self.db.execute("INSERT INTO s1_challenges VALUES(?,?,?,?,?,?,?,?)",
                            (cid, task_id, sid, actor["tenant_id"], actor["actor_id"], snapshot["digest"], expiry, "PENDING"))
        else:
            cid, expiry = challenge["challenge_id"], challenge["expires_at"]
        self.db.execute("UPDATE s1_current_bindings SET snapshot_id=?,cap_minor=? WHERE task_id=?",
                        (sid, draft["cash_cap_minor"], task_id))
        self._audit(task_id, actor["actor_id"], "snapshot_registered", sid,
                    {"snapshot_id": sid, "challenge_id": cid, "cash_minor": calculation["cash_minor"]})
        return {"snapshot_id": sid, "challenge_id": cid, "expires_at": expiry}

    def approve(self, task_id, snapshot_id, challenge_id, actor):
        """Explicit human consent atomically creates one mandate/op/command.

        Does not dispatch. HTTP idempotency is additionally enforced by TaskStore.
        """
        self._write()
        current = self._get("s1_current_bindings", "task_id", task_id); self._authorize(current, actor)
        challenge = self._get("s1_challenges", "challenge_id", challenge_id); self._authorize(challenge, actor)
        if challenge["task_id"] != task_id or challenge["snapshot_id"] != snapshot_id:
            raise Rejected("CHALLENGE_BINDING_MISMATCH")
        s = self._get("s1_snapshots", "snapshot_id", snapshot_id); self._authorize(s, actor)
        if s["task_id"] != task_id or challenge["snapshot_digest"] != s["digest"]:
            raise Rejected("CHALLENGE_BINDING_MISMATCH")
        if challenge["state"] == "CONSUMED":
            prior = self.db.execute("SELECT * FROM s1_operations WHERE snapshot_id=?", (snapshot_id,)).fetchone()
            if prior is None:
                raise Rejected("CHALLENGE_ALREADY_CONSUMED")
            return self._operation(prior)
        if challenge["state"] != "PENDING" or challenge["expires_at"] <= self.clock():
            raise Rejected("CHALLENGE_EXPIRED_OR_REVOKED")
        snapshot = json.loads(s["payload"])
        self._binding_matches(current, snapshot); self._validate_snapshot(snapshot)
        if current["snapshot_id"] != snapshot_id:
            raise Rejected("NEW_SNAPSHOT_REQUIRES_CONFIRMATION")
        if self.db.execute("SELECT 1 FROM s1_operations WHERE task_id=? AND state!='STOPPED'", (task_id,)).fetchone():
            raise Rejected("USE_ORIGINAL_OPERATION")
        mid, oid = "mandate_" + uuid.uuid4().hex, "operation_" + uuid.uuid4().hex
        self.db.execute("INSERT INTO s1_mandates VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (mid, task_id, snapshot_id, challenge_id, actor["tenant_id"], actor["actor_id"],
                         s["digest"], min(s["expires_at"], challenge["expires_at"]), 1, "ACTIVE"))
        self.db.execute("INSERT INTO s1_operations(operation_id,task_id,snapshot_id,mandate_id,tenant_id,owner_id,state,created_at) "
                        "VALUES(?,?,?,?,?,?,'QUEUED',?)", (oid, task_id, snapshot_id, mid, actor["tenant_id"], actor["actor_id"], self.clock()))
        command = {k: s[k] for k in ("task_id", "snapshot_id", "quote_id", "quote_version", "quote_digest",
                   "merchant_id", "merchant_sku", "purchase_quantity", "unit_price_minor", "goods_minor",
                   "fees_minor", "discount_minor", "cash_minor", "currency", "payee_ref", "payee_mapping_version")}
        command.update(operation_id=oid, provider_idempotency_key="local_" + uuid.uuid4().hex)
        self.db.execute("INSERT INTO s1_dispatch_commands VALUES(?,?,?,?,'PENDING',NULL)",
                        (oid, command["provider_idempotency_key"], canonical(command), digest(command)))
        changed = self.db.execute("UPDATE s1_current_bindings SET reserved_minor=reserved_minor+?,operation_id=? "
                                  "WHERE task_id=? AND reserved_minor+spent_minor+?<=cap_minor",
                                  (s["cash_minor"], oid, task_id, s["cash_minor"])).rowcount
        if changed != 1:
            raise Rejected("BUDGET_EXCEEDED")
        self.db.execute("UPDATE s1_challenges SET state='CONSUMED' WHERE challenge_id=?", (challenge_id,))
        self._audit(task_id, actor["actor_id"], "human_approved_and_queued", oid,
                    {"snapshot_id": snapshot_id, "mandate_id": mid, "cash_minor": s["cash_minor"]})
        return self._operation(self._get("s1_operations", "operation_id", oid))

    def _cancel_queued(self, operation, reason):
        if operation is None or operation["state"] != "QUEUED":
            return
        s = self._get("s1_snapshots", "snapshot_id", operation["snapshot_id"])
        self.db.execute("UPDATE s1_operations SET state='STOPPED',version=version+1,stop_reason=? WHERE operation_id=?",
                        (reason, operation["operation_id"]))
        self.db.execute("UPDATE s1_dispatch_commands SET state='CANCELLED' WHERE operation_id=? AND state='PENDING'",
                        (operation["operation_id"],))
        self.db.execute("UPDATE s1_current_bindings SET reserved_minor=reserved_minor-? WHERE task_id=?",
                        (s["cash_minor"], operation["task_id"]))

    def stop(self, task_id, actor, *, origin="user"):
        self._write()
        if origin not in ("user", "rule"):
            raise Rejected("INVALID_STOP_ORIGIN")
        current = self._get("s1_current_bindings", "task_id", task_id); self._authorize(current, actor)
        operation = (self._get("s1_operations", "operation_id", current["operation_id"])
                     if current["operation_id"] else None)
        self._cancel_queued(operation, "USER_STOP" if origin == "user" else "RULE_STOP")
        self.db.execute("UPDATE s1_current_bindings SET stopped=1 WHERE task_id=?", (task_id,))
        self.db.execute("UPDATE s1_mandates SET state='REVOKED',version=version+1 WHERE task_id=? AND state='ACTIVE'", (task_id,))
        self.db.execute("UPDATE s1_challenges SET state='REVOKED' WHERE task_id=? AND state='PENDING'", (task_id,))
        self._audit(task_id, actor["actor_id"] if origin == "user" else "rule_engine", origin + "_stop", task_id,
                    {"origin": origin, "dispatch_may_have_started": bool(operation and operation["state"] in ("DISPATCH_COMMITTED", "UNKNOWN", "SUCCEEDED"))})
        return self.inspect(task_id, actor)

    def invalidate_task(self, task_id, actor, new_constraints_version):
        self._write(); integer(new_constraints_version, 1)
        current = self._get("s1_current_bindings", "task_id", task_id); self._authorize(current, actor)
        if new_constraints_version <= current["constraints_version"]:
            raise Rejected("CONSTRAINTS_VERSION_MUST_INCREASE")
        operation = (self._get("s1_operations", "operation_id", current["operation_id"])
                     if current["operation_id"] else None)
        if operation and operation["state"] not in ("QUEUED", "STOPPED"):
            raise Rejected("EXECUTION_ALREADY_STARTED")
        self._cancel_queued(operation, "CONSTRAINTS_CHANGED")
        self.db.execute("UPDATE s1_mandates SET state='REVOKED',version=version+1 WHERE task_id=? AND state='ACTIVE'", (task_id,))
        self.db.execute("UPDATE s1_challenges SET state='REVOKED' WHERE task_id=? AND state='PENDING'", (task_id,))
        self.db.execute("UPDATE s1_current_bindings SET constraints_version=?,fact_version=NULL,quote_id=NULL,quote_digest=NULL,"
                        "payee_ref=NULL,payee_mapping_version=NULL,stopped=0,snapshot_id=NULL,operation_id=NULL,cap_minor=0 WHERE task_id=?",
                        (new_constraints_version, task_id))
        self._audit(task_id, actor["actor_id"], "constraints_invalidated", task_id,
                    {"constraints_version": new_constraints_version})
        return dict(self._get("s1_current_bindings", "task_id", task_id))

    def claim(self, operation_id):
        """Backend-only, single irreversible local dispatch claim. No lease retry.

        The transaction protects local bindings only, not outside merchant stock.
        No external provider call may occur until the caller commits this claim.
        """
        self._write()
        op = self._get("s1_operations", "operation_id", operation_id)
        if op["state"] != "QUEUED":
            return None
        s = self._get("s1_snapshots", "snapshot_id", op["snapshot_id"])
        current = self._get("s1_current_bindings", "task_id", op["task_id"])
        mandate = self._get("s1_mandates", "mandate_id", op["mandate_id"])
        command = self._get("s1_dispatch_commands", "operation_id", operation_id)
        try:
            snapshot = json.loads(s["payload"])
            self._binding_matches(current, snapshot); self._validate_snapshot(snapshot)
            if (current["snapshot_id"] != s["snapshot_id"] or current["operation_id"] != operation_id
                    or mandate["state"] != "ACTIVE" or mandate["expires_at"] <= self.clock()
                    or mandate["snapshot_digest"] != s["digest"] or command["state"] != "PENDING"):
                raise Rejected("AUTHORIZATION_CHANGED_OR_EXPIRED")
            payload = json.loads(command["payload"])
            if digest(payload) != command["payload_digest"]:
                raise Rejected("COMMAND_DIGEST_MISMATCH")
            for field in ("task_id", "snapshot_id", "quote_id", "quote_version", "quote_digest", "merchant_id",
                          "merchant_sku", "purchase_quantity", "unit_price_minor", "goods_minor", "fees_minor",
                          "discount_minor", "cash_minor", "currency", "payee_ref", "payee_mapping_version"):
                if payload[field] != s[field]:
                    raise Rejected("COMMAND_BINDING_MISMATCH")
            if payload["operation_id"] != operation_id or payload["provider_idempotency_key"] != command["provider_idempotency_key"]:
                raise Rejected("COMMAND_BINDING_MISMATCH")
        except Rejected as exc:
            self._cancel_queued(op, str(exc))
            self.db.execute("UPDATE s1_mandates SET state='REVOKED',version=version+1 WHERE mandate_id=?", (op["mandate_id"],))
            self.db.execute("UPDATE s1_current_bindings SET stopped=1 WHERE task_id=?", (op["task_id"],))
            self._audit(op["task_id"], "local_worker", "dispatch_blocked", operation_id, {"reason": str(exc)})
            return None
        self.db.execute("UPDATE s1_operations SET state='DISPATCH_COMMITTED',version=version+1,claimed_at=? WHERE operation_id=?",
                        (self.clock(), operation_id))
        self.db.execute("UPDATE s1_dispatch_commands SET state='CLAIMED',claimed_at=? WHERE operation_id=?",
                        (self.clock(), operation_id))
        self._audit(op["task_id"], "local_worker", "dispatch_committed", operation_id,
                    {"snapshot_id": s["snapshot_id"], "cash_minor": s["cash_minor"]})
        return payload

    def mark_unknown(self, operation_id):
        self._write()
        op = self._get("s1_operations", "operation_id", operation_id)
        if op["state"] == "DISPATCH_COMMITTED":
            self.db.execute("UPDATE s1_operations SET state='UNKNOWN',version=version+1 WHERE operation_id=?", (operation_id,))
            self._audit(op["task_id"], "local_worker", "payment_unknown", operation_id, {})
        return self._operation(self._get("s1_operations", "operation_id", operation_id))

    def recover(self, operation_id):
        """Backend-only query descriptor. Never issues a replacement command."""
        op = self._get("s1_operations", "operation_id", operation_id)
        return {"action": "query_original_only" if op["state"] in ("DISPATCH_COMMITTED", "UNKNOWN") else "none",
                "operation_id": operation_id, "state": op["state"]}

    def apply_local_result(self, event, signature, *, callback_key=b"synthetic-test-key-not-production"):
        self._write()
        expected_sig = hmac.new(callback_key, canonical(event).encode(), hashlib.sha256).hexdigest()
        if not isinstance(signature, str) or not hmac.compare_digest(expected_sig, signature):
            raise Rejected("LOCAL_CALLBACK_SIGNATURE_INVALID")
        if (set(event) != {"event_id", "operation_id", "payment_ref", "status", "command", "environment"}
                or event["status"] not in ("SUCCEEDED", "FAILED") or event["environment"] != "local_simulator"
                or any(not isinstance(event[k], str) or not event[k] for k in ("event_id", "operation_id", "payment_ref"))):
            raise Rejected("LOCAL_CALLBACK_SCHEMA_INVALID")
        op = self._get("s1_operations", "operation_id", event["operation_id"])
        command = self._get("s1_dispatch_commands", "operation_id", op["operation_id"])
        if canonical(event["command"]) != command["payload"]:
            raise Rejected("CALLBACK_BINDING_MISMATCH")
        if op["state"] in ("SUCCEEDED", "FAILED"):
            if op["provider_event_digest"] != digest(event):
                raise Rejected("CONFLICTING_TERMINAL_CALLBACK")
            return "already_applied"
        if op["state"] not in ("DISPATCH_COMMITTED", "UNKNOWN") or command["state"] != "CLAIMED":
            raise Rejected("PAYMENT_WAS_NOT_CLAIMED")
        if self.db.execute("SELECT 1 FROM s1_operations WHERE provider_ref=? AND operation_id!=?",
                           (event["payment_ref"], op["operation_id"])).fetchone():
            raise Rejected("PROVIDER_PAYMENT_ALREADY_BOUND")
        if self.db.execute("SELECT 1 FROM s1_operations WHERE provider_event_id=? AND operation_id!=?",
                           (event["event_id"], op["operation_id"])).fetchone():
            raise Rejected("PROVIDER_EVENT_ALREADY_BOUND")
        s = self._get("s1_snapshots", "snapshot_id", op["snapshot_id"])
        self.db.execute("UPDATE s1_current_bindings SET reserved_minor=reserved_minor-?,spent_minor=spent_minor+? WHERE task_id=?",
                        (s["cash_minor"], s["cash_minor"] if event["status"] == "SUCCEEDED" else 0, op["task_id"]))
        self.db.execute("UPDATE s1_operations SET state=?,version=version+1,provider_ref=?,provider_event_id=?,provider_event_digest=? "
                        "WHERE operation_id=?", (event["status"], event["payment_ref"], event["event_id"], digest(event), op["operation_id"]))
        self.db.execute("UPDATE s1_dispatch_commands SET state='COMPLETED' WHERE operation_id=?", (op["operation_id"],))
        self._audit(op["task_id"], "local_worker", "local_payment_result", op["operation_id"],
                    {"state": event["status"], "payment_ref": event["payment_ref"], "order_status": "NOT_CREATED"})
        return "applied"

    def _operation(self, op):
        s = self._get("s1_snapshots", "snapshot_id", op["snapshot_id"])
        # Deliberately exclude dispatch payload, raw idempotency key and session.
        result = {k: op[k] for k in ("operation_id", "task_id", "snapshot_id", "mandate_id", "state", "version",
                  "created_at", "claimed_at", "provider_ref", "order_status", "stop_reason")}
        result.update({k: s[k] for k in ("merchant_id", "merchant_sku", "purchase_quantity", "unit_price_minor",
                       "goods_minor", "fees_minor", "discount_minor", "cash_minor", "currency", "payee_ref", "payee_mapping_version")})
        result.update(environment="local_simulator", payment_status=op["state"])
        return result

    def inspect(self, task_id, actor):
        current = self._get("s1_current_bindings", "task_id", task_id); self._authorize(current, actor)
        op = self._get("s1_operations", "operation_id", current["operation_id"]) if current["operation_id"] else None
        challenge = self.db.execute("SELECT challenge_id,snapshot_id,expires_at,state FROM s1_challenges "
                                    "WHERE task_id=? AND state='PENDING' ORDER BY rowid DESC LIMIT 1", (task_id,)).fetchone()
        events = []
        for row in self.db.execute("SELECT seq,at,event,object_id,details FROM s1_event_audit WHERE task_id=? ORDER BY seq", (task_id,)):
            item = dict(row); item["details"] = json.loads(item["details"]); events.append(item)
        return {"task_id": task_id, "stopped": bool(current["stopped"]),
                "current_bindings": {k: current[k] for k in ("constraints_version", "fact_version", "quote_id", "quote_digest",
                                     "payee_ref", "payee_mapping_version", "snapshot_id")},
                "challenge": dict(challenge) if challenge else None,
                "operation": self._operation(op) if op else None,
                "budget": {"cap": current["cap_minor"], "reserved": current["reserved_minor"], "spent": current["spent_minor"]},
                "events": events}


SCHEMA = """
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS budgets(
 tenant TEXT, owner TEXT, currency TEXT, cap INTEGER NOT NULL,
 reserved INTEGER NOT NULL DEFAULT 0, spent INTEGER NOT NULL DEFAULT 0,
 PRIMARY KEY(tenant, owner, currency),
 CHECK(cap >= 0 AND reserved >= 0 AND spent >= 0 AND reserved+spent <= cap));
CREATE TABLE IF NOT EXISTS plans(
 id TEXT PRIMARY KEY, tenant TEXT NOT NULL, owner TEXT NOT NULL,
 version INTEGER NOT NULL, merchant TEXT NOT NULL, currency TEXT NOT NULL,
 goods INTEGER NOT NULL, fees INTEGER NOT NULL, amount INTEGER NOT NULL,
 quote_expires INTEGER NOT NULL, fingerprint TEXT NOT NULL, payload TEXT NOT NULL,
 CHECK(goods >= 0 AND fees >= 0 AND amount=goods+fees));
CREATE TRIGGER IF NOT EXISTS immutable_plan_update BEFORE UPDATE ON plans
 BEGIN SELECT RAISE(ABORT, 'immutable_plan'); END;
CREATE TRIGGER IF NOT EXISTS immutable_plan_delete BEFORE DELETE ON plans
 BEGIN SELECT RAISE(ABORT, 'immutable_plan'); END;
CREATE TABLE IF NOT EXISTS mandates(
 id TEXT PRIMARY KEY, plan_id TEXT UNIQUE NOT NULL REFERENCES plans(id),
 approved_by TEXT NOT NULL, plan_digest TEXT NOT NULL, plan_version INTEGER NOT NULL,
 merchant TEXT NOT NULL, currency TEXT NOT NULL, cash_cap INTEGER NOT NULL,
 expires INTEGER NOT NULL, state TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS operations(
 id TEXT PRIMARY KEY, command_key TEXT UNIQUE NOT NULL, command_digest TEXT NOT NULL,
 plan_id TEXT UNIQUE NOT NULL REFERENCES plans(id), mandate_id TEXT NOT NULL REFERENCES mandates(id),
 state TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1,
 provider_ref TEXT, reward_units INTEGER NOT NULL DEFAULT 0, reward_state TEXT NOT NULL DEFAULT 'pending');
CREATE TABLE IF NOT EXISTS audit(
 seq INTEGER PRIMARY KEY AUTOINCREMENT, at INTEGER NOT NULL, actor TEXT NOT NULL,
 event TEXT NOT NULL, object_id TEXT NOT NULL, details TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS outbox(
 id INTEGER PRIMARY KEY AUTOINCREMENT, at INTEGER NOT NULL, event TEXT NOT NULL,
 object_id TEXT NOT NULL, payload TEXT NOT NULL, delivered INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS provider_events(
 event_id TEXT PRIMARY KEY, digest TEXT NOT NULL, operation_id TEXT NOT NULL,
 accepted INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS alerts(
 id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT NOT NULL, object_id TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS refunds(
 id TEXT PRIMARY KEY, operation_id TEXT UNIQUE NOT NULL REFERENCES operations(id),
 original_ref TEXT NOT NULL, state TEXT NOT NULL, approved_by TEXT NOT NULL);
"""


class Kernel:
    def __init__(self, path, *, clock: Callable[[], int] | None = None,
                 local_callback_key: bytes = b"synthetic-test-key-not-production"):
        self.clock = clock or (lambda: int(time.time()))
        self.callback_key = local_callback_key
        self.db = sqlite3.connect(str(path), timeout=10, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)

    def close(self):
        self.db.close()

    @contextmanager
    def transaction(self):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self.db.rollback()
            raise
        else:
            self.db.commit()

    def check(self, principal, scope, *, kind=None, plan=None):
        if not isinstance(principal, TestPrincipal):
            raise Rejected("IDENTITY_INVALID")
        # Dataclass annotations are not runtime validation. Reject NaN/inf, bool,
        # strings and floats before any comparison; this principal contract is int.
        if type(principal.expires_at) is not int or not 0 < principal.expires_at <= 10**12:
            raise Rejected("IDENTITY_EXPIRY_INVALID")
        if principal.expires_at <= self.clock():
            raise Rejected("IDENTITY_EXPIRED")
        if scope not in principal.scopes or (kind and principal.kind != kind):
            raise Rejected("FORBIDDEN")
        if plan is not None and (principal.tenant_id != plan["tenant"] or
                                principal.owner_id != plan["owner"] or
                                plan["id"] not in principal.plan_ids):
            raise Rejected("RESOURCE_FORBIDDEN")

    def row(self, table, key):
        if table not in {"plans", "mandates", "operations", "refunds"}:
            raise Rejected("INVALID_TABLE")
        row = self.db.execute(f"SELECT * FROM {table} WHERE id=?", (key,)).fetchone()
        if row is None:
            raise Rejected("NOT_FOUND")
        return row

    def emit(self, actor, event, object_id, details):
        """Called only inside the same transaction as the financial state change."""
        payload = canonical(details)
        self.db.execute("INSERT INTO audit(at,actor,event,object_id,details) VALUES(?,?,?,?,?)",
                        (self.clock(), actor, event, object_id, payload))
        self.db.execute("INSERT INTO outbox(at,event,object_id,payload) VALUES(?,?,?,?)",
                        (self.clock(), event, object_id, payload))

    def seed_budget(self, principal, currency, cap):
        """Fixture setup only, not a public API or an Agent tool."""
        self.check(principal, "fixture_setup", kind="service")
        integer(cap)
        with self.transaction():
            self.db.execute("INSERT INTO budgets(tenant,owner,currency,cap) VALUES(?,?,?,?)",
                            (principal.tenant_id, principal.owner_id, currency, cap))

    def seed_plan(self, principal, *, plan_id, merchant, currency, goods, fees,
                  quote_expires, version=1):
        self.check(principal, "fixture_setup", kind="service")
        integer(goods); integer(fees); integer(version, 1); integer(quote_expires, 1)
        integer(goods+fees, 1)
        if not all(isinstance(x, str) and x for x in [plan_id, merchant, currency]):
            raise Rejected("INVALID_PLAN")
        plan = dict(id=plan_id, tenant=principal.tenant_id, owner=principal.owner_id,
                    version=version, merchant=merchant, currency=currency,
                    goods=goods, fees=fees, amount=goods+fees, quote_expires=quote_expires)
        fingerprint = digest(plan)
        with self.transaction():
            self.db.execute("INSERT INTO plans VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                            tuple(plan.values())+(fingerprint, canonical(plan)))
            self.emit(principal.actor_id, "plan_created", plan_id, {"digest": fingerprint})
        return fingerprint

    def approve(self, principal, plan_id, *, expected_digest, expected_version,
                merchant, currency, cash_cap, expires):
        """Human endpoint port; expected_digest is a human-confirmed immutable quote.

        A real UI must add a single-use, session-bound approval challenge and fresh
        authentication before invoking this port. This reference does not implement it.
        """
        integer(cash_cap, 1); integer(expires, 1)
        integer(expected_version, 1)
        with self.transaction():
            p = self.row("plans", plan_id)
            self.check(principal, "approve", kind="human", plan=p)
            if (p["fingerprint"] != expected_digest or p["version"] != expected_version or
                    p["merchant"] != merchant or p["currency"] != currency):
                raise Rejected("APPROVAL_BINDING_MISMATCH")
            if expires <= self.clock() or p["quote_expires"] <= self.clock():
                raise Rejected("AUTH_EXPIRED")
            if p["amount"] > cash_cap:
                raise Rejected("BUDGET_EXCEEDED")
            mid = "mandate-"+plan_id
            self.db.execute("INSERT INTO mandates VALUES(?,?,?,?,?,?,?,?,?,?,1)",
                            (mid, plan_id, principal.actor_id, expected_digest,
                             expected_version, merchant, currency, cash_cap, expires, "ACTIVE"))
            self.emit(principal.actor_id, "mandate_approved", mid, {"plan_id": plan_id})
        return mid

    def active(self, p, m):
        if m["state"] != "ACTIVE":
            raise Rejected("REVOKED")
        if m["expires"] <= self.clock():
            raise Rejected("AUTH_EXPIRED")
        if p["quote_expires"] <= self.clock():
            raise Rejected("STALE_QUOTE")
        if (m["plan_digest"] != p["fingerprint"] or m["plan_version"] != p["version"] or
                m["merchant"] != p["merchant"] or m["currency"] != p["currency"]):
            raise Rejected("APPROVAL_BINDING_MISMATCH")
        if p["amount"] > m["cash_cap"]:
            raise Rejected("BUDGET_EXCEEDED")

    def request_execution(self, principal, plan_id, *, command_key, authorization_version):
        """Agent supplies ONLY plan_id. Remaining arguments come from trusted workflow."""
        if not isinstance(command_key, str) or not command_key:
            raise Rejected("COMMAND_KEY_REQUIRED")
        with self.transaction():
            p = self.row("plans", plan_id)
            self.check(principal, "request_execution", kind="agent", plan=p)
            if principal.role != "buyer_planner":
                raise Rejected("FORBIDDEN")
            command_digest = digest({"actor": principal.actor_id, "plan_id": plan_id})
            prior = self.db.execute("SELECT * FROM operations WHERE command_key=?", (command_key,)).fetchone()
            if prior is not None:
                if prior["command_digest"] != command_digest:
                    raise Rejected("IDEMPOTENCY_CONFLICT")
                return self.receipt(prior, "already_exists")
            if self.db.execute("SELECT 1 FROM operations WHERE plan_id=?", (plan_id,)).fetchone():
                raise Rejected("USE_ORIGINAL_OPERATION_KEY")
            m = self.db.execute("SELECT * FROM mandates WHERE plan_id=?", (plan_id,)).fetchone()
            if m is None:
                raise Rejected("HUMAN_APPROVAL_REQUIRED")
            self.active(p, m)
            if authorization_version != m["version"]:
                raise Rejected("STALE_VERSION")
            changed = self.db.execute(
                "UPDATE budgets SET reserved=reserved+? WHERE tenant=? AND owner=? AND currency=? "
                "AND reserved+spent+? <= cap",
                (p["amount"], p["tenant"], p["owner"], p["currency"], p["amount"])).rowcount
            if changed != 1:
                raise Rejected("BUDGET_EXCEEDED")
            oid = str(uuid.uuid5(uuid.NAMESPACE_URL, "hacku-local:"+p["tenant"]+":"+plan_id))
            self.db.execute("INSERT INTO operations(id,command_key,command_digest,plan_id,mandate_id,state) "
                            "VALUES(?,?,?,?,?,?)", (oid, command_key, command_digest, plan_id, m["id"], "QUEUED"))
            self.emit(principal.actor_id, "execution_queued", oid, {"plan_id": plan_id, "amount": p["amount"]})
            return self.receipt(self.row("operations", oid), "queued")

    @staticmethod
    def receipt(o, state):
        return {"status": "ok", "data": {"workflow_id": "workflow-"+o["plan_id"],
                "operation_id": o["id"], "plan_id": o["plan_id"], "state": state,
                "state_version": o["version"]}, "error": None,
                "evidence_refs": [], "observed_versions": {"operation": o["version"]}}

    def release(self, p):
        self.db.execute("UPDATE budgets SET reserved=reserved-? WHERE tenant=? AND owner=? AND currency=?",
                        (p["amount"], p["tenant"], p["owner"], p["currency"]))

    def stop(self, principal, plan_id, *, stale_view_version=None):
        """Ignore stale *view* versions, never identity/resource authorization."""
        with self.transaction():
            p = self.row("plans", plan_id)
            self.check(principal, "stop", kind="human", plan=p)
            m = self.db.execute("SELECT * FROM mandates WHERE plan_id=?", (plan_id,)).fetchone()
            if m is None:
                raise Rejected("NOT_FOUND")
            if m["state"] != "REVOKED":
                self.db.execute("UPDATE mandates SET state='REVOKED',version=version+1 WHERE id=?", (m["id"],))
            o = self.db.execute("SELECT * FROM operations WHERE plan_id=?", (plan_id,)).fetchone()
            state = "no_pending_submission"
            if o and o["state"] == "QUEUED":
                self.release(p)
                self.db.execute("UPDATE operations SET state='STOPPED',version=version+1 WHERE id=?", (o["id"],))
                state = "stopped_before_dispatch"
            elif o and o["state"] == "DISPATCH_COMMITTED":
                state = "dispatch_already_started"
            elif o and o["state"] == "UNKNOWN":
                state = "payment_state_unknown"
            elif not o:
                state = "stopped_before_dispatch"
            self.emit(principal.actor_id, "mandate_revoked", m["id"], {"stop_state": state})
            return {"stop_state": state, "new_submissions_disabled": True,
                    "mandate_version": self.row("mandates", m["id"])["version"]}

    def claim_dispatch(self, principal, operation_id):
        """Serialization point. Only the worker winning this claim may send once.

        A crash after claim is reconciled, never blindly resent. A provider lookup of
        'not found' is kept unknown here; resolving that requires provider guarantees.
        """
        with self.transaction():
            o = self.row("operations", operation_id); p = self.row("plans", o["plan_id"])
            self.check(principal, "dispatch", kind="service", plan=p)
            if o["state"] != "QUEUED":
                return None
            m = self.row("mandates", o["mandate_id"])
            try:
                self.active(p, m)
            except Rejected as error:
                self.release(p)
                self.db.execute("UPDATE operations SET state='STOPPED',version=version+1 WHERE id=?", (operation_id,))
                self.emit(principal.actor_id, "dispatch_blocked", operation_id, {"reason": str(error)})
                return None
            self.db.execute("UPDATE operations SET state='DISPATCH_COMMITTED',version=version+1 WHERE id=?", (operation_id,))
            self.emit(principal.actor_id, "dispatch_committed", operation_id, {})
            return {"operation_id": operation_id, "amount": p["amount"],
                    "currency": p["currency"], "merchant": p["merchant"]}

    def mark_unknown(self, principal, operation_id):
        with self.transaction():
            o = self.row("operations", operation_id); p = self.row("plans", o["plan_id"])
            self.check(principal, "dispatch", kind="service", plan=p)
            if o["state"] == "DISPATCH_COMMITTED":
                self.db.execute("UPDATE operations SET state='UNKNOWN',version=version+1 WHERE id=?", (operation_id,))
                self.emit(principal.actor_id, "payment_unknown", operation_id, {})

    def verify_event(self, event, signature):
        expected = hmac.new(self.callback_key, canonical(event).encode(), hashlib.sha256).hexdigest()
        if not isinstance(signature, str) or not hmac.compare_digest(expected, signature):
            raise Rejected("CALLBACK_SIGNATURE_INVALID")
        if set(event) != {"event_id", "operation_id", "status", "payment_ref", "amount", "currency", "merchant", "reward_units"}:
            raise Rejected("CALLBACK_SCHEMA_INVALID")
        integer(event["amount"], 1); integer(event["reward_units"])
        if event["status"] not in {"SUCCEEDED", "FAILED"}:
            raise Rejected("CALLBACK_SCHEMA_INVALID")

    def apply_payment_event(self, principal, event, signature):
        """Synthetic callback verification: not any actual provider's signing scheme."""
        self.verify_event(event, signature)
        with self.transaction():
            oid = event["operation_id"]
            o = self.row("operations", oid); p = self.row("plans", o["plan_id"])
            self.check(principal, "reconcile", kind="service", plan=p)
            fingerprint = digest(event)
            seen = self.db.execute("SELECT * FROM provider_events WHERE event_id=?", (event["event_id"],)).fetchone()
            if seen:
                if seen["digest"] == fingerprint:
                    return "duplicate"
                self.db.execute("INSERT INTO alerts(code,object_id) VALUES('EVENT_ID_CONFLICT',?)", (oid,))
                return "conflict"
            matching = (event["amount"] == p["amount"] and event["currency"] == p["currency"] and
                        event["merchant"] == p["merchant"] and bool(event["payment_ref"]))
            terminal = o["state"] in {"SUCCEEDED", "FAILED", "REFUNDED"}
            equivalent = (o["state"] in {"SUCCEEDED", "REFUNDED"} and event["status"] == "SUCCEEDED" or
                          o["state"] == "FAILED" and event["status"] == "FAILED")
            consistent = matching and (not terminal or (equivalent and o["provider_ref"] == event["payment_ref"]
                                                       and o["reward_units"] == event["reward_units"]))
            allowed = o["state"] in {"DISPATCH_COMMITTED", "UNKNOWN"} or terminal
            if not consistent or not allowed:
                self.db.execute("INSERT INTO alerts(code,object_id) VALUES('PAYMENT_FACT_CONFLICT',?)", (oid,))
                self.db.execute("INSERT INTO provider_events VALUES(?,?,?,0)", (event["event_id"], fingerprint, oid))
                return "conflict"
            self.db.execute("INSERT INTO provider_events VALUES(?,?,?,1)", (event["event_id"], fingerprint, oid))
            if terminal:
                return "duplicate_fact"
            self.release(p)
            if event["status"] == "SUCCEEDED":
                self.db.execute("UPDATE budgets SET spent=spent+? WHERE tenant=? AND owner=? AND currency=?",
                                (p["amount"], p["tenant"], p["owner"], p["currency"]))
            reward_state = "posted" if event["status"] == "SUCCEEDED" else "cancelled"
            self.db.execute("UPDATE operations SET state=?,provider_ref=?,reward_units=?,reward_state=?,version=version+1 WHERE id=?",
                            (event["status"], event["payment_ref"], event["reward_units"], reward_state, oid))
            self.emit(principal.actor_id, "payment_"+event["status"].lower(), oid, {"amount": p["amount"]})
            return "applied"

    def request_refund(self, principal, operation_id):
        """Full-refund test port after a trusted human-approved settlement.

        It does not adjudicate refund entitlement or implement merchant acceptance.
        The production settlement service must check those before granting the scope.
        """
        with self.transaction():
            o = self.row("operations", operation_id); p = self.row("plans", o["plan_id"])
            self.check(principal, "approved_settlement_refund", kind="human", plan=p)
            prior = self.db.execute("SELECT * FROM refunds WHERE operation_id=?", (operation_id,)).fetchone()
            if prior:
                return dict(prior)
            if o["state"] != "SUCCEEDED":
                raise Rejected("REFUND_NOT_ALLOWED")
            rid = "refund-"+operation_id
            self.db.execute("INSERT INTO refunds VALUES(?,?,?,'QUEUED',?)",
                            (rid, operation_id, o["provider_ref"], principal.actor_id))
            self.emit(principal.actor_id, "refund_requested", rid, {"original_ref": o["provider_ref"]})
            return dict(self.row("refunds", rid))

    def finish_refund(self, principal, refund_id, *, confirmed_original_ref, confirmed_amount):
        """Trusted reconciliation port called after querying the original refund key."""
        with self.transaction():
            r = self.row("refunds", refund_id); o = self.row("operations", r["operation_id"])
            p = self.row("plans", o["plan_id"])
            self.check(principal, "reconcile", kind="service", plan=p)
            integer(confirmed_amount, 1)
            if r["original_ref"] != confirmed_original_ref or p["amount"] != confirmed_amount:
                raise Rejected("REFUND_ORIGINAL_PAYMENT_MISMATCH")
            if r["state"] == "SUCCEEDED":
                return "duplicate"
            self.db.execute("UPDATE refunds SET state='SUCCEEDED' WHERE id=?", (refund_id,))
            self.db.execute("UPDATE operations SET state='REFUNDED',reward_state='reversed',version=version+1 WHERE id=?", (o["id"],))
            self.db.execute("UPDATE budgets SET spent=spent-? WHERE tenant=? AND owner=? AND currency=?",
                            (p["amount"], p["tenant"], p["owner"], p["currency"]))
            self.emit(principal.actor_id, "refund_succeeded", refund_id, {"reward_state": "reversed"})
            return "applied"

    def inspect(self, principal, plan_id):
        p = self.row("plans", plan_id)
        self.check(principal, "inspect", plan=p)
        o = self.db.execute("SELECT * FROM operations WHERE plan_id=?", (plan_id,)).fetchone()
        b = self.db.execute("SELECT cap,reserved,spent FROM budgets WHERE tenant=? AND owner=? AND currency=?",
                            (p["tenant"], p["owner"], p["currency"])).fetchone()
        return {"operation": dict(o) if o else None, "budget": dict(b),
                "alerts": self.db.execute("SELECT count(*) FROM alerts WHERE object_id=?",
                                           (o["id"] if o else "",)).fetchone()[0]}
