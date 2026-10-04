"""Validate handoff artifacts only; this does not test a running application.

Run from any directory after installing qa/requirements.txt:
    python qa/validate_contracts.py
"""
from collections import Counter
import copy
import csv
import json
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker, ValidationError
from openapi_spec_validator import validate

ROOT = Path(__file__).resolve().parents[1]


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def rejects(validator, value):
    try:
        validator.validate(value)
    except ValidationError:
        return
    raise AssertionError("Invalid example was accepted")


schemas = {}
for path in sorted((ROOT / "contracts").glob("*.schema.json")):
    schema = read_json(path)
    Draft202012Validator.check_schema(schema)
    schemas[path.name.removesuffix(".schema.json")] = schema
assert len(schemas) == 21
assert len({s["$id"] for s in schemas.values()}) == len(schemas)
profile = read_json(ROOT / "contracts/ModelProfile.example.json")
profile_validator = Draft202012Validator(schemas["ModelProfile"], format_checker=FormatChecker())
profile_validator.validate(profile)
assert profile["doc_verified"] is True and profile["live_verified"] is False
unverified_activation = copy.deepcopy(profile)
unverified_activation["enabled"] = True
rejects(profile_validator, unverified_activation)

api = read_json(ROOT / "contracts/openapi.json")
validate(api)
operations = [
    operation
    for item in api["paths"].values()
    for method, operation in item.items()
    if method in {"get", "post", "put", "patch", "delete", "head", "options", "trace"}
]
assert len(operations) == 17
assert len({op["operationId"] for op in operations}) == len(operations)
assert all(op.get("x-authorized-roles") for op in operations)

tools = read_json(ROOT / "contracts/tool_registry.json")
manifest = read_json(ROOT / "contracts/agent_manifest.json")
roles = {a["role"]: a for a in manifest["agents"]}
assert len(roles) == 6
names = []
output_count = 0
for tool in tools:
    function = tool["definition"]["function"]
    names.append(function["name"])
    Draft202012Validator.check_schema(function["parameters"])
    assert function["parameters"]["additionalProperties"] is False
    assert tool["allowed_agent_roles"]
    assert set(tool["allowed_agent_roles"]).issubset(roles)
    assert {"actor_id", "tenant_id", "task_id", "authorization_version"}.issubset(tool["server_injected_context"])
    if "output_schemas_by_role" in tool:
        assert set(tool["output_schemas_by_role"]) == set(tool["allowed_agent_roles"])
        outputs = tool["output_schemas_by_role"].values()
    else:
        outputs = [tool["output_schema"]]
    for schema in outputs:
        Draft202012Validator.check_schema(schema)
        output_count += 1
assert len(names) == len(set(names)) == 13
for role, definition in roles.items():
    expected = {t["definition"]["function"]["name"] for t in tools if role in t["allowed_agent_roles"]}
    assert set(definition["allowed_tools"]) == expected
assert roles["knowledge_curator"]["enabled_by_default"] is False

seed_path = ROOT / "data/case_seeds.normalized.jsonl"
seeds = [json.loads(line) for line in seed_path.read_text(encoding="utf-8").splitlines() if line.strip()]
validator = Draft202012Validator(schemas["CaseRecord"], format_checker=FormatChecker())
for record in seeds:
    validator.validate(record)
assert len(seeds) == len({r["case_id"] for r in seeds}) == 18
assert len({r["source_group_id"] for r in seeds}) == 7
counts = Counter(r["record_kind"] for r in seeds)
assert counts == {"reported_complaint": 13, "reported_complaint_summary": 1,
                  "reported_enforcement_case": 1, "research_observation": 3}
assert all(r["review_status"] == "draft" and r["dataset_split"] == "unassigned" for r in seeds)
assert all(r["rights"]["external_model_use"] == "review_required" for r in seeds)
assert all(r["retrieved_at"] is None for r in seeds)

# Catch schema regressions that could conflate a study with a complaint.
study = copy.deepcopy(next(r for r in seeds if r["record_kind"] == "research_observation"))
study["buyer_claim"] = "Unsupported claim"
rejects(validator, study)
bad = copy.deepcopy(seeds[0])
bad["unchecked_authorization"] = True
rejects(validator, bad)
money_validator = Draft202012Validator(schemas["Money"])
money_validator.validate({"currency": "HKD", "amount_minor": 100})
rejects(money_validator, {"currency": "HKD", "amount_minor": -1})
rejects(money_validator, {"currency": "HKD", "amount_minor": 1.5})
rejects(money_validator, {"currency": "HKD", "amount_minor": 100, "reward_offset": 20})

with (ROOT / "qa/acceptance_matrix.csv").open(encoding="utf-8-sig", newline="") as handle:
    scenarios = list(csv.DictReader(handle))
assert len(scenarios) == len({row["id"] for row in scenarios}) == 28
assert all(all(row[column] for column in ("id", "scenario", "precondition", "action", "expected_result")) for row in scenarios)

print(json.dumps({
    "artifact_validation": "passed",
    "schemas": len(schemas), "openapi_operations": len(operations), "tool_definitions": len(tools),
    "tool_output_projections": output_count, "logical_agent_roles": len(roles),
    "seed_records": len(seeds), "source_groups": 7, "record_kinds": dict(counts),
    "acceptance_scenarios_defined": len(scenarios), "application_scenarios_executed": 0,
    "scope": "Schema structure, OpenAPI validity, fixture conformance and artifact consistency only. No model, payment, permissions or concurrency integration was executed."
}, ensure_ascii=False, indent=2))
