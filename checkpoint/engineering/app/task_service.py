"""Application coordination. Every authoritative mutation shares the kernel transaction."""
from __future__ import annotations
import copy
import hashlib
import json
import os
import secrets
from app.auth import APIError
from app.demo_cases import resolve_demo_case
from app.task_store import encode
from slice04.domain import Rejected, validate_task_v1, freeze_v1, digest
from reference.transaction_reference.kernel import StageOneKernel

FIELDS = {"product", "purchase_quantity", "cash_cap_minor", "destination_ref", "preference",
          "requires_change_of_mind_return", "latest_delivery_epoch", "text"}
MODES = {"live", "scripted"}
SCENARIOS = {"normal", "shipping_increase", "injection", "both_unavailable"}


def opaque(prefix):
    return prefix + secrets.token_hex(12)


class TaskService:
    def __init__(self, store):
        self.store = store

    def kernel(self, conn):
        return StageOneKernel(conn, clock=self.store.now)

    @staticmethod
    def config():
        from slice04.fixtures import V1_PRODUCT
        return {"capabilities": {"model": "configuration_present_unverified" if os.getenv("DEEPSEEK_API_KEY") else "blocked",
                   "goods": "synthetic_fixture", "payment": "local_simulator", "deployment": "local",
                   "merchant_execution": "not_provided_or_verified", "official_psp_sandbox": "not_provided_or_verified"},
                "demo_template": {"product": copy.deepcopy(V1_PRODUCT), "purchase_quantity": 1,
                   "cash_cap_minor": 10000, "destination_ref": "HK-DEMO-KOWLOON",
                   "preference": "lowest_cost", "requires_change_of_mind_return": False,
                   "latest_delivery_epoch": None},
                "default_preference": "lowest_cost", "automatic_payment_dispatch": False}

    def authorize(self, conn, actor, task_id=None, *, mutate=False):
        if actor.get("role") not in {"buyer", "merchant", "operator"}:
            raise APIError(403, "ROLE_FORBIDDEN")
        if mutate and actor["role"] != "buyer":
            raise APIError(403, "BUYER_ROLE_REQUIRED")
        if task_id is None:
            return None
        row = conn.execute("SELECT * FROM app_tasks WHERE task_id=?", (task_id,)).fetchone()
        if row is None or row["tenant_id"] != actor["tenant_id"]:
            raise APIError(404, "TASK_NOT_FOUND")
        if actor["role"] == "buyer" and row["owner_id"] != actor["actor_id"]:
            raise APIError(404, "TASK_NOT_FOUND")
        if actor["role"] == "merchant":
            proposal = json.loads(row["proposal_json"] or "null")
            quote = (proposal or {}).get("snapshot", {}).get("quote", {})
            if quote.get("merchant_id") != actor.get("merchant_id"):
                raise APIError(404, "TASK_NOT_FOUND")
        return row

    def mutation(self, actor, action, task_id, key, body):
        if not isinstance(key, str) or not 8 <= len(key) <= 160:
            raise APIError(400, "IDEMPOTENCY_KEY_REQUIRED")
        if not isinstance(body, dict):
            raise APIError(400, "OBJECT_BODY_REQUIRED")
        action_fields = {"answers": {"expected_state_version", "base_constraints_version", "answers"},
                         "run": {"expected_state_version"},
                         "approve": {"expected_state_version", "snapshot_id", "challenge_id"}, "stop": set()}
        if action in action_fields and set(body) != action_fields[action]:
            raise APIError(400, "INVALID_ACTION_FIELDS")
        target = task_id or "/intents"
        checksum = hashlib.sha256(encode({"action": action, "target_resource": target, "body": body}).encode()).hexdigest()
        with self.store.transaction() as conn:
            # This order is intentional: a cached result is never an authorization.
            row = self.authorize(conn, actor, task_id, mutate=True)
            scope = (actor["tenant_id"], actor["actor_id"], action, key)
            previous = conn.execute("""SELECT * FROM app_http_idempotency
                WHERE tenant_id=? AND actor_id=? AND action=? AND idem_key=?""", scope).fetchone()
            if previous:
                if previous["resource"] != target or previous["digest"] != checksum:
                    raise APIError(409, "IDEMPOTENCY_BODY_CONFLICT")
                return json.loads(previous["response_json"])
            if action == "create":
                task_id = self._create(conn, actor, body)
            elif action == "answers":
                self._answers(conn, actor, row, body)
            elif action == "run":
                self._version(row, body)
                if row["status"] in {"STOPPED", "QUEUED", "DISPATCH_COMMITTED", "UNKNOWN", "SUCCEEDED", "FAILED"}:
                    raise APIError(409, "TASK_NOT_RUNNABLE")
                if row["active_run"]:
                    run = conn.execute("SELECT status FROM app_runs WHERE run_id=?", (row["active_run"],)).fetchone()
                    if run and run["status"] in {"QUEUED", "RUNNING"}:
                        raise APIError(409, "RUN_ALREADY_ACTIVE")
                self._enqueue(conn, task_id, row["constraints_version"])
            elif action == "approve":
                self._version(row, body)
                proposal = json.loads(row["proposal_json"] or "null")
                if row["status"] != "AWAITING_APPROVAL" or not proposal:
                    raise APIError(409, "NO_APPROVABLE_PROPOSAL")
                if (body.get("snapshot_id") != proposal["snapshot_id"]
                        or body.get("challenge_id") != proposal["challenge_id"]):
                    raise APIError(409, "PROPOSAL_BINDING_CONFLICT")
                operation = self.kernel(conn).approve(task_id, body["snapshot_id"], body["challenge_id"], actor)
                self._status(conn, task_id, operation.get("state", "QUEUED"))
                self.store.event(conn, task_id, "user_approved", {"snapshot_id": body["snapshot_id"],
                    "operation_id": operation.get("operation_id")})
            elif action == "stop":
                result = self.kernel(conn).stop(task_id, actor)
                conn.execute("UPDATE app_runs SET status='CANCELLED',fence=fence+1 WHERE task_id=? AND status IN ('QUEUED','RUNNING')", (task_id,))
                inspection = self.kernel(conn).inspect(task_id, actor)
                operation = inspection.get("operation")
                actual = (operation or {}).get("state", "STOPPED")
                self._status(conn, task_id, actual, clear_run=True)
                self.store.event(conn, task_id, "user_stop_requested", {"actual_operation_status": actual})
            else:
                raise APIError(404, "ACTION_NOT_FOUND")
            result = self._project(conn, self.authorize(conn, actor, task_id), actor)
            conn.execute("INSERT INTO app_http_idempotency VALUES(?,?,?,?,?,?,?,?)",
                scope + (target, checksum, encode(result), self.store.now()))
            return result

    @staticmethod
    def _version(row, body):
        if type(body.get("expected_state_version")) is not int or body["expected_state_version"] != row["state_version"]:
            raise APIError(409, "STATE_VERSION_CONFLICT")

    @staticmethod
    def _merge(draft, fields):
        if not isinstance(fields, dict) or set(fields) - FIELDS:
            raise APIError(400, "UNKNOWN_TASK_FIELD")
        result = copy.deepcopy(draft)
        for key, value in fields.items():
            if key == "product" and isinstance(value, dict):
                result[key] = dict(result.get(key) or {}, **value)
            else:
                result[key] = value
        validate_task_v1(result)
        return result

    def _create(self, conn, actor, body):
        if set(body) - {"text", "fields", "mode", "scenario"}:
            raise APIError(400, "UNKNOWN_REQUEST_FIELD")
        text = body.get("text", "")
        if not isinstance(text, str) or not text.strip() or len(text) > 8000:
            raise APIError(400, "TASK_TEXT_REQUIRED")
        mode, scenario = body.get("mode", "live"), body.get("scenario", "normal")
        if mode not in MODES or scenario not in SCENARIOS:
            raise APIError(400, "INVALID_RUN_MODE_OR_SCENARIO")
        draft = self._merge({"text": text}, body.get("fields", {}))
        demo = resolve_demo_case(actor, mode=mode, text=text, fields=body.get("fields", {}),
                                 scenario=body.get("scenario"))
        if demo:
            draft = self._merge({"text": text}, demo["fields"])
            scenario = demo["scenario"]
        task_id, now = opaque("task_"), self.store.now()
        conn.execute("""INSERT INTO app_tasks(task_id,tenant_id,owner_id,state_version,constraints_version,
          status,draft_json,missing_json,questions_json,mode,scenario,created_at,updated_at)
          VALUES(?,?,?,1,1,'DRAFT',?,?,'[]',?,?,?,?)""",
          (task_id, actor["tenant_id"], actor["actor_id"], encode(draft), encode(validate_task_v1(draft)), mode, scenario, now, now))
        self.kernel(conn).update_current_bindings(task_id, actor, constraints_version=1, fact_version=0,
                                                  payee_ref=None, payee_mapping_version=None)
        details = {"mode": mode, "draft": draft, "field_origin": "explicit"}
        if demo:
            details.update(field_origin="explicit_with_demo_exact_match", demo_case_id=demo["case_id"],
                           capability_label="synthetic_fixture")
        self.store.event(conn, task_id, "user_input", details)
        self._enqueue(conn, task_id, 1)
        return task_id

    def _answers(self, conn, actor, row, body):
        self._version(row, body)
        if type(body.get("base_constraints_version")) is not int or body["base_constraints_version"] != row["constraints_version"]:
            raise APIError(409, "CONSTRAINTS_VERSION_CONFLICT")
        draft = self._merge(json.loads(row["draft_json"]), body.get("answers"))
        if draft == json.loads(row["draft_json"]):
            raise APIError(409, "NO_TASK_CHANGE")
        version = row["constraints_version"] + 1
        self.kernel(conn).invalidate_task(row["task_id"], actor, version)
        conn.execute("UPDATE app_runs SET status='CANCELLED',fence=fence+1 WHERE task_id=? AND status IN ('QUEUED','RUNNING')", (row["task_id"],))
        conn.execute("""UPDATE app_tasks SET constraints_version=?,draft_json=?,missing_json=?,questions_json='[]',
          proposal_json=NULL,result_json=NULL WHERE task_id=?""",
          (version, encode(draft), encode(validate_task_v1(draft)), row["task_id"]))
        self.store.event(conn, row["task_id"], "user_answer", {"constraints_version": version,
                         "fields": sorted(body["answers"]), "answers": body["answers"], "field_origin": "explicit"})
        self._enqueue(conn, row["task_id"], version)

    def _enqueue(self, conn, task_id, version):
        run_id = opaque("run_")
        conn.execute("INSERT INTO app_runs(run_id,task_id,constraints_version,status,created_at) VALUES(?,?,?,'QUEUED',?)",
                     (run_id, task_id, version, self.store.now()))
        conn.execute("""UPDATE app_tasks SET active_run=?,status='PLANNING',state_version=state_version+1,
                      proposal_json=NULL,updated_at=? WHERE task_id=?""", (run_id, self.store.now(), task_id))
        self.store.event(conn, task_id, "model_run_queued", {"run_id": run_id, "constraints_version": version})

    def _status(self, conn, task_id, status, *, clear_run=False):
        conn.execute("UPDATE app_tasks SET status=?,state_version=state_version+1,updated_at=? WHERE task_id=?",
                     (status, self.store.now(), task_id))
        if clear_run:
            conn.execute("UPDATE app_tasks SET active_run=NULL WHERE task_id=?", (task_id,))

    def get(self, actor, task_id):
        with self.store.read_transaction() as conn:
            return self._project(conn, self.authorize(conn, actor, task_id), actor)

    def list(self, actor):
        with self.store.read_transaction() as conn:
            self.authorize(conn, actor)
            rows = conn.execute("SELECT * FROM app_tasks WHERE tenant_id=? ORDER BY created_at DESC,task_id DESC LIMIT 100", (actor["tenant_id"],)).fetchall()
            result = []
            for row in rows:
                try:
                    authorized_row = self.authorize(conn, actor, row["task_id"])
                except APIError:
                    continue
                result.append(self._project(conn, authorized_row, actor))
            return {"tasks": result}

    def _project(self, conn, row, actor):
        owner = {"actor_id": row["owner_id"], "tenant_id": row["tenant_id"], "role": "buyer"}
        internal = self.kernel(conn).inspect(row["task_id"], owner)
        operation = internal.get("operation")
        proposal = json.loads(row["proposal_json"] or "null")
        status = operation.get("state", row["status"]) if operation else row["status"]
        base = {"task_id": row["task_id"], "state_version": row["state_version"],
                "constraints_version": row["constraints_version"], "status": status,
                "created_at": row["created_at"], "updated_at": row["updated_at"]}
        if actor["role"] != "buyer":
            # Read-only operational views, not copies of buyer input/identity/budget.
            quote = (proposal or {}).get("snapshot", {}).get("quote", {})
            allowed_quote = {k: quote[k] for k in ("merchant_id", "merchant_sku", "purchase_quantity", "product", "currency", "line_subtotal_minor") if k in quote}
            safe_op = {k: operation[k] for k in ("operation_id", "state", "payment_status", "order_status", "provider_ref") if operation and k in operation}
            return dict(base, role_view=actor["role"], item=allowed_quote, operation=safe_op or None,
                        capabilities=self.config()["capabilities"])
        result = json.loads(row["result_json"] or "{}")
        run = None
        if row["active_run"]:
            record = conn.execute("SELECT run_id,status,attempts,constraints_version FROM app_runs WHERE run_id=?", (row["active_run"],)).fetchone()
            run = dict(record) if record else None
        events = [{"event_id": e["event_id"], "kind": e["kind"], "details": json.loads(e["details_json"]), "created_at": e["created_at"]}
                  for e in conn.execute("SELECT * FROM app_events WHERE task_id=? ORDER BY event_id", (row["task_id"],))]
        caps = self.config()["capabilities"]
        caps["model"] = result.get("model_status", "scripted" if row["mode"] == "scripted" else caps["model"])
        caps["model_online_verified"] = bool(result.get("model_online_verified", False))
        return dict(base, draft=json.loads(row["draft_json"]), missing_fields=json.loads(row["missing_json"]),
            questions=json.loads(row["questions_json"]), run=run, comparison=result.get("comparison"), proposal=proposal,
            operation=operation, capabilities=caps, events=events,
            model_result={key: result.get(key) for key in ("reason", "model_status", "task_success", "research_success", "replanning_demonstrated", "model_requests", "roles_used", "extractions", "trace")},
            metrics={"input_count": sum(e["kind"] == "user_input" for e in events),
                     "answer_count": sum(e["kind"] == "user_answer" for e in events),
                     "approval_count": sum(e["kind"] == "user_approved" for e in events),
                     "manual_baseline": None, "user_value_validated": False})

    def claim_run(self, worker_id, lease_seconds=180):
        with self.store.transaction() as conn:
            now = self.store.now()
            row = conn.execute("""SELECT r.* FROM app_runs r JOIN app_tasks t ON t.active_run=r.run_id
              WHERE t.status='PLANNING' AND r.constraints_version=t.constraints_version
              AND (r.status='QUEUED' OR (r.status='RUNNING' AND r.lease_until<=?))
              ORDER BY r.created_at,r.run_id LIMIT 1""", (now,)).fetchone()
            if row is None:
                return None
            if row["attempts"] >= 3:
                conn.execute("UPDATE app_runs SET status='BLOCKED',fence=fence+1 WHERE run_id=?", (row["run_id"],))
                self._status(conn, row["task_id"], "BLOCKED")
                self.store.event(conn, row["task_id"], "model_retry_exhausted")
                return None
            conn.execute("""UPDATE app_runs SET status='RUNNING',attempts=attempts+1,fence=fence+1,
                          lease_owner=?,lease_until=? WHERE run_id=?""", (worker_id, now + lease_seconds, row["run_id"]))
            run = dict(conn.execute("SELECT * FROM app_runs WHERE run_id=?", (row["run_id"],)).fetchone())
            task = conn.execute("SELECT * FROM app_tasks WHERE task_id=?", (row["task_id"],)).fetchone()
            run.update(draft=json.loads(task["draft_json"]), mode=task["mode"], scenario=task["scenario"])
            self.store.event(conn, row["task_id"], "model_run_claimed", {"run_id": row["run_id"], "attempt": run["attempts"]})
            return run

    def is_current_run(self, run):
        with self.store.connection() as conn:
            row = conn.execute("""SELECT r.status,r.fence,r.lease_until,r.lease_owner,t.active_run,t.constraints_version,t.status task_status
               FROM app_runs r JOIN app_tasks t ON t.task_id=r.task_id WHERE r.run_id=?""", (run["run_id"],)).fetchone()
            return bool(row and row["status"] == "RUNNING" and row["task_status"] == "PLANNING"
                        and row["active_run"] == run["run_id"] and row["fence"] == run["fence"]
                        and row["lease_owner"] == run["lease_owner"] and row["lease_until"] > self.store.now()
                        and row["constraints_version"] == run["constraints_version"])

    def finish_run(self, run, result):
        with self.store.transaction() as conn:
            record = conn.execute("SELECT * FROM app_runs WHERE run_id=?", (run["run_id"],)).fetchone()
            row = conn.execute("SELECT * FROM app_tasks WHERE task_id=?", (run["task_id"],)).fetchone()
            if (not record or not row or record["status"] != "RUNNING" or record["fence"] != run["fence"]
                or record["lease_owner"] != run["lease_owner"] or record["lease_until"] <= self.store.now()
                or row["active_run"] != run["run_id"] or row["constraints_version"] != run["constraints_version"]
                or row["status"] != "PLANNING"):
                self.store.event(conn, run["task_id"], "stale_model_result_discarded", {"run_id": run["run_id"]})
                return False
            draft = result.get("draft", run["draft"])
            validate_task_v1(draft)
            # The model may fill unknown draft fields, never change supplied values.
            for key, value in run["draft"].items():
                if isinstance(value, dict):
                    if any(v not in (None, "") and (draft.get(key) or {}).get(k) != v for k, v in value.items()):
                        raise Rejected("MODEL_CHANGED_USER_CONSTRAINT")
                elif value not in (None, "") and draft.get(key) != value:
                    raise Rejected("MODEL_CHANGED_USER_CONSTRAINT")
            actor = {"actor_id": row["owner_id"], "tenant_id": row["tenant_id"], "role": "buyer"}
            proposal = None
            status = {"clarifying": "CLARIFYING", "blocked": "BLOCKED", "stopped": "STOPPED", "proposed": "AWAITING_APPROVAL"}.get(result.get("status"), "BLOCKED")
            if status == "AWAITING_APPROVAL":
                snapshot = freeze_v1(row["task_id"], row["constraints_version"], draft, result["quote"], now=self.store.now())
                self.kernel(conn).update_current_bindings(row["task_id"], actor,
                    constraints_version=row["constraints_version"], fact_version=snapshot["fact_version"],
                    payee_ref=snapshot["payee_ref"], payee_mapping_version=snapshot["payee_mapping_version"],
                    quote_id=snapshot["quote"]["quote_id"], quote_digest=digest(snapshot["quote"]))
                binding = self.kernel(conn).register_snapshot(snapshot, actor)
                proposal = dict(binding, snapshot=snapshot)
            elif status == "STOPPED":
                self.kernel(conn).stop(row["task_id"], actor, origin="rule")
            conn.execute("""UPDATE app_runs SET status='FINISHED',result_json=?,lease_until=NULL WHERE run_id=?""", (encode(result), run["run_id"]))
            conn.execute("""UPDATE app_tasks SET draft_json=?,missing_json=?,questions_json=?,result_json=?,proposal_json=? WHERE task_id=?""",
                         (encode(draft), encode(result.get("missing_fields", [])), encode(result.get("questions", [])), encode(result), encode(proposal), row["task_id"]))
            self._status(conn, row["task_id"], status)
            self.store.event(conn, row["task_id"], "model_run_finished", {"run_id": run["run_id"], "model_status": result.get("model_status"), "outcome": status,
                             "snapshot_id": (proposal or {}).get("snapshot_id")})
            return True
