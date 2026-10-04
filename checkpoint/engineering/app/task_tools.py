"""Task-scoped, in-process research handlers. No authorization or money effects.

The catalog is explicitly synthetic. A coordinator persists the returned draft,
clarification or proposal only after its own version/fencing check.
"""
from __future__ import annotations
import copy
import json
import re
import time
from decimal import Decimal
from slice04.domain import Rejected, digest


def object_schema(properties):
    return {"type": "object", "properties": properties, "required": list(properties),
            "additionalProperties": False}


TEXT = {"type": "string", "maxLength": 2048}
EXTRACTABLE = ("product.brand", "product.variant", "product.net_content", "product.unit",
               "product.pack_count", "product.packaging", "product.origin", "product.region_version",
               "purchase_quantity", "cash_cap_minor", "destination_ref", "preference")
QUESTIONS = {
    "product": "请补充需要购买的商品及规格。", "product.brand": "需要哪个品牌？",
    "product.variant": "需要哪一种型号或款式？", "product.net_content": "每件的容量或净含量是多少？",
    "product.unit": "容量单位是什么（g、ml 或 item）？", "product.pack_count": "每个购买单位包含几件？",
    "product.packaging": "需要什么包装规格？", "purchase_quantity": "需要购买多少个单位？",
    "cash_cap_minor": "包括运费在内，现金预算上限是多少港元？",
    "destination_ref": "配送到香港哪个地区？", "preference": "优先考虑最低现金支出、最快送达还是方便退货？",
}


class TaskTools:
    def __init__(self, draft, task_id, constraints_version, scenario="normal", now=None):
        from slice04.domain import validate_task_v1
        self.draft = copy.deepcopy(draft)
        self.task_id, self.version = task_id, constraints_version
        self.scenario, self.now = scenario, int(time.time()) if now is None else now
        self.missing = validate_task_v1(self.draft)
        self.trace, self.questions = [], []
        self.catalog = None
        self.seen, self.refreshed = set(), set()
        self.initial_candidate = None
        self.initial_comparison = None
        self.comparison = None
        self.extractions = []

    def _ensure_catalog(self):
        from slice04.domain import catalog_v1
        if self.catalog is None:
            if self.missing:
                raise Rejected("TASK_CONSTRAINTS_INCOMPLETE")
            self.catalog = catalog_v1(self.draft, scenario="normal", now=self.now)

    def _quote(self, quote_id):
        self._ensure_catalog()
        for quote in self.catalog["quotes"]:
            if quote["quote_id"] == quote_id:
                return quote
        raise Rejected("QUOTE_NOT_IN_TASK_SCOPE")

    def _extract(self, changes):
        from slice04.domain import validate_task_v1
        candidate = copy.deepcopy(self.draft)
        text = str(self.draft.get("text", ""))
        accepted = []
        for change in changes:
            path, raw, span = change["field"], change["value"], change["source_text"]
            if not span or span not in text or path not in EXTRACTABLE:
                raise Rejected("EXTRACTION_REQUIRES_ORIGINAL_TEXT")
            target = candidate
            parts = path.split(".")
            if len(parts) == 2:
                if not isinstance(candidate.get("product"), dict):
                    candidate["product"] = {}
                target = candidate["product"]
            key = parts[-1]
            if target.get(key) not in (None, ""):
                raise Rejected("CANNOT_OVERWRITE_USER_FIELD")
            if path in {"purchase_quantity", "cash_cap_minor", "product.net_content", "product.pack_count"}:
                if not raw.isdigit():
                    raise Rejected("EXTRACTED_INTEGER_REQUIRED")
                value = int(raw)
                if path == "cash_cap_minor":
                    match = re.fullmatch(r"(?:HKD|HK\$|港幣|港币)\s*(\d+(?:\.\d{1,2})?)", span, re.I)
                    match = match or re.fullmatch(r"(\d+(?:\.\d{1,2})?)\s*港元", span)
                    if not match or int(Decimal(match.group(1)) * 100) != value:
                        raise Rejected("CASH_EXTRACTION_REQUIRES_EXPLICIT_HKD_VALUE")
                else:
                    match = re.fullmatch(r"(\d+)\s*(?:包|件|支|個|个|瓶|盒|袋|g|克|ml|毫升|items?)?", span, re.I)
                    if not match or int(match.group(1)) != value:
                        raise Rejected("NUMERIC_EXTRACTION_NOT_SUPPORTED_BY_SPAN")
            else:
                value = raw
                aliases = {"product.unit": {"克": "g", "毫升": "ml", "件": "item"},
                           "preference": {"最低价": "lowest_cost", "最低價": "lowest_cost", "省钱": "lowest_cost", "省錢": "lowest_cost",
                                          "最快送达": "fastest_delivery", "最快送達": "fastest_delivery",
                                          "方便退货": "easiest_returns", "方便退貨": "easiest_returns"}}
                if value != span.strip() and aliases.get(path, {}).get(span.strip()) != value:
                    raise Rejected("STRING_EXTRACTION_NOT_SUPPORTED_BY_SPAN")
            target[key] = value
            accepted.append({"field": path, "value": value, "source_text": span,
                             "status": "inferred_from_user_text_not_authorization"})
        missing = validate_task_v1(candidate)
        self.draft, self.missing = candidate, missing
        self.extractions.extend(accepted)
        return {"draft": copy.deepcopy(self.draft), "missing_fields": missing,
                "extractions": accepted, "authority_created": False}

    def handle(self, name, args):
        from slice04.domain import evaluate_v1, rank_v1, catalog_v1
        if name == "buyer_get_draft":
            data = {"draft": copy.deepcopy(self.draft), "missing_fields": self.missing,
                    "authority_created": False}
        elif name == "buyer_propose_fields":
            data = self._extract(args["changes"])
        elif name == "buyer_request_clarification":
            if not self.missing:
                raise Rejected("NO_MISSING_FIELDS")
            # Questions are a draft result, not a sent message or persistent write.
            self.questions = [QUESTIONS.get(k, "请补充：" + k) for k in self.missing]
            data = {"missing_fields": self.missing, "questions": self.questions,
                    "authority_created": False}
        elif name == "facts_list_candidates":
            self._ensure_catalog()
            data = {"candidates": [{"quote_id": q["quote_id"], "merchant_id": q["merchant_id"]}
                                    for q in self.catalog["quotes"]],
                    "preference": self.draft.get("preference", "lowest_cost"),
                    "capability_label": "synthetic_fixture"}
        elif name == "facts_get_quote":
            q = self._quote(args["quote_id"])
            self.seen.add(q["quote_id"])
            data = {"quote": q, "evaluation": evaluate_v1(q, self.draft)}
        elif name == "commerce_compare":
            self._ensure_catalog()
            if self.seen != {q["quote_id"] for q in self.catalog["quotes"]}:
                raise Rejected("READ_BOTH_CANDIDATES_FIRST")
            self.comparison = rank_v1(self.catalog["quotes"], self.draft)
            if self.initial_comparison is None:
                self.initial_comparison = copy.deepcopy(self.comparison)
            data = self.comparison
        elif name == "facts_record_candidate":
            q = self._quote(args["quote_id"])
            if self.initial_candidate is not None or self.comparison is None:
                raise Rejected("INITIAL_ASSESSMENT_ALREADY_SET_OR_MISSING")
            if self.comparison.get("selected_quote_id") != q["quote_id"]:
                raise Rejected("INITIAL_CANDIDATE_CONTRADICTS_USER_PREFERENCE")
            self.initial_candidate = q["quote_id"]
            self.initial_comparison = copy.deepcopy(self.comparison)
            data = {"initial_candidate": self.initial_candidate, "authority_created": False}
        elif name == "facts_refresh_quote":
            self._quote(args["quote_id"])
            if self.initial_candidate is None and self.initial_comparison is None:
                raise Rejected("INITIAL_ASSESSMENT_REQUIRED")
            newer = catalog_v1(self.draft, scenario=self.scenario, now=self.now)
            replacement = next(q for q in newer["quotes"] if q["quote_id"] == args["quote_id"])
            self.catalog["quotes"] = [replacement if q["quote_id"] == replacement["quote_id"] else q
                                      for q in self.catalog["quotes"]]
            self.refreshed.add(replacement["quote_id"])
            self.comparison = None
            data = {"quote": replacement, "evaluation": evaluate_v1(replacement, self.draft)}
        else:
            raise Rejected("UNREGISTERED_TASK_TOOL")
        evidence_refs = sorted(data.get("quote", {}).get("evidence", {}))
        result = {"status": "ok", "data": copy.deepcopy(data), "error": None,
                  "task_id": self.task_id, "constraints_version": self.version,
                  "evidence_refs": evidence_refs, "observed_versions": {"constraints": self.version}}
        self.trace.append({"seq": len(self.trace) + 1, "tool": name,
                           "arguments": copy.deepcopy(args), "result": copy.deepcopy(data),
                           "result_digest": digest(result), "task_id": self.task_id,
                           "constraints_version": self.version, "skill_version": "stage1-1"})
        return result

    def final_research(self, answer):
        from slice04.domain import rank_v1
        self._ensure_catalog()
        ids = {q["quote_id"] for q in self.catalog["quotes"]}
        if self.seen != ids or self.refreshed != ids or self.initial_comparison is None:
            raise Rejected("COMPARISON_AND_REFRESH_REQUIRED")
        ranking = rank_v1(self.catalog["quotes"], self.draft)
        expected = ranking.get("selected_quote_id")
        if expected:
            if answer != {"action": "propose", "selected_quote_id": expected}:
                raise Rejected("FINAL_CONTRADICTS_VERIFIED_USER_PREFERENCE")
            quote = self._quote(expected)
            status = "proposed"
        else:
            if answer != {"action": "stop", "selected_quote_id": ""}:
                raise Rejected("NO_FEASIBLE_OFFER")
            quote, status = None, "stopped"
        initial_digest = digest(self.initial_comparison)
        changed_facts = initial_digest != digest(ranking)
        changed_action = expected != self.initial_candidate
        projection = copy.deepcopy(ranking)
        projection["quotes"] = copy.deepcopy(self.catalog["quotes"])
        return {"status": status, "comparison": projection, "selected_quote_id": expected,
                "quote": copy.deepcopy(quote), "reason": ranking.get("reason", ""),
                "replanning_demonstrated": bool(changed_facts and changed_action)}


def registry_v1():
    # Import the shared bounded runtime through the package's existing compatibility path.
    from slice04.agent import ToolRegistry
    from slice04.domain import ROOT
    defs = json.loads((ROOT / "schemas/commerce.schema.json").read_text())["$defs"]
    strings = {"type": "array", "items": TEXT}
    no_authority = {"const": False}
    quote_data = object_schema({"quote": defs["QuoteV1"], "evaluation": defs["EvaluationV1"]})
    data_schemas = {
        "buyer_get_draft": object_schema({"draft": defs["TaskDraftV1"], "missing_fields": strings, "authority_created": no_authority}),
        "buyer_propose_fields": object_schema({"draft": defs["TaskDraftV1"], "missing_fields": strings,
            "extractions": {"type": "array", "items": object_schema({"field": TEXT,
                 "value": {"anyOf": [{"type": "string"}, {"type": "integer"}]},
                 "source_text": TEXT, "status": {"const": "inferred_from_user_text_not_authorization"}})},
            "authority_created": no_authority}),
        "buyer_request_clarification": object_schema({"missing_fields": strings, "questions": strings, "authority_created": no_authority}),
        "facts_list_candidates": object_schema({"candidates": {"type": "array", "items": object_schema({"quote_id": TEXT, "merchant_id": TEXT})},
            "preference": {"anyOf": [TEXT, {"type": "null"}]}, "capability_label": {"const": "synthetic_fixture"}}),
        "facts_get_quote": quote_data, "facts_refresh_quote": quote_data,
        "facts_record_candidate": object_schema({"initial_candidate": TEXT, "authority_created": no_authority}),
        "commerce_compare": object_schema({"candidates": {"type": "array", "items": defs["EvaluationV1"]},
            "selected_quote_id": {"anyOf": [TEXT, {"type": "null"}]}, "preference": TEXT,
            "reason": TEXT, "tradeoffs": {"type": "array", "items": {"type": "object"}}}),
    }
    change = object_schema({"field": {"type": "string", "enum": list(EXTRACTABLE)},
                            "value": TEXT, "source_text": TEXT})
    definitions = [
        ("buyer_get_draft", "buyer_planner", object_schema({}), "Read this task draft and missing fields."),
        ("buyer_propose_fields", "buyer_planner", object_schema({"changes": {"type": "array", "items": change, "maxItems": 12}}),
         "Propose only missing fields explicitly supported by an exact span in the user's text. Existing fields cannot change. Cash values use HKD integer cents. No authority is created."),
        ("buyer_request_clarification", "buyer_planner", object_schema({}), "Draft questions for required missing fields; no message is sent and no authority is created."),
        ("facts_list_candidates", "offer_analyst", object_schema({}), "List task-scoped synthetic candidate references and user preference."),
        ("facts_get_quote", "offer_analyst", object_schema({"quote_id": TEXT}), "Read a task-scoped quote and deterministic feasibility. Read both candidates."),
        ("commerce_compare", "offer_analyst", object_schema({}), "Compare already-read candidates using the user's preference and hard constraints."),
        ("facts_record_candidate", "offer_analyst", object_schema({"quote_id": TEXT}), "Record the initial candidate after comparison for evaluation; not an authorization."),
        ("facts_refresh_quote", "offer_analyst", object_schema({"quote_id": TEXT}), "Refresh after initial comparison; facts may change. Refresh both before final recommendation."),
    ]
    entries = []
    for name, role, params, description in definitions:
        output = object_schema({"status": {"const": "ok"}, "data": data_schemas[name],
                                "error": {"type": "null"}, "task_id": {"type": "string"},
                                "constraints_version": {"type": "integer"},
                                "evidence_refs": {"type": "array", "items": {"type": "string"}},
                                "observed_versions": object_schema({"constraints": {"type": "integer"}})})
        entries.append({"definition": {"type": "function", "function": {"name": name,
                        "description": description, "parameters": params}}, "effect": "read",
                        "allowed_agent_roles": [role], "output_schema": output})
    return ToolRegistry(entries)


class TaskPolicy:
    def __init__(self, context, handlers):
        self.context, self.handlers = context, handlers

    def check(self, context, spec, arguments):
        from slice04.agent import RuntimeRejected
        if (context != self.context or context.task_id != self.handlers.task_id
                or context.authorization_version != self.handlers.version or spec["effect"] != "read"
                or context.role not in spec["allowed_agent_roles"]):
            raise RuntimeRejected("TASK_SCOPE_OR_ROLE_DENIED")
        if "quote_id" in arguments:
            self.handlers._quote(arguments["quote_id"])

    def check_output(self, context, spec, arguments, result):
        from slice04.agent import RuntimeRejected
        self.check(context, spec, arguments)
        if result["task_id"] != context.task_id or result["constraints_version"] != self.handlers.version:
            raise RuntimeRejected("TASK_OUTPUT_SCOPE_MISMATCH")
        if spec["definition"]["function"]["name"] in {"facts_get_quote", "facts_refresh_quote"}:
            from slice04.domain import evaluate_v1
            quote = result["data"]["quote"]
            if quote["quote_id"] != arguments["quote_id"] or result["data"]["evaluation"] != evaluate_v1(quote, self.handlers.draft):
                raise RuntimeRejected("TASK_OUTPUT_QUOTE_MISMATCH")
        if spec["definition"]["function"]["name"] == "commerce_compare":
            from slice04.domain import rank_v1
            if result["data"] != rank_v1(self.handlers.catalog["quotes"], self.handlers.draft):
                raise RuntimeRejected("TASK_OUTPUT_COMPARISON_MISMATCH")


class ScriptedTaskTransport:
    """Local interaction exercise driven by actual tool results, never online AI proof."""
    def __init__(self):
        self.counter = 0

    def complete(self, payload):
        self.counter += 1
        tools = {x["function"]["name"] for x in payload["tools"]}
        calls, results = [], {}
        for message in payload["messages"]:
            for call in message.get("tool_calls", []):
                calls.append(call["function"]["name"])
            if message["role"] == "tool":
                results[calls[-1]] = json.loads(message["content"])["data"]
        name, args, answer = None, {}, None
        if "buyer_get_draft" in tools:
            if "buyer_get_draft" not in calls:
                name = "buyer_get_draft"
            elif results["buyer_get_draft"]["missing_fields"]:
                if "buyer_request_clarification" not in calls:
                    name = "buyer_request_clarification"
                else:
                    answer = {"action": "clarify"}
            else:
                answer = {"action": "research"}
        else:
            if "facts_list_candidates" not in calls:
                name = "facts_list_candidates"
            else:
                ids = [x["quote_id"] for x in results["facts_list_candidates"]["candidates"]]
                quoted = [json.loads(c["function"]["arguments"])["quote_id"] for m in payload["messages"]
                          for c in m.get("tool_calls", []) if c["function"]["name"] == "facts_get_quote"]
                refreshed = [json.loads(c["function"]["arguments"])["quote_id"] for m in payload["messages"]
                             for c in m.get("tool_calls", []) if c["function"]["name"] == "facts_refresh_quote"]
                if len(quoted) < len(ids):
                    name, args = "facts_get_quote", {"quote_id": next(x for x in ids if x not in quoted)}
                elif "commerce_compare" not in calls:
                    name = "commerce_compare"
                elif "facts_record_candidate" not in calls:
                    selected = results["commerce_compare"].get("selected_quote_id")
                    if selected:
                        name, args = "facts_record_candidate", {"quote_id": selected}
                    elif len(refreshed) < len(ids):
                        name, args = "facts_refresh_quote", {"quote_id": next(x for x in ids if x not in refreshed)}
                    elif calls[-1] != "commerce_compare":
                        name = "commerce_compare"
                    else:
                        answer = {"action": "stop", "selected_quote_id": ""}
                elif len(refreshed) < len(ids):
                    name, args = "facts_refresh_quote", {"quote_id": next(x for x in ids if x not in refreshed)}
                elif calls[-1] != "commerce_compare":
                    name = "commerce_compare"
                else:
                    selected = results["commerce_compare"].get("selected_quote_id")
                    answer = {"action": "propose" if selected else "stop", "selected_quote_id": selected or ""}
        message = {"role": "assistant", "content": json.dumps(answer) if answer else None}
        if name:
            message["tool_calls"] = [{"id": "scripted-task-" + str(self.counter), "type": "function",
                                      "function": {"name": name, "arguments": json.dumps(args)}}]
        return {"id": "scripted-task-" + str(self.counter), "model": "scripted_local_exercise",
                "choices": [{"message": message, "finish_reason": "tool_calls" if name else "stop"}]}
