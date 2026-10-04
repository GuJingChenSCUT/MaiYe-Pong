"""Pure preflight diagnostics, never an execution permission or another ledger.

Every report remains executable=False because no connector is implemented. Later
integration must authenticate these server-owned inputs, call the existing domain
and kernel, and retain UNKNOWN on unverifiable results. A list of references is
not cryptographic proof, merchant consent, or authorization by itself.
"""
from __future__ import annotations
import re
from urllib.parse import urlsplit
from .contracts import (AccountReservation, BackendContext, Capability, CheckoutCommand,
    CallerKind, CloseRequest, Currency, Integration, MerchantChannel, OperationState,
    OriginalRefundRef, OriginalTransactionRef, PreflightReport, QuoteRequest, RefundRequest, RefundState, SecretRef)


def _text(value):
    return isinstance(value, str) and bool(value.strip()) and len(value) <= 512


def _integer(value):
    return type(value) is int and value > 0


def _digest(value):
    return isinstance(value, str) and re.fullmatch(r"[a-f0-9]{64}", value) is not None


def _account(account, capability, context, now):
    blockers = ["ADAPTER_NOT_IMPLEMENTED"]
    if context.kind is not CallerKind.BACKEND:
        blockers.append("BACKEND_ONLY_MODEL_HAS_NO_TRANSACTION_AUTHORITY")
    if not _text(context.tenant_id) or not _text(context.user_id):
        blockers.append("AUTHENTICATED_USER_CONTEXT_REQUIRED")
    if not _text(account.account_ref) or not _text(account.account_evidence_ref):
        blockers.append("ACCOUNT_VERIFICATION_REQUIRED")
    if not isinstance(account.secret_ref, SecretRef):
        blockers.append("SECRET_REFERENCE_REQUIRED")
    if (not _text(account.authorization_evidence_ref)
            or not _integer(now) or not _integer(account.valid_until)
            or account.valid_until <= now):
        blockers.append("PLATFORM_AUTHORIZATION_REQUIRED")
    if capability not in account.granted_capabilities:
        blockers.append("CAPABILITY_GRANT_REQUIRED")
    if (not _text(account.merchant_id) or not _text(account.payee_ref)
            or not _integer(account.payee_mapping_version)
            or not _text(account.merchant_binding_evidence_ref)):
        blockers.append("MERCHANT_PAYEE_BINDING_REQUIRED")
    if not isinstance(account.currency, Currency) or not _text(account.currency_evidence_ref):
        blockers.append("ACCOUNT_CURRENCY_EVIDENCE_REQUIRED")
    return blockers


def _report(account, capability, blockers):
    return PreflightReport(account.integration, capability, tuple(dict.fromkeys(blockers)))


def _currency(account, currency, blockers):
    if currency is not Currency.HKD:
        blockers.append("HKD_EXECUTION_POLICY_REQUIRED")
    if account.currency != currency:
        blockers.append("ACCOUNT_CURRENCY_MISMATCH")
    if account.integration is Integration.WECHAT_CN:
        blockers.append("WECHAT_CN_OUTSIDE_HKD_EXECUTION_POLICY")
        if currency is not Currency.CNY:
            blockers.append("WECHAT_CN_CURRENCY_MISMATCH")
    if account.integration is Integration.WECHAT_HK and currency is not Currency.HKD:
        blockers.append("WECHAT_HK_CURRENCY_MISMATCH")


def inspect_quote_request(account: AccountReservation, request: QuoteRequest,
                          context: BackendContext, *, now: int) -> PreflightReport:
    """Data read reservation only: never upgrades a CNY price to HKD execution."""
    capability = Capability.MERCHANT_QUOTE
    blockers = _account(account, capability, context, now)
    if (request.merchant_id != account.merchant_id or not _text(request.merchant_sku)
            or not _digest(request.product_spec_digest) or not _integer(request.quantity)
            or not _text(request.destination_ref)):
        blockers.append("QUOTE_REQUEST_BINDING_INCOMPLETE")
    return _report(account, capability, blockers)


def inspect_handoff_url(url: str, account: AccountReservation) -> tuple[str, ...]:
    """Validate a future server-sourced checkout URL against reviewed EXACT origins.

    The allowlist must come from trusted onboarding, never model/request JSON.
    This does not authenticate a URL, follow redirects, or create a handoff. A
    future provider implementation must review redirect behavior independently.
    """
    def origin(value, *, origin_only=False):
        if not isinstance(value, str) or len(value) > 8192 or any(ord(c) <= 32 for c in value):
            return None
        try:
            parsed = urlsplit(value)
            if (parsed.scheme != "https" or not parsed.hostname or parsed.username is not None
                    or parsed.password is not None or parsed.port not in (None, 443)
                    or not parsed.hostname.isascii() or "%" in parsed.hostname
                    or "\\" in value or parsed.fragment):
                return None
            if origin_only and (parsed.path not in ("", "/") or parsed.query):
                return None
            return parsed.hostname.lower(), 443
        except ValueError:
            return None
    blockers = []
    if not account.official_checkout_origins or not _text(account.checkout_origin_evidence_ref):
        blockers.append("OFFICIAL_CHECKOUT_ORIGIN_VERIFICATION_REQUIRED")
    actual = origin(url)
    allowed = {item for value in account.official_checkout_origins if (item := origin(value, origin_only=True))}
    if actual is None:
        blockers.append("UNSAFE_CHECKOUT_URL")
    elif actual not in allowed:
        blockers.append("CHECKOUT_ORIGIN_NOT_ALLOWLISTED")
    return tuple(blockers)


def inspect_checkout(account: AccountReservation, capability: Capability, request: CheckoutCommand,
                     context: BackendContext, *, now: int) -> PreflightReport:
    blockers = _account(account, capability, context, now)
    quote, auth, command = request.quote, request.authorization, request.kernel_command
    binding = quote.binding
    if (binding.tenant_id, binding.user_id) != (context.tenant_id, context.user_id):
        blockers.append("USER_BINDING_MISMATCH")
    if (binding.merchant_id != account.merchant_id or binding.payee_ref != account.payee_ref
            or binding.payee_mapping_version != account.payee_mapping_version):
        blockers.append("MERCHANT_PAYEE_BINDING_MISMATCH")
    if (not _text(binding.task_id) or not _text(binding.merchant_id)
            or not _text(binding.merchant_sku) or not _text(binding.destination_ref)
            or not _digest(binding.product_spec_digest) or not _integer(binding.quantity)
            or not _text(binding.quote_id) or not _integer(binding.quote_version)
            or not _digest(binding.quote_digest) or not _text(binding.payee_ref)
            or not _integer(binding.payee_mapping_version)):
        blockers.append("QUOTE_BINDING_INCOMPLETE")
    if (not _integer(quote.observed_at) or not _integer(quote.expires_at)
            or quote.observed_at > now or quote.expires_at <= now):
        blockers.append("QUOTE_NOT_CURRENT")
    if quote.fees_complete is not True or not _text(quote.fee_evidence_ref):
        blockers.append("COMPLETE_FEE_EVIDENCE_REQUIRED")
    if not all(_text(ref) for ref in (quote.product_evidence_ref, quote.delivery_evidence_ref, quote.after_sales_evidence_ref)):
        blockers.append("PRODUCT_DELIVERY_AFTER_SALES_EVIDENCE_REQUIRED")
    _currency(account, binding.cash.currency, blockers)
    if account.integration is Integration.TAOBAO:
        if binding.merchant_channel is not MerchantChannel.TAOBAO:
            blockers.append("TAOBAO_MERCHANT_CHANNEL_MISMATCH")
        if capability is not Capability.OFFICIAL_CHECKOUT_HANDOFF:
            blockers.append("TAOBAO_OFFICIAL_CHECKOUT_ONLY")
        elif not any(not inspect_handoff_url(url, account) for url in account.official_checkout_origins):
            blockers.append("OFFICIAL_CHECKOUT_ORIGIN_VERIFICATION_REQUIRED")
    elif binding.merchant_channel is not MerchantChannel.APPROVED_DIRECT:
        # Team PSP credentials do not collect or settle another platform's order.
        blockers.append("TAOBAO_PLATFORM_CHECKOUT_REQUIRED_NO_OWN_WECHAT_COLLECTION")
    if auth is None:
        blockers.append("USER_AUTHORIZATION_REQUIRED")
    else:
        if capability not in auth.allowed_capabilities:
            blockers.append("USER_AUTHORIZATION_SCOPE_REQUIRED")
        if (not _text(auth.authorization_ref) or not _text(auth.approval_event_ref)
                or not _text(auth.snapshot_id)):
            blockers.append("USER_AUTHORIZATION_EVIDENCE_REQUIRED")
        if auth.revoked is not False:
            blockers.append("USER_AUTHORIZATION_REVOKED")
        if (not _integer(auth.valid_from) or not _integer(auth.expires_at)
                or auth.valid_from > now or auth.expires_at <= now):
            blockers.append("USER_AUTHORIZATION_EXPIRED_OR_NOT_YET_VALID")
        if auth.binding != binding:
            blockers.append("AUTHORIZATION_BINDING_CHANGED_RECONFIRM_REQUIRED")
        if auth.cash_cap.currency != binding.cash.currency or binding.cash.minor > auth.cash_cap.minor:
            blockers.append("CASH_BUDGET_EXCEEDED_OR_CURRENCY_MISMATCH")
    if command is not None and command.state is OperationState.UNKNOWN:
        blockers.append("UNKNOWN_QUERY_ORIGINAL_ONLY")
    if capability in {Capability.PAYMENT_SESSION_CREATE, Capability.MERCHANT_ORDER_CREATE}:
        if command is None:
            blockers.append("DURABLE_KERNEL_COMMAND_REQUIRED")
        else:
            if command.state is not OperationState.DISPATCH_COMMITTED:
                blockers.append("KERNEL_DISPATCH_CLAIM_REQUIRED")
            if (not _text(command.operation_id) or not _text(command.idempotency_key)
                    or not _digest(command.snapshot_digest)
                    or command.snapshot_id != "snap_" + command.snapshot_digest
                    or command.quote_digest != binding.quote_digest
                    or auth is None or command.snapshot_id != auth.snapshot_id):
                blockers.append("KERNEL_COMMAND_BINDING_MISMATCH")
    return _report(account, capability, blockers)


def inspect_original(account: AccountReservation, capability: Capability, original: OriginalTransactionRef,
                     context: BackendContext, *, now: int) -> PreflightReport:
    """Recovery always uses the durable original, not renewed purchase permission."""
    blockers = _account(account, capability, context, now)
    if (original.tenant_id, original.user_id) != (context.tenant_id, context.user_id):
        blockers.append("USER_BINDING_MISMATCH")
    if (not _text(original.operation_id) or not _text(original.idempotency_key)
            or original.merchant_id != account.merchant_id or original.payee_ref != account.payee_ref
            or original.payee_mapping_version != account.payee_mapping_version):
        blockers.append("ORIGINAL_TRANSACTION_BINDING_REQUIRED")
    if account.integration is Integration.TAOBAO:
        blockers.append("TAOBAO_ORDER_API_NOT_APPROVED")
    elif original.merchant_channel is not MerchantChannel.APPROVED_DIRECT:
        blockers.append("TAOBAO_PLATFORM_CHECKOUT_REQUIRED_NO_OWN_WECHAT_COLLECTION")
    _currency(account, original.cash.currency, blockers)
    if original.state is OperationState.UNKNOWN and capability not in {Capability.PAYMENT_QUERY, Capability.WEBHOOK_VERIFY}:
        blockers.append("UNKNOWN_QUERY_ORIGINAL_ONLY")
    return _report(account, capability, blockers)


def inspect_close(account: AccountReservation, request: CloseRequest, context: BackendContext, *, now: int) -> PreflightReport:
    report = inspect_original(account, Capability.PAYMENT_CLOSE, request.original, context, now=now)
    blockers = list(report.blockers)
    if not all(_text(ref) for ref in (request.close_command_ref, request.close_idempotency_key, request.human_authorization_ref)):
        blockers.append("DURABLE_AUTHORIZED_CLOSE_COMMAND_REQUIRED")
    if request.original.state in {OperationState.SUCCEEDED, OperationState.FAILED, OperationState.STOPPED}:
        blockers.append("PAID_OR_FINAL_ORIGINAL_CANNOT_CLOSE")
    if not _text(request.unpaid_status_evidence_ref):
        blockers.append("PROVIDER_UNPAID_STATE_EVIDENCE_REQUIRED")
    return _report(account, report.capability, blockers)


def inspect_refund(account: AccountReservation, request: RefundRequest, context: BackendContext, *, now: int) -> PreflightReport:
    report = inspect_original(account, Capability.PAYMENT_REFUND, request.original, context, now=now)
    blockers = list(report.blockers)
    if request.original.state is not OperationState.SUCCEEDED:
        blockers.append("VERIFIED_ORIGINAL_PAYMENT_REQUIRED")
    if request.amount.currency != request.original.cash.currency or request.amount.minor > request.original.cash.minor:
        blockers.append("REFUND_AMOUNT_OR_CURRENCY_INVALID")
    if not all(_text(ref) for ref in (request.refund_command_ref, request.refund_idempotency_key, request.human_authorization_ref)):
        blockers.append("DURABLE_AUTHORIZED_REFUND_COMMAND_REQUIRED")
    return _report(account, report.capability, blockers)


def inspect_original_refund(account: AccountReservation, original: OriginalRefundRef,
                            context: BackendContext, *, now: int) -> PreflightReport:
    report = inspect_original(account, Capability.PAYMENT_REFUND_QUERY, original.original_payment, context, now=now)
    blockers = list(report.blockers)
    payment = original.original_payment
    if (not _text(original.refund_operation_id) or not _text(original.refund_idempotency_key)
            or original.refund_operation_id == payment.operation_id
            or original.refund_idempotency_key == payment.idempotency_key
            or not isinstance(original.state, RefundState)):
        blockers.append("ORIGINAL_REFUND_BINDING_REQUIRED")
    if payment.state is not OperationState.SUCCEEDED:
        blockers.append("VERIFIED_ORIGINAL_PAYMENT_REQUIRED")
    if original.refund_amount.currency != payment.cash.currency or original.refund_amount.minor > payment.cash.minor:
        blockers.append("REFUND_AMOUNT_OR_CURRENCY_INVALID")
    return _report(account, report.capability, blockers)
