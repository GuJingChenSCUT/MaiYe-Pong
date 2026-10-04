"""Safety contracts for disconnected reservations; no provider/network claims."""
from dataclasses import replace
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from integrations.adapters import DisabledTaobaoAdapter, DisabledWeChatAdapter
from integrations.contracts import (AccountReservation, BackendContext, Capability,
    CapabilityUnavailable, CallerKind, CheckoutCommand, CloseRequest, Currency,
    Integration, KernelCommandRef, MerchantChannel, Money, OperationState,
    OriginalRefundRef, OriginalTransactionRef, PurchaseBinding, QuoteEnvelope, QuoteRequest,
    RefundRequest, RefundState, SecretRef, UserAuthorization, WebhookEnvelope, MODEL_TOOL_CAPABILITIES)
from integrations.gates import inspect_checkout, inspect_handoff_url, inspect_original, inspect_original_refund, inspect_refund


class IntegrationReservationTests(unittest.TestCase):
    def setUp(self):
        self.now = 1000
        self.context = BackendContext("tenant-a", "buyer-a", CallerKind.BACKEND)
        self.binding = PurchaseBinding("tenant-a", "buyer-a", "task-a", MerchantChannel.APPROVED_DIRECT,
            "merchant-a", "sku-a", "a" * 64, 2, "hk-destination", "quote-a", 7, "b" * 64,
            Money(6000, Currency.HKD), "payee-a", 3)
        self.quote = QuoteEnvelope(self.binding, 990, 1100, True,
                                  "fees-reviewed", "product-reviewed", "delivery-reviewed", "returns-reviewed")
        self.auth = UserAuthorization("auth-a", "approval-event-a", self.binding,
            "snap_" + "c" * 64, 990, 1090, Money(6500, Currency.HKD), frozenset(Capability))
        self.command = KernelCommandRef("operation-a", "original-idempotency-key",
            "snap_" + "c" * 64, "c" * 64, "b" * 64, OperationState.DISPATCH_COMMITTED)
        self.request = CheckoutCommand(self.quote, self.auth, self.command)
        self.original = OriginalTransactionRef("tenant-a", "buyer-a", MerchantChannel.APPROVED_DIRECT,
            "merchant-a", "payee-a", 3, Money(6000, Currency.HKD), "operation-a",
            "original-idempotency-key", None, None, OperationState.UNKNOWN)
        self.original_refund = OriginalRefundRef(replace(self.original, state=OperationState.SUCCEEDED),
            "refund-operation-a", "original-refund-idempotency-key", Money(100, Currency.HKD), None, RefundState.UNKNOWN)

    def account(self, integration=Integration.WECHAT_HK):
        return AccountReservation(integration, "account-ref", SecretRef("secret://test/account-key"),
            "account-reviewed", "platform-authorization-reviewed", frozenset(Capability),
            "merchant-a", "payee-a", 3, "merchant-binding-reviewed", Currency.HKD,
            "currency-reviewed", 1200, ("https://checkout.test.invalid",), "origin-review-example-only")

    def inspect(self, request=None, account=None, context=None):
        return inspect_checkout(account or self.account(), Capability.PAYMENT_SESSION_CREATE,
            request or self.request, context or self.context, now=self.now)

    def assert_blocked(self, callback, code="ADAPTER_NOT_IMPLEMENTED"):
        with self.assertRaises(CapabilityUnavailable) as caught:
            callback()
        self.assertFalse(caught.exception.report.executable)
        self.assertEqual(caught.exception.report.implementation_state, "designed")
        self.assertIn(code, caught.exception.report.blockers)
        return caught.exception.report

    def test_money_rejects_float_boolean_zero_and_missing_currency(self):
        for value in (1.0, True, 0, -1, 10**13):
            with self.subTest(value=value), self.assertRaises(ValueError):
                Money(value, Currency.HKD)
        with self.assertRaises(ValueError):
            Money(100, "HKD")

    def test_secret_reference_never_accepts_or_displays_a_key(self):
        for value in ("sk-secret-sentinel", "https://secret.invalid/key", "", None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                SecretRef(value)
        ref = SecretRef("secret://test/account-key")
        self.assertNotIn("account-key", repr(ref))

    def test_default_missing_account_and_authorization_fail_closed(self):
        adapter = DisabledWeChatAdapter(Integration.WECHAT_HK)
        report = self.assert_blocked(lambda: adapter.create_session(replace(self.request, authorization=None), self.context, now=self.now))
        for code in ("ACCOUNT_VERIFICATION_REQUIRED", "SECRET_REFERENCE_REQUIRED", "PLATFORM_AUTHORIZATION_REQUIRED",
                     "CAPABILITY_GRANT_REQUIRED", "MERCHANT_PAYEE_BINDING_REQUIRED",
                     "ACCOUNT_CURRENCY_EVIDENCE_REQUIRED", "USER_AUTHORIZATION_REQUIRED"):
            self.assertIn(code, report.blockers)

    def test_complete_metadata_does_not_enable_any_real_capability(self):
        report = self.inspect()
        self.assertEqual(report.blockers, ("ADAPTER_NOT_IMPLEMENTED",))
        self.assertFalse(report.executable)
        adapter = DisabledWeChatAdapter(Integration.WECHAT_HK, self.account())
        self.assert_blocked(lambda: adapter.create_session(self.request, self.context, now=self.now))

    def test_model_has_no_payment_order_or_callback_authority(self):
        model = replace(self.context, kind=CallerKind.MODEL)
        self.assertIn("BACKEND_ONLY_MODEL_HAS_NO_TRANSACTION_AUTHORITY", self.inspect(context=model).blockers)
        report = inspect_original(self.account(), Capability.WEBHOOK_VERIFY, self.original, model, now=self.now)
        self.assertIn("BACKEND_ONLY_MODEL_HAS_NO_TRANSACTION_AUTHORITY", report.blockers)
        from app.task_tools import registry_v1
        self.assertEqual(MODEL_TOOL_CAPABILITIES, frozenset())
        self.assertFalse({c.value for c in Capability} & set(registry_v1().entries))
        self.assertEqual(len(registry_v1().entries), 8)

    def test_scope_and_revocation_and_expiry_are_explicit(self):
        variants = (
            (replace(self.auth, allowed_capabilities=frozenset()), "USER_AUTHORIZATION_SCOPE_REQUIRED"),
            (replace(self.auth, revoked=True), "USER_AUTHORIZATION_REVOKED"),
            (replace(self.auth, expires_at=self.now), "USER_AUTHORIZATION_EXPIRED_OR_NOT_YET_VALID"),
            (replace(self.auth, valid_from=self.now + 1), "USER_AUTHORIZATION_EXPIRED_OR_NOT_YET_VALID"),
            (replace(self.auth, approval_event_ref=""), "USER_AUTHORIZATION_EVIDENCE_REQUIRED"),
        )
        for authorization, code in variants:
            with self.subTest(code=code):
                self.assertIn(code, self.inspect(replace(self.request, authorization=authorization)).blockers)

    def test_changed_quote_or_purchase_binding_requires_new_confirmation(self):
        changes = {"quote_version": 8, "quote_digest": "d" * 64, "quantity": 3,
                   "product_spec_digest": "e" * 64, "merchant_sku": "different-sku",
                   "cash": Money(6100, Currency.HKD), "destination_ref": "different-destination",
                   "user_id": "other-buyer", "payee_mapping_version": 4}
        for field, value in changes.items():
            with self.subTest(field=field):
                changed = replace(self.quote, binding=replace(self.binding, **{field: value}))
                self.assertIn("AUTHORIZATION_BINDING_CHANGED_RECONFIRM_REQUIRED",
                              self.inspect(replace(self.request, quote=changed)).blockers)

    def test_cash_budget_cannot_be_offset_by_future_rewards(self):
        auth = replace(self.auth, cash_cap=Money(5900, Currency.HKD))
        self.assertIn("CASH_BUDGET_EXCEEDED_OR_CURRENCY_MISMATCH",
                      self.inspect(replace(self.request, authorization=auth)).blockers)
        self.assertNotIn("future_reward", self.quote.__dataclass_fields__)

    def test_unknown_fees_or_missing_supporting_evidence_block(self):
        for quote in (replace(self.quote, fees_complete=False), replace(self.quote, fee_evidence_ref=None)):
            self.assertIn("COMPLETE_FEE_EVIDENCE_REQUIRED", self.inspect(replace(self.request, quote=quote)).blockers)
        for field in ("product_evidence_ref", "delivery_evidence_ref", "after_sales_evidence_ref"):
            self.assertIn("PRODUCT_DELIVERY_AFTER_SALES_EVIDENCE_REQUIRED",
                          self.inspect(replace(self.request, quote=replace(self.quote, **{field: None}))).blockers)

    def test_quote_validity_and_version_are_required(self):
        for quote in (replace(self.quote, expires_at=1000), replace(self.quote, observed_at=1001)):
            self.assertIn("QUOTE_NOT_CURRENT", self.inspect(replace(self.request, quote=quote)).blockers)
        quote = replace(self.quote, binding=replace(self.binding, quote_version=None))
        self.assertIn("QUOTE_BINDING_INCOMPLETE", self.inspect(replace(self.request, quote=quote)).blockers)

    def test_account_identity_binding_and_currency_must_match(self):
        for field, value, code in (("payee_ref", "other-payee", "MERCHANT_PAYEE_BINDING_MISMATCH"),
                                   ("merchant_id", "other-merchant", "MERCHANT_PAYEE_BINDING_MISMATCH"),
                                   ("currency", Currency.CNY, "ACCOUNT_CURRENCY_MISMATCH"),
                                   ("valid_until", 999, "PLATFORM_AUTHORIZATION_REQUIRED")):
            self.assertIn(code, self.inspect(account=replace(self.account(), **{field: value})).blockers)
        context = replace(self.context, tenant_id="other-tenant")
        self.assertIn("USER_BINDING_MISMATCH", self.inspect(context=context).blockers)

    def test_wechat_hk_and_cn_cannot_be_conflated(self):
        self.assertEqual(DisabledWeChatAdapter().integration, Integration.WECHAT_HK)
        with self.assertRaises(ValueError):
            DisabledWeChatAdapter("wechat")
        with self.assertRaises(ValueError):
            DisabledWeChatAdapter(Integration.WECHAT_CN, self.account())
        cn = self.account(Integration.WECHAT_CN)
        blockers = self.inspect(account=cn).blockers
        self.assertIn("WECHAT_CN_OUTSIDE_HKD_EXECUTION_POLICY", blockers)
        self.assertIn("WECHAT_CN_CURRENCY_MISMATCH", blockers)
        cny_binding = replace(self.binding, cash=Money(6000, Currency.CNY))
        cny_request = replace(self.request, quote=replace(self.quote, binding=cny_binding))
        blockers = self.inspect(cny_request, replace(cn, currency=Currency.CNY)).blockers
        self.assertIn("HKD_EXECUTION_POLICY_REQUIRED", blockers)
        self.assertIn("WECHAT_CN_OUTSIDE_HKD_EXECUTION_POLICY", blockers)

    def test_taobao_order_cannot_be_collected_with_own_wechat_even_with_matching_payee(self):
        binding = replace(self.binding, merchant_channel=MerchantChannel.TAOBAO)
        request = replace(self.request, quote=replace(self.quote, binding=binding), authorization=replace(self.auth, binding=binding))
        self.assertIn("TAOBAO_PLATFORM_CHECKOUT_REQUIRED_NO_OWN_WECHAT_COLLECTION", self.inspect(request).blockers)

    def test_taobao_only_reserves_data_and_official_handoff_never_order_api(self):
        adapter = DisabledTaobaoAdapter(self.account(Integration.TAOBAO))
        self.assertEqual(adapter.proposed_scope, (Capability.MERCHANT_QUOTE, Capability.OFFICIAL_CHECKOUT_HANDOFF))
        binding = replace(self.binding, merchant_channel=MerchantChannel.TAOBAO)
        command = replace(self.request, quote=replace(self.quote, binding=binding), authorization=replace(self.auth, binding=binding))
        self.assert_blocked(lambda: adapter.official_checkout_handoff(command, self.context, now=self.now))
        self.assert_blocked(lambda: adapter.create_order(command, self.context, now=self.now), "TAOBAO_OFFICIAL_CHECKOUT_ONLY")
        self.assert_blocked(lambda: adapter.query_order(self.original, self.context, now=self.now), "TAOBAO_ORDER_API_NOT_APPROVED")

    def test_official_handoff_url_requires_reviewed_exact_https_origin(self):
        account = self.account(Integration.TAOBAO)
        self.assertEqual(inspect_handoff_url("https://checkout.test.invalid/order?id=123", account), ())
        for url in ("http://checkout.test.invalid", "https://checkout.test.invalid.evil.invalid/",
                    "https://checkout.test.invalid@evil.invalid/", "https://evil.invalid@checkout.test.invalid/",
                    "https://checkout.test.invalid:8443/", "javascript:alert(1)", "//checkout.test.invalid",
                    "https://checkout.test.invalid\\@evil.invalid/", "https://checkout.test.invalid/#redirect",
                    "https://checkout.test.invalid\n.evil.invalid"):
            with self.subTest(url=url):
                self.assertTrue(inspect_handoff_url(url, account))
        self.assertIn("OFFICIAL_CHECKOUT_ORIGIN_VERIFICATION_REQUIRED",
                      inspect_handoff_url("https://checkout.test.invalid", AccountReservation(Integration.TAOBAO)))

    def test_unknown_only_queries_same_original_and_cannot_create_another_session(self):
        request = replace(self.request, kernel_command=replace(self.command, state=OperationState.UNKNOWN))
        self.assertIn("UNKNOWN_QUERY_ORIGINAL_ONLY", self.inspect(request).blockers)
        adapter = DisabledWeChatAdapter(Integration.WECHAT_HK, self.account())
        self.assert_blocked(lambda: adapter.query_original(self.original, self.context, now=self.now))
        self.assertEqual(self.original.operation_id, "operation-a")
        self.assertEqual(self.original.idempotency_key, "original-idempotency-key")
        self.assertEqual(self.original.state, OperationState.UNKNOWN)

    def test_kernel_claim_and_snapshot_binding_are_required_for_payment(self):
        self.assertIn("DURABLE_KERNEL_COMMAND_REQUIRED", self.inspect(replace(self.request, kernel_command=None)).blockers)
        for command, code in ((replace(self.command, state=OperationState.QUEUED), "KERNEL_DISPATCH_CLAIM_REQUIRED"),
                              (replace(self.command, idempotency_key=""), "KERNEL_COMMAND_BINDING_MISMATCH"),
                              (replace(self.command, quote_digest="f" * 64), "KERNEL_COMMAND_BINDING_MISMATCH"),
                              (replace(self.command, snapshot_id="other-snapshot"), "KERNEL_COMMAND_BINDING_MISMATCH")):
            self.assertIn(code, self.inspect(replace(self.request, kernel_command=command)).blockers)

    def test_refund_and_close_need_original_durable_authorized_commands(self):
        request = RefundRequest(self.original, Money(6100, Currency.HKD), "", "", "")
        report = inspect_refund(self.account(), request, self.context, now=self.now)
        for code in ("UNKNOWN_QUERY_ORIGINAL_ONLY", "VERIFIED_ORIGINAL_PAYMENT_REQUIRED",
                     "REFUND_AMOUNT_OR_CURRENCY_INVALID", "DURABLE_AUTHORIZED_REFUND_COMMAND_REQUIRED"):
            self.assertIn(code, report.blockers)
        adapter = DisabledWeChatAdapter(Integration.WECHAT_HK, self.account())
        self.assert_blocked(lambda: adapter.close_original(CloseRequest(self.original, "", "", ""), self.context, now=self.now),
                            "DURABLE_AUTHORIZED_CLOSE_COMMAND_REQUIRED")

    def test_browser_success_and_arbitrary_webhook_cannot_become_provider_evidence(self):
        adapter = DisabledWeChatAdapter(Integration.WECHAT_HK, self.account())
        headers = (("Wechatpay-Signature", "not-a-verified-signature"),
                   ("Wechatpay-Serial", "serial-sentinel"), ("Wechatpay-Timestamp", "1000"),
                   ("Wechatpay-Nonce", "nonce-sentinel"))
        envelope = WebhookEnvelope(b'{"paid": true, "return_url": "/success"}', headers, 1000)
        self.assert_blocked(lambda: adapter.verify_webhook(envelope, self.original, self.context, now=self.now))
        self.assertEqual(self.original.state, OperationState.UNKNOWN)
        self.assertNotIn("not-a-verified-signature", repr(envelope))
        self.assertNotIn("serial-sentinel", repr(envelope))
        self.assertNotIn("nonce-sentinel", repr(envelope))
        self.assertEqual(envelope.raw_headers, headers)
        self.assertEqual(envelope.raw_body, b'{"paid": true, "return_url": "/success"}')

    def test_paid_original_cannot_be_closed_and_unpaid_evidence_is_required(self):
        adapter = DisabledWeChatAdapter(Integration.WECHAT_HK, self.account())
        request = CloseRequest(replace(self.original, state=OperationState.SUCCEEDED),
                               "close-command", "close-idem", "human-approval", "contradictory-unpaid-claim")
        self.assert_blocked(lambda: adapter.close_original(request, self.context, now=self.now),
                            "PAID_OR_FINAL_ORIGINAL_CANNOT_CLOSE")
        pending = replace(request, original=replace(self.original, state=OperationState.DISPATCH_COMMITTED),
                          unpaid_status_evidence_ref=None)
        self.assert_blocked(lambda: adapter.close_original(pending, self.context, now=self.now),
                            "PROVIDER_UNPAID_STATE_EVIDENCE_REQUIRED")

    def test_refund_query_uses_separate_original_and_never_resubmits(self):
        adapter = DisabledWeChatAdapter(Integration.WECHAT_HK, self.account())
        with patch.object(adapter, "refund_original", side_effect=AssertionError("NO_REFUND_RESUBMIT")) as submit, \
                patch.object(adapter, "query_original", side_effect=AssertionError("NO_PAYMENT_QUERY_SUBSTITUTE")) as payment_query:
            report = self.assert_blocked(lambda: adapter.query_refund_original(self.original_refund, self.context, now=self.now))
            self.assertEqual(report.capability, Capability.PAYMENT_REFUND_QUERY)
            self.assertEqual(report.blockers, ("ADAPTER_NOT_IMPLEMENTED",))
            submit.assert_not_called()
            payment_query.assert_not_called()
        self.assertEqual(self.original_refund.refund_operation_id, "refund-operation-a")
        self.assertEqual(self.original_refund.refund_idempotency_key, "original-refund-idempotency-key")
        self.assertEqual(self.original_refund.state, RefundState.UNKNOWN)
        self.assertEqual(self.original_refund.original_payment.state, OperationState.SUCCEEDED)

    def test_refund_query_rejects_payment_id_reuse_or_amount_currency_mismatch(self):
        variants = (
            (replace(self.original_refund, refund_operation_id=self.original.operation_id), "ORIGINAL_REFUND_BINDING_REQUIRED"),
            (replace(self.original_refund, refund_idempotency_key=self.original.idempotency_key), "ORIGINAL_REFUND_BINDING_REQUIRED"),
            (replace(self.original_refund, refund_idempotency_key=""), "ORIGINAL_REFUND_BINDING_REQUIRED"),
            (replace(self.original_refund, refund_amount=Money(6100, Currency.HKD)), "REFUND_AMOUNT_OR_CURRENCY_INVALID"),
            (replace(self.original_refund, refund_amount=Money(100, Currency.CNY)), "REFUND_AMOUNT_OR_CURRENCY_INVALID"),
            (replace(self.original_refund, original_payment=self.original), "VERIFIED_ORIGINAL_PAYMENT_REQUIRED"),
        )
        for original, code in variants:
            with self.subTest(code=code):
                report = inspect_original_refund(self.account(), original, self.context, now=self.now)
                self.assertIn(code, report.blockers)
                self.assertFalse(report.executable)

    def test_every_reserved_port_is_disabled_without_network_io(self):
        tb = DisabledTaobaoAdapter()
        wx = DisabledWeChatAdapter(Integration.WECHAT_HK)
        quote = QuoteRequest("merchant-a", "sku-a", "a" * 64, 2, "hk-destination")
        calls = (
            lambda: tb.get_quote(quote, self.context, now=self.now),
            lambda: tb.official_checkout_handoff(self.request, self.context, now=self.now),
            lambda: tb.create_order(self.request, self.context, now=self.now),
            lambda: tb.query_order(self.original, self.context, now=self.now),
            lambda: wx.create_session(self.request, self.context, now=self.now),
            lambda: wx.query_original(self.original, self.context, now=self.now),
            lambda: wx.close_original(CloseRequest(self.original, "close-cmd", "close-key", "human-auth"), self.context, now=self.now),
            lambda: wx.refund_original(RefundRequest(self.original, Money(100, Currency.HKD), "refund-cmd", "refund-key", "human-auth"), self.context, now=self.now),
            lambda: wx.query_refund_original(self.original_refund, self.context, now=self.now),
            lambda: wx.verify_webhook(WebhookEnvelope(b"{}", (("Signature", "unverified"),), 1000), self.original, self.context, now=self.now),
        )
        with patch("socket.create_connection", side_effect=AssertionError("NO_NETWORK")) as connect, \
                patch("urllib.request.urlopen", side_effect=AssertionError("NO_NETWORK")) as urlopen:
            for callback in calls:
                self.assert_blocked(callback)
            connect.assert_not_called()
            urlopen.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
