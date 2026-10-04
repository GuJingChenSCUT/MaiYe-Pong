"""Offline policy for a future visible, isolated shopping browser.

This is NOT a browser executor, TOP authorization, or a payment verifier. A future
executor must apply these decisions before each UI action and after redirects,
derive ownership from authenticated server state, and implement user takeover.
It must never accept account cookies, payment secrets or this scope from an LLM.
"""
from dataclasses import dataclass
from enum import Enum
from urllib.parse import urlsplit

from .contracts import BackendContext, CallerKind


class BrowserAction(str, Enum):
    OPEN = "open_official_page"
    SEARCH = "search_products"
    READ = "read_product_conditions"
    SELECT_VARIANT = "select_variant"
    LOGIN = "login"
    CHALLENGE = "captcha_or_risk_challenge"
    CART = "change_cart"
    ORDER = "submit_order"
    PAY = "confirm_payment"
    COOKIE = "extract_session_cookie"
    INTERNAL_API = "call_undocumented_api"


class BrowserDecision(str, Enum):
    READ_ONLY_STEP = "read_only_step_permitted_by_local_policy"
    TAKEOVER = "user_takeover_required"
    DENY = "denied"


@dataclass(frozen=True, slots=True)
class BrowserResearchScope:
    # Trusted server-side configuration, not request JSON or proof by itself.
    tenant_id: str = ""
    user_id: str = ""
    task_id: str = ""
    isolated_session_ref: str = ""
    approval_evidence_ref: str = ""
    platform_review_evidence_ref: str = ""
    exact_origins: tuple[str, ...] = ()
    allowed_read_actions: frozenset[BrowserAction] = frozenset()
    valid_from: int = 0
    valid_until: int = 0
    revoked: bool = True


@dataclass(frozen=True, slots=True)
class BrowserStepDecision:
    decision: BrowserDecision
    reasons: tuple[str, ...]
    # No result here may be used as a transaction kernel dispatch authorization.
    transaction_authorized: bool = False
    payment_verified: bool = False


def _origin(value, *, origin_only=False):
    if not isinstance(value, str) or len(value) > 8192:
        return None
    if any(ord(c) <= 32 or ord(c) == 127 for c in value) or "\\" in value:
        return None
    try:
        parsed = urlsplit(value)
        if (parsed.scheme != "https" or not parsed.hostname or not parsed.hostname.isascii()
                or parsed.username is not None or parsed.password is not None
                or parsed.port not in (None, 443) or "%" in parsed.hostname or parsed.fragment):
            return None
        if origin_only and (parsed.path not in ("", "/") or parsed.query):
            return None
        return parsed.hostname.lower(), 443
    except ValueError:
        return None


def inspect_browser_step(scope: BrowserResearchScope, context: BackendContext, *,
                         task_id: str, session_ref: str, action: BrowserAction,
                         url: str, now: int, stopped: bool = False) -> BrowserStepDecision:
    blockers = []
    if context.kind is not CallerKind.BACKEND:
        blockers.append("AUTHENTICATED_BACKEND_REQUIRED")
    if (not scope.tenant_id or not scope.user_id
            or (context.tenant_id, context.user_id) != (scope.tenant_id, scope.user_id)):
        blockers.append("BROWSER_USER_BINDING_MISMATCH")
    if (not scope.task_id or not scope.isolated_session_ref
            or (task_id, session_ref) != (scope.task_id, scope.isolated_session_ref)):
        blockers.append("ISOLATED_TASK_SESSION_REQUIRED")
    if not scope.approval_evidence_ref or not scope.platform_review_evidence_ref:
        blockers.append("USER_AND_PLATFORM_REVIEW_REQUIRED")
    if (type(now) is not int or type(scope.valid_from) is not int or type(scope.valid_until) is not int
            or not 0 < scope.valid_from <= now < scope.valid_until):
        blockers.append("BROWSER_SCOPE_EXPIRED_OR_INVALID")
    if scope.revoked is not False or stopped is not False:
        blockers.append("BROWSER_RESEARCH_STOPPED")
    actual = _origin(url)
    origins = [_origin(item, origin_only=True) for item in scope.exact_origins]
    if not origins or any(item is None for item in origins) or actual is None or actual not in origins:
        blockers.append("REVIEWED_EXACT_HTTPS_ORIGIN_REQUIRED")
    if not isinstance(action, BrowserAction):
        blockers.append("UNKNOWN_BROWSER_ACTION")
    if blockers:
        return BrowserStepDecision(BrowserDecision.DENY, tuple(blockers))
    if action in {BrowserAction.COOKIE, BrowserAction.INTERNAL_API}:
        return BrowserStepDecision(BrowserDecision.DENY, ("NO_CREDENTIAL_EXTRACTION_OR_INTERNAL_API_BYPASS",))
    if action in {BrowserAction.LOGIN, BrowserAction.CHALLENGE, BrowserAction.CART,
                  BrowserAction.ORDER, BrowserAction.PAY, BrowserAction.SELECT_VARIANT}:
        return BrowserStepDecision(BrowserDecision.TAKEOVER, ("CURRENT_FALLBACK_IS_RESEARCH_ONLY",))
    if action not in scope.allowed_read_actions:
        return BrowserStepDecision(BrowserDecision.DENY, ("READ_ACTION_NOT_APPROVED",))
    return BrowserStepDecision(BrowserDecision.READ_ONLY_STEP, ("OBSERVATION_ONLY_NOT_AN_EXECUTABLE_QUOTE",))


@dataclass(frozen=True, slots=True)
class BrowserOutcome:
    """A visual observation cannot certify a server-side transaction."""
    page_status: str
    payment_status: str = "UNVERIFIED"
    merchant_order_status: str = "UNVERIFIED"


def observe_browser_outcome(page_status: str) -> BrowserOutcome:
    if page_status not in {"not_checked", "checkout_visible", "success_page_visible", "error_page_visible"}:
        raise ValueError("UNKNOWN_PAGE_OBSERVATION")
    return BrowserOutcome(page_status)
