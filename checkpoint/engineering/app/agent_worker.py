"""Leased research jobs only. Payment dispatch commands never use this retry loop."""
from __future__ import annotations
import argparse
import threading
import time
from app.task_service import TaskService, opaque
from app.task_store import TaskStore, StorageUnavailable


class AgentWorker:
    def __init__(self, service, *, runner=None):
        from slice04.agent import run_task_v1
        self.service, self.runner = service, runner or run_task_v1
        self.worker_id = opaque("worker_")
        self.stopping = threading.Event()
        self._health_lock = threading.Lock()
        self._health = {"status": "starting", "reason": None, "failure_count": 0,
                        "consecutive_failures": 0, "last_progress_at": int(time.time())}

    def health(self):
        with self._health_lock:
            return dict(self._health)

    def _state(self, status, reason=None):
        with self._health_lock:
            self._health.update(status=status, reason=reason, last_progress_at=int(time.time()))
            if status == "unavailable":
                self._health["failure_count"] += 1
                self._health["consecutive_failures"] += 1
            elif status == "idle":
                self._health["consecutive_failures"] = 0

    def _failed(self, error, fallback):
        self._state("unavailable", "STORAGE_UNAVAILABLE" if isinstance(error, StorageUnavailable) else fallback)
        return False

    def once(self):
        try:
            run = self.service.claim_run(self.worker_id)
        except Exception as error:
            return self._failed(error, "WORKER_CLAIM_FAILED")
        if not run:
            self._state("idle")
            return False
        self._state("running")
        try:
            def check_current():
                if self.stopping.is_set():
                    raise RuntimeError("WORKER_STOPPING")
                if not self.service.is_current_run(run):
                    raise RuntimeError("STALE_RUN")
                self._state("running")
            result = self.runner(run["draft"], mode=run["mode"], scenario=run["scenario"],
                task_id=run["task_id"], constraints_version=run["constraints_version"], run_id=run["run_id"],
                cancel_check=check_current)
            self.service.finish_run(run, result)
        except StorageUnavailable as error:
            # A failed result commit is uncertain. Do not attempt another write or
            # claim payment success; the fenced research lease recovers later.
            return self._failed(error, "STORAGE_UNAVAILABLE")
        except Exception:
            # No raw provider exception, API key or response body in user-visible state.
            try:
                self.service.finish_run(run, {"status": "blocked", "draft": run["draft"],
                    "reason": "WORKER_RESULT_REJECTED", "model_status": "blocked", "model_online_verified": False})
            except Exception as error:
                return self._failed(error, "WORKER_RESULT_PERSIST_FAILED")
        self._state("idle")
        return True

    def serve(self):
        try:
            while not self.stopping.is_set():
                try:
                    worked = self.once()
                except Exception as error:
                    # Last supervision boundary; one task must not kill the worker.
                    worked = self._failed(error, "WORKER_UNEXPECTED_FAILURE")
                if not worked:
                    failures = min(self.health()["consecutive_failures"], 5)
                    self.stopping.wait(min(5.0, 0.25 * 2 ** failures))
        finally:
            self._state("stopped")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    worker = AgentWorker(TaskService(TaskStore(args.db)))
    if args.once:
        worker.once()
    else:
        try:
            worker.serve()
        except KeyboardInterrupt:
            worker.stopping.set()


if __name__ == "__main__":
    main()
