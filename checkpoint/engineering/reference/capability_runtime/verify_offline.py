"""Run reference checks without model credentials, network or payment access."""
from __future__ import annotations
import hashlib
import io
import json
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path

from loader import CapabilityLoader, CapabilityRejected, TrustedCapabilityContext


def main():
    here = Path(__file__).resolve().parent
    package = here.parent
    buffer = io.StringIO()
    suite = unittest.defaultTestLoader.discover(str(here / "tests"))
    result = unittest.TextTestRunner(stream=buffer, verbosity=2).run(suite)
    manifest_path = package / "capabilities/manifest.json"
    manifest = json.loads(manifest_path.read_text())
    registry = json.loads((package / "contracts/tool_registry.json").read_text())
    stage_policy = json.loads((here / "stage_tool_policy.reference.json").read_text())["stages"]
    args = dict(expected_manifest_sha256=hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
                expected_format_version="1.0.0", expected_pack_version="0.3.0",
                tool_registry=registry,
                deployed_handler_allowlist=frozenset(r["definition"]["function"]["name"] for r in registry),
                stage_tool_policy={k: frozenset(v) for k, v in stage_policy.items()})
    # Calculating a pin from the same local files is useful only in an offline
    # packaging check. Production expected pins come from an independent registry.
    live_rejected = False
    try:
        CapabilityLoader(package / "capabilities", **args)
    except CapabilityRejected as exc:
        live_rejected = str(exc) == "UNPUBLISHED_CAPABILITY"
    loader = CapabilityLoader(package / "capabilities", **args, offline_test_mode=True)
    prepared_records = []
    for entry in manifest["capabilities"]:
        procedure = json.loads((package / "capabilities" / entry["procedure_path"]).read_text())
        context = TrustedCapabilityContext(
            "synthetic-pack-check", "synthetic-run-" + entry["id"], entry["role"],
            entry["workflow_nodes"][0], entry["objective_types"][0], entry["phase"],
            frozenset(procedure["allowed_tools"]), time.time() + 600,
            frozenset({entry["id"]}) if not entry["enabled_by_default"] else frozenset(),
        )
        projected = {key: "SYNTHETIC STRUCTURAL CHECK ONLY; NOT REAL BUSINESS DATA" for key in procedure["minimum_context"]}
        prepared = loader.prepare(context, entry["id"], projected)
        prepared_records.append({"capability_id": entry["id"],
                                 "tested_stage": context.workflow_node,
                                 "effective_tools": list(prepared.allowed_tools),
                                 "status": "offline_prepared_only"})
    report = {
        "verified_at": datetime.now(timezone.utc).isoformat(),
        "tests_run": result.testsRun, "failures": len(result.failures), "errors": len(result.errors),
        "draft_pack_refused_without_offline_flag": live_rejected,
        "pack_manifest_sha256": args["expected_manifest_sha256"],
        "real_pack_procedures_checked": prepared_records,
        "data_status": "synthetic_fixtures_no_market_rates_or_actual_cases",
        "live_model_verified": False, "identity_service_verified": False,
        "payment_sandbox_verified": False, "production_ready": False,
    }
    (here / "offline_test_output.txt").write_text(buffer.getvalue(), encoding="utf-8")
    (here / "offline_verification.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"tests_run": result.testsRun, "tests_passed": result.wasSuccessful(),
                      "procedures_prepared": len(prepared_records),
                      "draft_live_loading_refused": live_rejected,
                      "live_model_verified": False}))
    if not result.wasSuccessful() or not live_rejected:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
