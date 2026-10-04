"""Run actual local tests; save machine-readable status without claiming external QA."""
import datetime
import io
import json
from pathlib import Path
import unittest

root = Path(__file__).resolve().parent
suite = unittest.defaultTestLoader.discover(str(root / "tests"))
stream = io.StringIO()
result = unittest.TextTestRunner(stream=stream, verbosity=2).run(suite)
(root / "test_output.txt").write_text(stream.getvalue(), encoding="utf-8")
report = {
    "checked_at_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    "component": "local_sqlite_transaction_reference",
    "tests_run": result.testsRun,
    "failures": len(result.failures), "errors": len(result.errors),
    "successful": result.wasSuccessful(),
    "external_api_calls": 0,
    "payment_provider": "synthetic_local_simulator_only",
    "real_payment_or_bank_sandbox_verified": False,
    "production_identity_verified": False,
    "production_database_verified": False,
    "model_api_verified": False,
    "application_end_to_end_verified": False,
    "evidence": ["independent SQLite connections race a shared cash limit",
                 "stop/dispatch race resolves to one serialized outcome",
                 "fresh Python process reconciles an accepted-but-timed-out payment",
                 "failed outbox insertion rolls back cash reservation and operation",
                 "callback and refund idempotency preserve cash and fixture reward state"]
}
(root / "verification.json").write_text(json.dumps(report, indent=2)+"\n", encoding="utf-8")
print(json.dumps(report, indent=2))
raise SystemExit(0 if result.wasSuccessful() else 1)
