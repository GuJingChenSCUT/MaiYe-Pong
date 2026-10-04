"""Validate data from a reviewed browser observation; never operate a browser.

The caller must supply server-owned context and sanitized, actually observed
facts. Validation binds a record to a task and preserves field evidence; it does
not prove a page's truth, platform permission or quotation completeness. This
module performs no network, credential lookup, screenshot read or persistence.
Persist ``to_record()`` through an authenticated task owner check. Render all text
as text, never HTML or instructions, and review rights before external model use.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
import hashlib
import ipaddress
import json
import re
from urllib.parse import parse_qsl, urlsplit


_FIELDS = frozenset({'source_url', 'observed_at', 'source_level', 'title',
                    'specifications', 'merchant', 'currency', 'displayed_price',
                    'price_conditions', 'delivery', 'after_sales', 'field_evidence'})
_SPECS = frozenset({'brand', 'variant', 'model', 'net_content', 'unit', 'pack_count',
                   'packaging', 'origin', 'region_version', 'color', 'size'})
_SECRET_NAME = re.compile(r'(?:access.?token|refresh.?token|id.?token|token|cookie|'
                          r'authorization|password|passwd|pwd|session(?:id)?|'
                          r'api.?key|secret|csrf|credential|signature|sign|auth|code)', re.I)
_SECRET_TEXT = re.compile(r'(?:\bBearer\s+[A-Za-z0-9._~-]{8,}|'
                          r'\b(?:Cookie|Set-Cookie|Authorization)\s*:|'
                          r'\b(?:access_token|refresh_token|password|api_key|token|cookie|'
                          r'session(?:id)?|secret|csrf)\s*[=:]|'
                          r'\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b|'
                          r'-----BEGIN [A-Z ]*PRIVATE KEY-----)', re.I)


class ObservationRejected(ValueError):
    """Errors contain fixed codes only, never the rejected source content."""


@dataclass(frozen=True, slots=True)
class BrowserObservationContext:
    """Trusted backend values, not fields accepted from the observation JSON."""
    tenant_id: str
    user_id: str
    task_id: str
    constraints_version: int
    exact_origins: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class BrowserProductObservation:
    # JSON owns an immutable copy. The returned dict is independent each time.
    _record_json: str = field(repr=False)

    @property
    def can_execute(self):
        return False

    def to_record(self):
        return json.loads(self._record_json)


def _reject(code):
    raise ObservationRejected(code)


def _text(value, *, nullable=False, maximum=2048):
    if nullable and value is None:
        return None
    if (not isinstance(value, str) or not value.strip() or len(value) > maximum
            or any(ord(c) < 32 and c not in '\n\t' for c in value)
            or _SECRET_TEXT.search(value)):
        _reject('INVALID_OR_SENSITIVE_OBSERVATION_TEXT')
    return value  # Do not rewrite factual text or treat it as instructions.


def _url(value, *, origin_only=False):
    _text(value, maximum=4096)
    if any(ord(c) <= 32 or ord(c) == 127 for c in value) or '\\' in value:
        _reject('INVALID_SOURCE_URL')
    try:
        parsed = urlsplit(value)
        if (parsed.scheme != 'https' or not parsed.hostname or not parsed.hostname.isascii()
                or parsed.username is not None or parsed.password is not None
                or parsed.port not in (None, 443) or parsed.fragment or '%' in parsed.hostname):
            _reject('EXACT_PUBLIC_HTTPS_SOURCE_REQUIRED')
        host = parsed.hostname.lower()
        try:
            ipaddress.ip_address(host)
        except ValueError:
            pass
        else:
            _reject('PUBLIC_HOSTNAME_REQUIRED')
        if ('.' not in host or host.endswith(('.localhost', '.local', '.internal'))
                or not re.fullmatch(r'[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?', host)):
            _reject('PUBLIC_HOSTNAME_REQUIRED')
        if origin_only and (parsed.path not in ('', '/') or parsed.query):
            _reject('EXACT_ORIGIN_REQUIRED')
        if any(_SECRET_NAME.fullmatch(name) for name, _ in parse_qsl(parsed.query, keep_blank_values=True)):
            _reject('CREDENTIAL_URL_FORBIDDEN')
        return host, 443
    except ValueError as exc:
        if isinstance(exc, ObservationRejected):
            raise
        _reject('INVALID_SOURCE_URL')


def _utc(value):
    if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|\+00:00)', value):
        _reject('UTC_OBSERVATION_TIME_REQUIRED')
    try:
        return datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()
    except ValueError:
        _reject('INVALID_OBSERVATION_TIME')


def _price_minor(displayed, currency):
    # Only an unambiguous single displayed amount. A range, "from", coupon price
    # explanation or unidentified currency remains text, never guessed money.
    if displayed is None or currency is None:
        return None
    match = re.fullmatch(r'\s*(HK\$|HKD|CNY|RMB|¥|￥)?\s*(\d{1,10}(?:\.\d{1,2})?)\s*(HKD|CNY|RMB)?\s*', displayed)
    if not match:
        return None
    markers = {x for x in (match[1], match[3]) if x}
    allowed = {'HKD', 'HK$'} if currency == 'HKD' else {'CNY', 'RMB', '¥', '￥'}
    if not markers.issubset(allowed):
        _reject('DISPLAYED_PRICE_CURRENCY_CONFLICT')
    return int(Decimal(match[2]) * 100)


def prepare_browser_observation(payload, *, context, now):
    """Return a research-only record; no default specs, fees or transaction data.

    ``now`` is a backend UTC epoch in seconds. Unknown observed values use None;
    lists are required (empty means no conditions were captured, not no conditions
    exist). Each non-null factual field needs quote + page locator evidence. URLs
    are validated but never opened. No cross-currency ranking is implemented.
    """
    if (not isinstance(context, BrowserObservationContext)
            or type(context.constraints_version) is not int or context.constraints_version < 1
            or type(now) is not int or now <= 0):
        _reject('SERVER_CONTEXT_REQUIRED')
    for value in (context.tenant_id, context.user_id, context.task_id):
        if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}', value):
            _reject('SERVER_CONTEXT_REQUIRED')
    if (not isinstance(context.exact_origins, tuple) or not context.exact_origins):
        _reject('REVIEWED_SOURCE_ORIGIN_REQUIRED')
    origins = {_url(origin, origin_only=True) for origin in context.exact_origins}
    if not isinstance(payload, dict) or set(payload) != _FIELDS:
        _reject('UNKNOWN_OR_MISSING_OBSERVATION_FIELD')
    if _url(payload['source_url']) not in origins:
        _reject('SOURCE_ORIGIN_NOT_REVIEWED')
    observed = _utc(payload['observed_at'])
    if observed <= 0 or observed > now:
        _reject('OBSERVATION_TIME_IN_FUTURE_OR_INVALID')
    if payload['source_level'] not in ('product_detail', 'search_card'):
        _reject('INVALID_SOURCE_LEVEL')
    facts = {'title': _text(payload['title'])}
    for key in ('merchant', 'displayed_price', 'delivery', 'after_sales'):
        if payload[key] is not None:
            facts[key] = _text(payload[key])
    if payload['currency'] not in (None, 'HKD', 'CNY'):
        _reject('UNSUPPORTED_OR_UNIDENTIFIED_CURRENCY')
    if payload['currency'] is not None:
        facts['currency'] = payload['currency']
    specs = payload['specifications']
    if not isinstance(specs, dict) or set(specs) - _SPECS:
        _reject('INVALID_OBSERVED_SPECIFICATIONS')
    for key, value in specs.items():
        if value is not None:
            facts['specifications.' + key] = _text(value, maximum=512)
    conditions = payload['price_conditions']
    if not isinstance(conditions, list) or len(conditions) > 20:
        _reject('INVALID_PRICE_CONDITIONS')
    for index, value in enumerate(conditions):
        facts['price_conditions.' + str(index)] = _text(value, maximum=1024)
    evidence = payload['field_evidence']
    if not isinstance(evidence, dict) or set(evidence) != set(facts):
        _reject('FIELD_EVIDENCE_REQUIRED')
    for name, value in evidence.items():
        if not isinstance(value, dict) or set(value) != {'quote', 'locator'}:
            _reject('INVALID_FIELD_EVIDENCE')
        _text(value['quote'], maximum=4096)
        _text(value['locator'], maximum=512)
        # These are attributed excerpts, not an NLP entailment/authenticity check.
    record = dict(payload, record_kind='browser_product_observation', schema_version=1,
                  tenant_id=context.tenant_id, user_id=context.user_id, task_id=context.task_id,
                  constraints_version=context.constraints_version,
                  provenance='public_browser_observation', review_status='unverified_observation',
                  external_model_use='review_required', environment='public_research',
                  can_execute=False, checkout_verified=False, fees_complete=False,
                  cash_total_minor=None, price_conditions_verified=False,
                  observed_display_price_minor=_price_minor(payload['displayed_price'], payload['currency']))
    raw = json.dumps(record, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False)
    record['observation_id'] = 'obs_' + hashlib.sha256(raw.encode()).hexdigest()
    return BrowserProductObservation(json.dumps(record, sort_keys=True, ensure_ascii=False, allow_nan=False))
