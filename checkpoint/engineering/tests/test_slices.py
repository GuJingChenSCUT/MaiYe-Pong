import copy,json,math,os,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from slice04.domain import (Rejected,digest,calculate_cash,evaluate_quote,freeze_decision,assert_snapshot,buyer_card,verify_claim)
from slice04.fixtures import make_fixture,refresh_evidence
from slice04.agent import CommerceTools,run
from slice04.benchmark import summarize,CHECKS

class CommerceBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.now=2_000_000_000
        self.task,self.offers,self.evidence=make_fixture(self.now)
        self.q=self.offers['A']
    def gate(self):return evaluate_quote(self.q,self.task,self.evidence,self.now)
    def test_complete_offer_can_be_frozen(self):
        card=buyer_card(freeze_decision(self.q,self.task,self.evidence,self.now))
        self.assertEqual(card['cash_minor'],5000);self.assertEqual(card['payment_status'],'NOT_REQUESTED')
    def test_unknown_shipping_is_not_zero(self):
        self.q['fee_lines'][0]['amount_minor']=None
        with self.assertRaisesRegex(Rejected,'FEES_UNKNOWN'):calculate_cash(self.q,self.now)
    def test_missing_fee_line_rejected(self):
        self.q['fee_lines']=[]
        with self.assertRaises(Rejected):calculate_cash(self.q,self.now)
    def test_duplicate_fee_line_rejected(self):
        self.q['fee_lines'].append(self.q['fee_lines'][0])
        with self.assertRaises(Rejected):calculate_cash(self.q,self.now)
    def test_boolean_money_rejected(self):
        self.q['goods_minor']=True
        with self.assertRaises(Rejected):self.gate()
    def test_currency_not_hkd_rejected(self):
        self.q['currency']='CNY'
        with self.assertRaises(Rejected):self.gate()
    def test_expired_quote_rejected(self):
        self.q['expires_at']=self.now
        self.assertIn('QUOTE_EXPIRED',self.gate()['blockers'])
    def test_future_observation_rejected(self):
        self.q['observed_at']=self.now+1
        self.assertFalse(self.gate()['eligible'])
    def test_origin_mismatch_rejected(self):
        self.q['product']['origin']='other-region';refresh_evidence(self.q,self.evidence)
        self.assertIn('PRODUCT_MISMATCH',self.gate()['blockers'])
    def test_equal_total_weight_different_pack_rejected(self):
        self.q['product']['net_content']=50;self.q['product']['pack_count']=2
        self.assertIn('PRODUCT_MISMATCH',self.gate()['blockers'])
    def test_another_destination_rejected(self):
        self.q['destination_ref']='another-address'
        self.assertIn('DESTINATION_MISMATCH',self.gate()['blockers'])
    def test_estimate_does_not_meet_hard_deadline(self):
        self.task['latest_delivery_epoch']=self.now+86400
        self.assertIn('DELIVERY_DEADLINE_UNSUPPORTED',self.gate()['blockers'])
    def test_return_requirement_must_match(self):
        self.task['requires_change_of_mind_return']=True
        self.assertIn('RETURN_REQUIREMENT_UNMET',self.gate()['blockers'])
    def test_unknown_discount_not_subtracted(self):
        self.q['discounts']=[{'amount_minor':1000,'eligibility':'unknown','expires_at':self.now+100,'rule_id':'fixture'}]
        with self.assertRaisesRegex(Rejected,'DISCOUNT_NOT_VERIFIED'):calculate_cash(self.q,self.now)
    def test_expired_discount_rejected(self):
        self.q['discounts']=[{'amount_minor':1000,'eligibility':'eligible','expires_at':self.now,'rule_id':'fixture'}]
        with self.assertRaises(Rejected):calculate_cash(self.q,self.now)
    def test_cash_above_budget_rejected(self):
        self.q['fee_lines'][0]['amount_minor']=2000;refresh_evidence(self.q,self.evidence)
        self.assertIn('BUDGET_EXCEEDED',self.gate()['blockers'])
    def test_missing_evidence_rejected(self):
        self.evidence.clear();self.assertFalse(self.gate()['eligible'])
    def test_wrong_merchant_evidence_rejected(self):
        eid=self.q['claim_bindings']['goods_minor'];self.evidence[eid]['merchant_id']='other'
        self.assertFalse(verify_claim(self.q,'goods_minor',5000,self.evidence,self.now))
    def test_corrupted_extraction_rejected(self):
        eid=self.q['claim_bindings']['goods_minor'];self.evidence[eid]['assertions']['goods_minor']=6000
        self.assertFalse(self.gate()['eligible'])
    def test_public_research_never_authorizes_payment(self):
        self.q['environment']='public_research';self.task['environment']='public_research'
        self.assertIn('EXECUTION_CAPABILITY_MISSING',self.gate()['blockers'])
    def test_read_only_connector_rejected(self):
        self.q['connector']['checkout']=False
        self.assertFalse(self.gate()['eligible'])
    def test_expired_capability_rejected(self):
        self.q['connector']['expires_at']=self.now
        self.assertFalse(self.gate()['eligible'])
    def test_snapshot_changes_require_new_approval(self):
        snap=freeze_decision(self.q,self.task,self.evidence,self.now)
        self.q['connector']['payee_ref']='different-payee'
        with self.assertRaisesRegex(Rejected,'NEW_APPROVAL_REQUIRED'):assert_snapshot(snap,self.q,self.task,self.evidence,self.now)
    def test_evidence_revocation_after_approval_blocks(self):
        snap=freeze_decision(self.q,self.task,self.evidence,self.now)
        self.evidence[self.q['claim_bindings']['goods_minor']]['review_status']='revoked'
        with self.assertRaisesRegex(Rejected,'EVIDENCE_CHANGED'):assert_snapshot(snap,self.q,self.task,self.evidence,self.now)
    def test_tampered_card_cannot_show_changed_money(self):
        snap=freeze_decision(self.q,self.task,self.evidence,self.now);snap['body']['cash_minor']=1
        with self.assertRaisesRegex(Rejected,'SNAPSHOT_TAMPERED'):buyer_card(snap)
    def test_valid_claim_shape_does_not_verify_false_amount(self):
        self.assertFalse(verify_claim(self.q,'goods_minor',1,self.evidence,self.now))
    def test_integral_float_money_is_not_ledger_integer(self):
        self.q['goods_minor']=5000.0
        with self.assertRaisesRegex(Rejected,'MONEY_MUST_BE_INTEGER_MINOR_UNITS'):calculate_cash(self.q,self.now)
    def test_integral_float_cap_is_not_ledger_integer(self):
        self.task['cash_cap_minor']=6000.0
        with self.assertRaisesRegex(Rejected,'CASH_CAP_MUST_BE_INTEGER_MINOR_UNITS'):self.gate()

class AgentBoundaryTests(unittest.TestCase):
    def test_offline_replans_from_a_to_b(self):
        r=run('offline','shipping_increase');self.assertEqual(r['decision']['selected_offer'],'B')
        self.assertFalse(r['model_online_verified']);self.assertFalse(r['payment_invoked'])
    def test_offline_stops_after_both_unavailable(self):
        self.assertEqual(run('offline','both_unavailable')['decision']['action'],'stop')
    def test_offline_normal_path_is_not_always_rejected(self):
        self.assertEqual(run('offline','no_change')['decision']['selected_offer'],'A')
    def test_offline_injection_has_no_privileged_tool(self):
        r=run('offline','injection');self.assertEqual(r['decision']['selected_offer'],'B')
        self.assertFalse(r['payment_invoked']) # gateway test, NOT a live-model robustness claim
    def test_no_key_has_no_fake_fallback(self):
        with patch.dict(os.environ,{'DEEPSEEK_API_KEY':''}):
            with self.assertRaisesRegex(Rejected,'DEEPSEEK_API_KEY_MISSING'):run('live')
    def test_hallucinated_final_without_tools_is_rejected(self):
        c=CommerceTools()
        with self.assertRaisesRegex(Rejected,'MISSING_INITIAL_ASSESSMENT'):
            c.finalize('{"action":"propose","selected_offer":"B","reason_code":"LOWEST_FEASIBLE_CASH"}')
    def test_fabricated_payment_state_in_final_is_rejected(self):
        with self.assertRaisesRegex(Rejected,'FINAL_SCHEMA_INVALID'):
            CommerceTools().finalize('{"payment_status":"SUCCEEDED"}')
    def test_no_unearned_safety_claim_when_refresh_skipped(self):
        c=CommerceTools();c.call('get_quote',{'offer_id':'A'});c.call('get_quote',{'offer_id':'B'});c.call('assess_candidate',{'offer_id':'A'})
        with self.assertRaises(Rejected):c.finalize('{"action":"propose","selected_offer":"A","reason_code":"LOWEST_FEASIBLE_CASH"}')

class ValueBoundaryTests(unittest.TestCase):
    def row(self,arm='manual'):
        return {'record_kind':'observed','participant_id':'P001','task_family':'T1','evidence_version':'E1','arm':arm,
                'elapsed_seconds':120,'outcome':'completed','correctness':{k:True for k in CHECKS},
                'merchant_seconds':None,'merchant_hkd_per_hour':None}
    def test_empty_results_not_measured(self):self.assertEqual(summarize([])['status'],'not_measured')
    def test_missing_merchant_rate_stays_unknown(self):self.assertIsNone(summarize([self.row()])['arms']['manual']['merchant_labor_hkd'])
    def test_failure_kept_in_denominator(self):
        a=self.row('agent');a['outcome']='failed';a['correctness']['cash_total']=False
        r=summarize([self.row(),a]);self.assertEqual(r['arms']['agent']['attempts'],1)
        self.assertEqual(r['paired'][0]['both_correct_completed_pairs'],0)
    def test_synthetic_results_cannot_be_value_evidence(self):
        r=self.row();r['record_kind']='synthetic'
        with self.assertRaises(Rejected):summarize([r])
    def test_duplicate_arms_rejected(self):
        with self.assertRaises(Rejected):summarize([self.row(),self.row()])
    def test_nan_time_rejected(self):
        r=self.row();r['elapsed_seconds']=math.nan
        with self.assertRaises(Rejected):summarize([r])
    def test_different_snapshot_not_paired(self):
        a=self.row('agent');a['evidence_version']='E2'
        self.assertEqual(summarize([self.row(),a])['paired'][0]['matched_pairs'],0)
    def test_correctly_paired_time_difference(self):
        a=self.row('agent');a['elapsed_seconds']=80
        self.assertEqual(summarize([self.row(),a])['paired'][0]['median_seconds_saved_conditional'],40)



class ApplicationDomainV1Tests(unittest.TestCase):
    def setUp(self):
        from slice04.fixtures import V1_PRODUCT
        self.now = 2_000_000_000
        self.draft = {'text': '同规格补购一包，送香港', 'product': copy.deepcopy(V1_PRODUCT),
                      'purchase_quantity': 1, 'cash_cap_minor': 6000,
                      'destination_ref': 'fixture-hk-address', 'preference': 'lowest_cost'}
        from slice04.domain import catalog_v1
        self.catalog = catalog_v1(self.draft, now=self.now)
        self.quote = self.catalog['quotes'][0]

    def check(self, quote=None):
        from slice04.domain import evaluate_v1
        return evaluate_v1(quote or self.quote, self.draft, now=self.now)

    def refresh(self):
        from slice04.fixtures import refresh_evidence_v1
        refresh_evidence_v1(self.quote)

    def coupon(self, instance='coupon-001', rule='rule-001', amount=1000):
        return {'coupon_instance_id': instance, 'subject_ref': self.quote['benefit_subject_ref'],
                'order_scope_ref': self.quote['order_scope_ref'], 'rule_id': rule,
                'amount_minor': amount, 'eligibility': 'eligible', 'expires_at': self.now + 300,
                'stackable_with': [], 'exclusive_group': None}

    def test_draft_does_not_invent_critical_details(self):
        from slice04.domain import validate_task_v1
        missing = validate_task_v1({'text': '帮我买日用品'})
        self.assertIn('product.net_content', missing)
        self.assertIn('purchase_quantity', missing)
        self.assertIn('destination_ref', missing)

    def test_origin_not_required_when_user_did_not_constrain_it(self):
        from slice04.domain import validate_task_v1
        self.draft['product'].pop('origin')
        self.draft['product'].pop('region_version')
        self.assertEqual(validate_task_v1(self.draft), [])
        self.assertTrue(self.check()['eligible'])

    def test_arbitrary_product_does_not_silently_become_soap(self):
        from slice04.domain import catalog_v1
        self.draft['product']['variant'] = 'toothpaste'
        result = catalog_v1(self.draft, now=self.now)
        self.assertEqual(result['quotes'], [])
        self.assertEqual(result['unavailable_reason'], 'SYNTHETIC_CATALOG_NO_MATCH')

    def test_purchase_quantity_and_pack_count_are_independent(self):
        from slice04.domain import catalog_v1
        self.draft['purchase_quantity'] = 2
        self.draft['cash_cap_minor'] = 12000
        q = catalog_v1(self.draft, now=self.now)['quotes'][0]
        self.assertEqual(q['product']['pack_count'], 1)
        self.assertEqual(q['purchase_quantity'], 2)
        result = self.check(q)
        self.assertTrue(result['eligible'])
        self.assertEqual(result['goods_minor'], 10000)
        self.assertEqual(result['cash_minor'], 10000)

    def test_purchase_quantity_zero_negative_bool_float_rejected(self):
        from slice04.domain import validate_task_v1
        for value in (0, -1, True, 1.0):
            with self.subTest(value=value):
                self.draft['purchase_quantity'] = value
                with self.assertRaises(Rejected):
                    validate_task_v1(self.draft)

    def test_unknown_fee_blocks_rather_than_zero(self):
        self.quote['fee_lines'][0]['amount_minor'] = None
        self.refresh()
        result = self.check()
        self.assertFalse(result['eligible'])
        self.assertIsNone(result['cash_minor'])
        self.assertIn('FEES_UNKNOWN', result['blockers'])

    def test_line_subtotal_must_match_exact_sku_quantity(self):
        self.quote['purchase_quantity'] = 2
        self.refresh()
        result = self.check()
        self.assertIn('GOODS_QUANTITY_AMOUNT_MISMATCH', result['blockers'])
        self.assertIn('PURCHASE_QUANTITY_MISMATCH', result['blockers'])

    def test_source_sku_cannot_change_without_evidence(self):
        self.quote['merchant_sku'] = 'another-sku'
        self.assertFalse(self.check()['eligible'])

    def test_float_return_fee_cannot_enter_ledger_contract(self):
        self.quote['returns']['return_fee_minor'] = 0.0
        self.refresh()
        with self.assertRaisesRegex(Rejected, 'INTEGER_FIELDS_REQUIRED'):
            self.check()

    def test_repeated_coupon_with_new_rule_id_is_still_duplicate(self):
        first = self.coupon()
        second = self.coupon(rule='another-rule')
        first['stackable_with'] = ['another-rule']
        second['stackable_with'] = ['rule-001']
        self.quote['discounts'] = [first, second]
        self.refresh()
        result = self.check()
        self.assertIn('DUPLICATE_DISCOUNT_INSTANCE', result['blockers'])
        self.assertIsNone(result['cash_minor'])

    def test_coupon_subject_and_order_scope_both_bind(self):
        for field in ('subject_ref', 'order_scope_ref'):
            with self.subTest(field=field):
                coupon = self.coupon()
                coupon[field] = 'someone-elses-resource'
                self.quote['discounts'] = [coupon]
                self.refresh()
                self.assertIn('DISCOUNT_SCOPE_MISMATCH', self.check()['blockers'])

    def test_valid_coupon_keeps_goods_and_discount_separate(self):
        self.quote['discounts'] = [self.coupon()]
        self.refresh()
        result = self.check()
        self.assertTrue(result['eligible'])
        self.assertEqual((result['goods_minor'], result['fees_minor'], result['discount_minor'], result['cash_minor']),
                         (5000, 0, 1000, 4000))

    def test_coupon_stacking_requires_both_rules(self):
        first, second = self.coupon(), self.coupon('coupon-002', 'rule-002', 200)
        first['stackable_with'] = ['rule-002']
        self.quote['discounts'] = [first, second]
        self.refresh()
        self.assertIn('DISCOUNT_STACKING_UNVERIFIED', self.check()['blockers'])
        second['stackable_with'] = ['rule-001']
        self.refresh()
        self.assertEqual(self.check()['cash_minor'], 3800)

    def test_exclusive_group_blocks_even_when_stacking_whitelisted(self):
        first, second = self.coupon(), self.coupon('coupon-002', 'rule-002')
        first['stackable_with'], second['stackable_with'] = ['rule-002'], ['rule-001']
        first['exclusive_group'] = second['exclusive_group'] = 'one-welcome-offer'
        self.quote['discounts'] = [first, second]
        self.refresh()
        self.assertIn('DISCOUNT_EXCLUSIVE_GROUP', self.check()['blockers'])

    def test_expired_or_unknown_coupon_is_not_subtracted(self):
        self.quote['discounts'] = [self.coupon()]
        self.quote['discounts'][0]['expires_at'] = self.now
        self.refresh()
        self.assertIn('DISCOUNT_NOT_VERIFIED', self.check()['blockers'])

    def test_expected_points_do_not_reduce_cash(self):
        self.quote['rewards'] = [{'kind': 'expected_points', 'amount_minor': None, 'points': 200,
            'eligibility': 'unknown', 'rule_id': 'fixture-reward', 'source_ref': 'fixture://reward',
            'observed_at': self.now, 'redemption_conditions': '尚未核实，不能计入预算', 'cash_equivalent_minor': None}]
        self.refresh()
        self.assertEqual(self.check()['cash_minor'], 5000)

    def test_fastest_choice_can_cost_more_than_cheapest(self):
        from slice04.domain import rank_v1
        self.draft['preference'] = 'fastest_delivery'
        ranking = rank_v1(self.catalog['quotes'], self.draft, now=self.now)
        self.assertEqual(ranking['selected_quote_id'], 'fixture-B')
        self.assertEqual(ranking['tradeoffs'][0]['additional_cash_minor'], 500)

    def test_preference_never_relaxes_budget(self):
        from slice04.domain import rank_v1
        self.draft['preference'], self.draft['cash_cap_minor'] = 'fastest_delivery', 5100
        ranking = rank_v1(self.catalog['quotes'], self.draft, now=self.now)
        self.assertEqual(ranking['selected_quote_id'], 'fixture-A')

    def test_easier_returns_prefers_confirmed_change_of_mind(self):
        from slice04.domain import rank_v1
        self.draft['preference'] = 'easiest_returns'
        self.assertEqual(rank_v1(self.catalog['quotes'], self.draft, now=self.now)['selected_quote_id'], 'fixture-B')

    def test_missing_preference_discloses_default(self):
        from slice04.domain import rank_v1
        self.draft.pop('preference')
        ranking = rank_v1(self.catalog['quotes'], self.draft, now=self.now)
        self.assertEqual(ranking['preference'], 'lowest_cost')
        self.assertIn('默认规则', ranking['reason'])

    def test_shipping_change_reassesses_budget_without_fixed_story(self):
        from slice04.domain import catalog_v1, rank_v1
        changed = catalog_v1(self.draft, 'shipping_increase', now=self.now)
        ranking = rank_v1(changed['quotes'], self.draft, now=self.now)
        self.assertEqual(ranking['selected_quote_id'], 'fixture-B')
        self.assertIn('BUDGET_EXCEEDED', ranking['candidates'][0]['blockers'])

    def test_both_unavailable_returns_no_selection(self):
        from slice04.domain import catalog_v1, rank_v1
        changed = catalog_v1(self.draft, 'both_unavailable', now=self.now)
        self.assertIsNone(rank_v1(changed['quotes'], self.draft, now=self.now)['selected_quote_id'])

    def test_injection_description_cannot_change_deterministic_budget(self):
        from slice04.domain import catalog_v1, rank_v1
        changed = catalog_v1(self.draft, 'injection', now=self.now)
        ranking = rank_v1(changed['quotes'], self.draft, now=self.now)
        self.assertIn('忽略预算', changed['quotes'][0]['description'])
        self.assertIn('BUDGET_EXCEEDED', ranking['candidates'][0]['blockers'])
        self.assertEqual(ranking['selected_quote_id'], 'fixture-B')

    def test_freeze_binds_purchase_details_payee_and_task_version(self):
        from slice04.domain import freeze_v1
        snap = freeze_v1('opaque-task-id', 7, self.draft, self.quote, now=self.now)
        body = {k: v for k, v in snap.items() if k not in ('snapshot_id', 'digest')}
        self.assertEqual(digest(body), snap['digest'])
        self.assertEqual(snap['snapshot_id'], 'snap_' + snap['digest'])
        self.assertEqual(snap['constraints_version'], 7)
        self.assertEqual(snap['quote']['merchant_sku'], self.quote['merchant_sku'])
        self.assertEqual(snap['payee_ref'], self.quote['payee_ref'])
        self.quote['merchant_sku'] = 'mutated-after-freeze'
        self.assertNotEqual(snap['quote']['merchant_sku'], self.quote['merchant_sku'])

    def test_fixture_cannot_be_relabelled_as_provider_execution(self):
        self.quote['environment'] = 'provider_sandbox'
        self.assertIn('EXECUTION_CAPABILITY_MISSING', self.check()['blockers'])

    def test_legacy_duplicate_discount_regression(self):
        _, offers, _ = make_fixture(self.now)
        q = offers['A']
        coupon = {'amount_minor': 1000, 'eligibility': 'eligible', 'expires_at': self.now + 100, 'rule_id': 'same-instance'}
        q['discounts'] = [coupon, copy.deepcopy(coupon)]
        with self.assertRaisesRegex(Rejected, 'DUPLICATE_DISCOUNT_INSTANCE'):
            calculate_cash(q, self.now)

if __name__=='__main__':
    unittest.main(verbosity=2)
