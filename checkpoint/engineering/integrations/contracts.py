"""Typed INTERNAL ports/DTOs, not provider endpoints or authenticated credentials.

The future backend must resolve these records from reviewed account capabilities,
the authenticated session, existing domain validation and the durable kernel.
Constructing a DTO or providing an evidence reference does not verify its truth.
Do not deserialize a model's output directly into any authority-bearing DTO.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import re
from typing import Literal


class Currency(str, Enum):
    HKD = "HKD"
    CNY = "CNY"


class Integration(str, Enum):
    TAOBAO = "taobao"
    WECHAT_HK = "wechat_hk"
    WECHAT_CN = "wechat_cn"


class MerchantChannel(str, Enum):
    TAOBAO = "taobao"
    APPROVED_DIRECT = "approved_direct_merchant"


class CallerKind(str, Enum):
    BACKEND = "authenticated_backend"
    MODEL = "model"


class Capability(str, Enum):
    MERCHANT_QUOTE = "internal.merchant.quote"
    OFFICIAL_CHECKOUT_HANDOFF = "internal.merchant.official_checkout_handoff"
    MERCHANT_ORDER_CREATE = "internal.merchant.order_create"
    MERCHANT_ORDER_QUERY = "internal.merchant.order_query"
    PAYMENT_SESSION_CREATE = "internal.payment.session_create"
    PAYMENT_QUERY = "internal.payment.query_original"
    PAYMENT_CLOSE = "internal.payment.close_original"
    PAYMENT_REFUND = "internal.payment.refund_original"
    PAYMENT_REFUND_QUERY = "internal.payment.query_refund_original"
    WEBHOOK_VERIFY = "internal.payment.verify_webhook"


class OperationState(str, Enum):
    QUEUED = "QUEUED"
    DISPATCH_COMMITTED = "DISPATCH_COMMITTED"
    UNKNOWN = "UNKNOWN"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    STOPPED = "STOPPED"


class RefundState(str, Enum):
    REQUESTED = "REQUESTED"
    UNKNOWN = "UNKNOWN"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


@dataclass(frozen=True, slots=True)
class Money:
    minor: int
    currency: Currency

    def __post_init__(self):
        if type(self.minor) is not int or not 0 < self.minor <= 10**12:
            raise ValueError("POSITIVE_INTEGER_MINOR_UNITS_REQUIRED")
        if not isinstance(self.currency, Currency):
            raise ValueError("EXPLICIT_SUPPORTED_CURRENCY_REQUIRED")


@dataclass(frozen=True, slots=True)
class SecretRef:
    """Opaque reference only. No key lookup, value, environment read or network I/O."""
    reference: str = field(repr=False)

    def __post_init__(self):
        if not isinstance(self.reference, str) or not re.fullmatch(
                r"secret://[a-z0-9][a-z0-9/_-]{2,159}", self.reference):
            raise ValueError("OPAQUE_SECRET_REFERENCE_REQUIRED")


@dataclass(frozen=True, slots=True)
class BackendContext:
    tenant_id: str
    user_id: str
    kind: CallerKind


@dataclass(frozen=True, slots=True)
class AccountReservation:
    """Unverified until a future trusted onboarding component checks every record.

    Supplying all fields still cannot enable the disabled adapter.
    """
    integration: Integration
    account_ref: str | None = None
    secret_ref: SecretRef | None = None
    account_evidence_ref: str | None = None
    authorization_evidence_ref: str | None = None
    granted_capabilities: frozenset[Capability] = frozenset()
    merchant_id: str | None = None
    payee_ref: str | None = None
    payee_mapping_version: int | None = None
    merchant_binding_evidence_ref: str | None = None
    currency: Currency | None = None
    currency_evidence_ref: str | None = None
    valid_until: int | None = None
    official_checkout_origins: tuple[str, ...] = ()
    checkout_origin_evidence_ref: str | None = None


@dataclass(frozen=True, slots=True)
class PurchaseBinding:
    tenant_id: str
    user_id: str
    task_id: str
    merchant_channel: MerchantChannel
    merchant_id: str
    merchant_sku: str
    product_spec_digest: str
    quantity: int
    destination_ref: str
    quote_id: str
    quote_version: int | None
    quote_digest: str
    cash: Money
    payee_ref: str | None
    payee_mapping_version: int | None


@dataclass(frozen=True, slots=True)
class QuoteEnvelope:
    """Existing domain service supplies cash; this package does not recalculate it.

    Future points/rewards have no cash field here and cannot reduce the budget.
    Before kernel integration, convert a reviewed merchant response to the existing
    QuoteV1 and run its evidence, specification, fee and snapshot validations.
    """
    binding: PurchaseBinding
    observed_at: int | None
    expires_at: int | None
    fees_complete: bool = False
    fee_evidence_ref: str | None = None
    product_evidence_ref: str | None = None
    delivery_evidence_ref: str | None = None
    after_sales_evidence_ref: str | None = None


@dataclass(frozen=True, slots=True)
class UserAuthorization:
    authorization_ref: str
    approval_event_ref: str
    binding: PurchaseBinding
    snapshot_id: str
    valid_from: int
    expires_at: int
    cash_cap: Money
    allowed_capabilities: frozenset[Capability] = frozenset()
    revoked: bool = False


@dataclass(frozen=True, slots=True)
class KernelCommandRef:
    """Reference to the EXISTING durable command, never a new in-memory ledger."""
    operation_id: str
    idempotency_key: str
    snapshot_id: str
    snapshot_digest: str
    quote_digest: str
    state: OperationState


@dataclass(frozen=True, slots=True)
class CheckoutCommand:
    quote: QuoteEnvelope
    authorization: UserAuthorization | None
    kernel_command: KernelCommandRef | None


@dataclass(frozen=True, slots=True)
class QuoteRequest:
    merchant_id: str
    merchant_sku: str
    product_spec_digest: str
    quantity: int
    destination_ref: str


@dataclass(frozen=True, slots=True)
class OfficialCheckoutHandoff:
    """Future provider-evidenced navigation only, never proof of authorization/pay."""
    checkout_url: str
    platform_evidence_ref: str
    task_id: str
    quote_digest: str
    expires_at: int
    provider_session_ref: str | None
    operation_id: str | None
    requires_user_confirmation: Literal[True] = field(default=True, init=False)
    payment_authority_created: Literal[False] = field(default=False, init=False)


@dataclass(frozen=True, slots=True)
class OriginalTransactionRef:
    """Load this from durable original state, including when provider_ref is absent.

    A provider timeout preserves operation_id/idempotency_key and UNKNOWN. The
    existing kernel alone controls reconciliation, reservations and next actions.
    """
    tenant_id: str
    user_id: str
    merchant_channel: MerchantChannel
    merchant_id: str
    payee_ref: str
    payee_mapping_version: int
    cash: Money
    operation_id: str
    idempotency_key: str
    provider_payment_ref: str | None
    merchant_order_ref: str | None
    state: OperationState


@dataclass(frozen=True, slots=True)
class CloseRequest:
    original: OriginalTransactionRef
    close_command_ref: str
    close_idempotency_key: str
    human_authorization_ref: str
    unpaid_status_evidence_ref: str | None = None


@dataclass(frozen=True, slots=True)
class RefundRequest:
    original: OriginalTransactionRef
    amount: Money
    refund_command_ref: str
    refund_idempotency_key: str
    human_authorization_ref: str


@dataclass(frozen=True, slots=True)
class OriginalRefundRef:
    """Immutable original REFUND identity, separate from the original payment.

    Load from the existing durable refund record. UNKNOWN is queried with the
    same refund operation/key and amount; never turn a query into another refund
    request. A missing provider reference after timeout does not create a new ID.
    """
    original_payment: OriginalTransactionRef
    refund_operation_id: str
    refund_idempotency_key: str
    refund_amount: Money
    provider_refund_ref: str | None
    state: RefundState


@dataclass(frozen=True, slots=True)
class RefundObservation:
    """Future server-verified refund evidence, not the status of its payment.

    Provider success does not establish that the customer's funds are available.
    No implementation currently produces this DTO.
    """
    original_payment_operation_id: str
    original_refund_operation_id: str
    provider_refund_ref: str | None
    refund_amount: Money
    merchant_id: str
    payee_ref: str
    refund_status: Literal["PENDING", "UNKNOWN", "SUCCEEDED", "FAILED"]
    verification_evidence_ref: str
    observed_at: int
    funds_availability: Literal["UNKNOWN", "CONFIRMED"] = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class WebhookEnvelope:
    raw_body: bytes = field(repr=False)
    raw_headers: tuple[tuple[str, str], ...] = field(repr=False)
    received_at: int


@dataclass(frozen=True, slots=True)
class ProviderObservation:
    """Future verified server evidence, not a browser redirect or ledger mutation.

    The implementation must verify signature plus original order/payee/amount/
    currency/event binding before returning this type. No implementation exists.
    Payment observation and merchant order acceptance remain distinct.
    """
    original_operation_id: str
    provider_event_ref: str
    verification_evidence_ref: str
    merchant_id: str
    payee_ref: str
    transaction_cash: Money
    provider_payment_ref: str | None
    merchant_order_ref: str | None
    observed_at: int
    payment_status: Literal["PENDING", "UNKNOWN", "SUCCEEDED", "FAILED", "CLOSED", "REFUNDED"]
    merchant_order_status: Literal["UNKNOWN", "NOT_CREATED", "ACCEPTED", "REJECTED"]


@dataclass(frozen=True, slots=True)
class PreflightReport:
    integration: Integration
    capability: Capability
    blockers: tuple[str, ...]
    implementation_state: Literal["designed"] = field(default="designed", init=False)
    executable: Literal[False] = field(default=False, init=False)


class CapabilityUnavailable(RuntimeError):
    def __init__(self, report: PreflightReport):
        self.report = report
        super().__init__("CAPABILITY_UNAVAILABLE:" + report.integration.value + ":" + report.capability.value)


# None of these backend-only reservations are added to the model tool registry.
MODEL_TOOL_CAPABILITIES: frozenset[Capability] = frozenset()
