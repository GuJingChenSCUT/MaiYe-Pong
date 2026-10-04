"""Local fault injection only; does not close the historical corruption incident."""
from __future__ import annotations

import copy
import concurrent.futures
import http.client
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.agent_worker import AgentWorker
from app.auth import Auth
from app.task_api import ApplicationServer
from app.task_service import TaskService
from app.task_store import StorageUnavailable, TaskStore
from slice04.fixtures import V1_PRODUCT


class StorageReliabilityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "fault-injection-only.sqlite3"
        self.store = TaskStore(self.path)
        self.service = TaskService(self.store)
        self.actor = {"actor_id": "buyer", "tenant_id": "test", "role": "buyer"}

    def create(self, key="create-fault-test"):
        return self.service.mutation(self.actor, "create", None, key, {
            "text": "同規格日用品", "mode": "scripted", "scenario": "normal",
            "fields": {"product": copy.deepcopy(V1_PRODUCT), "purchase_quantity": 1,
                       "cash_cap_minor": 6000, "destination_ref": "HK-DEMO-KOWLOON",
                       "preference": "lowest_cost"}})

    def assert_no_authority(self):
        with self.store.connection() as conn:
            for table in ("s1_snapshots", "s1_mandates", "s1_operations", "s1_dispatch_commands"):
                self.assertEqual(conn.execute("SELECT count(*) FROM " + table).fetchone()[0], 0, table)

    def test_connection_closes_when_initial_pragma_fails(self):
        connection = MagicMock()
        connection.execute.side_effect = sqlite3.OperationalError("private-path-and-secret-sentinel")
        with patch("app.task_store.sqlite3.connect", return_value=connection):
            with self.assertRaises(StorageUnavailable) as raised:
                with self.store.connection():
                    self.fail("Failed PRAGMA must not yield a connection")
        connection.close.assert_called_once()
        self.assertEqual(str(raised.exception), "STORAGE_UNAVAILABLE")
        self.assertNotIn("sentinel", json.dumps(self.store.health()))
        self.assertEqual(self.store.health(probe=True)["status"], "available")

    def test_wal_and_full_sync_are_enforced(self):
        with self.store.connection() as conn:
            self.assertEqual(conn.execute("PRAGMA journal_mode").fetchone()[0], "wal")
            self.assertEqual(conn.execute("PRAGMA synchronous").fetchone()[0], 2)
            self.assertEqual(conn.execute("PRAGMA foreign_keys").fetchone()[0], 1)

    def test_constructor_failure_closes_its_connection(self):
        connection = MagicMock()
        connection.execute.side_effect = sqlite3.OperationalError("initialization failed")
        with patch("app.task_store.sqlite3.connect", return_value=connection):
            with self.assertRaises(StorageUnavailable):
                TaskStore(Path(self.temp.name) / "failing-initialization.sqlite")
        connection.close.assert_called_once()

    def test_missing_database_is_not_recreated(self):
        self.path.unlink()
        with self.assertRaises(StorageUnavailable):
            self.create()
        self.assertFalse(self.path.exists())
        self.assertEqual(self.store.health()["status"], "unavailable")

    def test_corrupt_database_is_quarantined_and_never_silently_replaced(self):
        task = self.create()
        original = self.path.read_bytes()
        # Only this test's disposable database is damaged; source evidence is untouched.
        damaged = b"deliberately invalid SQLite test file" * 64
        self.path.write_bytes(damaged)
        health = self.store.health(probe=True)
        self.assertEqual(health["reason"], "INTEGRITY_FAILURE")
        self.assertTrue(health["recovery_required"])
        self.assertEqual(self.path.read_bytes(), damaged)
        with self.assertRaises(StorageUnavailable):
            self.service.mutation(self.actor, "stop", task["task_id"], "stop-corrupt-test", {})
        self.path.write_bytes(original)
        self.assertEqual(self.store.health(probe=True)["status"], "unavailable")
        # Explicit new process/store only after operator recovery: no in-place auto-repair.
        restored = TaskStore(self.path)
        self.assertEqual(restored.health()["status"], "available")
        self.assertEqual(TaskService(restored).get(self.actor, task["task_id"])["status"], "PLANNING")

    def test_claim_failure_does_not_kill_worker_and_recovers_original_job(self):
        task = self.create()
        worker = AgentWorker(self.service)
        with patch.object(self.service, "claim_run", side_effect=StorageUnavailable()):
            self.assertFalse(worker.once())
            self.assertEqual(worker.health()["status"], "unavailable")
        self.assert_no_authority()
        self.assertTrue(worker.once())
        result = self.service.get(self.actor, task["task_id"])
        self.assertEqual(result["status"], "AWAITING_APPROVAL")
        self.assertEqual(result["run"]["attempts"], 1)
        self.assertEqual(worker.health()["status"], "idle")

    def test_failed_result_commit_rolls_back_and_fenced_lease_recovers(self):
        task = self.create()
        worker = AgentWorker(self.service)
        original_event = self.store.event
        def fail_finish(conn, task_id, kind, details=None):
            if kind == "model_run_finished":
                raise sqlite3.OperationalError("fault after snapshot before commit")
            return original_event(conn, task_id, kind, details)
        with patch.object(self.store, "event", side_effect=fail_finish):
            self.assertFalse(worker.once())
        self.assert_no_authority()
        before = self.service.get(self.actor, task["task_id"])
        self.assertEqual(before["status"], "PLANNING")
        self.assertEqual(before["run"]["attempts"], 1)
        self.assertFalse(worker.once())  # no retry while existing lease is valid
        with self.store.transaction() as conn:
            conn.execute("UPDATE app_runs SET lease_until=? WHERE run_id=?",
                         (self.store.now() - 1, before["run"]["run_id"]))
        self.assertTrue(worker.once())
        after = self.service.get(self.actor, task["task_id"])
        self.assertEqual(after["run"]["run_id"], before["run"]["run_id"])
        self.assertEqual(after["run"]["attempts"], 2)
        self.assertEqual(after["status"], "AWAITING_APPROVAL", after["model_result"])
        with self.store.connection() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM s1_snapshots").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT count(*) FROM s1_dispatch_commands").fetchone()[0], 0)

    def test_runner_failure_and_failure_record_error_are_both_isolated(self):
        self.create()
        runner = MagicMock(side_effect=RuntimeError("provider-secret-sentinel"))
        worker = AgentWorker(self.service, runner=runner)
        with patch.object(self.service, "finish_run", side_effect=StorageUnavailable()) as finish:
            self.assertFalse(worker.once())
        finish.assert_called_once()
        self.assertEqual(worker.health()["reason"], "STORAGE_UNAVAILABLE")
        self.assertNotIn("sentinel", json.dumps(worker.health()))
        self.assert_no_authority()

    def test_background_worker_survives_repeated_claim_errors(self):
        worker = AgentWorker(self.service)
        attempted = threading.Event()
        def fail_claim(*args):
            attempted.set()
            raise StorageUnavailable()
        with patch.object(self.service, "claim_run", side_effect=fail_claim):
            thread = threading.Thread(target=worker.serve)
            thread.start()
            try:
                self.assertTrue(attempted.wait(3))
                self.assertTrue(thread.is_alive())
                self.assertEqual(worker.health()["status"], "unavailable")
            finally:
                worker.stopping.set()
                thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(worker.health()["status"], "stopped")

    def test_busy_write_rolls_back_and_recovers_without_duplicate_job(self):
        with self.store.transaction():
            with self.assertRaises(StorageUnavailable):
                self.create()
        self.assertEqual(self.store.health(probe=True)["status"], "available")
        task = self.create()
        with self.store.connection() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM app_tasks").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT count(*) FROM app_runs").fetchone()[0], 1)
        self.assertEqual(task["status"], "PLANNING")

    def start_http(self):
        server = ApplicationServer(("127.0.0.1", 0), self.store, start_worker=False)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
        thread.start()
        def close():
            server.shutdown()
            server.server_close()
            thread.join(3)
        self.addCleanup(close)
        return server

    def request(self, server, method, path, body=None, headers=None):
        client = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
        try:
            headers = dict(headers or {})
            if body is not None:
                headers["Content-Type"] = "application/json"
            client.request(method, path, json.dumps(body) if body is not None else None, headers)
            response = client.getresponse()
            return response.status, json.loads(response.read()), dict(response.getheaders())
        finally:
            client.close()

    def test_health_and_stop_report_unavailable_without_false_stop(self):
        task = self.create()
        code = Auth(self.store).provision(actor_id="buyer", tenant_id="test", role="buyer")
        server = self.start_http()
        _, login, headers = self.request(server, "POST", "/api/v1/session", {"access_code": code})
        credentials = {"Cookie": headers["Set-Cookie"].split(";", 1)[0],
                       "X-CSRF-Token": login["csrf_token"], "Idempotency-Key": "stop-outage-test"}
        with patch("app.task_store.sqlite3.connect", side_effect=sqlite3.OperationalError("private-secret-path")):
            status, health, _ = self.request(server, "GET", "/api/v1/health")
            self.assertEqual(status, 503)
            self.assertEqual(health["storage"]["status"], "unavailable")
            status, error, _ = self.request(server, "POST", "/api/v1/tasks/" + task["task_id"] + "/stop", {}, credentials)
            self.assertEqual(status, 503)
            self.assertEqual(error["error"], "STORAGE_UNAVAILABLE")
            self.assertNotIn("private-secret", json.dumps(error) + json.dumps(health))
        self.assertEqual(self.request(server, "GET", "/api/v1/health")[0], 200)
        self.assertEqual(self.service.get(self.actor, task["task_id"])["status"], "PLANNING")
        self.assert_no_authority()
        status, stopped, _ = self.request(server, "POST", "/api/v1/tasks/" + task["task_id"] + "/stop", {}, credentials)
        self.assertEqual(status, 200)
        self.assertEqual(stopped["status"], "STOPPED")

    def test_static_content_types_ignore_windows_registry_mappings(self):
        server = self.start_http()
        with patch("mimetypes.guess_type", return_value=("text/plain", None)):
            for path, expected in (("/", "text/html"), ("/app.js", "text/javascript"), ("/styles.css", "text/css")):
                client = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
                try:
                    client.request("GET", path)
                    response = client.getresponse()
                    self.assertEqual(response.status, 200)
                    self.assertEqual(response.getheader("Content-Type"), expected + "; charset=utf-8")
                    self.assertEqual(response.getheader("X-Content-Type-Options"), "nosniff")
                    self.assertTrue(response.read())
                finally:
                    client.close()

    def test_dead_worker_blocks_new_work_but_existing_task_can_be_stopped(self):
        task = self.create()
        code = Auth(self.store).provision(actor_id="buyer", tenant_id="test", role="buyer")
        server = self.start_http()
        _, login, headers = self.request(server, "POST", "/api/v1/session", {"access_code": code})
        credentials = {"Cookie": headers["Set-Cookie"].split(";", 1)[0],
                       "X-CSRF-Token": login["csrf_token"], "Idempotency-Key": "worker-down-test"}
        server.worker = AgentWorker(server.service)
        server.worker_thread = MagicMock()
        server.worker_thread.is_alive.return_value = False
        status, health, _ = self.request(server, "GET", "/api/v1/health")
        self.assertEqual(status, 503)
        self.assertEqual(health["worker"]["reason"], "WORKER_NOT_RUNNING")
        status, error, _ = self.request(server, "POST", "/api/v1/intents", {"text": "do not queue this"}, credentials)
        self.assertEqual((status, error["error"]), (503, "WORKER_UNAVAILABLE"))
        status, stopped, _ = self.request(server, "POST", "/api/v1/tasks/" + task["task_id"] + "/stop", {}, credentials)
        self.assertEqual((status, stopped["status"]), (200, "STOPPED"))
        self.assert_no_authority()

    def test_concurrent_create_stop_with_worker_survives_reopen(self):
        worker = AgentWorker(self.service)
        thread = threading.Thread(target=worker.serve)
        thread.start()
        try:
            def create_stop(index):
                task = self.create("concurrent-create-%03d" % index)
                stopped = self.service.mutation(self.actor, "stop", task["task_id"],
                                                "concurrent-stop-%03d" % index, {})
                self.assertEqual(stopped["status"], "STOPPED")
                return task["task_id"]
            with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
                task_ids = list(pool.map(create_stop, range(24)))
            self.assertTrue(thread.is_alive())
        finally:
            worker.stopping.set()
            thread.join(10)
        self.assertFalse(thread.is_alive())
        reopened = TaskStore(self.path)
        self.assertEqual(reopened.health(probe=True)["status"], "available")
        service = TaskService(reopened)
        for task_id in task_ids:
            self.assertEqual(service.get(self.actor, task_id)["status"], "STOPPED")
        with reopened.connection() as conn:
            self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(conn.execute("SELECT count(*) FROM app_tasks").fetchone()[0], 24)
            self.assertEqual(conn.execute("SELECT count(*) FROM s1_dispatch_commands").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
