"""Rebuild the proposed v0.4 local schemas. Does not alter v0.3 contracts."""
import json
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
def obj(p): return {"type":"object","properties":p,"required":list(p),"additionalProperties":False}
def enum(*v): return {"enum":list(v)}
def arr(v): return {"type":"array","items":v}
S={"type":"string","minLength":1}
I={"type":"integer","minimum":0,"maximum":10**12}
N={"anyOf":[I,{"type":"null"}]}
B={"type":"boolean"}
ENV=enum("local_simulator","provider_sandbox","production","public_research")
P=obj({"brand":S,"variant":S,"net_content":{"type":"integer","minimum":1},"unit":enum("g","ml","item"),
       "pack_count":{"type":"integer","minimum":1},"packaging":S,"origin":{"anyOf":[S,{"type":"null"}]}})
D=obj({"status":enum("confirmed","unknown","conflicting"),"guaranteed_by_epoch":N,"policy_id":S})
R=obj({"status":enum("confirmed","unknown","conflicting"),"change_of_mind":B,"policy_id":S})
C=obj({"environment":ENV,"checkout":B,"query_original":B,"payee_ref":{"anyOf":[S,{"type":"null"}]},"expires_at":I})
Fee=obj({"code":S,"amount_minor":N})
Benefit=obj({"amount_minor":I,"eligibility":enum("eligible","ineligible","unknown"),"expires_at":I,"rule_id":S})
Q=obj({"quote_id":S,"version":{"type":"integer","minimum":1},"environment":ENV,"merchant_id":S,"sku":S,
       "product":P,"currency":{"const":"HKD"},"goods_minor":I,"fee_lines":arr(Fee),"discounts":arr(Benefit),
       "fees_complete":B,"stock":enum("available","unavailable","unknown"),"destination_ref":S,"delivery":D,"returns":R,
       "observed_at":I,"expires_at":I,"fact_status":enum("verified","partial","conflicting"),"connector":C,
       "claim_bindings":{"type":"object","additionalProperties":S}})
Task=obj({"product":P,"cash_cap_minor":I,"destination_ref":S,"latest_delivery_epoch":N,"requires_change_of_mind_return":B,"environment":ENV})
Evidence=obj({"evidence_id":S,"merchant_id":S,"sku":S,"source_url":S,
              "provenance":enum("synthetic_fixture","public_research","authorized_merchant_api"),
              "review_status":enum("research_only","released","revoked"),"valid_from":I,"expires_at":I,
              "assertions":{"type":"object"},"extraction_sha256":{"type":"string","pattern":"^[a-f0-9]{64}$"},
              "hash_scope":{"const":"structured_assertions_only"}})
Assessment=obj({"eligible":B,"cash_minor":N,"blockers":arr(S),"quote_id":S,"quote_version":{"type":"integer","minimum":1}})
Hash={"type":"string","pattern":"^[a-f0-9]{64}$"}
SnapshotBody=obj({"quote":Q,"task":Task,"cash_minor":I,"currency":{"const":"HKD"},
                  "evidence_digests":{"type":"object","minProperties":1,"additionalProperties":Hash},
                  "frozen_at":I,"calculator_version":{"const":"hk-cash-0.4.0"}})
Snapshot=obj({"snapshot_id":Hash,"body":SnapshotBody})
schema={"$schema":"https://json-schema.org/draft/2020-12/schema","$id":"https://example.invalid/hacku/proposed/0.4/commerce.schema.json",
        "$defs":{"ProductIdentity":P,"ExecutableQuote":Q,"FulfillmentPolicy":D,"ReturnPolicy":R,
                 "BenefitEligibility":Benefit,"ConnectorCapability":C,"EvidenceEnvelope":Evidence,"TaskConstraints":Task,"Assessment":Assessment,
                 "DecisionSnapshot":Snapshot}}
# The legacy 0.4 definitions above remain unchanged for regression fixtures.
# V1 has explicit purchasing units, lifecycle-bound coupons and draft contracts.
Positive={"type":"integer","minimum":1,"maximum":10**12}
NullableString={"anyOf":[S,{"type":"null"}]}
def nullable(v): return {"anyOf":[v,{"type":"null"}]}
PV1=obj({"brand":S,"variant":S,"net_content":Positive,"unit":enum("g","ml","item"),
         "pack_count":Positive,"packaging":S,"origin":NullableString,"region_version":NullableString})
ProductDraft={"type":"object","properties":{k:nullable(v) for k,v in PV1["properties"].items()},"additionalProperties":False}
TaskDraftV1={"type":"object","properties":{
    "text":{"type":"string","maxLength":20000},"product":nullable(ProductDraft),
    "purchase_quantity":nullable({"type":"integer","minimum":1,"maximum":10000}),
    "cash_cap_minor":nullable(Positive),"destination_ref":NullableString,
    "preference":nullable(enum("lowest_cost","fastest_delivery","easiest_returns")),
    "requires_change_of_mind_return":nullable(B),"latest_delivery_epoch":nullable(Positive)
},"additionalProperties":False}
DeliveryV1=obj({"status":enum("confirmed","unknown","conflicting"),"guaranteed_by_epoch":N,"policy_id":S,
    "segments":arr(obj({"segment_id":S,"origin_region":S,"destination_region":S,
                        "carrier_ref":NullableString,"responsible_party":S,"fee_minor":N}))})
ReturnsV1=obj({"status":enum("confirmed","unknown","conflicting"),"change_of_mind":B,"policy_id":S,
               "window_days":N,"return_fee_minor":N,"responsible_party":S})
DiscountV1=obj({"coupon_instance_id":S,"subject_ref":S,"order_scope_ref":S,"rule_id":S,
                "amount_minor":I,"eligibility":enum("eligible","ineligible","unknown"),"expires_at":I,
                "stackable_with":{"type":"array","items":S,"uniqueItems":True},"exclusive_group":NullableString})
EvidenceV1=obj({"evidence_id":S,"merchant_id":S,"merchant_sku":S,"source_url":S,
    "provenance":enum("synthetic_fixture","public_research","authorized_merchant_api"),
    "review_status":enum("released","research_only","revoked"),"observed_at":I,"expires_at":I,
    "assertions":{"type":"object"},"assertions_digest":Hash})
PaymentOptionV1=obj({"option_id":S,"label":S,"eligibility":enum("eligible","ineligible","unknown"),
    "provenance":enum("synthetic_fixture","authorized_provider"),"source_ref":S,"observed_at":I})
RewardV1=obj({"kind":enum("expected_cashback","expected_points"),"amount_minor":N,
    "points":N,"eligibility":enum("eligible","ineligible","unknown"),"rule_id":S,"source_ref":S,
    "observed_at":I,"redemption_conditions":S,"cash_equivalent_minor":N})
QuoteV1=obj({"quote_id":S,"version":Positive,"merchant_id":S,"merchant_sku":S,"product":PV1,
    "purchase_quantity":{"type":"integer","minimum":1,"maximum":10000},"unit_price_minor":I,"line_subtotal_minor":I,
    "currency":{"const":"HKD"},"fee_lines":arr(Fee),"fees_complete":B,"discounts":arr(DiscountV1),
    "benefit_subject_ref":S,"order_scope_ref":S,"stock":enum("available","unavailable","unknown"),
    "destination_ref":S,"delivery":DeliveryV1,"returns":ReturnsV1,
    "warranty":obj({"responsible_party":S,"policy_id":S}),"payee_ref":S,"payee_mapping_version":Positive,
    "observed_at":I,"expires_at":I,"environment":ENV,
    "provenance":enum("synthetic_fixture","public_research","authorized_merchant_api"),
    "submission_guard":enum("exact_terms","research_only"),"description":{"type":"string","maxLength":20000},
    "payment_options":{"type":"array","items":PaymentOptionV1,"minItems":1},"rewards":arr(RewardV1),
    "evidence":{"type":"object","additionalProperties":EvidenceV1},
    "claim_bindings":{"type":"object","additionalProperties":S}})
EvaluationV1=obj({"quote_id":S,"quote_version":Positive,"merchant_id":S,"eligible":B,"blockers":arr(S),
    "cash_minor":N,"goods_minor":I,"fees_minor":N,"discount_minor":N,"delivery":DeliveryV1,"returns":ReturnsV1})
SnapshotV1=obj({"snapshot_id":S,"digest":Hash,"task_id":S,"constraints_version":Positive,"fact_version":Positive,
    "quote":QuoteV1,"draft":TaskDraftV1,"calculation":EvaluationV1,"payee_ref":S,"payee_mapping_version":Positive,
    "expires_at":I,"frozen_at":I,"calculator_version":{"const":"hk-cash-v1.0"}})
schema["$defs"].update({"ProductV1":PV1,"TaskDraftV1":TaskDraftV1,"QuoteV1":QuoteV1,
    "DiscountV1":DiscountV1,"EvidenceV1":EvidenceV1,"EvaluationV1":EvaluationV1,"SnapshotV1":SnapshotV1})
(ROOT/'schemas/commerce.schema.json').write_text(json.dumps(schema,ensure_ascii=False,indent=2)+'\n')
print('Wrote legacy 0.4 and V1 local contracts; no provider capability is implied.')
