"""Offline TOP request preparation for specifically approved SELLER data reads.

No network, OAuth, credential loading, model tool, payment or order creation is
implemented here. The application must supply authenticated BackendContext and
server-owned approval records; never deserialize model/browser JSON into them.
Constructing these records does not itself prove a platform grant. No current
application route calls this module, and no methods are approved by default.

The official 2026 protocol permits HMAC-SHA256, while individual API pages still
list MD5/HMAC-MD5. Each method's reviewed signing policy must explicitly allow
the chosen algorithm. Downloaded SDK compatibility and actual grants still
require platform verification. See verification/platform_access_20261004.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import re
from types import MappingProxyType
from typing import Mapping
from urllib.parse import urlencode

from .contracts import BackendContext, CallerKind


TOP_GATEWAY = "https://gw.api.taobao.com/router/rest"
TOP_TIMEZONE = timezone(timedelta(hours=8))
SIGN_METHODS = frozenset({"md5", "hmac", "hmac-sha256"})
READ_METHODS = MappingProxyType({
    "taobao.item.seller.get": "authorized_seller_catalog_read",
    # docId=46 is SELLER sold orders; it is never a buyer-history API.
    "taobao.trades.sold.get": "authorized_seller_sold_orders_read",
})
_SYSTEM_PARAMETERS = frozenset({
    "method", "app_key", "session", "timestamp", "v", "sign_method", "sign",
    "format", "simplify", "target_app_key", "partner_id", "app_secret",
    "secret", "client_secret", "access_token", "sessionkey", "session_key",
})
_ALLOWED_PARAMETERS = {
    "taobao.item.seller.get": frozenset({"fields", "num_iid"}),
    "taobao.trades.sold.get": frozenset({
        "fields", "start_created", "end_created", "page_no", "page_size", "use_has_next",
    }),
}
_ALLOWED_FIELDS = {
    "taobao.item.seller.get": frozenset({"num_iid", "title", "nick", "price", "num", "approve_status", "skus", "detail_url", "modified"}),
    # Deliberately omit buyer identity, address, contact and order-item details.
    "taobao.trades.sold.get": frozenset({"tid", "status", "payment", "created", "modified"}),
}


class TopPreparationError(ValueError):
    """Stable diagnostic code only; never include credential or input values."""


def _fail(code: str):
    raise TopPreparationError(code)


def _text(value, *, limit=512):
    return (isinstance(value, str) and 0 < len(value) <= limit
            and value == value.strip() and not any(ord(c) < 32 or ord(c) == 127 for c in value))


@dataclass(frozen=True, slots=True)
class TopCredentials:
    """In-memory backend inputs only. repr/str do not reveal values.

    This is not a vault: explicit attribute access or dataclasses.asdict would
    disclose data. Do not log/serialize it or send it to a model or browser.
    """
    app_key: str = field(repr=False)
    app_secret: str = field(repr=False)
    session: str = field(repr=False)
    seller_ref: str = field(repr=False)

    def __post_init__(self):
        if not all(_text(value, limit=8192) for value in (self.app_key, self.app_secret, self.session, self.seller_ref)):
            _fail("TOP_CREDENTIAL_INPUT_INVALID")


@dataclass(frozen=True, slots=True)
class TopReadApproval:
    """Reviewed server-owned metadata, not a user-supplied permission token.

    Authority is enforced by the future backend loader. Default empty methods
    and policies are intentional. No automatic discovery/approval is performed.
    """
    tenant_id: str = ""
    principal_id: str = ""
    app_key: str = field(default="", repr=False)
    seller_ref: str = field(default="", repr=False)
    approval_evidence_ref: str = ""
    session_evidence_ref: str = ""
    valid_from: int = 0
    expires_at: int = 0
    granted_methods: frozenset[str] = frozenset()
    method_signing_policies: tuple[tuple[str, frozenset[str]], ...] = ()
    revoked: bool = False


@dataclass(frozen=True, slots=True)
class PreparedTopRead:
    """Sensitive wire inputs for a future transport; preparing sends nothing.

    Per TOP protocol, public/system parameters are in query, business parameters
    in the POST form body. The query includes SessionKey: never log a combined
    URL, query, body or signature. Only endpoint/method/data_scope are log-safe.
    No success, quote completeness or payment status can be inferred from this.
    """
    api_method: str
    data_scope: str
    query: bytes = field(repr=False)
    body: bytes = field(repr=False)
    endpoint: str = field(default=TOP_GATEWAY, init=False)
    http_method: str = field(default="POST", init=False)
    content_type: str = field(default="application/x-www-form-urlencoded; charset=utf-8", init=False)
    network_dispatched: bool = field(default=False, init=False)


def sign_top_parameters(parameters: Mapping[str, str], secret: str, sign_method: str) -> str:
    """Sign already serialized, nonbinary parameters; no URL encoding yet.

    Deliberately reject an existing sign parameter instead of ambiguously signing
    it again. This strict subset does not support multipart/binary uploads.
    This helper is an algorithm utility, not permission to prepare/send an API.
    """
    if not isinstance(sign_method, str) or sign_method not in SIGN_METHODS:
        _fail("TOP_SIGN_METHOD_UNSUPPORTED")
    if not isinstance(secret, str) or not secret:
        _fail("TOP_SECRET_REQUIRED")
    if not isinstance(parameters, Mapping) or "sign" in parameters:
        _fail("TOP_SIGN_INPUT_INVALID")
    if any(not isinstance(key, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key)
           or not isinstance(value, str) for key, value in parameters.items()):
        _fail("TOP_SIGN_INPUT_INVALID")
    # Official examples omit empty values. Preparation below rejects required
    # empty values, so empty optional values are absent from its wire request.
    data = "".join(key + parameters[key] for key in sorted(parameters) if parameters[key])
    try:
        encoded, key = data.encode("utf-8"), secret.encode("utf-8")
    except UnicodeError:
        _fail("TOP_UTF8_REQUIRED")
    if sign_method == "md5":
        return hashlib.md5(key + encoded + key).hexdigest().upper()
    algorithm = hashlib.md5 if sign_method == "hmac" else hashlib.sha256
    return hmac.new(key, encoded, algorithm).hexdigest().upper()


def _business_parameters(method: str, supplied: Mapping[str, str | int | bool]) -> dict[str, str]:
    if not isinstance(supplied, Mapping):
        _fail("TOP_BUSINESS_PARAMETERS_INVALID")
    # Copy before validation: caller-owned dictionaries cannot later change the
    # signed/prepared bytes, and supplied system fields never override ours.
    values = dict(supplied)
    if any(not isinstance(key, str) for key in values):
        _fail("TOP_BUSINESS_PARAMETERS_INVALID")
    if any(key.lower() in _SYSTEM_PARAMETERS for key in values):
        _fail("TOP_SYSTEM_PARAMETER_OVERRIDE_FORBIDDEN")
    if set(values) - _ALLOWED_PARAMETERS[method]:
        _fail("TOP_BUSINESS_PARAMETER_NOT_ALLOWED")
    fields = values.get("fields")
    if not _text(fields, limit=256):
        _fail("TOP_FIELDS_REQUIRED")
    requested = fields.split(",")
    if len(requested) != len(set(requested)) or not set(requested) <= _ALLOWED_FIELDS[method]:
        _fail("TOP_FIELD_NOT_ALLOWED")
    for name, value in values.items():
        if name == "fields":
            continue
        if name == "use_has_next":
            if type(value) is not bool:
                _fail("TOP_BOOLEAN_PARAMETER_REQUIRED")
        elif name in {"num_iid", "page_no", "page_size"}:
            maximum = 100 if name == "page_size" else (100000 if name == "page_no" else 2**63 - 1)
            if type(value) is not int or not 1 <= value <= maximum:
                _fail("TOP_POSITIVE_INTEGER_PARAMETER_REQUIRED")
        elif name in {"start_created", "end_created"}:
            if not _text(value, limit=19):
                _fail("TOP_DATE_PARAMETER_INVALID")
            try:
                parsed = datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
            except ValueError:
                _fail("TOP_DATE_PARAMETER_INVALID")
            if parsed.strftime("%Y-%m-%d %H:%M:%S") != value:
                _fail("TOP_DATE_PARAMETER_INVALID")
    if method == "taobao.item.seller.get" and "num_iid" not in values:
        _fail("TOP_ITEM_ID_REQUIRED")
    if ("start_created" in values and "end_created" in values
            and values["start_created"] > values["end_created"]):
        _fail("TOP_DATE_RANGE_INVALID")
    return {name: ("true" if value is True else "false" if value is False else str(value))
            for name, value in values.items()}


def prepare_top_read(*, method: str, business_parameters: Mapping[str, str | int | bool],
                     credentials: TopCredentials, context: BackendContext,
                     approval: TopReadApproval | None = None,
                     sign_method: str = "hmac", now: datetime) -> PreparedTopRead:
    """Prepare only a reviewed method for its bound account; never dispatch.

    now must be an aware backend clock value. No endpoint/method/parameter values
    are accepted from a model tool, and no platform permission is auto-granted.
    """
    if not isinstance(method, str) or method not in READ_METHODS:
        _fail("TOP_READ_METHOD_NOT_ALLOWED")
    if (not isinstance(context, BackendContext) or context.kind is not CallerKind.BACKEND
            or not _text(context.tenant_id) or not _text(context.user_id)):
        _fail("TOP_AUTHENTICATED_BACKEND_REQUIRED")
    if not isinstance(credentials, TopCredentials):
        _fail("TOP_CREDENTIAL_INPUT_INVALID")
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        _fail("TOP_AWARE_BACKEND_CLOCK_REQUIRED")
    record = approval if isinstance(approval, TopReadApproval) else TopReadApproval()
    if not isinstance(record.granted_methods, frozenset) or method not in record.granted_methods:
        _fail("TOP_METHOD_APPROVAL_REQUIRED")
    if (record.tenant_id, record.principal_id) != (context.tenant_id, context.user_id):
        _fail("TOP_APPROVAL_PRINCIPAL_MISMATCH")
    if (record.app_key, record.seller_ref) != (credentials.app_key, credentials.seller_ref):
        _fail("TOP_APPROVAL_ACCOUNT_MISMATCH")
    if not _text(record.approval_evidence_ref) or not _text(record.session_evidence_ref):
        _fail("TOP_APPROVAL_EVIDENCE_REQUIRED")
    current = now.timestamp()
    if (record.revoked is not False or type(record.valid_from) is not int
            or type(record.expires_at) is not int
            or not 0 < record.valid_from <= current < record.expires_at):
        _fail("TOP_APPROVAL_REVOKED_OR_EXPIRED")
    if not isinstance(record.method_signing_policies, tuple):
        _fail("TOP_METHOD_SIGNING_POLICY_REQUIRED")
    policies = {}
    for item in record.method_signing_policies:
        if (not isinstance(item, tuple) or len(item) != 2 or not isinstance(item[0], str)
                or item[0] not in READ_METHODS or item[0] in policies
                or not isinstance(item[1], frozenset) or not item[1] <= SIGN_METHODS):
            _fail("TOP_METHOD_SIGNING_POLICY_INVALID")
        policies[item[0]] = item[1]
    if not isinstance(sign_method, str) or sign_method not in policies.get(method, frozenset()):
        _fail("TOP_METHOD_SIGNING_POLICY_REQUIRED")
    business = _business_parameters(method, business_parameters)
    system = {
        "method": method, "app_key": credentials.app_key, "session": credentials.session,
        "timestamp": now.astimezone(TOP_TIMEZONE).strftime("%Y-%m-%d %H:%M:%S"),
        "v": "2.0", "format": "json", "sign_method": sign_method,
    }
    system["sign"] = sign_top_parameters({**system, **business}, credentials.app_secret, sign_method)
    try:
        query = urlencode(system, encoding="utf-8", errors="strict").encode("ascii")
        body = urlencode(business, encoding="utf-8", errors="strict").encode("ascii")
    except UnicodeError:
        _fail("TOP_UTF8_REQUIRED")
    return PreparedTopRead(method, READ_METHODS[method], query, body)
