"""Bounded DeepSeek tool runtime. No payment kernel or production authorization is supplied.

The server supplies identity, authorization, role-filtered tool implementations and
output contracts. The model supplies only untrusted tool arguments. Defaults never
connect to a provider; choose an explicit Transport at the composition root.
"""
from __future__ import annotations

import copy
import json
import math
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol

from jsonschema import Draft202012Validator, FormatChecker


class RuntimeRejected(Exception):
    """Safe error code only: raw upstream bodies and private reasoning are not logged."""


def reject(code: str) -> None:
    raise RuntimeRejected(code)


def encode(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def parse_json(value: str) -> Any:
    """Reject duplicate keys and non-finite numbers, not merely invalid syntax."""
    def pairs(items: list[tuple[str, Any]]) -> dict:
        result = {}
        for key, item in items:
            if key in result:
                reject("duplicate_json_key")
            result[key] = item
        return result
    def constant(_: str) -> None:
        reject("non_finite_number")
    try:
        return json.loads(value, object_pairs_hook=pairs, parse_constant=constant)
    except (ValueError, TypeError):
        reject("invalid_json")


def validate(instance: Any, schema: dict, error_code: str) -> None:
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    if next(validator.iter_errors(instance), None) is not None:
        reject(error_code)


def strict_wire_schema(schema: dict) -> dict:
    """Adapt only the documented strict subset; retain the original local schema.

    Unsupported length/item counts and date/date-time formats are enforced locally.
    Optional properties become mandatory on the wire (a conservative restriction).
    Composition/reference types not implemented by this reference adapter fail
    configuration; this does not assert that the provider rejects every such type.
    """
    dropped = {"minLength", "maxLength", "minItems", "maxItems", "$schema", "$id", "title", "default"}
    supported = {"type", "properties", "required", "additionalProperties", "description",
                 "items", "enum", "anyOf", "pattern", "format", "const", "default",
                 "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf"}
    formats = {"email", "hostname", "ipv4", "ipv6", "uuid"}
    allowed_types = {"object", "string", "number", "integer", "boolean", "array"}
    if not isinstance(schema, dict):
        reject("unsupported_wire_schema")
    out = {}
    for key, value in schema.items():
        if key in dropped:
            continue
        if key not in supported:
            reject("unsupported_wire_schema_keyword")
        if key == "format" and value not in formats:
            continue
        if key == "const":
            if value is None or isinstance(value, (dict, list)):
                reject("unsupported_wire_const")
            if "enum" in schema and value not in schema["enum"]:
                reject("inconsistent_wire_const_enum")
            out["enum"] = [copy.deepcopy(value)]
            continue
        if key == "enum" and "const" in schema:
            continue
        if key == "properties":
            out[key] = {name: strict_wire_schema(child) for name, child in value.items()}
        elif key == "items":
            out[key] = strict_wire_schema(value)
        elif key == "anyOf":
            out[key] = [strict_wire_schema(child) for child in value]
        else:
            out[key] = copy.deepcopy(value)
    kind = out.get("type")
    if kind is not None and (not isinstance(kind, str) or kind not in allowed_types):
        reject("unsupported_wire_schema_type")
    if kind == "object":
        out.setdefault("properties", {})
        out["required"] = list(out["properties"])
        out["additionalProperties"] = False
    return out


@dataclass(frozen=True)
class TrustedContext:
    """Construct only from authenticated server/workflow state, never model/user JSON.

    operation_key is a durable logical-step ID, not a model tool_call_id. The durable
    workflow must deduplicate it and compare the command digest atomically.
    """
    actor_id: str
    tenant_id: str
    task_id: str
    role: str
    authorization_version: int
    operation_key: str
    expires_at_epoch: float
    run_id: str = ""
    state_version: int = 1


@dataclass(frozen=True)
class Config:
    model: str = "deepseek-flash"
    base_url: str = "https://api.deepseek.com/beta"
    thinking: bool = False
    json_output: bool = False
    initial_read_tool: str | None = None
    reasoning_effort: str = "high"
    max_tokens: int = 2048
    max_rounds: int = 6
    max_tool_calls: int = 12
    max_calls_per_round: int = 4
    max_history_bytes: int = 131072
    max_result_bytes: int = 16384
    max_argument_bytes: int = 8192
    max_elapsed_seconds: float = 90.0

    def __post_init__(self) -> None:
        if self.base_url != "https://api.deepseek.com/beta":
            reject("strict_requires_official_beta_endpoint")
        if self.reasoning_effort not in {"low", "high", "max"}:
            reject("unsupported_reasoning_effort")
        if min(self.max_tokens, self.max_rounds, self.max_tool_calls,
               self.max_calls_per_round, self.max_history_bytes,
               self.max_result_bytes, self.max_argument_bytes,
               self.max_elapsed_seconds) <= 0:
            reject("invalid_runtime_limits")


class Transport(Protocol):
    def complete(self, payload: dict) -> dict: ...


class Authorizer(Protocol):
    def check(self, context: TrustedContext, tool: dict, arguments: dict) -> None:
        """Raise on wrong owner/tenant/scope/state/version/expiry/stale evidence.

        Must re-read authoritative state for each call. request_stop may accept stale
        *versions* by explicit policy, but still requires identity and task ownership.
        Workflow repeats these checks atomically before accepting any write command.
        """
        ...

    def check_output(self, context: TrustedContext, tool: dict,
                     arguments: dict, result: dict) -> None:
        """Check field projection, evidence/version freshness and domain invariants.

        A schema-valid merchant result can still be stale or belong to another buyer.
        Do not implement this as an unconditional allow in a production integration.
        """
        ...


class DurableWorkflow(Protocol):
    def enqueue_once(self, *, context: TrustedContext, tool_name: str,
                     arguments: dict, operation_key: str) -> dict:
        """Persist command + dedupe + authorization atomically. Never a raw payment call.

        On uncertain enqueue result, caller must reconcile by operation_key; do not
        re-run the model, mint a new key, or assert that the command did not happen.
        """
        ...


@dataclass(frozen=True)
class Binding:
    """Every tool needs an explicit local output contract and real implementation."""
    read: Callable[[TrustedContext, dict], dict] | None = None


class UrllibTransport:
    """Opt-in live HTTP transport, no automatic retries and no raw error/body logging."""
    def __init__(self, api_key: str, config: Config, timeout_seconds: float = 30.0):
        if not api_key:
            reject("api_key_missing")
        self._key = api_key
        self._url = config.base_url + "/chat/completions"
        self._timeout = timeout_seconds

    def complete(self, payload: dict) -> dict:
        request = urllib.request.Request(
            self._url, data=encode(payload).encode("utf-8"), method="POST",
            headers={"Authorization": "Bearer " + self._key,
                     "Content-Type": "application/json"},
        )
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                reject("provider_redirect_denied")
        try:
            opener = urllib.request.build_opener(NoRedirect())
            with opener.open(request, timeout=self._timeout) as response:
                body = response.read(2_000_001)
            if len(body) > 2_000_000:
                reject("provider_response_too_large")
            return parse_json(body.decode("utf-8"))
        except urllib.error.HTTPError as exc:
            reject("provider_http_" + str(exc.code))
        except (urllib.error.URLError, TimeoutError, UnicodeError):
            reject("provider_transport_error")


class ToolRegistry:
    def __init__(self, entries: list[dict]):
        self.entries = {}
        for item in copy.deepcopy(entries):
            function = item["definition"]["function"]
            name = function["name"]
            if name in self.entries:
                reject("duplicate_registered_tool")
            Draft202012Validator.check_schema(function["parameters"])
            if item["effect"] not in {"read", "draft_write", "request_only", "guarded_write"}:
                reject("unknown_tool_effect")
            strict_wire_schema(function["parameters"])
            self.entries[name] = item

    @classmethod
    def from_file(cls, path: str | Path) -> ToolRegistry:
        return cls(parse_json(Path(path).read_text(encoding="utf-8")))

    def wire_tools(self, role: str) -> list[dict]:
        tools = []
        for item in self.entries.values():
            if role in item["allowed_agent_roles"]:
                definition = copy.deepcopy(item["definition"])
                definition["function"]["parameters"] = strict_wire_schema(
                    definition["function"]["parameters"])
                definition["function"]["strict"] = True
                tools.append(definition)
        return tools


@dataclass
class RunResult:
    status: str  # assistant_response or workflow_handoff; never "payment_succeeded"
    assistant_text: str | None = None
    workflow_receipt: dict | None = None
    # Private protocol state, never put into user output, analytics or audit logs.
    _protocol_messages: list[dict] = field(default_factory=list, repr=False)


class ToolRuntime:
    def __init__(self, registry: ToolRegistry, transport: Transport,
                 authorizer: Authorizer, bindings: Mapping[str, Binding],
                 *, config: Config | None = None,
                 workflow: DurableWorkflow | None = None,
                 audit: Callable[[dict], None] | None = None,
                 cancellation_check: Callable[[], None] | None = None):
        self.registry, self.transport, self.authorizer = registry, transport, authorizer
        self.bindings = dict(bindings)
        self.config = config or Config()
        self.workflow = workflow
        self.audit = audit or (lambda event: None)
        self.audit_failure_count = 0
        self.cancellation_check = cancellation_check

    def _emit_audit(self, event: dict) -> None:
        """Best-effort telemetry only; financial audit belongs in the workflow transaction."""
        try:
            self.audit(event)
        except Exception:
            # Never turn an accepted write into a misleading ordinary retryable error.
            self.audit_failure_count += 1

    def _check_authority(self, context: TrustedContext, spec: dict, arguments: dict) -> None:
        try:
            self.authorizer.check(context, copy.deepcopy(spec), copy.deepcopy(arguments))
        except RuntimeRejected:
            raise
        except Exception:
            reject("authorization_check_failed")

    def _check_deadline(self, started: float, context: TrustedContext) -> None:
        if self.cancellation_check is not None:
            try:
                self.cancellation_check()
            except RuntimeRejected:
                raise
            except Exception:
                reject("task_cancelled_or_run_stale")
        deadline = context.expires_at_epoch
        if (isinstance(deadline, bool) or not isinstance(deadline, (int, float))
                or not math.isfinite(deadline)):
            reject("invalid_task_deadline")
        if time.monotonic() - started >= self.config.max_elapsed_seconds:
            reject("elapsed_budget_exceeded")
        if time.time() >= context.expires_at_epoch:
            reject("task_context_expired")

    def build_payload(self, messages: list[dict], context: TrustedContext) -> dict:
        if len(encode(messages).encode("utf-8")) > self.config.max_history_bytes:
            reject("history_budget_exceeded")
        tools = self.registry.wire_tools(context.role)
        if not tools:
            reject("role_has_no_tools")
        for definition in tools:
            name = definition["function"]["name"]
            if name not in self.bindings:
                reject("tool_service_not_bound")
            Draft202012Validator.check_schema(self._output_schema(name, context))
        payload = {
            "model": self.config.model, "messages": copy.deepcopy(messages),
            "tools": tools, "tool_choice": "auto", "stream": False,
            "max_tokens": self.config.max_tokens,
            "thinking": {"type": "enabled" if self.config.thinking else "disabled"},
        }
        if self.config.thinking:
            payload["reasoning_effort"] = self.config.reasoning_effort
        if self.config.json_output:
            payload["response_format"] = {"type": "json_object"}
        if self.config.initial_read_tool and not any(m.get('role') == 'assistant' for m in messages):
            name = self.config.initial_read_tool
            if (self.config.thinking or name not in {t['function']['name'] for t in tools}
                    or self.registry.entries[name]['effect'] != 'read'):
                reject('invalid_initial_read_tool')
            payload['tool_choice'] = {'type': 'function', 'function': {'name': name}}
        return payload

    def _output_schema(self, name: str, context: TrustedContext) -> dict:
        spec = self.registry.entries[name]
        schema = spec.get("output_schemas_by_role", {}).get(context.role,
                                                          spec.get("output_schema"))
        if not isinstance(schema, dict):
            reject("tool_output_contract_missing")
        return schema

    def _validate_output(self, name: str, spec: dict, context: TrustedContext,
                         arguments: dict, output: dict) -> str:
        validate(output, self._output_schema(name, context), "tool_output_rejected")
        # Cross-field meaning, not just correct JSON or shape.
        status = output.get("status")
        if status in {"ok", "pending"}:
            if output.get("data") is None or output.get("error") is not None:
                reject("inconsistent_tool_envelope")
        elif status == "rejected":
            if output.get("data") is not None or not isinstance(output.get("error"), dict):
                reject("inconsistent_tool_envelope")
        else:
            reject("unknown_tool_result_status")
        self.authorizer.check_output(context, copy.deepcopy(spec),
                                     copy.deepcopy(arguments), copy.deepcopy(output))
        serialized = encode(output)
        if len(serialized.encode("utf-8")) > self.config.max_result_bytes:
            reject("tool_output_too_large")
        return serialized

    def _preflight(self, call: dict, context: TrustedContext) -> tuple[str, dict, dict]:
        if call.get("type") != "function" or not isinstance(call.get("function"), dict):
            reject("invalid_tool_call")
        function = call["function"]
        name = function.get("name")
        if not isinstance(name, str) or name not in self.registry.entries:
            reject("unregistered_tool")
        spec = self.registry.entries[name]
        if context.role not in spec["allowed_agent_roles"]:
            reject("tool_role_denied")
        raw = function.get("arguments")
        if not isinstance(raw, str) or len(raw.encode("utf-8")) > self.config.max_argument_bytes:
            reject("invalid_or_oversize_arguments")
        arguments = parse_json(raw)
        validate(arguments, spec["definition"]["function"]["parameters"], "arguments_rejected")
        # Context travels separately and cannot be overwritten via dict merging.
        self._check_authority(context, spec, arguments)
        binding = self.bindings.get(name)
        if binding is None or (spec["effect"] == "read" and binding.read is None):
            reject("tool_service_not_bound")
        return name, spec, arguments

    def run(self, *, system_prompt: str, user_text: str, context: TrustedContext) -> RunResult:
        """A fresh bounded task. Only trusted server code provides system_prompt/context.

        Model/merchant/user text cannot supply prior assistant, tool or system messages.
        A production workflow may persist private transcript state with scoped access and
        retention; this reference does not expose a client-controlled resume endpoint.
        """
        messages = [{"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_text}]
        started, calls_used = time.monotonic(), 0
        call_ids: set[str] = set()
        for _ in range(self.config.max_rounds):
            self._check_deadline(started, context)
            response = self.transport.complete(self.build_payload(messages, context))
            self._check_deadline(started, context)
            try:
                choices = response["choices"]
                if len(choices) != 1:
                    reject("ambiguous_provider_response")
                message = copy.deepcopy(choices[0]["message"])
                finish = choices[0]["finish_reason"]
            except (KeyError, TypeError, IndexError):
                reject("invalid_provider_response")
            if not isinstance(message, dict) or message.get("role") != "assistant":
                reject("invalid_assistant_message")
            calls = message.get("tool_calls") or []
            if not isinstance(calls, list) or finish not in {"stop", "tool_calls"}:
                reject("incomplete_or_invalid_completion")
            # Preserve the complete assistant protocol message, including reasoning_content.
            messages.append(message)
            if not calls:
                if finish != "stop" or not isinstance(message.get("content"), str):
                    reject("invalid_final_message")
                return RunResult("assistant_response", assistant_text=message["content"],
                                 _protocol_messages=messages)
            if finish != "tool_calls":
                reject("invalid_tool_finish_reason")
            if len(calls) > self.config.max_calls_per_round:
                reject("too_many_parallel_requests")
            calls_used += len(calls)
            if calls_used > self.config.max_tool_calls:
                reject("tool_budget_exceeded")
            # Validate the entire batch before executing even one handler.
            prepared = []
            for call in calls:
                if not isinstance(call, dict):
                    reject("invalid_tool_call")
                call_id = call.get("id")
                if not isinstance(call_id, str) or not call_id or call_id in call_ids:
                    reject("duplicate_or_missing_tool_call_id")
                call_ids.add(call_id)
                prepared.append((call_id, *self._preflight(call, context)))
            writes = [item for item in prepared if item[2]["effect"] != "read"]
            if writes and len(prepared) != 1:
                reject("write_requires_single_command")
            for call_id, name, spec, arguments in prepared:
                self._check_deadline(started, context)
                # Recheck immediately before dispatch; durable writes check atomically again.
                self._check_authority(context, spec, arguments)
                self._check_deadline(started, context)
                if writes:
                    if self.workflow is None:
                        reject("durable_workflow_not_bound")
                    if not context.operation_key:
                        reject("durable_operation_key_missing")
                    self._emit_audit({"event": "workflow_dispatch_attempt", "tool": name,
                                      "task_id": context.task_id})
                    try:
                        output = self.workflow.enqueue_once(
                            context=context, tool_name=name, arguments=copy.deepcopy(arguments),
                            operation_key=context.operation_key)
                    except Exception:
                        # A command may already be committed: reconcile the same operation key.
                        reject("workflow_outcome_unknown_reconcile_operation_key")
                else:
                    try:
                        output = self.bindings[name].read(context, copy.deepcopy(arguments))
                    except Exception:
                        reject("tool_read_service_error")
                try:
                    serialized = self._validate_output(name, spec, context, arguments, output)
                except Exception:
                    if writes:
                        reject("workflow_receipt_invalid_reconcile_operation_key")
                    reject("tool_output_rejected")
                self._emit_audit({"event": "workflow_handoff" if writes else "read_completed",
                                  "tool": name, "task_id": context.task_id})
                messages.append({"role": "tool", "tool_call_id": call_id, "content": serialized})
                if writes:
                    return RunResult("workflow_handoff", workflow_receipt=output,
                                     _protocol_messages=messages)
        reject("round_budget_exceeded")
