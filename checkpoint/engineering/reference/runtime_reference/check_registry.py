"""Offline check of current shared registry; no count or service success is hard-coded."""
import json
from pathlib import Path

from jsonschema import Draft202012Validator
from agent_runtime import ToolRegistry


def main():
    path = Path(__file__).resolve().parents[1] / "contracts" / "tool_registry.json"
    registry = ToolRegistry.from_file(path)
    roles = sorted({role for item in registry.entries.values() for role in item["allowed_agent_roles"]})
    role_tools = {}
    for role in roles:
        wire = registry.wire_tools(role)
        for definition in wire:
            name = definition["function"]["name"]
            spec = registry.entries[name]
            output_schema = spec.get("output_schemas_by_role", {}).get(role, spec.get("output_schema"))
            if output_schema is None:
                raise ValueError("Missing output contract: " + name + ":" + role)
            Draft202012Validator.check_schema(output_schema)
            Draft202012Validator.check_schema(definition["function"]["parameters"])
        role_tools[role] = [tool["function"]["name"] for tool in wire]
    print(json.dumps({"status": "passed", "tool_count": len(registry.entries),
                      "role_count": len(roles), "role_tools": role_tools,
                      "live_provider_schema_acceptance": "not_tested",
                      "business_services": "not_implemented"}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
