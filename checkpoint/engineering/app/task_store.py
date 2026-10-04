"""One durable SQLite database and one transaction boundary for tasks and money."""
from __future__ import annotations
import contextlib
import json
import sqlite3
import threading
import time
from pathlib import Path


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


SCHEMA = """
CREATE TABLE IF NOT EXISTS app_tasks (
 task_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, owner_id TEXT NOT NULL,
 state_version INTEGER NOT NULL, constraints_version INTEGER NOT NULL,
 status TEXT NOT NULL, draft_json TEXT NOT NULL, missing_json TEXT NOT NULL,
 questions_json TEXT NOT NULL, result_json TEXT, proposal_json TEXT,
 mode TEXT NOT NULL, scenario TEXT NOT NULL, active_run TEXT,
 created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS app_tasks_owner ON app_tasks(tenant_id,owner_id);
CREATE TABLE IF NOT EXISTS app_runs (
 run_id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES app_tasks(task_id),
 constraints_version INTEGER NOT NULL, status TEXT NOT NULL,
 attempts INTEGER NOT NULL DEFAULT 0, fence INTEGER NOT NULL DEFAULT 0,
 lease_owner TEXT, lease_until INTEGER, result_json TEXT, created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS app_http_idempotency (
 tenant_id TEXT NOT NULL, actor_id TEXT NOT NULL, action TEXT NOT NULL,
 idem_key TEXT NOT NULL, resource TEXT NOT NULL, digest TEXT NOT NULL,
 response_json TEXT NOT NULL, created_at INTEGER NOT NULL,
 PRIMARY KEY(tenant_id,actor_id,action,idem_key)
);
CREATE TABLE IF NOT EXISTS app_access_codes (
 code_hash TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, actor_id TEXT NOT NULL,
 role TEXT NOT NULL, merchant_id TEXT, active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS app_sessions (
 session_hash TEXT PRIMARY KEY, code_hash TEXT NOT NULL REFERENCES app_access_codes(code_hash),
 csrf TEXT NOT NULL, expires_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS app_events (
 event_id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL,
 kind TEXT NOT NULL, details_json TEXT NOT NULL, created_at INTEGER NOT NULL
);
CREATE TRIGGER IF NOT EXISTS app_events_no_update BEFORE UPDATE ON app_events
 BEGIN SELECT RAISE(ABORT,'immutable_task_event'); END;
CREATE TRIGGER IF NOT EXISTS app_events_no_delete BEFORE DELETE ON app_events
 BEGIN SELECT RAISE(ABORT,'immutable_task_event'); END;
"""


class StorageUnavailable(RuntimeError):
    """Safe boundary error. Never include paths, SQL, or raw database messages."""

    def __init__(self):
        super().__init__("STORAGE_UNAVAILABLE")


class TaskStore:
    def __init__(self, path, *, clock=None):
        self.path = str(Path(path).resolve())
        self.clock = clock or time.time
        self._health_lock = threading.Lock()
        self._initialized = False
        self._quarantined = False
        self._health = {"status": "starting", "reason": None, "failure_count": 0,
                        "last_error_at": None, "recovery_required": False}
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as conn:
            conn.executescript(SCHEMA)
            from reference.transaction_reference.kernel import StageOneKernel
            StageOneKernel.install(conn)
        self._initialized = True
        self.health(probe=True)

    def now(self):
        return int(self.clock())

    @contextlib.contextmanager
    def connection(self):
        with self._health_lock:
            if self._quarantined:
                raise StorageUnavailable()
        conn = None
        try:
            # A lost database must not silently become an empty replacement.
            mode = "rw" if self._initialized else "rwc"
            conn = sqlite3.connect(Path(self.path).as_uri() + "?mode=" + mode,
                                   uri=True, timeout=2, isolation_level=None)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=2000")
            conn.execute("PRAGMA synchronous=FULL")
            # WAL is persistent; do not try changing journal mode on every request.
            journal = "PRAGMA journal_mode" if self._initialized else "PRAGMA journal_mode=WAL"
            if conn.execute(journal).fetchone()[0].lower() != "wal":
                self._unavailable("JOURNAL_MODE_UNEXPECTED", quarantine=True)
                raise StorageUnavailable()
            yield conn
        except (sqlite3.IntegrityError, sqlite3.ProgrammingError):
            # Constraint / programming errors are not proof of a storage outage.
            raise
        except sqlite3.DatabaseError as error:
            code = getattr(error, "sqlite_errorcode", 0) & 0xff
            corrupt = code in {sqlite3.SQLITE_CORRUPT, sqlite3.SQLITE_NOTADB}
            self._unavailable("INTEGRITY_FAILURE" if corrupt else "STORAGE_UNAVAILABLE",
                              quarantine=corrupt)
            raise StorageUnavailable() from error
        finally:
            # Includes failures during connection PRAGMAs, before the yield.
            if conn is not None:
                conn.close()

    def _unavailable(self, reason, *, quarantine=False):
        with self._health_lock:
            self._quarantined = self._quarantined or quarantine
            self._health.update(status="unavailable", reason=reason,
                                last_error_at=self.now(), recovery_required=self._quarantined)
            self._health["failure_count"] += 1

    def health(self, *, probe=False):
        """Readiness, not a repair. Corruption requires operator investigation/restart."""
        if probe:
            with self._health_lock:
                failures_before_probe = self._health["failure_count"]
            try:
                with self.read_transaction() as conn:
                    ok = conn.execute("PRAGMA quick_check").fetchall()
                    if len(ok) != 1 or ok[0][0] != "ok":
                        self._unavailable("INTEGRITY_FAILURE", quarantine=True)
                        raise StorageUnavailable()
                    conn.execute("SELECT task_id FROM app_tasks LIMIT 1").fetchone()
                with self._health_lock:
                    if not self._quarantined and self._health["failure_count"] == failures_before_probe:
                        self._health.update(status="available", reason=None)
            except StorageUnavailable:
                pass
        with self._health_lock:
            return dict(self._health)

    @contextlib.contextmanager
    def transaction(self):
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
                conn.commit()
            except BaseException:
                conn.rollback()
                raise

    @contextlib.contextmanager
    def read_transaction(self):
        """Authorization and projection must observe the same database snapshot."""
        with self.connection() as conn:
            conn.execute("BEGIN")
            try:
                yield conn
            finally:
                conn.rollback()

    def event(self, conn, task_id, kind, details=None):
        conn.execute("INSERT INTO app_events(task_id,kind,details_json,created_at) VALUES (?,?,?,?)",
                     (task_id, kind, encode(details or {}), self.now()))
