"""Deterministic evidence, quotation and decision gates. Money is HKD cents.

Inputs to this module MUST come from server-owned adapters and reviewed registries.
JSON validation cannot authenticate a merchant or establish textual entailment.
Public research can be compared, but can never become an executable quotation here.
"""
from __future__ import annotations
import copy
import hashlib
import json
from pathlib import Path
from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[1]


class Rejected(ValueError):
    pass


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def validate(kind, value):
    schema = json.loads((ROOT / "schemas/commerce.schema.json").read_text())
    selected = {"$ref": "#/$defs/" + kind, "$defs": schema["$defs"]}
    if next(Draft202012Validator(selected).iter_errors(value), None):
        raise Rejected("INVALID_" + kind.upper())


CRITICAL = ("product", "goods_minor", "fee_lines", "discounts", "fees_complete",
            "stock", "delivery", "returns", "destination_ref")


def verify_claim(quote, field, value, evidence, now, *, execution=False):
    """Exact structured-assertion binding, NOT an NLP fact-checker.

    Review/release is an external trusted process. The caller may not let a model
    set assertions, release status, expiry, connector permissions or quote fields.
    """
    eid = quote["claim_bindings"].get(field)
    e = evidence.get(eid)
    if not e:
        return False
    validate("EvidenceEnvelope", e)
    if (e["merchant_id"] != quote["merchant_id"] or e["sku"] != quote["sku"]
            or e["valid_from"] > now or e["expires_at"] <= now):
        return False
    if field not in e["assertions"] or canonical(e["assertions"][field]) != canonical(value):
        return False
    if e["extraction_sha256"] != digest(e["assertions"]):
        return False
    if execution:
        if e["review_status"] != "released":
            return False
        if quote["environment"] == "local_simulator":
            return e["provenance"] == "synthetic_fixture"
        return e["provenance"] == "authorized_merchant_api"
    return True


def calculate_cash(quote, now):
    validate("ExecutableQuote", quote)
    codes = [f["code"] for f in quote["fee_lines"]]
    if len(codes) != len(set(codes)) or not {"shipping", "payment"}.issubset(codes):
        raise Rejected("FEE_LINES_INCOMPLETE_OR_DUPLICATED")
    if not quote["fees_complete"] or any(f["amount_minor"] is None for f in quote["fee_lines"]):
        raise Rejected("FEES_UNKNOWN")
    # JSON Schema permits integral floats for type=integer. The ledger does not.
    amounts = [quote["goods_minor"]] + [f["amount_minor"] for f in quote["fee_lines"]] + [d["amount_minor"] for d in quote["discounts"]]
    if any(type(amount) is not int for amount in amounts):
        raise Rejected("MONEY_MUST_BE_INTEGER_MINOR_UNITS")
    discount = 0
    # Legacy quotes lack coupon-instance identity; reject repeated rule IDs
    # conservatively rather than double count an unidentifiable application.
    rule_ids = [d["rule_id"] for d in quote["discounts"]]
    if len(rule_ids) != len(set(rule_ids)):
        raise Rejected("DUPLICATE_DISCOUNT_INSTANCE")
    for d in quote["discounts"]:
        if d["eligibility"] != "eligible" or d["expires_at"] <= now:
            raise Rejected("DISCOUNT_NOT_VERIFIED")
        discount += d["amount_minor"]
    total = quote["goods_minor"] + sum(f["amount_minor"] for f in quote["fee_lines"]) - discount
    if total <= 0:
        raise Rejected("INVALID_CASH_TOTAL")
    return total  # no subtraction for future points or cashback


def evaluate_quote(quote, task, evidence, now):
    validate("ExecutableQuote", quote)
    validate("TaskConstraints", task)
    if type(task["cash_cap_minor"]) is not int:
        raise Rejected("CASH_CAP_MUST_BE_INTEGER_MINOR_UNITS")
    codes = []
    if quote["observed_at"] > now or quote["expires_at"] <= now:
        codes.append("QUOTE_EXPIRED")
    if quote["fact_status"] != "verified":
        codes.append("FACTS_UNVERIFIED")
    if quote["product"] != task["product"]:
        codes.append("PRODUCT_MISMATCH")
    if quote["destination_ref"] != task["destination_ref"]:
        codes.append("DESTINATION_MISMATCH")
    if quote["stock"] != "available":
        codes.append("STOCK_UNCONFIRMED")
    if quote["delivery"]["status"] != "confirmed":
        codes.append("DELIVERY_UNCONFIRMED")
    if task["latest_delivery_epoch"] is not None:
        deadline = quote["delivery"]["guaranteed_by_epoch"]
        if deadline is None or deadline > task["latest_delivery_epoch"]:
            codes.append("DELIVERY_DEADLINE_UNSUPPORTED")
    if quote["returns"]["status"] != "confirmed":
        codes.append("RETURN_TERMS_UNCONFIRMED")
    if task["requires_change_of_mind_return"] and not quote["returns"]["change_of_mind"]:
        codes.append("RETURN_REQUIREMENT_UNMET")
    cap = quote["connector"]
    if (quote["environment"] != task["environment"] or quote["environment"] == "public_research"
            or cap["environment"] != quote["environment"] or not cap["checkout"]
            or not cap["query_original"] or not cap["payee_ref"] or cap["expires_at"] <= now):
        codes.append("EXECUTION_CAPABILITY_MISSING")
    for field in CRITICAL:
        if not verify_claim(quote, field, quote[field], evidence, now, execution=True):
            codes.append("EVIDENCE_UNSUPPORTED:" + field)
    try:
        total = calculate_cash(quote, now)
        if total > task["cash_cap_minor"]:
            codes.append("BUDGET_EXCEEDED")
    except Rejected as e:
        total = None
        codes.append(str(e))
    return {"eligible": not codes, "cash_minor": total, "blockers": codes,
            "quote_id": quote["quote_id"], "quote_version": quote["version"]}


def freeze_decision(quote, task, evidence, now):
    result = evaluate_quote(quote, task, evidence, now)
    if not result["eligible"]:
        raise Rejected("CANNOT_FREEZE:" + ",".join(result["blockers"]))
    body = {"quote": copy.deepcopy(quote), "task": copy.deepcopy(task),
            "cash_minor": result["cash_minor"], "currency": "HKD",
            "evidence_digests": {eid: digest(evidence[eid]) for eid in sorted(set(quote["claim_bindings"].values()))},
            "frozen_at": now, "calculator_version": "hk-cash-0.4.0"}
    snapshot = {"snapshot_id": digest(body), "body": body}
    validate("DecisionSnapshot", snapshot)
    return snapshot


def assert_snapshot(snapshot, quote, task, evidence, now):
    validate("DecisionSnapshot", snapshot)
    body = snapshot["body"]
    if snapshot["snapshot_id"] != digest(body):
        raise Rejected("SNAPSHOT_TAMPERED")
    if canonical(body["quote"]) != canonical(quote) or canonical(body["task"]) != canonical(task):
        raise Rejected("NEW_APPROVAL_REQUIRED")
    for eid, expected in body["evidence_digests"].items():
        if eid not in evidence or digest(evidence[eid]) != expected:
            raise Rejected("EVIDENCE_CHANGED")
    result = evaluate_quote(quote, task, evidence, now)
    if not result["eligible"] or result["cash_minor"] != body["cash_minor"]:
        raise Rejected("EXECUTION_REVALIDATION_FAILED")


def buyer_card(snapshot):
    """Render critical values from server data, never from assistant prose."""
    validate("DecisionSnapshot", snapshot)
    body = snapshot["body"]
    if snapshot["snapshot_id"] != digest(body):
        raise Rejected("SNAPSHOT_TAMPERED")
    q = body["quote"]
    return {"merchant_id": q["merchant_id"], "sku": q["sku"], "product": q["product"],
            "cash_minor": body["cash_minor"], "currency": "HKD", "quote_version": q["version"],
            "snapshot_id": snapshot["snapshot_id"], "payment_status": "NOT_REQUESTED",
            "environment": q["environment"], "delivery": q["delivery"], "returns": q["returns"]}


# Versioned application contracts. Legacy helpers above remain a separate test slice.
# These helpers consume server-owned adapter objects, never model-authored quotes.
import time

V1_PREFERENCES = ('lowest_cost', 'fastest_delivery', 'easiest_returns')
V1_PRODUCT_REQUIRED = ('brand', 'variant', 'net_content', 'unit', 'pack_count', 'packaging')
V1_CRITICAL = ('merchant_sku', 'product', 'purchase_quantity', 'unit_price_minor',
               'line_subtotal_minor', 'fee_lines', 'fees_complete', 'discounts',
               'benefit_subject_ref', 'order_scope_ref', 'stock', 'destination_ref',
               'delivery', 'returns', 'warranty', 'payee_ref', 'payee_mapping_version',
               'payment_options', 'rewards')


def _integer_v1(value, name, *, minimum=0, maximum=10**12):
    if type(value) is not int or not minimum <= value <= maximum:
        raise Rejected('INVALID_INTEGER:' + name)


def validate_task_v1(draft):
    """Return missing purchasing facts without inventing defaults for them.

    A draft may omit facts; any supplied value must already be well formed.
    Origin and regional version are optional constraints. Their absence does not
    authorize a model to describe an origin or regional version as user verified.
    """
    validate('TaskDraftV1', draft)
    product = draft.get('product') or {}
    for name in ('net_content', 'pack_count'):
        if product.get(name) is not None:
            _integer_v1(product[name], 'product.' + name, minimum=1, maximum=1000000)
    for name in ('purchase_quantity', 'cash_cap_minor', 'latest_delivery_epoch'):
        if draft.get(name) is not None:
            _integer_v1(draft[name], name, minimum=1,
                        maximum=10000 if name == 'purchase_quantity' else 10**12)
    missing = ['product.' + name for name in V1_PRODUCT_REQUIRED
               if product.get(name) in (None, '')]
    missing += [name for name in ('purchase_quantity', 'cash_cap_minor', 'destination_ref')
                if draft.get(name) in (None, '')]
    return missing


def catalog_v1(draft, scenario='normal', now=None):
    """Explicitly synthetic adapter used only for the local application exercise."""
    from .fixtures import make_catalog_v1
    missing = validate_task_v1(draft)
    if missing:
        raise Rejected('TASK_INCOMPLETE:' + ','.join(missing))
    return make_catalog_v1(draft, scenario=scenario, now=now)


def _discount_total_v1(quote, now):
    """Coupon instance identity is independent of the application/rule row ID."""
    seen, exclusive, total = set(), set(), 0
    discounts = quote['discounts']
    for d in discounts:
        _integer_v1(d['amount_minor'], 'discount.amount_minor')
        if d['eligibility'] != 'eligible' or d['expires_at'] <= now:
            raise Rejected('DISCOUNT_NOT_VERIFIED')
        if (d['subject_ref'] != quote['benefit_subject_ref']
                or d['order_scope_ref'] != quote['order_scope_ref']):
            raise Rejected('DISCOUNT_SCOPE_MISMATCH')
        instance = (d['subject_ref'], d['coupon_instance_id'])
        if instance in seen:
            raise Rejected('DUPLICATE_DISCOUNT_INSTANCE')
        seen.add(instance)
        group = d['exclusive_group']
        if group is not None:
            if group in exclusive:
                raise Rejected('DISCOUNT_EXCLUSIVE_GROUP')
            exclusive.add(group)
        total += d['amount_minor']
    # Both rules must affirm stacking; silence is not permission.
    for i, d in enumerate(discounts):
        for other in discounts[i + 1:]:
            if (other['rule_id'] not in d['stackable_with']
                    or d['rule_id'] not in other['stackable_with']):
                raise Rejected('DISCOUNT_STACKING_UNVERIFIED')
    return total


def _assert_quote_evidence_v1(quote, now):
    for field in V1_CRITICAL:
        eid = quote['claim_bindings'].get(field)
        e = quote['evidence'].get(eid)
        if not e:
            raise Rejected('EVIDENCE_UNSUPPORTED:' + field)
        validate('EvidenceV1', e)
        if (e['evidence_id'] != eid or e['merchant_id'] != quote['merchant_id']
                or e['merchant_sku'] != quote['merchant_sku']
                or e['provenance'] != quote['provenance']
                or e['review_status'] != 'released'
                or e['observed_at'] > now or e['expires_at'] <= now
                or e['assertions_digest'] != digest(e['assertions'])
                or field not in e['assertions']
                or canonical(e['assertions'][field]) != canonical(quote[field])):
            raise Rejected('EVIDENCE_UNSUPPORTED:' + field)
        # A fixture URL/hash is an integrity check for exercise data, not a claim
        # of merchant access, fact verification or a production trust signature.
        if quote['provenance'] == 'synthetic_fixture' and not e['source_url'].startswith('fixture://'):
            raise Rejected('SYNTHETIC_SOURCE_MISLABELED')


def _reject_fractional_contract_numbers_v1(value):
    # All V1 numeric contract fields are integer minor units/counts/epochs.
    # jsonschema accepts 1.0 as integer; the persisted ledger must not.
    if isinstance(value, float):
        raise Rejected('INTEGER_FIELDS_REQUIRED')
    if isinstance(value, dict):
        for item in value.values():
            _reject_fractional_contract_numbers_v1(item)
    elif isinstance(value, list):
        for item in value:
            _reject_fractional_contract_numbers_v1(item)


def evaluate_v1(quote, draft, now=None):
    """Hard constraints and cash arithmetic; preferences never relax a hard gate."""
    now = int(time.time()) if now is None else now
    _integer_v1(now, 'now')
    missing = validate_task_v1(draft)
    validate('QuoteV1', quote)
    _reject_fractional_contract_numbers_v1(quote)
    blockers = ['TASK_INCOMPLETE:' + name for name in missing]
    for name in ('version', 'purchase_quantity', 'unit_price_minor', 'line_subtotal_minor',
                 'payee_mapping_version'):
        _integer_v1(quote[name], name, minimum=1 if name in
                    ('version', 'purchase_quantity', 'payee_mapping_version') else 0)
    if quote['line_subtotal_minor'] != quote['unit_price_minor'] * quote['purchase_quantity']:
        blockers.append('GOODS_QUANTITY_AMOUNT_MISMATCH')
    if quote['purchase_quantity'] != draft.get('purchase_quantity'):
        blockers.append('PURCHASE_QUANTITY_MISMATCH')
    for key, expected in (draft.get('product') or {}).items():
        if expected is not None and expected != quote['product'].get(key):
            blockers.append('PRODUCT_MISMATCH:' + key)
    if quote['destination_ref'] != draft.get('destination_ref'):
        blockers.append('DESTINATION_MISMATCH')
    if quote['observed_at'] > now or quote['expires_at'] <= now:
        blockers.append('QUOTE_EXPIRED')
    if quote['stock'] != 'available':
        blockers.append('STOCK_UNCONFIRMED')
    if quote['delivery']['status'] != 'confirmed':
        blockers.append('DELIVERY_UNCONFIRMED')
    deadline = draft.get('latest_delivery_epoch')
    promised = quote['delivery']['guaranteed_by_epoch']
    if promised is not None and promised <= now:
        blockers.append('DELIVERY_GUARANTEE_EXPIRED')
    if deadline is not None and (promised is None or promised > deadline):
        blockers.append('DELIVERY_DEADLINE_UNSUPPORTED')
    if quote['returns']['status'] != 'confirmed':
        blockers.append('RETURN_TERMS_UNCONFIRMED')
    if draft.get('requires_change_of_mind_return') and not quote['returns']['change_of_mind']:
        blockers.append('RETURN_REQUIREMENT_UNMET')
    if not any(option['eligibility'] == 'eligible' and option['provenance'] == 'synthetic_fixture'
               and option['option_id'] == 'local-psp' and option['observed_at'] <= now
               for option in quote['payment_options']):
        blockers.append('PAYMENT_OPTION_UNVERIFIED')
    if (quote['environment'] != 'local_simulator'
            or quote['provenance'] != 'synthetic_fixture'
            or quote['submission_guard'] != 'exact_terms'):
        # Stop point one deliberately has no authorized external execution adapter.
        blockers.append('EXECUTION_CAPABILITY_MISSING')
    codes = [f['code'] for f in quote['fee_lines']]
    goods = quote['line_subtotal_minor']
    fees = discount = cash = None
    try:
        if len(codes) != len(set(codes)) or not {'shipping', 'payment'}.issubset(codes):
            raise Rejected('FEE_LINES_INCOMPLETE_OR_DUPLICATED')
        if not quote['fees_complete'] or any(f['amount_minor'] is None for f in quote['fee_lines']):
            raise Rejected('FEES_UNKNOWN')
        for f in quote['fee_lines']:
            _integer_v1(f['amount_minor'], 'fee.' + f['code'])
        fees = sum(f['amount_minor'] for f in quote['fee_lines'])
        discount = _discount_total_v1(quote, now)
        cash = goods + fees - discount
        if cash <= 0 or cash > 10**12:
            raise Rejected('INVALID_CASH_TOTAL')
        cap = draft.get('cash_cap_minor')
        if cap is not None and cash > cap:
            blockers.append('BUDGET_EXCEEDED')
    except Rejected as exc:
        blockers.append(str(exc))
        cash = None
    try:
        _assert_quote_evidence_v1(quote, now)
    except Rejected as exc:
        blockers.append(str(exc))
    # Expected rewards never reduce cash. Each reward retains its own rule source,
    # observation, eligibility and redemption conditions; no invented HKD value.
    return {'quote_id': quote['quote_id'], 'quote_version': quote['version'],
            'merchant_id': quote['merchant_id'], 'eligible': not blockers,
            'blockers': blockers, 'cash_minor': cash, 'goods_minor': goods,
            'fees_minor': fees, 'discount_minor': discount,
            'delivery': copy.deepcopy(quote['delivery']), 'returns': copy.deepcopy(quote['returns'])}


def rank_v1(quotes, draft, now=None):
    """Transparent user-selected rule, applied only after hard constraints pass."""
    now = int(time.time()) if now is None else now
    evaluations = [evaluate_v1(q, draft, now=now) for q in quotes]
    eligible = [e for e in evaluations if e['eligible']]
    preference = draft.get('preference') or 'lowest_cost'
    if preference not in V1_PREFERENCES:
        raise Rejected('INVALID_PREFERENCE')
    def key(e):
        tie = (e['cash_minor'], e['quote_id'])
        if preference == 'fastest_delivery':
            deadline = e['delivery']['guaranteed_by_epoch']
            return (deadline is None, deadline or 10**12) + tie
        if preference == 'easiest_returns':
            r = e['returns']
            return (not r['change_of_mind'], r['return_fee_minor'] is None,
                    r['return_fee_minor'] or 0, -(r['window_days'] or 0)) + tie
        return tie
    eligible.sort(key=key)
    selected = eligible[0] if eligible else None
    explanations = {
        'lowest_cost': '在符合全部硬条件的候选中，按本次现金总支出排序。',
        'fastest_delivery': '在符合全部硬条件的候选中，先按已确认送达期限排序，再比较现金总支出。',
        'easiest_returns': '在符合全部硬条件的候选中，优先可改变主意退货，再比较已知退货费用和退货期限。',
    }
    reason = explanations[preference] if selected else '没有同时满足硬条件且具备执行能力的候选，保留停止状态。'
    if not draft.get('preference'):
        reason += '未设置偏好，已明确采用默认规则：优先省钱。'
    tradeoffs = []
    if selected:
        cheapest = min(eligible, key=lambda e: (e['cash_minor'], e['quote_id']))
        if selected['cash_minor'] > cheapest['cash_minor']:
            tradeoffs.append({'kind': 'additional_cash_for_preference',
                              'additional_cash_minor': selected['cash_minor'] - cheapest['cash_minor'],
                              'compared_quote_id': cheapest['quote_id']})
        if preference == 'fastest_delivery' and selected['delivery']['guaranteed_by_epoch'] is None:
            tradeoffs.append({'kind': 'delivery_time_unknown'})
    return {'candidates': evaluations, 'selected_quote_id': selected['quote_id'] if selected else None,
            'preference': preference, 'reason': reason, 'tradeoffs': tradeoffs}


def freeze_v1(task_id, constraints_version, draft, quote, now=None):
    now = int(time.time()) if now is None else now
    _integer_v1(constraints_version, 'constraints_version', minimum=1)
    if not isinstance(task_id, str) or not task_id:
        raise Rejected('TASK_ID_REQUIRED')
    calculation = evaluate_v1(quote, draft, now=now)
    if not calculation['eligible']:
        raise Rejected('CANNOT_FREEZE:' + ','.join(calculation['blockers']))
    normalized_draft = copy.deepcopy(draft)
    normalized_draft['preference'] = draft.get('preference') or 'lowest_cost'
    body = {'task_id': task_id, 'constraints_version': constraints_version,
            'fact_version': quote['version'], 'quote': copy.deepcopy(quote),
            'draft': normalized_draft, 'calculation': calculation,
            'payee_ref': quote['payee_ref'], 'payee_mapping_version': quote['payee_mapping_version'],
            'expires_at': min(quote['expires_at'], now + 300), 'frozen_at': now,
            'calculator_version': 'hk-cash-v1.0'}
    checksum = digest(body)
    snapshot = dict(body, snapshot_id='snap_' + checksum, digest=checksum)
    validate('SnapshotV1', snapshot)
    return snapshot
