"""Explicit weak local-demo credentials, isolated identities, and private images.

Only temporary databases and synthetic JPEG bytes are used. All HTTP servers
bind loopback and no model/provider request is needed by this suite.
"""
import copy
import http.client
from http.cookies import SimpleCookie
import json
import os
from pathlib import Path
import queue
import re
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

ENGINEERING = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ENGINEERING))
from app.auth import APIError, Auth, hashed
from app.agent_worker import AgentWorker
from app.local_demo_auth import LOCAL_DEMO_BINDINGS, provision_short_demo_accounts
from app.task_api import ApplicationServer
from app.task_store import TaskStore
from slice04.fixtures import V1_PRODUCT


class LocalDemoAuthTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="maiyebang-short-demo-")
        self.addCleanup(self.temp.cleanup)
        self.store = TaskStore(Path(self.temp.name) / "demo.sqlite")
        self.auth = Auth(self.store)

    def enable(self):
        provision_short_demo_accounts(self.store, bind_address="127.0.0.1")
        self.auth.enable_local_demo_short_codes(bind_address="127.0.0.1")

    def invalid_login(self, code, peer="127.0.0.1"):
        with self.assertRaises(APIError) as error:
            self.auth.login(code, peer_address=peer)
        self.assertEqual(error.exception.code, "INVALID_ACCESS_CODE")

    def test_default_disabled_even_when_rows_exist(self):
        provision_short_demo_accounts(self.store, bind_address="127.0.0.1")
        self.assertFalse(self.auth.local_demo_short_codes_enabled)
        for code in LOCAL_DEMO_BINDINGS:
            self.invalid_login(code)

    def test_only_literal_loopback_binding_and_peer_are_allowed(self):
        for binding in ("0.0.0.0", "192.0.2.1", "::", "localhost", None):
            with self.subTest(binding=binding):
                with self.assertRaises(ValueError):
                    self.auth.enable_local_demo_short_codes(bind_address=binding)
                with self.assertRaises(ValueError):
                    provision_short_demo_accounts(self.store, bind_address=binding)
        self.enable()
        for peer in ("198.51.100.2", "0.0.0.0", "localhost", "127.0.0.1.evil.invalid", None, ""):
            with self.subTest(peer=peer):
                self.invalid_login("0001", peer)

    def test_general_auth_length_checks_and_long_codes_are_unchanged(self):
        self.enable()
        for code in (None, 1, "0000", "0003", "1", " 0001", "0001 ", "x" * 15, "x" * 201):
            self.invalid_login(code)
        private = self.auth.provision(actor_id="regular-buyer")
        _, session = self.auth.login(private)
        self.assertEqual(session["actor"]["actor_id"], "regular-buyer")
        self.assertGreaterEqual(len(private), 16)

    def test_two_buyers_are_stable_separate_and_only_hashes_are_stored(self):
        self.enable()
        first = provision_short_demo_accounts(self.store, bind_address="127.0.0.1")
        self.assertEqual(first, provision_short_demo_accounts(self.store, bind_address="127.0.0.1"))
        sessions = [self.auth.login(code, peer_address="127.0.0.1") for code in LOCAL_DEMO_BINDINGS]
        self.assertNotEqual(sessions[0][0], sessions[1][0])
        self.assertNotEqual(sessions[0][1]["csrf_token"], sessions[1][1]["csrf_token"])
        self.assertNotEqual(sessions[0][1]["actor"]["actor_id"], sessions[1][1]["actor"]["actor_id"])
        with self.store.connection() as conn:
            rows = [dict(row) for row in conn.execute("SELECT * FROM app_access_codes")]
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(row["role"] == "buyer" and row["merchant_id"] is None for row in rows))
        self.assertEqual({row["code_hash"] for row in rows}, {hashed(code) for code in LOCAL_DEMO_BINDINGS})
        self.assertNotIn("access_code", rows[0])

    def test_disabled_buyer_is_not_revived_and_session_is_rejected(self):
        self.enable()
        token, _ = self.auth.login("0001", peer_address="127.0.0.1")
        with self.store.transaction() as conn:
            conn.execute("UPDATE app_access_codes SET active=0 WHERE code_hash=?", (hashed("0001"),))
        with self.assertRaises(ValueError):
            provision_short_demo_accounts(self.store, bind_address="127.0.0.1")
        self.invalid_login("0001")
        with self.assertRaises(APIError):
            self.auth.session(token, peer_address="127.0.0.1")
        with self.store.connection() as conn:
            self.assertEqual(conn.execute("SELECT active FROM app_access_codes WHERE code_hash=?", (hashed("0001"),)).fetchone()[0], 0)

    def test_role_actor_tenant_or_merchant_mismatch_never_grants_privilege(self):
        self.enable()
        token, _ = self.auth.login("0001", peer_address="127.0.0.1")
        for field, value in (("role", "operator"), ("role", "merchant"), ("actor_id", "other-user"),
                             ("tenant_id", "other-tenant"), ("merchant_id", "merchant-1")):
            with self.subTest(field=field, value=value):
                with self.store.transaction() as conn:
                    conn.execute("UPDATE app_access_codes SET actor_id=?,tenant_id='local-hk',role='buyer',merchant_id=NULL WHERE code_hash=?",
                                 (LOCAL_DEMO_BINDINGS["0001"], hashed("0001")))
                    conn.execute("UPDATE app_access_codes SET " + field + "=? WHERE code_hash=?", (value, hashed("0001")))
                with self.assertRaises(ValueError):
                    provision_short_demo_accounts(self.store, bind_address="127.0.0.1")
                self.invalid_login("0001")
                with self.assertRaises(APIError):
                    self.auth.session(token, peer_address="127.0.0.1")

    def test_conflict_provision_is_atomic_and_reserved_actor_is_not_replaced(self):
        self.auth.provision(actor_id=LOCAL_DEMO_BINDINGS["0002"])
        with self.assertRaises(ValueError):
            provision_short_demo_accounts(self.store, bind_address="127.0.0.1")
        with self.store.connection() as conn:
            rows = conn.execute("SELECT code_hash FROM app_access_codes").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertNotIn(rows[0][0], {hashed("0001"), hashed("0002")})

    def test_session_requires_enable_and_local_peer_after_store_reopen(self):
        self.enable()
        token, first = self.auth.login("0001", peer_address="127.0.0.1")
        reopened = Auth(TaskStore(self.store.path))
        with self.assertRaises(APIError):
            reopened.session(token, peer_address="127.0.0.1")
        reopened.enable_local_demo_short_codes(bind_address="127.0.0.1")
        self.assertEqual(reopened.session(token, peer_address="127.0.0.1"), first)
        for peer in (None, "198.51.100.2"):
            with self.assertRaises(APIError):
                reopened.session(token, peer_address=peer)


class PeerTestServer(ApplicationServer):
    forced_peer = None

    def get_request(self):
        connection, address = super().get_request()
        return connection, (self.forced_peer, address[1]) if self.forced_peer else address


class LocalDemoHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="maiyebang-short-http-")
        self.clock = [0]
        self.store = TaskStore(Path(self.temp.name) / "demo.sqlite", clock=lambda: int(time.time()) + self.clock[0])
        provision_short_demo_accounts(self.store, bind_address="127.0.0.1")
        self.server = PeerTestServer(("127.0.0.1", 0), self.store, start_worker=False)
        self.server.auth.enable_local_demo_short_codes(bind_address=self.server.server_address[0])
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        self.thread.start()
        self.port = self.server.server_address[1]
        self.image = Path(self.temp.name) / "payment-demo-contact.jpg"
        self.synthetic_jpeg = b"\xff\xd8\xff\xe0TEST-ONLY-NO-PERSONAL-IMAGE\xff\xd9"

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join(timeout=3)
        self.temp.cleanup()

    def request(self, method, path, body=None, identity=None, headers=None):
        request_headers = {"Origin": f"http://127.0.0.1:{self.port}"}
        if body is not None:
            request_headers["Content-Type"] = "application/json"
        if identity:
            request_headers.update(Cookie=identity["cookie"])
            if method != "GET":
                request_headers["X-CSRF-Token"] = identity["csrf"]
        request_headers.update(headers or {})
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            connection.request(method, path, json.dumps(body) if body is not None else None, request_headers)
            response = connection.getresponse(); raw = response.read()
            data = raw if response.getheader("Content-Type", "").startswith("image/") else json.loads(raw)
            return response.status, data, {k.lower(): v for k, v in response.getheaders()}
        finally:
            connection.close()

    def login(self, code="0001"):
        status, result, headers = self.request("POST", "/api/v1/session", {"access_code": code})
        self.assertEqual(status, 200, result)
        cookie = SimpleCookie(); cookie.load(headers["set-cookie"])
        return {"cookie": "hacku_session=" + cookie["hacku_session"].value,
                "csrf": result["csrf_token"], "actor": result["actor"]}

    def create(self, identity, *, key="local-demo-create-0001"):
        fields = {"product": copy.deepcopy(V1_PRODUCT), "purchase_quantity": 1, "cash_cap_minor": 6000,
                  "destination_ref": "HK-DEMO-KOWLOON", "preference": "lowest_cost"}
        status, task, _ = self.request("POST", "/api/v1/intents",
            {"text": "TEST-ONLY explicit synthetic request", "fields": fields, "mode": "scripted"},
            identity, {"Idempotency-Key": key})
        self.assertEqual(status, 200, task)
        return task

    def ready(self, identity):
        created = self.create(identity)
        with patch("urllib.request.urlopen", side_effect=AssertionError("External network forbidden")):
            self.assertTrue(AgentWorker(self.server.service).once())
        task = self.server.service.get(identity["actor"], created["task_id"])
        self.assertEqual(task["status"], "AWAITING_APPROVAL")
        return task

    @staticmethod
    def route(task):
        return "/api/v1/tasks/" + task["task_id"] + "/demo-payment-image"

    def test_short_login_and_config_are_default_closed_on_a_standard_server(self):
        self.server.auth = Auth(self.store)
        self.assertFalse(self.request("GET", "/api/v1/config")[1]["local_demo_short_codes_enabled"])
        self.assertEqual(self.request("POST", "/api/v1/session", {"access_code": "0001"})[0], 401)

    def test_host_origin_cross_site_and_forwarding_headers_cannot_enable_login(self):
        for headers, expected in (({"Host": "rebinding.evil.invalid"}, 403),
                ({"Origin": "https://evil.invalid"}, 403), ({"Sec-Fetch-Site": "cross-site"}, 403),
                ({"X-Forwarded-For": "127.0.0.1"}, 401), ({"Forwarded": "for=127.0.0.1"}, 401)):
            with self.subTest(headers=headers):
                self.assertEqual(self.request("POST", "/api/v1/session", {"access_code": "0001"}, headers=headers)[0], expected)

    def test_actual_socket_peer_overrides_host_and_forwarding_claims(self):
        identity = self.login()
        self.server.forced_peer = "198.51.100.9"
        self.assertEqual(self.request("POST", "/api/v1/session", {"access_code": "0001"})[0], 401)
        self.assertEqual(self.request("GET", "/api/v1/session", identity=identity)[0], 401)
        self.assertEqual(self.request("POST", "/api/v1/session", {"access_code": "0001"},
                                      headers={"X-Forwarded-For": "127.0.0.1"})[0], 401)

    def test_public_interface_binding_is_refused(self):
        for binding in ("0.0.0.0", "192.0.2.1", "::"):
            with self.assertRaises(ValueError):
                ApplicationServer((binding, 0), self.store, start_worker=False)

    def test_browser_cannot_request_a_role_or_actor(self):
        for extra in ({"role": "operator"}, {"actor_id": LOCAL_DEMO_BINDINGS["0002"]}):
            self.assertEqual(self.request("POST", "/api/v1/session", {"access_code": "0001", **extra})[0], 400)

    def test_two_short_buyers_and_regular_buyer_have_separate_task_access(self):
        identities = [self.login("0001"), self.login("0002")]
        normal = self.server.auth.provision(actor_id="normal-http-buyer")
        identities.append(self.login(normal))
        tasks = [self.create(identity) for identity in identities]
        for index, identity in enumerate(identities):
            listed = self.request("GET", "/api/v1/tasks", identity=identity)[1]["tasks"]
            self.assertEqual([task["task_id"] for task in listed], [tasks[index]["task_id"]])
            for other, task in enumerate(tasks):
                if other == index:
                    continue
                path = "/api/v1/tasks/" + task["task_id"]
                self.assertEqual(self.request("GET", path, identity=identity)[0], 404)
                self.assertEqual(self.request("POST", path + "/stop", {}, identity,
                    {"Idempotency-Key": "foreign-stop-request"})[0], 404)

    def test_short_sessions_keep_csrf_and_cookie_protections(self):
        status, result, headers = self.request("POST", "/api/v1/session", {"access_code": "0001"})
        self.assertEqual(status, 200)
        for attribute in ("HttpOnly", "SameSite=Strict", "Path=/"):
            self.assertIn(attribute, headers["set-cookie"])
        identity = self.login()
        self.assertEqual(self.request("DELETE", "/api/v1/session", identity=identity,
                                      headers={"X-CSRF-Token": "incorrect"})[0], 403)

    def test_private_image_requires_login_owner_and_existing_file(self):
        first, second = self.login("0001"), self.login("0002")
        task = self.ready(first); route = self.route(task)
        self.assertEqual(self.request("GET", route)[0], 401)
        self.assertEqual(self.request("GET", route, identity=second)[0], 404)
        self.assertEqual(self.request("GET", route, identity=first)[0], 404)
        self.image.write_bytes(self.synthetic_jpeg)
        status, raw, headers = self.request("GET", route, identity=first)
        self.assertEqual(status, 200)
        self.assertEqual(raw, self.synthetic_jpeg)
        self.assertEqual(headers["content-type"], "image/jpeg")
        self.assertEqual(headers["cache-control"], "no-store")
        self.assertEqual(self.request("GET", "/payment-demo-contact.jpg", identity=first)[0], 404)

    def test_image_rejects_stopped_task_and_never_creates_payment(self):
        identity = self.login(); task = self.ready(identity); self.image.write_bytes(self.synthetic_jpeg)
        self.assertEqual(self.request("GET", self.route(task), identity=identity)[0], 200)
        self.assertIsNone(self.server.service.get(identity["actor"], task["task_id"])["operation"])
        status, _, _ = self.request("POST", "/api/v1/tasks/" + task["task_id"] + "/stop", {}, identity,
                                    {"Idempotency-Key": "stop-before-image"})
        self.assertEqual(status, 200)
        self.assertEqual(self.request("GET", self.route(task), identity=identity)[0], 404)

    def test_image_rejects_missing_proposal_expiry_and_live_mode(self):
        identity = self.login(); task = self.create(identity); self.image.write_bytes(self.synthetic_jpeg)
        self.assertEqual(self.request("GET", self.route(task), identity=identity)[0], 404)
        self.assertTrue(AgentWorker(self.server.service).once())
        self.assertEqual(self.request("GET", self.route(task), identity=identity)[0], 200)
        with self.store.transaction() as conn:
            conn.execute("UPDATE app_tasks SET mode='live' WHERE task_id=?", (task["task_id"],))
        self.assertEqual(self.request("GET", self.route(task), identity=identity)[0], 404)
        with self.store.transaction() as conn:
            conn.execute("UPDATE app_tasks SET mode='scripted' WHERE task_id=?", (task["task_id"],))
        self.clock[0] += 301
        self.assertEqual(self.request("GET", self.route(task), identity=identity)[0], 404)

    def test_image_rejects_tampered_snapshot_and_unsupported_paths_or_queries(self):
        identity = self.login(); task = self.ready(identity); self.image.write_bytes(self.synthetic_jpeg)
        route = self.route(task)
        self.assertEqual(self.request("GET", route + "?file=other.jpg", identity=identity)[0], 400)
        self.assertEqual(self.request("GET", route + "/../payment-demo-contact.jpg", identity=identity)[0], 404)
        proposal = copy.deepcopy(task["proposal"]); proposal["snapshot"]["calculation"]["cash_minor"] += 1
        with self.store.transaction() as conn:
            conn.execute("UPDATE app_tasks SET proposal_json=? WHERE task_id=?", (json.dumps(proposal), task["task_id"]))
        self.assertEqual(self.request("GET", route, identity=identity)[0], 404)

    def test_image_rejects_oversize_and_non_jpeg_bytes(self):
        identity = self.login(); task = self.ready(identity)
        for raw in (b"<svg>not-jpeg</svg>", b"\xff\xd8\xff" + b"x" * (3 * 1024 * 1024) + b"\xff\xd9"):
            self.image.write_bytes(raw)
            self.assertEqual(self.request("GET", self.route(task), identity=identity)[0], 404)

    def test_regular_buyer_operator_and_legacy_demo_cannot_read_contact_image(self):
        self.image.write_bytes(self.synthetic_jpeg)
        for actor_id, role in (("regular", "buyer"), ("local-demo-buyer", "buyer"), ("operator", "operator")):
            identity = self.login(self.server.auth.provision(actor_id=actor_id, role=role))
            if role == "buyer":
                task = self.ready(identity)
            else:
                task = self.ready(self.login())
            self.assertEqual(self.request("GET", self.route(task), identity=identity)[0], 404)


class LocalDemoLauncherTests(unittest.TestCase):
    def test_explicit_cli_flag_and_persistent_session_across_real_processes(self):
        launcher = ENGINEERING.parents[1] / "scripts/run-local.py"
        with tempfile.TemporaryDirectory(prefix="maiyebang-short-process-") as temporary:
            environment = dict(os.environ)
            environment.pop("DEEPSEEK_API_KEY", None)
            token = None
            for enabled in (False, True, False, True):
                arguments = [sys.executable, str(launcher), "--port", "0", "--state-dir", temporary]
                if enabled:
                    arguments.append("--enable-demo-short-codes")
                process = subprocess.Popen(arguments, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                    encoding="utf-8", env=environment, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                try:
                    lines = queue.Queue()
                    reader = threading.Thread(target=lambda: lines.put(process.stdout.readline()), daemon=True)
                    reader.start()
                    line = lines.get(timeout=15)
                    match = re.search(r"http://127\.0\.0\.1:(\d+)", line)
                    self.assertIsNotNone(match, line)
                    port = int(match.group(1))
                    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                    try:
                        connection.request("GET", "/api/v1/config")
                        response = connection.getresponse(); config = json.loads(response.read())
                        self.assertEqual(config["local_demo_short_codes_enabled"], enabled)
                        if token:
                            connection.request("GET", "/api/v1/session", headers={"Cookie": token})
                            response = connection.getresponse(); response.read()
                            self.assertEqual(response.status, 200 if enabled else 401)
                        connection.request("POST", "/api/v1/session", json.dumps({"access_code": "0001"}),
                                           {"Content-Type": "application/json"})
                        response = connection.getresponse(); result = json.loads(response.read())
                        self.assertEqual(response.status, 200 if enabled else 401, result)
                        if enabled and token is None:
                            token = response.getheader("Set-Cookie").split(";", 1)[0]
                    finally:
                        connection.close()
                finally:
                    process.terminate(); process.communicate(timeout=10)


if __name__ == "__main__":
    unittest.main(verbosity=2)
