"""Real loopback HTTP boundary tests; synthetic data, no external model or PSP.

ThreadingHTTPServer binds port zero, sessions use the real local Auth service,
and each test has a separate persistent SQLite database. Research completion is
injected through the fenced service boundary because model behavior has its own
suite; these tests exercise HTTP identity, projection and mutation guarantees.
"""
import concurrent.futures
import copy
import http.client
from http.cookies import SimpleCookie
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.auth import Auth, hashed
from app.task_api import ApplicationServer
from app.task_store import TaskStore
from slice04.domain import catalog_v1, rank_v1
from slice04.fixtures import V1_PRODUCT


class TaskHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp.name) / "http-task.sqlite3"
        self.clock = [int(time.time())]
        self.store = TaskStore(self.db_path, clock=lambda: self.clock[0])
        self.auth = Auth(self.store)
        self.identities = {}
        for name, tenant, role, merchant in (
                ("buyer-a", "tenant-a", "buyer", None),
                ("buyer-b", "tenant-a", "buyer", None),
                ("buyer-other", "tenant-b", "buyer", None),
                ("merchant-a", "tenant-a", "merchant", "fixture-merchant-A"),
                ("merchant-b", "tenant-a", "merchant", "fixture-merchant-B"),
                ("operator", "tenant-a", "operator", None)):
            code = self.auth.provision(actor_id=name, tenant_id=tenant, role=role, merchant_id=merchant)
            self.identities[name] = {"code": code}
        self._start_server()
        for identity in self.identities.values():
            status, result, headers = self.request("POST", "/api/v1/session", {"access_code": identity["code"]})
            self.assertEqual(status, 200)
            cookie = SimpleCookie(); cookie.load(headers["set-cookie"])
            identity.update(cookie="hacku_session=" + cookie["hacku_session"].value,
                            token=cookie["hacku_session"].value, csrf=result["csrf_token"], actor=result["actor"])
        self.buyer = self.identities["buyer-a"]

    def _start_server(self):
        self.server = ApplicationServer(("127.0.0.1", 0), self.store, start_worker=False)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        self.thread.start()
        self.port = self.server.server_address[1]

    def _stop_server(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join(timeout=3)

    def tearDown(self):
        self._stop_server()
        self.temp.cleanup()

    def request(self, method, path, body=None, *, identity=None, key=None, csrf=True, headers=None):
        request_headers = {"Origin": "http://127.0.0.1:" + str(self.port)}
        if identity:
            request_headers["Cookie"] = identity["cookie"]
            if csrf and method != "GET":
                request_headers["X-CSRF-Token"] = identity["csrf"]
        if key:
            request_headers["Idempotency-Key"] = key
        if body is not None:
            request_headers["Content-Type"] = "application/json"
        request_headers.update(headers or {})
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.request(method, path, None if body is None else json.dumps(body), request_headers)
            response = conn.getresponse(); data = response.read()
            return response.status, json.loads(data), {k.lower(): v for k, v in response.getheaders()}
        finally:
            conn.close()

    def fields(self):
        return {"product": copy.deepcopy(V1_PRODUCT), "purchase_quantity": 1,
                "cash_cap_minor": 12345, "destination_ref": "HK-PRIVATE-DESTINATION",
                "preference": "lowest_cost", "requires_change_of_mind_return": False,
                "latest_delivery_epoch": None}

    def create(self, name="create-test-0001", identity=None):
        identity = identity or self.buyer
        body = {"text": "PRIVATE-ORIGINAL-TEXT-DO-NOT-PROJECT", "fields": self.fields(),
                "mode": "scripted", "scenario": "normal"}
        status, result, _ = self.request("POST", "/api/v1/intents", body, identity=identity, key=name)
        self.assertEqual(status, 200, result)
        return result

    def ready(self, name="create-ready-0001", identity=None):
        identity = identity or self.buyer
        created = self.create(name, identity)
        run = self.server.service.claim_run("http-test-worker")
        self.assertEqual(run["task_id"], created["task_id"])
        catalog = catalog_v1(run["draft"], now=self.store.now())
        comparison = rank_v1(catalog["quotes"], run["draft"], now=self.store.now())
        quote = next(q for q in catalog["quotes"] if q["quote_id"] == comparison["selected_quote_id"])
        result = {"status": "proposed", "draft": run["draft"], "missing_fields": [], "questions": [],
                  "comparison": comparison, "quote": quote, "model_status": "test_transport",
                  "model_online_verified": False, "task_success": False, "reason": "synthetic HTTP fixture"}
        self.assertTrue(self.server.service.finish_run(run, result))
        status, projection, _ = self.request("GET", "/api/v1/tasks/" + run["task_id"], identity=identity)
        self.assertEqual(status, 200, projection)
        self.assertEqual(projection["status"], "AWAITING_APPROVAL")
        return projection

    @staticmethod
    def approval_body(task):
        return {"snapshot_id": task["proposal"]["snapshot_id"],
                "challenge_id": task["proposal"]["challenge_id"],
                "expected_state_version": task["state_version"]}

    def command_count(self, task_id):
        with self.store.connection() as conn:
            # Inspect the authoritative queue, not merely the cached HTTP body.
            return conn.execute("SELECT COUNT(*) FROM s1_dispatch_commands d JOIN s1_operations o ON o.operation_id=d.operation_id WHERE o.task_id=?", (task_id,)).fetchone()[0]

    def test_double_click_approval_replays_old_version_once(self):
        task = self.ready()
        path = "/api/v1/tasks/" + task["task_id"] + "/approvals"
        body = self.approval_body(task)
        barrier = threading.Barrier(2)
        def click():
            barrier.wait(timeout=5)
            return self.request("POST", path, body, identity=self.buyer, key="same-approval-key")
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            first, second = list(pool.map(lambda _: click(), range(2)))
        self.assertEqual(first[0], 200, first[1]); self.assertEqual(second[0], 200, second[1])
        self.assertEqual(first[1], second[1])
        self.assertGreater(first[1]["state_version"], task["state_version"])
        self.assertEqual(self.command_count(task["task_id"]), 1)
        replay = self.request("POST", path, body, identity=self.buyer, key="same-approval-key")
        self.assertEqual(replay[1], first[1])
        changed = dict(body, expected_state_version=first[1]["state_version"])
        result = self.request("POST", path, changed, identity=self.buyer, key="same-approval-key")
        self.assertEqual((result[0], result[1]["error"]), (409, "IDEMPOTENCY_BODY_CONFLICT"))

    def test_authorization_precedes_cached_approval_lookup(self):
        task = self.ready(); body = self.approval_body(task)
        path = "/api/v1/tasks/" + task["task_id"] + "/approvals"
        status, accepted, _ = self.request("POST", path, body, identity=self.buyer, key="approval-owner-key")
        self.assertEqual(status, 200)
        operation_id = accepted["operation"]["operation_id"]
        for name in ("buyer-b", "buyer-other"):
            response = self.request("POST", path, body, identity=self.identities[name], key="approval-owner-key")
            self.assertEqual(response[0], 404)
            self.assertNotIn(operation_id, json.dumps(response[1]))
        anonymous = self.request("POST", path, body, key="approval-owner-key")
        self.assertEqual(anonymous[0], 401)
        self.assertNotIn(operation_id, json.dumps(anonymous[1]))
        self.assertEqual(self.command_count(task["task_id"]), 1)

    def test_same_actor_key_on_different_owned_resource_conflicts(self):
        first = self.ready("create-resource-one")
        second = self.ready("create-resource-two")
        accepted = self.request("POST", "/api/v1/tasks/" + first["task_id"] + "/approvals",
                                self.approval_body(first), identity=self.buyer, key="one-key-two-resources")
        self.assertEqual(accepted[0], 200)
        denied = self.request("POST", "/api/v1/tasks/" + second["task_id"] + "/approvals",
                              self.approval_body(second), identity=self.buyer, key="one-key-two-resources")
        self.assertEqual((denied[0], denied[1]["error"]), (409, "IDEMPOTENCY_BODY_CONFLICT"))
        self.assertEqual(self.command_count(first["task_id"]), 1)
        self.assertEqual(self.command_count(second["task_id"]), 0)

    def test_same_key_is_separate_for_different_actors_and_tenants(self):
        first = self.create("shared-create-key", self.buyer)
        other = self.create("shared-create-key", self.identities["buyer-b"])
        other_tenant = self.create("shared-create-key", self.identities["buyer-other"])
        self.assertEqual(len({first["task_id"], other["task_id"], other_tenant["task_id"]}), 3)
        listing = self.request("GET", "/api/v1/tasks", identity=self.buyer)
        self.assertEqual([t["task_id"] for t in listing[1]["tasks"]], [first["task_id"]])

    def test_nonbuyer_views_hide_private_input_budget_and_competitor(self):
        task = self.ready()
        for name in ("merchant-a", "operator"):
            identity = self.identities[name]
            response = self.request("GET", "/api/v1/tasks/" + task["task_id"], identity=identity)
            self.assertEqual(response[0], 200, response[1])
            raw = json.dumps(response[1])
            for forbidden in ("cash_cap_minor", "PRIVATE-ORIGINAL-TEXT", "HK-PRIVATE-DESTINATION", "fixture-merchant-B", "draft", "trace", "comparison", "12345"):
                self.assertNotIn(forbidden, raw)
            for action, body in (("approvals", self.approval_body(task)), ("stop", {}),
                                 ("answers", {"expected_state_version": task["state_version"], "base_constraints_version": 1, "answers": {"cash_cap_minor": 20000}}),
                                 ("runs", {"expected_state_version": task["state_version"]})):
                denied = self.request("POST", "/api/v1/tasks/" + task["task_id"] + "/" + action,
                                      body, identity=identity, key="nonbuyer-mutation-" + action)
                self.assertEqual((denied[0], denied[1]["error"]), (403, "BUYER_ROLE_REQUIRED"))
        hidden = self.request("GET", "/api/v1/tasks/" + task["task_id"], identity=self.identities["merchant-b"])
        self.assertEqual(hidden[0], 404)
        listing = self.request("GET", "/api/v1/tasks", identity=self.identities["merchant-b"])
        self.assertEqual(listing[1]["tasks"], [])

    def test_csrf_host_origin_checks_precede_mutations(self):
        task = self.create(); path = "/api/v1/tasks/" + task["task_id"] + "/stop"
        missing = self.request("POST", path, {}, identity=self.buyer, key="csrf-check-key", csrf=False)
        self.assertEqual((missing[0], missing[1]["error"]), (403, "CSRF_REJECTED"))
        wrong = self.request("POST", path, {}, identity=self.buyer, key="csrf-check-key",
                             headers={"X-CSRF-Token": "incorrect"})
        self.assertEqual((wrong[0], wrong[1]["error"]), (403, "CSRF_REJECTED"))
        for headers, error in (({"Host": "attacker.invalid"}, "HOST_REJECTED"),
                               ({"Origin": "https://attacker.invalid"}, "ORIGIN_REJECTED"),
                               ({"Sec-Fetch-Site": "cross-site"}, "CROSS_SITE_REJECTED")):
            response = self.request("POST", path, {}, identity=self.buyer, key="csrf-check-key", headers=headers)
            self.assertEqual((response[0], response[1]["error"]), (403, error))
        unchanged = self.request("GET", "/api/v1/tasks/" + task["task_id"], identity=self.buyer)
        self.assertEqual(unchanged[1]["state_version"], task["state_version"])

    def test_expired_and_revoked_session_cannot_read_cached_mutation(self):
        task = self.create(); path = "/api/v1/tasks/" + task["task_id"] + "/stop"
        accepted = self.request("POST", path, {}, identity=self.buyer, key="session-cached-stop")
        self.assertEqual(accepted[0], 200)
        with self.store.transaction() as conn:
            conn.execute("UPDATE app_sessions SET expires_at=? WHERE session_hash=?", (self.store.now() - 1, hashed(self.buyer["token"])))
        denied = self.request("POST", path, {}, identity=self.buyer, key="session-cached-stop")
        self.assertEqual((denied[0], denied[1]["error"]), (401, "SESSION_EXPIRED"))
        with self.store.transaction() as conn:
            conn.execute("UPDATE app_access_codes SET active=0 WHERE code_hash=?", (hashed(self.identities["buyer-b"]["code"]),))
        denied = self.request("GET", "/api/v1/tasks", identity=self.identities["buyer-b"])
        self.assertEqual(denied[0], 401)

    def test_session_role_is_server_derived_and_rechecked(self):
        forged = self.request("POST", "/api/v1/session", {"access_code": self.buyer["code"], "role": "operator"})
        self.assertEqual((forged[0], forged[1]["error"]), (400, "ACCESS_CODE_ONLY"))
        task = self.create()
        with self.store.transaction() as conn:
            conn.execute("UPDATE app_access_codes SET role='operator' WHERE code_hash=?", (hashed(self.buyer["code"]),))
        identity = self.request("GET", "/api/v1/session", identity=self.buyer)
        self.assertEqual(identity[1]["actor"]["role"], "operator")
        denied = self.request("POST", "/api/v1/tasks/" + task["task_id"] + "/stop", {},
                              identity=self.buyer, key="server-role-recheck")
        self.assertEqual(denied[0], 403)

    def test_stale_answers_conflict_and_replay_does_not_increment_again(self):
        task = self.create()
        body = {"expected_state_version": task["state_version"], "base_constraints_version": task["constraints_version"],
                "answers": {"cash_cap_minor": 20000}}
        path = "/api/v1/tasks/" + task["task_id"] + "/answers"
        accepted = self.request("POST", path, body, identity=self.buyer, key="answer-first-key")
        self.assertEqual(accepted[0], 200, accepted[1])
        replay = self.request("POST", path, body, identity=self.buyer, key="answer-first-key")
        self.assertEqual(replay[1], accepted[1])
        conflict = self.request("POST", path, body, identity=self.buyer, key="answer-other-key")
        self.assertEqual((conflict[0], conflict[1]["error"]), (409, "STATE_VERSION_CONFLICT"))
        latest = self.request("GET", "/api/v1/tasks/" + task["task_id"], identity=self.buyer)[1]
        self.assertEqual(latest["constraints_version"], task["constraints_version"] + 1)

    def test_restart_keeps_session_task_and_requires_authority_to_resume(self):
        task = self.create()
        run = self.server.service.claim_run("before-restart-worker")
        self.assertTrue(self.server.service.finish_run(run, {"status": "blocked", "draft": run["draft"],
                      "model_status": "blocked", "reason": "TEST_INTERRUPTION", "missing_fields": [], "questions": []}))
        prior = self.request("GET", "/api/v1/tasks/" + task["task_id"], identity=self.buyer)[1]
        self._stop_server()
        self.store = TaskStore(self.db_path, clock=lambda: self.clock[0])
        self._start_server()
        who = self.request("GET", "/api/v1/session", identity=self.buyer)
        self.assertEqual(who[0], 200)
        self.assertEqual(who[1]["actor"], self.buyer["actor"])
        persisted = self.request("GET", "/api/v1/tasks/" + task["task_id"], identity=self.buyer)
        self.assertEqual(persisted[1]["draft"], prior["draft"])
        path = "/api/v1/tasks/" + task["task_id"] + "/runs"
        body = {"expected_state_version": persisted[1]["state_version"]}
        denied = self.request("POST", path, body, identity=self.identities["buyer-b"], key="resume-after-restart")
        self.assertEqual(denied[0], 404)
        resumed = self.request("POST", path, body, identity=self.buyer, key="resume-after-restart")
        self.assertEqual(resumed[0], 200, resumed[1])
        self.assertNotEqual(resumed[1]["run"]["run_id"], run["run_id"])
        replay = self.request("POST", path, body, identity=self.buyer, key="resume-after-restart")
        self.assertEqual(replay[1], resumed[1])

    def test_login_cookie_and_logout_are_protected(self):
        login = self.request("POST", "/api/v1/session", {"access_code": self.buyer["code"]})
        self.assertIn("HttpOnly", login[2]["set-cookie"])
        self.assertIn("SameSite=Strict", login[2]["set-cookie"])
        refused = self.request("DELETE", "/api/v1/session", identity=self.buyer, csrf=False)
        self.assertEqual(refused[0], 403)
        accepted = self.request("DELETE", "/api/v1/session", identity=self.buyer)
        self.assertEqual(accepted[0], 200)
        self.assertEqual(self.request("GET", "/api/v1/session", identity=self.buyer)[0], 401)


if __name__ == "__main__":
    unittest.main()
