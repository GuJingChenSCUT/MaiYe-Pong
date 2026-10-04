"""Run reproducible local checks and save their scope/result. Never enables --live."""
import hashlib
import io
import json
import subprocess
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path


def main():
    root = Path(__file__).resolve().parent
    suite = unittest.defaultTestLoader.discover(str(root / "tests"))
    capture = io.StringIO()
    result = unittest.TextTestRunner(stream=capture, verbosity=2).run(suite)
    commands = [
        [sys.executable, str(root / "check_registry.py")],
        [sys.executable, str(root / "smoke_probe.py")],
        [sys.executable, str(root / "smoke_probe.py"), "--thinking"],
    ]
    checks = []
    for command in commands:
        completed = subprocess.run(command, text=True, capture_output=True, check=False)
        checks.append({"command": " ".join(["python3"] + [Path(command[1]).name] + command[2:]),
                       "exit_code": completed.returncode,
                       "result": json.loads(completed.stdout) if completed.returncode == 0
                                 else {"error": completed.stderr}})
    tracked = [root / "agent_runtime.py", root / "tests" / "test_runtime.py",
               root.parent / "contracts" / "tool_registry.json"]
    passed = result.wasSuccessful() and all(item["exit_code"] == 0 for item in checks)
    report = {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "status": "passed" if passed else "failed",
        "unit_tests_run": result.testsRun, "failures": len(result.failures),
        "errors": len(result.errors), "checks": checks,
        "sha256": {str(path.relative_to(root.parent)): hashlib.sha256(path.read_bytes()).hexdigest()
                   for path in tracked},
        "live_api_calls": 0, "real_payments": 0,
        "scope": "Reference runtime only; no production identity, workflow or payment implementation.",
    }
    (root / "offline_verification.json").write_text(json.dumps(report, indent=2, ensure_ascii=False)+"\n", encoding="utf-8")
    (root / "offline_test_output.txt").write_text(capture.getvalue(), encoding="utf-8")
    print(json.dumps({"status": report["status"], "unit_tests_run": result.testsRun,
                      "failures": report["failures"], "errors": report["errors"],
                      "auxiliary_checks": len(checks), "live_api_calls": 0}))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
