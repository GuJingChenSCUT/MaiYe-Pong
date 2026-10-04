"""Five exact local-demo prompts, not general product understanding or authority.

Only the server-authenticated local demo buyer in scripted mode can use these
fixtures. No request, order, payment, credential or approval is created here.
Unmatched text never receives a product default. The synthetic catalog remains
the same catalog_v1 used by every existing domain and transaction check.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass

from app.auth import APIError
from app.local_demo_auth import LOCAL_DEMO_ACTORS, LOCAL_DEMO_TENANT
from slice04.fixtures import V1_PRODUCT

DEMO_ACTORS = LOCAL_DEMO_ACTORS | {'local-demo-buyer'}


@dataclass(frozen=True)
class DemoCase:
    case_id: str
    prompt: str
    cash_cap_minor: int | None
    scenario: str
    expected_status: str


CASES = (
    DemoCase('normal', '幫我補購1件100克原味番梘，送到九龍，連運費預算HK$100，揀總價最低嘅方案。',
             10000, 'normal', 'AWAITING_APPROVAL'),
    DemoCase('clarification', '幫我補購1件100克原味番梘，送到九龍，揀總價最低嘅方案，預算我想先確認。',
             None, 'normal', 'CLARIFYING'),
    DemoCase('shipping_increase', '幫我補購1件100克原味番梘，送到九龍，連運費預算HK$60；如果運費加咗，重新比較兩家。',
             6000, 'shipping_increase', 'AWAITING_APPROVAL'),
    DemoCase('low_budget', '幫我補購1件100克原味番梘，送到九龍，連運費最多HK$40，超過就停低。',
             4000, 'normal', 'STOPPED'),
    DemoCase('both_unavailable', '幫我補購1件100克原味番梘，送到九龍，連運費預算HK$100；如果兩家都冇貨，就停低。',
             10000, 'both_unavailable', 'STOPPED'),
)


def case_fields(case):
    fields = {'product': copy.deepcopy(V1_PRODUCT), 'purchase_quantity': 1,
              'destination_ref': 'HK-DEMO-KOWLOON', 'preference': 'lowest_cost',
              'requires_change_of_mind_return': False}
    if case.cash_cap_minor is not None:
        fields['cash_cap_minor'] = case.cash_cap_minor
    return fields


def resolve_demo_case(actor, *, mode, text, fields, scenario=None):
    """Return an independent expansion, or None outside the exact demo scope.

`normal` is the existing compose form's default scenario, so matching a fee or
stock case may route it. Any non-normal explicit scenario must agree. Explicit
fields must agree with facts fixed by the matched prompt; unknown facts such as
the clarification case's budget may be supplied, and are kept unchanged.
"""
    if (not isinstance(actor, dict) or actor.get('tenant_id') != LOCAL_DEMO_TENANT
            or actor.get('actor_id') not in DEMO_ACTORS or actor.get('role') != 'buyer'
            or mode != 'scripted'):
        return None
    case = next((item for item in CASES if item.prompt == text), None)
    if case is None:
        return None
    if not isinstance(fields, dict):
        raise APIError(400, 'UNKNOWN_TASK_FIELD')
    if scenario not in (None, 'normal', case.scenario):
        raise APIError(409, 'DEMO_CASE_CONFLICT', '演練句子與已選場景不一致，請先確認。')
    defaults = case_fields(case)
    # Work on independent copies: neither caller input nor shared fixtures mutate.
    result = copy.deepcopy(fields)
    for name, expected in defaults.items():
        if name not in result:
            result[name] = expected
        elif isinstance(expected, dict) and isinstance(result[name], dict):
            for key, value in expected.items():
                if key not in result[name]:
                    result[name][key] = value
                elif type(result[name][key]) is not type(value) or result[name][key] != value:
                    raise APIError(409, 'DEMO_CASE_CONFLICT', '演練句子與已填規格不一致，請先確認。')
        elif type(result[name]) is not type(expected) or result[name] != expected:
            raise APIError(409, 'DEMO_CASE_CONFLICT', '演練句子與已填條件不一致，請先確認。')
    if 'text' in result and result['text'] != text:
        raise APIError(409, 'DEMO_CASE_CONFLICT', '演練句子與已填文字不一致，請先確認。')
    return {'case_id': case.case_id, 'fields': result, 'scenario': case.scenario}
