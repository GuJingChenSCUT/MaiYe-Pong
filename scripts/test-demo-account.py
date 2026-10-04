"""Disposable-database checks for the dedicated local buyer login helper.

Never reads the workspace's real local_state or displays a real credential.
Run with checkpoint/engineering/.venv/Scripts/python.exe scripts/test-demo-account.py.
"""
from __future__ import annotations

import concurrent.futures
from contextlib import closing
import copy
import importlib.util
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "checkpoint/engineering"))
from app.auth import APIError, Auth
from app.task_service import TaskService
from app.task_store import TaskStore
from slice04.fixtures import V1_PRODUCT

SPEC = importlib.util.spec_from_file_location("local_demo_account", ROOT / "scripts/demo-account.py")
DEMO = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(DEMO)


class DemoAccountTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="maiyebang-demo-test-")
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.db = self.directory / "maiyebang.sqlite"
        self.record_path = self.directory / "demo_account_private.json"
        self.store = TaskStore(self.db)

    def rows(self, directory=None):
        db = (directory or self.directory) / "maiyebang.sqlite"
        with closing(sqlite3.connect(db)) as conn:
            return conn.execute("SELECT * FROM app_access_codes ORDER BY code_hash").fetchall()

    def cli(self, directory=None):
        return subprocess.run([sys.executable, str(ROOT / "scripts/demo-account.py"),
                               "--state-dir", str(directory or self.directory)],
                              text=True, encoding="utf-8", capture_output=True, timeout=15)

    def test_missing_database_does_not_create_directory_or_database(self):
        absent = self.directory / "never-created"
        with self.assertRaises(RuntimeError):
            DEMO.provision(absent)
        self.assertFalse(absent.exists())
        self.assertEqual(self.rows(), [])

    def test_existing_empty_database_is_not_bootstrapped_or_given_an_account(self):
        empty = self.directory / "empty-existing"
        empty.mkdir(); (empty / "maiyebang.sqlite").touch()
        with self.assertRaises(sqlite3.Error):
            DEMO.provision(empty)
        self.assertFalse((empty / "demo_account_private.json").exists())
        with closing(sqlite3.connect(empty / "maiyebang.sqlite")) as conn:
            self.assertEqual(conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall(), [])

    def test_two_runs_reuse_the_same_code_and_buyer_identity(self):
        first = DEMO.provision(self.directory)
        original_bytes = self.record_path.read_bytes()
        original_rows = self.rows()
        second = DEMO.provision(self.directory)
        self.assertEqual(first, second)
        self.assertEqual(self.record_path.read_bytes(), original_bytes)
        self.assertEqual(self.rows(), original_rows)
        self.assertEqual(len(original_rows), 1)
        _, session = Auth(self.store).login(first["access_code"])
        self.assertEqual(session["actor"], {"tenant_id": DEMO.TENANT, "actor_id": DEMO.ACTOR, "role": "buyer"})
        self.assertNotIn("merchant_id", session["actor"])

    def test_disabled_account_is_not_reenabled_or_replaced(self):
        DEMO.provision(self.directory)
        with self.store.transaction() as conn:
            conn.execute("UPDATE app_access_codes SET active=0 WHERE actor_id=?", (DEMO.ACTOR,))
        before = self.rows(); private = self.record_path.read_bytes()
        with self.assertRaises(RuntimeError):
            DEMO.provision(self.directory)
        self.assertEqual(self.rows(), before)
        self.assertEqual(self.record_path.read_bytes(), private)

    def test_role_merchant_scope_actor_and_tenant_mismatch_are_not_repaired(self):
        DEMO.provision(self.directory)
        for column, value in (("role", "operator"), ("role", "merchant"), ("merchant_id", "foreign-shop"),
                               ("actor_id", "other-buyer"), ("tenant_id", "other-tenant")):
            with self.subTest(column=column, value=value):
                with self.store.transaction() as conn:
                    conn.execute("UPDATE app_access_codes SET actor_id=?,tenant_id=?,role='buyer',merchant_id=NULL", (DEMO.ACTOR, DEMO.TENANT))
                    conn.execute("UPDATE app_access_codes SET " + column + "=?", (value,))
                before = self.rows()
                with self.assertRaises(RuntimeError):
                    DEMO.provision(self.directory)
                self.assertEqual(self.rows(), before)

    def test_saved_code_from_another_database_never_creates_a_replacement_actor(self):
        DEMO.provision(self.directory)
        other = self.directory / "other-state"; other.mkdir()
        TaskStore(other / "maiyebang.sqlite")
        (other / "demo_account_private.json").write_bytes(self.record_path.read_bytes())
        with self.assertRaises(RuntimeError):
            DEMO.provision(other)
        self.assertEqual(self.rows(other), [])
        self.assertEqual(len(self.rows()), 1)

    def test_existing_actor_without_credential_file_requires_explicit_recovery(self):
        Auth(self.store).provision(actor_id=DEMO.ACTOR, tenant_id=DEMO.TENANT, role="buyer")
        before = self.rows()
        with self.assertRaises(RuntimeError):
            DEMO.provision(self.directory)
        self.assertEqual(self.rows(), before)
        self.assertFalse(self.record_path.exists())
        self.assertFalse((self.directory / "demo_account_private.txt").exists())

    def test_invalid_saved_code_is_rejected_without_creating_an_identity(self):
        for code in (None, "too-short", 12345, "x" * 201):
            with self.subTest(kind=type(code).__name__):
                self.record_path.write_text(json.dumps({"access_code": code}), encoding="utf-8")
                with self.assertRaises(RuntimeError):
                    DEMO.provision(self.directory)
                self.assertEqual(self.rows(), [])

    def test_malformed_json_shape_reports_a_controlled_cli_error(self):
        for value in ([], "TEST-ONLY-not-an-object", 42, None):
            with self.subTest(kind=type(value).__name__):
                self.record_path.write_text(json.dumps(value), encoding="utf-8")
                result = self.cli()
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("Demo account unavailable", result.stderr)
                self.assertNotIn("Traceback", result.stderr)
                self.assertNotIn("TEST-ONLY-not-an-object", result.stdout + result.stderr)
                self.assertEqual(self.rows(), [])

    def test_corrupted_database_is_preserved_and_no_private_file_is_created(self):
        corrupt = self.directory / "corrupt-state"; corrupt.mkdir()
        path = corrupt / "maiyebang.sqlite"
        data = b"TEST-ONLY invalid database, never a real incident artifact" * 128
        path.write_bytes(data)
        with self.assertRaises(sqlite3.Error):
            DEMO.provision(corrupt)
        self.assertEqual(path.read_bytes(), data)
        self.assertFalse((corrupt / "demo_account_private.json").exists())

    def test_cli_never_prints_access_code_and_keeps_identity_stable_across_processes(self):
        first = self.cli(); self.assertEqual(first.returncode, 0, first.stderr)
        record = json.loads(self.record_path.read_text(encoding="utf-8"))
        second = self.cli(); self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(record, json.loads(self.record_path.read_text(encoding="utf-8")))
        for result in (first, second):
            self.assertNotIn(record["access_code"], result.stdout + result.stderr)
            self.assertEqual(json.loads(result.stdout), {"account": "demo-buyer", "role": "buyer", "status": "ready", "code_printed": False})
        self.assertEqual(len(self.rows()), 1)

    def test_concurrent_provisioning_returns_one_durable_identity(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            records = list(executor.map(lambda _: DEMO.provision(self.directory), range(2)))
        self.assertEqual(records[0], records[1])
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(records[0], json.loads(self.record_path.read_text(encoding="utf-8")))

    def test_credential_file_write_failure_rolls_back_new_identity(self):
        original = Path.open
        def reject_file(path, *args, **kwargs):
            if path == self.record_path:
                raise PermissionError("TEST-ONLY denied private record write")
            return original(path, *args, **kwargs)
        with patch.object(Path, "open", reject_file):
            with self.assertRaises(PermissionError):
                DEMO.provision(self.directory)
        self.assertEqual(self.rows(), [])
        self.assertFalse(self.record_path.exists())
        DEMO.provision(self.directory)
        self.assertEqual(len(self.rows()), 1)

    def test_normal_buyer_and_demo_buyer_cannot_read_or_stop_each_others_tasks(self):
        demo_record = DEMO.provision(self.directory)
        auth = Auth(self.store)
        _, demo = auth.login(demo_record["access_code"])
        normal_code = auth.provision(actor_id="normal-test-buyer", role="buyer")
        _, normal = auth.login(normal_code)
        service = TaskService(self.store)
        body = {"text": "日用品演練", "mode": "scripted", "scenario": "normal", "fields": {
            "product": copy.deepcopy(V1_PRODUCT), "purchase_quantity": 1, "cash_cap_minor": 6000,
            "destination_ref": "HK-DEMO-KOWLOON", "preference": "lowest_cost"}}
        demo_task = service.mutation(demo["actor"], "create", None, "create-demo-owned", body)
        normal_task = service.mutation(normal["actor"], "create", None, "create-normal-owned", body)
        self.assertNotEqual(demo["actor"]["actor_id"], normal["actor"]["actor_id"])
        for caller, foreign in ((demo["actor"], normal_task), (normal["actor"], demo_task)):
            with self.subTest(caller=caller["actor_id"]):
                with self.assertRaises(APIError) as read_error:
                    service.get(caller, foreign["task_id"])
                self.assertEqual(read_error.exception.status, 404)
                with self.assertRaises(APIError) as stop_error:
                    service.mutation(caller, "stop", foreign["task_id"], "stop-foreign", {})
                self.assertEqual(stop_error.exception.status, 404)
                self.assertEqual(len(service.list(caller)["tasks"]), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
