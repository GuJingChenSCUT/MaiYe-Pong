"""Default: offline protocol smoke. Explicit --live --allow-provider-charge enables HTTP.

The only available tool reads a fixed fictional fixture. No personal/order/merchant/
payment data are sent. This verifies API protocol, not shopping accuracy or payment.
"""
import argparse
import copy
import json
import os
import time

from agent_runtime import (Binding, Config, RuntimeRejected, ToolRegistry, ToolRuntime,
                           TrustedContext, UrllibTransport)


def obj(properties):
    return {"type": "object", "properties": properties, "required": list(properties),
            "additionalProperties": False}


ENTRY = {
    "definition": {"type": "function", "function": {
        "name": "read_public_probe", "description": "Read the fixed public fictional API test fixture.",
        "parameters": obj({"fixture_id": {"type": "string", "enum": ["protocol-probe"]}})}},
    "effect": "read", "allowed_agent_roles": ["api_probe"],
    "output_schema": obj({
        "status": {"const": "ok"},
        "data": obj({"probe_value": {"type": "string"}}),
        "error": {"type": "null"},
        "evidence_refs": {"type": "array", "items": {"type": "string"}},
        "observed_versions": obj({"fixture": {"const": 1}}),
    }),
}


class PublicProbePolicy:
    def check(self, context, tool, arguments):
        if (context.role != "api_probe" or context.tenant_id != "public-fixture" or
                arguments != {"fixture_id": "protocol-probe"} or tool["effect"] != "read"):
            raise RuntimeRejected("probe_scope_denied")

    def check_output(self, context, tool, arguments, result):
        if result["observed_versions"] != {"fixture": 1}:
            raise RuntimeRejected("probe_version_denied")


class OfflineProbeTransport:
    def __init__(self):
        self.calls = 0

    def complete(self, payload):
        self.calls += 1
        if self.calls == 1:
            message = {"role": "assistant", "content": "Reading a fictional fixture.",
                       "tool_calls": [{"id": "probe-call-1", "type": "function", "function": {
                           "name": "read_public_probe",
                           "arguments": '{"fixture_id":"protocol-probe"}'}}]}
            if payload["thinking"]["type"] == "enabled":
                message["reasoning_content"] = "Private offline protocol fixture."
            finish = "tool_calls"
        else:
            message = {"role": "assistant", "content": "PROBE_OK"}
            finish = "stop"
        return {"choices": [{"message": message, "finish_reason": finish}]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="Send requests to the official DeepSeek API")
    parser.add_argument("--allow-provider-charge", action="store_true", help="Acknowledge API usage may incur fees")
    parser.add_argument("--thinking", action="store_true", help="Exercise thinking protocol preservation")
    args = parser.parse_args()
    if args.live and not args.allow_provider_charge:
        parser.error("--live also requires --allow-provider-charge; no request was sent")
    if args.allow_provider_charge and not args.live:
        parser.error("--allow-provider-charge is only meaningful with --live")
    config = Config(thinking=args.thinking, max_rounds=3, max_tool_calls=1,
                    max_calls_per_round=1, max_tokens=1024)
    transport = (UrllibTransport(os.environ.get("DEEPSEEK_API_KEY", ""), config)
                 if args.live else OfflineProbeTransport())
    read_count = 0

    def read_fixture(context, arguments):
        nonlocal read_count
        read_count += 1
        return {"status": "ok", "data": {"probe_value": "PROBE_OK"}, "error": None,
                "evidence_refs": ["public-fictional-fixture:protocol-probe"],
                "observed_versions": {"fixture": 1}}

    runtime = ToolRuntime(ToolRegistry([copy.deepcopy(ENTRY)]), transport, PublicProbePolicy(),
                          {"read_public_probe": Binding(read=read_fixture)}, config=config)
    context = TrustedContext("probe-runner", "public-fixture", "public-api-probe", "api_probe",
                             1, "read-only-no-command", time.time()+90)
    result = runtime.run(
        system_prompt="This is a public fictional protocol test. Call the read tool exactly once; then return its probe_value.",
        user_text="Read fixture protocol-probe and report its probe_value.", context=context)
    if read_count != 1 or result.assistant_text is None or "PROBE_OK" not in result.assistant_text:
        raise RuntimeRejected("probe_did_not_complete_expected_tool_exchange")
    # Never print private protocol messages or reasoning_content.
    print(json.dumps({"status": "passed", "mode": "live" if args.live else "offline",
                      "thinking": args.thinking, "model": config.model,
                      "executed_tool": "read_public_probe", "read_count": read_count,
                      "real_business_integration": False}, ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except RuntimeRejected as exc:
        print(json.dumps({"status": "failed", "code": str(exc)}, ensure_ascii=False))
        raise SystemExit(1)
