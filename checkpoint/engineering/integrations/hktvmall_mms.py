"""Offline HKTVmall/Shoalter MMS read-request preparation; never sends traffic.

No account is approved by default. BackendContext and MmsReadApproval must come
from a future trusted backend loader, never browser/model JSON. Constructing a
record is not proof that Shoalter granted access. No HTTP route, model tool,
production key loader, transport, writes, orders or customer data are added.

Official sources checked 2026-10-04 (public document data, not executed code):
https://developers.shoalter.com/tutorial  (RS256 JWT, refresh iat every 30 min)
https://developers.shoalter.com/qna       (2026-07-03: no separate sandbox)
https://developers.shoalter.com/apis
https://developers.shoalter.com/api/1758855073947  (store)
https://developers.shoalter.com/api/1762336693464  (product codes)
https://developers.shoalter.com/api/1758859588353  (product details)
https://developers.shoalter.com/api/1758855294667  (inventory)

All four plans target PRODUCTION. The two documented GET-with-JSON-body APIs
retain their bodies; a future transport must not convert them to POST/query.
Product limit: 1 request/s/store, burst 100; Inventory: 3/s/store, burst 100.
Store-specific rate is undocumented. These are hints, not implemented throttles.
"""
from __future__ import annotations

import base64
from dataclasses import dataclass, field
from datetime import datetime
import hashlib
import json
import re
from types import MappingProxyType
from typing import Mapping
from urllib.parse import urlencode
from uuid import UUID

from .contracts import BackendContext, CallerKind


MMS_BASE_URL = "https://merchant-oapi.shoalter.com"
MMS_READ_ENDPOINTS = MappingProxyType({
    "store.details": "/oapi/api/store/details",
    "product.codes": "/oapi/api/product/hktv/products/queryProductCodeByStore",
    "product.details": "/oapi/api/product/hktv/product/details",
    "inventory.details": "/oapi/api/inventory/stock/details",
})
MMS_RATE_LIMIT_HINTS = MappingProxyType({
    "store.details": None,
    "product.codes": (1, 100), "product.details": (1, 100),
    "inventory.details": (3, 100),
})
TOKEN_REFRESH_SECONDS = 30 * 60


class MmsPreparationError(ValueError):
    """Stable code only. Never include input, private key or JWT values."""


def _fail(code):
    raise MmsPreparationError(code)


def _text(value, limit=512):
    return (isinstance(value, str) and 0 < len(value) <= limit and value == value.strip()
            and not any(ord(char) < 32 or ord(char) == 127 for char in value))


def _epoch(value):
    # Local validation distinguishes epoch seconds from milliseconds and bools.
    return type(value) is int and 0 < value <= 9_999_999_999


def _now_seconds(now):
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        _fail("MMS_AWARE_BACKEND_CLOCK_REQUIRED")
    value = int(now.timestamp())
    if not _epoch(value):
        _fail("MMS_EPOCH_SECONDS_REQUIRED")
    return value


@dataclass(frozen=True, slots=True)
class MmsCredentials:
    """Already-loaded backend values only, not a vault or filesystem loader.

    repr/str are redacted. Explicit attributes/serialization can reveal secrets:
    do not log, serialize or send this object to a model/browser.
    store_code is the storefront H... code, never the MMS store identifier.
    """
    api_key: str = field(repr=False)
    private_key_pem: bytes = field(repr=False)
    store_code: str = field(repr=False)

    def __post_init__(self):
        if not _text(self.api_key, 36):
            _fail("MMS_UUID_REQUIRED")
        try:
            parsed = UUID(self.api_key)
        except (ValueError, AttributeError):
            _fail("MMS_UUID_REQUIRED")
        if str(parsed) != self.api_key.lower():
            _fail("MMS_UUID_REQUIRED")
        # Conservative local input subset; no undocumented fixed store length.
        if not isinstance(self.store_code, str) or not re.fullmatch(r"H[0-9]{1,32}", self.store_code):
            _fail("MMS_STOREFRONT_STORE_CODE_REQUIRED")
        if not isinstance(self.private_key_pem, bytes) or not 1 <= len(self.private_key_pem) <= 16384:
            _fail("MMS_PRIVATE_KEY_INPUT_INVALID")


@dataclass(frozen=True, slots=True)
class MmsReadApproval:
    """Server-owned, reviewed account binding; empty means no permission.

    expires_at is OUR authorization expiry, not an invented JWT exp requirement.
    An explicit warehouse read also needs an approved warehouse ID (local policy).
    """
    tenant_id: str = ""
    principal_id: str = ""
    store_code: str = ""
    api_key: str = field(default="", repr=False)
    public_key_sha256: str = ""
    approval_evidence_ref: str = ""
    credential_binding_evidence_ref: str = ""
    valid_from: int = 0
    expires_at: int = 0
    granted_methods: frozenset[str] = frozenset()
    warehouse_ids: frozenset[str] = frozenset()
    revoked: bool = False


@dataclass(frozen=True, slots=True)
class MmsJwt:
    """No repr leak, including when nested in a headers mapping.

    reveal() is for a future backend transport only; never use it for logging.
    """
    _value: str = field(repr=False)

    def reveal(self):
        return self._value

    def __repr__(self):
        return "MmsJwt(<redacted>)"


@dataclass(frozen=True, slots=True)
class PreparedMmsRead:
    """Immutable wire plan, not a dispatch authorization or API result.

    headers contains a redacted MmsJwt value. wire_headers() explicitly reveals
    it for a future backend transport; that dict must never be logged/serialized.
    A future transport must recheck grant revocation/expiry and token freshness
    immediately before sending, preserve GET bodies, and enforce store limits.
    """
    api_method: str
    endpoint: str
    headers: Mapping[str, str | MmsJwt] = field(repr=False)
    query: bytes = field(repr=False)
    body: bytes | None = field(repr=False)
    prepared_at: int
    token_issued_at: int
    token_refresh_at: int
    approval_expires_at: int
    rate_limit_hint: tuple[int, int] | None
    http_method: str = field(default="GET", init=False)
    environment: str = field(default="production", init=False)
    network_dispatched: bool = field(default=False, init=False)

    def wire_headers(self):
        return {name: value.reveal() if isinstance(value, MmsJwt) else value
                for name, value in self.headers.items()}


def _load_key(credentials):
    try:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.exceptions import UnsupportedAlgorithm
    except ImportError:
        _fail("MMS_OPTIONAL_CRYPTOGRAPHY_DEPENDENCY_REQUIRED")
    try:
        key = serialization.load_pem_private_key(credentials.private_key_pem, password=None)
    except (ValueError, TypeError, UnsupportedAlgorithm):
        _fail("MMS_RSA_PRIVATE_KEY_INVALID")
    if not isinstance(key, rsa.RSAPrivateKey) or not 2048 <= key.key_size <= 8192:
        _fail("MMS_RSA_KEY_SIZE_OR_TYPE_UNSUPPORTED")
    der = key.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    return key, hashlib.sha256(der).hexdigest()


def _jwt(credentials, key, issued_at):
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding

    def b64(raw):
        return base64.urlsafe_b64encode(raw).rstrip(b"=")

    def compact(value):
        return json.dumps(value, separators=(",", ":"), ensure_ascii=True).encode("ascii")

    header = {"alg": "RS256", "typ": "JWT"}
    # Follow the tutorial's claim shape, with UTC epoch seconds and no added exp.
    claims = {"sub": "shoalter", "name": "shoalter", "iat": issued_at, "x-api-key": credentials.api_key}
    message = b64(compact(header)) + b"." + b64(compact(claims))
    signature = key.sign(message, padding.PKCS1v15(), hashes.SHA256())
    return MmsJwt((message + b"." + b64(signature)).decode("ascii"))


def _request_data(method, query, body, credentials, approval):
    if query is None:
        query = {}
    if not isinstance(query, Mapping) or any(not isinstance(name, str) for name in query):
        _fail("MMS_QUERY_PARAMETERS_INVALID")
    query = dict(query)
    if method == "product.codes":
        if body is not None or set(query) - {"page", "pageSize"}:
            _fail("MMS_REQUEST_PARAMETER_NOT_ALLOWED")
        page, size = query.get("page", 1), query.get("pageSize", 10)
        if type(page) is not int or not 1 <= page <= 2**31 - 1:
            _fail("MMS_PAGE_INVALID")
        if type(size) is not int or not 1 <= size <= 100:
            _fail("MMS_PAGE_SIZE_INVALID")
        return urlencode({"page": page, "pageSize": size}).encode("ascii"), None
    if query:
        _fail("MMS_REQUEST_PARAMETER_NOT_ALLOWED")
    if method == "store.details":
        if body is not None:
            _fail("MMS_REQUEST_PARAMETER_NOT_ALLOWED")
        return b"", None
    if not isinstance(body, (list, tuple)) or not 1 <= len(body) <= 100:
        _fail("MMS_BODY_ARRAY_1_TO_100_REQUIRED")
    output = []
    for original in body:
        if not isinstance(original, Mapping):
            _fail("MMS_BODY_ITEM_INVALID")
        item = dict(original)
        sku_field = "skuCode" if method == "product.details" else "productId"
        allowed = {sku_field} if method == "product.details" else {sku_field, "warehouseId"}
        if set(item) - allowed or sku_field not in item:
            _fail("MMS_REQUEST_PARAMETER_NOT_ALLOWED")
        sku = item[sku_field]
        prefix = credentials.store_code + "_S_"
        if not _text(sku, 1024) or not sku.startswith(prefix) or len(sku) == len(prefix):
            _fail("MMS_FULL_SKU_STORE_BINDING_REQUIRED")
        if "warehouseId" in item and item["warehouseId"] is not None:
            warehouse = item["warehouseId"]
            if not _text(warehouse, 128) or warehouse not in approval.warehouse_ids:
                _fail("MMS_WAREHOUSE_APPROVAL_REQUIRED")
        output.append(item)
    try:
        return b"", json.dumps(output, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, UnicodeError):
        _fail("MMS_BODY_ENCODING_INVALID")


def prepare_mms_read(*, method: str, credentials: MmsCredentials, context: BackendContext,
                     now: datetime, approval: MmsReadApproval | None = None,
                     query: Mapping | None = None, body: list | tuple | None = None,
                     issued_at: int | None = None) -> PreparedMmsRead:
    """Prepare one fixed read API; no arbitrary URL, headers, claims or HTTP verb.

    issued_at is optional backend token-cache metadata, never client input. Local
    policy rejects future/stale/millisecond values; no provider exp claim is
    required or invented. This function does not establish a Shoalter session.
    """
    if not isinstance(method, str) or method not in MMS_READ_ENDPOINTS:
        _fail("MMS_READ_METHOD_NOT_ALLOWED")
    if (not isinstance(context, BackendContext) or context.kind is not CallerKind.BACKEND
            or not _text(context.tenant_id) or not _text(context.user_id)):
        _fail("MMS_AUTHENTICATED_BACKEND_REQUIRED")
    if not isinstance(credentials, MmsCredentials):
        _fail("MMS_CREDENTIAL_INPUT_INVALID")
    current = _now_seconds(now)
    record = approval if isinstance(approval, MmsReadApproval) else MmsReadApproval()
    if not isinstance(record.granted_methods, frozenset) or method not in record.granted_methods:
        _fail("MMS_METHOD_APPROVAL_REQUIRED")
    if (record.tenant_id, record.principal_id) != (context.tenant_id, context.user_id):
        _fail("MMS_APPROVAL_PRINCIPAL_MISMATCH")
    if (record.store_code, record.api_key) != (credentials.store_code, credentials.api_key):
        _fail("MMS_APPROVAL_STORE_OR_ACCOUNT_MISMATCH")
    if (not _text(record.approval_evidence_ref) or not _text(record.credential_binding_evidence_ref)
            or not isinstance(record.public_key_sha256, str)
            or not re.fullmatch(r"[0-9a-f]{64}", record.public_key_sha256)):
        _fail("MMS_APPROVAL_EVIDENCE_REQUIRED")
    if (record.revoked is not False or not _epoch(record.valid_from) or not _epoch(record.expires_at)
            or not record.valid_from <= current < record.expires_at):
        _fail("MMS_APPROVAL_REVOKED_OR_EXPIRED")
    if (not isinstance(record.warehouse_ids, frozenset)
            or any(not _text(value, 128) for value in record.warehouse_ids)):
        _fail("MMS_WAREHOUSE_APPROVAL_INVALID")
    iat = current if issued_at is None else issued_at
    if not _epoch(iat):
        _fail("MMS_IAT_EPOCH_SECONDS_REQUIRED")
    if not 0 <= current - iat < TOKEN_REFRESH_SECONDS:
        _fail("MMS_TOKEN_REFRESH_REQUIRED")
    encoded_query, encoded_body = _request_data(method, query, body, credentials, record)
    key, fingerprint = _load_key(credentials)
    if fingerprint != record.public_key_sha256:
        _fail("MMS_APPROVED_SIGNING_KEY_MISMATCH")
    headers = MappingProxyType({
        "Content-Type": "application/json", "x-auth-token": _jwt(credentials, key, iat),
        "storeCode": credentials.store_code, "platformCode": "HKTV", "businessType": "eCommerce",
    })
    return PreparedMmsRead(
        api_method=method, endpoint=MMS_BASE_URL + MMS_READ_ENDPOINTS[method],
        headers=headers, query=encoded_query, body=encoded_body, prepared_at=current,
        token_issued_at=iat, token_refresh_at=iat + TOKEN_REFRESH_SECONDS,
        approval_expires_at=record.expires_at, rate_limit_hint=MMS_RATE_LIMIT_HINTS[method],
    )
