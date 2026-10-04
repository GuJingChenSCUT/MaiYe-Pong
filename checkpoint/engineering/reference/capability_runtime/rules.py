"""Deterministic selection of reviewed executable rules, not money calculation.

This does not authorize payment, interpret law, scrape rates or adjudicate a case.
Source evidence, user preferences, authorization state and executable rules are
distinct input classes. Only the last belongs in resolve(). No market rates ship.
"""
from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping


def _date(value: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError("timestamp string required")
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("timezone required")
    return result.astimezone(timezone.utc)


@dataclass(frozen=True)
class RuleRequest:
    rule_key: str
    jurisdiction: str
    currency: str
    now: datetime
    facts: Mapping[str, Any]


@dataclass(frozen=True)
class RuleDecision:
    status: str
    reasons: tuple[str, ...]
    rule_id: str | None = None
    rule_version: str | None = None
    parameters: dict | None = None
    source_ref: str | None = None
    # An applicable tariff/benefit rule is never authorization to transact.
    grants_transaction_authority: bool = False


class DeterministicRuleResolver:
    """Fail closed for ambiguous, stale, unknown or unapproved rule candidates.

    records must come from a server-selected reviewed release, not model-generated
    JSON. approved_release_versions is server deployment configuration. A future
    production repository must verify its release signatures/digests before use.
    """
    def __init__(self, *, approved_release_versions: frozenset[str],
                 offline_test_mode: bool = False):
        self._versions = frozenset(approved_release_versions)
        self._offline = offline_test_mode

    def resolve(self, records: list[dict], request: RuleRequest) -> RuleDecision:
        if (not isinstance(request, RuleRequest) or not isinstance(request.now, datetime)
                or request.now.tzinfo is None or not isinstance(request.facts, Mapping)
                or not all(isinstance(getattr(request, k), str) and getattr(request, k)
                           for k in ("rule_key", "jurisdiction", "currency"))):
            return RuleDecision("blocked", ("INVALID_REQUEST",))
        if not isinstance(records, list) or not all(isinstance(r, dict) for r in records):
            return RuleDecision("blocked", ("RULE_RECORD_INVALID",))
        try:
            json.dumps(records, allow_nan=False)
            json.dumps(dict(request.facts), allow_nan=False)
        except (ValueError, TypeError):
            return RuleDecision("blocked", ("NON_FINITE_OR_NON_JSON_DATA",))
        relevant = [copy.deepcopy(r) for r in records if r.get("rule_key") == request.rule_key]
        if not relevant:
            return RuleDecision("needs_clarification", ("NO_VERIFIED_RULE",))
        applicable, failures, not_applicable = [], [], 0
        for rule in relevant:
            if rule.get("record_kind") != "executable_rule":
                failures.append("NOT_AN_EXECUTABLE_RULE")
                continue
            if rule.get("publication_status") != "published":
                failures.append("RULE_NOT_PUBLISHED")
                continue
            if rule.get("release_version") not in self._versions:
                failures.append("UNAPPROVED_RULE_RELEASE")
                continue
            if not isinstance(rule.get("synthetic"), bool):
                failures.append("RULE_PROVENANCE_MISSING")
                continue
            if rule["synthetic"] and not self._offline:
                failures.append("SYNTHETIC_RULE_FORBIDDEN")
                continue
            if rule.get("jurisdiction") != request.jurisdiction or rule.get("currency") != request.currency:
                not_applicable += 1
                continue
            try:
                start, end, checked = (_date(rule[k]) for k in ("effective_from", "effective_until", "verified_at"))
                ttl = rule["max_age_seconds"]
                if type(ttl) is not int or ttl <= 0 or end <= start or checked > request.now:
                    raise ValueError("invalid freshness")
                if not start <= request.now < end:
                    failures.append("RULE_OUTSIDE_EFFECTIVE_WINDOW")
                    continue
                if (request.now - checked).total_seconds() > ttl:
                    failures.append("RULE_VERIFICATION_STALE")
                    continue
                conditions = rule["eligibility_equals"]
                if not isinstance(conditions, dict):
                    raise ValueError("invalid eligibility")
                mismatched, unknown = False, False
                for field, expected in conditions.items():
                    actual = request.facts.get(field)
                    if actual is None:
                        unknown = True
                    elif type(actual) is not type(expected) or actual != expected:
                        mismatched = True
                if mismatched:
                    not_applicable += 1
                    continue
                if unknown:
                    failures.append("ELIGIBILITY_UNKNOWN")
                    continue
                if not all(isinstance(rule.get(k), str) and rule[k] for k in ("rule_id", "version", "source_ref")):
                    raise ValueError("missing provenance")
                if not isinstance(rule.get("parameters"), dict):
                    raise ValueError("invalid parameters")
            except (ValueError, TypeError, KeyError, OverflowError):
                failures.append("RULE_RECORD_INVALID")
                continue
            applicable.append(rule)
        # Do not silently select one rule while another relevant candidate is
        # unverified/unknown; repository curation must first remove ambiguity.
        if failures:
            return RuleDecision("needs_clarification", tuple(sorted(set(failures))))
        if len(applicable) > 1:
            return RuleDecision("needs_clarification", ("CONFLICTING_RULES",))
        if len(applicable) == 0:
            return RuleDecision("not_applicable", ("JURISDICTION_CURRENCY_OR_ELIGIBILITY_MISMATCH",))
        chosen = applicable[0]
        return RuleDecision("applicable", (), chosen["rule_id"], chosen["version"],
                            copy.deepcopy(chosen["parameters"]), chosen["source_ref"])
