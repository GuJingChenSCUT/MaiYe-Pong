"""Disabled internal adapters. No network client, credentials, fake sandbox or success.

Even fully populated reservation metadata ends in CapabilityUnavailable. The
preflight report is a design checklist and must never be treated as a mandate.
"""
from __future__ import annotations
from typing import NoReturn
from .contracts import (AccountReservation, BackendContext, Capability, CapabilityUnavailable,
    CheckoutCommand, CloseRequest, Integration, OfficialCheckoutHandoff,
    OriginalRefundRef, OriginalTransactionRef, ProviderObservation, QuoteEnvelope, QuoteRequest,
    RefundObservation, RefundRequest, WebhookEnvelope)
from .gates import inspect_checkout, inspect_close, inspect_original, inspect_original_refund, inspect_quote_request, inspect_refund


def _unavailable(report) -> NoReturn:
    raise CapabilityUnavailable(report)


class DisabledTaobaoAdapter:
    """Only quote-data + official checkout navigation are in the proposed scope.

    create_order/query_order exist to expose the missing contract, not a Taobao
    platform permission. They remain prohibited, even if metadata is supplied.
    """
    integration = Integration.TAOBAO
    implementation_state = "designed"
    implemented = False
    proposed_scope = (Capability.MERCHANT_QUOTE, Capability.OFFICIAL_CHECKOUT_HANDOFF)

    def __init__(self, account: AccountReservation | None = None):
        self.account = account or AccountReservation(self.integration)
        if self.account.integration is not self.integration:
            raise ValueError("ADAPTER_ACCOUNT_INTEGRATION_MISMATCH")

    def get_quote(self, request: QuoteRequest, context: BackendContext, *, now: int) -> QuoteEnvelope:
        _unavailable(inspect_quote_request(self.account, request, context, now=now))

    def official_checkout_handoff(self, command: CheckoutCommand, context: BackendContext, *, now: int) -> OfficialCheckoutHandoff:
        _unavailable(inspect_checkout(self.account, Capability.OFFICIAL_CHECKOUT_HANDOFF, command, context, now=now))

    def create_order(self, command: CheckoutCommand, context: BackendContext, *, now: int) -> ProviderObservation:
        _unavailable(inspect_checkout(self.account, Capability.MERCHANT_ORDER_CREATE, command, context, now=now))

    def query_order(self, original: OriginalTransactionRef, context: BackendContext, *, now: int) -> ProviderObservation:
        _unavailable(inspect_original(self.account, Capability.MERCHANT_ORDER_QUERY, original, context, now=now))


class DisabledWeChatAdapter:
    """Separate wallet identities. WeChat CN cannot execute under the HKD policy.

    No WeChat collection is joined to Taobao orders here. Handoff remains on the
    commerce platform; a future direct merchant PSP flow is a different approval.
    """
    implementation_state = "designed"
    implemented = False

    def __init__(self, integration: Integration = Integration.WECHAT_HK, account: AccountReservation | None = None):
        if integration not in (Integration.WECHAT_HK, Integration.WECHAT_CN) or not isinstance(integration, Integration):
            raise ValueError("EXPLICIT_WECHAT_HK_OR_CN_REQUIRED")
        self.integration = integration
        self.account = account or AccountReservation(integration)
        if self.account.integration is not integration:
            raise ValueError("ADAPTER_ACCOUNT_INTEGRATION_MISMATCH")

    def create_session(self, command: CheckoutCommand, context: BackendContext, *, now: int) -> OfficialCheckoutHandoff:
        _unavailable(inspect_checkout(self.account, Capability.PAYMENT_SESSION_CREATE, command, context, now=now))

    def query_original(self, original: OriginalTransactionRef, context: BackendContext, *, now: int) -> ProviderObservation:
        _unavailable(inspect_original(self.account, Capability.PAYMENT_QUERY, original, context, now=now))

    def close_original(self, request: CloseRequest, context: BackendContext, *, now: int) -> ProviderObservation:
        _unavailable(inspect_close(self.account, request, context, now=now))

    def refund_original(self, request: RefundRequest, context: BackendContext, *, now: int) -> RefundObservation:
        _unavailable(inspect_refund(self.account, request, context, now=now))

    def query_refund_original(self, original: OriginalRefundRef, context: BackendContext, *, now: int) -> RefundObservation:
        _unavailable(inspect_original_refund(self.account, original, context, now=now))

    def verify_webhook(self, envelope: WebhookEnvelope, original: OriginalTransactionRef,
                       context: BackendContext, *, now: int) -> ProviderObservation:
        # There is intentionally no signature parsing/verification implementation.
        # A browser return URL or arbitrary callback body cannot become success.
        _unavailable(inspect_original(self.account, Capability.WEBHOOK_VERIFY, original, context, now=now))
