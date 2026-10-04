"""Offline TOP protocol tests. Synthetic credentials only; no platform claims."""
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
import socket
import sys
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from integrations.contracts import BackendContext, CallerKind
from integrations.taobao_top import (
    TOP_GATEWAY, TopCredentials, TopReadApproval, TopPreparationError,
    prepare_top_read, sign_top_parameters,
)


class TaobaoTopPreparationTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 4, 1, 2, 3, tzinfo=timezone.utc)
        self.credentials = TopCredentials("synthetic-app", "synthetic-secret-sentinel", "synthetic-session-sentinel", "seller-a")
        self.context = BackendContext("tenant-a", "operator-a", CallerKind.BACKEND)
        self.method = "taobao.item.seller.get"
        self.approval = TopReadApproval(
            "tenant-a", "operator-a", "synthetic-app", "seller-a", "offline-fixture-not-platform-evidence",
            "offline-session-fixture", int(self.now.timestamp()) - 1, int(self.now.timestamp()) + 60,
            frozenset({self.method}), ((self.method, frozenset({"hmac", "md5"})),),
        )
        self.business = {"fields": "num_iid,title,price", "num_iid": 123456}

    def prepare(self, **changes):
        args = dict(method=self.method, business_parameters=self.business,
                    credentials=self.credentials, context=self.context,
                    approval=self.approval, now=self.now)
        args.update(changes)
        return prepare_top_read(**args)

    def assert_code(self, code, **changes):
        with self.assertRaises(TopPreparationError) as caught:
            self.prepare(**changes)
        self.assertEqual(str(caught.exception), code)
        self.assertNotIn(self.credentials.app_secret, str(caught.exception))
        self.assertNotIn(self.credentials.session, str(caught.exception))

    def test_official_top_md5_vector(self):
        # TOP articleId=101617 example, secret=helloworld. Published expected
        # signature is independent of this implementation and covers sorting.
        parameters = {"method": "taobao.item.seller.get", "app_key": "12345678", "session": "test",
                      "timestamp": "2016-01-01 12:00:00", "format": "json", "v": "2.0", "sign_method": "md5",
                      "fields": "num_iid,title,nick,price,num", "num_iid": "11223344"}
        self.assertEqual(sign_top_parameters(parameters, "helloworld", "md5"), "66987CB115214E59E6EC978214934FB8")

    def test_rfc2202_hmac_md5_vector(self):
        # RFC 2202 case 1. Parameter concatenation yields the published Hi There.
        self.assertEqual(sign_top_parameters({"Hi": " There"}, "\x0b" * 16, "hmac"),
                         "9294727A3638BB1C13F48EF8158BFC9D")

    def test_rfc4231_hmac_sha256_vector(self):
        self.assertEqual(sign_top_parameters({"Hi": " There"}, "\x0b" * 20, "hmac-sha256"),
                         "B0344C61D8DB38535CA8AFCEAF0BF12B881DC200C9833DA726E9376C2E32CFF7")

    def test_unknown_algorithm_and_existing_signature_rejected(self):
        with self.assertRaisesRegex(TopPreparationError, "TOP_SIGN_METHOD_UNSUPPORTED"):
            sign_top_parameters({"a": "b"}, "synthetic", "sha1")
        with self.assertRaisesRegex(TopPreparationError, "TOP_SIGN_INPUT_INVALID"):
            sign_top_parameters({"a": "b", "sign": "attacker"}, "synthetic", "hmac")

    def test_signing_excludes_unsupported_binary_or_nonstring_values(self):
        for parameters in ({"a": b"binary"}, {"a": 1}, {"a&b": "c"}, {"a": "x", 5: "z"}):
            with self.subTest(parameters=parameters), self.assertRaisesRegex(TopPreparationError, "TOP_SIGN_INPUT_INVALID"):
                sign_top_parameters(parameters, "synthetic", "hmac")

    def test_empty_optional_signing_values_are_omitted(self):
        self.assertEqual(sign_top_parameters({"a": "x", "b": ""}, "synthetic", "hmac"),
                         sign_top_parameters({"a": "x"}, "synthetic", "hmac"))

    def test_default_no_platform_methods_are_approved(self):
        self.assert_code("TOP_METHOD_APPROVAL_REQUIRED", approval=None)
        self.assert_code("TOP_METHOD_APPROVAL_REQUIRED", approval=TopReadApproval())

    def test_create_order_and_arbitrary_methods_stay_prohibited(self):
        for method in ("taobao.trade.create", "taobao.trade.close", "taobao.user.buyer.get", "https://attacker.invalid", ""):
            with self.subTest(method=method):
                self.assert_code("TOP_READ_METHOD_NOT_ALLOWED", method=method,
                                 approval=replace(self.approval, granted_methods=frozenset({method})))

    def test_model_caller_never_prepares_signed_request(self):
        self.assert_code("TOP_AUTHENTICATED_BACKEND_REQUIRED", context=replace(self.context, kind=CallerKind.MODEL))

    def test_tenant_and_principal_bindings_are_enforced(self):
        for record in (replace(self.approval, tenant_id="other"), replace(self.approval, principal_id="other")):
            self.assert_code("TOP_APPROVAL_PRINCIPAL_MISMATCH", approval=record)

    def test_app_key_and_authorized_seller_bindings_are_enforced(self):
        for record in (replace(self.approval, app_key="other"), replace(self.approval, seller_ref="other")):
            self.assert_code("TOP_APPROVAL_ACCOUNT_MISMATCH", approval=record)

    def test_evidence_references_required(self):
        for record in (replace(self.approval, approval_evidence_ref=""), replace(self.approval, session_evidence_ref="")):
            self.assert_code("TOP_APPROVAL_EVIDENCE_REQUIRED", approval=record)

    def test_expired_future_and_revoked_approvals_fail_closed(self):
        current = int(self.now.timestamp())
        for record in (replace(self.approval, expires_at=current), replace(self.approval, valid_from=current + 1),
                       replace(self.approval, revoked=True), replace(self.approval, valid_from=True)):
            self.assert_code("TOP_APPROVAL_REVOKED_OR_EXPIRED", approval=record)

    def test_naive_clock_is_rejected(self):
        self.assert_code("TOP_AWARE_BACKEND_CLOCK_REQUIRED", now=self.now.replace(tzinfo=None))

    def test_each_method_requires_explicit_signing_policy(self):
        self.assert_code("TOP_METHOD_SIGNING_POLICY_REQUIRED", approval=replace(self.approval, method_signing_policies=()))
        self.assert_code("TOP_METHOD_SIGNING_POLICY_REQUIRED", sign_method="hmac-sha256")
        policy = ((self.method, frozenset({"hmac-sha256"})),)
        prepared = self.prepare(sign_method="hmac-sha256", approval=replace(self.approval, method_signing_policies=policy))
        self.assertEqual(len(parse_qs(prepared.query.decode())["sign"][0]), 64)

    def test_duplicate_and_invalid_policies_fail_closed(self):
        for policies in (self.approval.method_signing_policies * 2, ((self.method, frozenset({"sha1"})),),
                         (("taobao.trade.create", frozenset({"md5"})),)):
            self.assert_code("TOP_METHOD_SIGNING_POLICY_INVALID", approval=replace(self.approval, method_signing_policies=policies))

    def test_all_system_parameter_overrides_are_blocked(self):
        for name in ("method", "app_key", "session", "timestamp", "sign", "SIGN", "sign_method", "target_app_key", "client_secret", "access_token"):
            with self.subTest(name=name):
                self.assert_code("TOP_SYSTEM_PARAMETER_OVERRIDE_FORBIDDEN", business_parameters={**self.business, name: "attacker"})

    def test_unknown_business_parameters_and_sensitive_fields_are_blocked(self):
        self.assert_code("TOP_BUSINESS_PARAMETER_NOT_ALLOWED", business_parameters={**self.business, "redirect_uri": "https://attacker.invalid"})
        for fields in ("", "title,title", "title,buyer_nick", "title, price"):
            code = "TOP_FIELDS_REQUIRED" if not fields else "TOP_FIELD_NOT_ALLOWED"
            self.assert_code(code, business_parameters={**self.business, "fields": fields})

    def test_item_identifier_strict_type_and_bounds(self):
        self.assert_code("TOP_ITEM_ID_REQUIRED", business_parameters={"fields": "title"})
        for value in (True, 1.0, "123", 0, -1, 2**63):
            self.assert_code("TOP_POSITIVE_INTEGER_PARAMETER_REQUIRED", business_parameters={**self.business, "num_iid": value})

    def test_post_wire_is_https_gmt8_and_separates_public_business_params(self):
        prepared = self.prepare()
        public, business = parse_qs(prepared.query.decode()), parse_qs(prepared.body.decode())
        self.assertEqual(prepared.endpoint, TOP_GATEWAY)
        self.assertEqual(prepared.http_method, "POST")
        self.assertEqual(public["timestamp"], ["2026-10-04 09:02:03"])
        self.assertEqual(public["format"], ["json"])
        self.assertEqual(public["v"], ["2.0"])
        self.assertEqual(business["num_iid"], ["123456"])
        self.assertNotIn("fields", public)
        self.assertNotIn("session", business)
        self.assertNotIn(self.credentials.app_secret, prepared.query.decode() + prepared.body.decode())

    def test_url_encoding_is_not_parameter_injection(self):
        credentials = replace(self.credentials, session="synthetic&method=taobao.trade.create+雪")
        prepared = self.prepare(credentials=credentials)
        decoded = parse_qs(prepared.query.decode())
        self.assertEqual(decoded["session"], [credentials.session])
        self.assertEqual(decoded["method"], [self.method])
        self.assertIn(b"%E9%9B%AA", prepared.query)

    def test_repr_and_errors_do_not_expose_credentials_or_request_data(self):
        prepared = self.prepare()
        for displayed in (repr(self.credentials), str(self.credentials), repr(prepared), str(prepared)):
            for secret in (self.credentials.app_key, self.credentials.app_secret, self.credentials.session, "123456", "num_iid,title"):
                self.assertNotIn(secret, displayed)
        with self.assertRaises(TopPreparationError) as caught:
            TopCredentials("synthetic", "secret\n", "session-sentinel", "seller")
        self.assertEqual(str(caught.exception), "TOP_CREDENTIAL_INPUT_INVALID")

    def test_prepared_request_cannot_be_changed_by_input_mutation(self):
        prepared = self.prepare()
        self.business["num_iid"] = 654321
        self.assertEqual(parse_qs(prepared.body.decode())["num_iid"], ["123456"])
        with self.assertRaises(AttributeError):
            prepared.endpoint = "https://attacker.invalid"

    def test_seller_order_read_is_explicitly_not_buyer_history(self):
        method = "taobao.trades.sold.get"
        approval = replace(self.approval, granted_methods=frozenset({method}),
                           method_signing_policies=((method, frozenset({"hmac"})),))
        prepared = self.prepare(method=method, approval=approval,
                                business_parameters={"fields": "tid,status,payment", "page_size": 100, "use_has_next": True})
        self.assertEqual(prepared.data_scope, "authorized_seller_sold_orders_read")
        self.assertEqual(parse_qs(prepared.body.decode())["use_has_next"], ["true"])
        self.assert_code("TOP_FIELD_NOT_ALLOWED", method=method, approval=approval,
                         business_parameters={"fields": "tid,buyer_nick"})

    def test_order_dates_pagination_and_booleans_are_validated(self):
        method = "taobao.trades.sold.get"
        approval = replace(self.approval, granted_methods=frozenset({method}),
                           method_signing_policies=((method, frozenset({"hmac"})),))
        cases = [({"page_size": 101}, "TOP_POSITIVE_INTEGER_PARAMETER_REQUIRED"),
                 ({"page_no": 0}, "TOP_POSITIVE_INTEGER_PARAMETER_REQUIRED"),
                 ({"use_has_next": "true"}, "TOP_BOOLEAN_PARAMETER_REQUIRED"),
                 ({"start_created": "2026-13-01 00:00:00"}, "TOP_DATE_PARAMETER_INVALID"),
                 ({"start_created": "2026-1-1 00:00:00"}, "TOP_DATE_PARAMETER_INVALID"),
                 ({"start_created": "2026-10-04 01:00:00", "end_created": "2026-10-04 00:00:00"}, "TOP_DATE_RANGE_INVALID")]
        for parameters, code in cases:
            self.assert_code(code, method=method, approval=approval, business_parameters={"fields": "tid", **parameters})

    def test_preparation_does_not_use_network_or_report_platform_success(self):
        with patch.object(socket, "socket", side_effect=AssertionError("network forbidden")), \
                patch.object(socket, "create_connection", side_effect=AssertionError("network forbidden")):
            prepared = self.prepare()
        self.assertFalse(prepared.network_dispatched)
        self.assertFalse(hasattr(prepared, "payment_status"))
        self.assertFalse(hasattr(prepared, "success"))


if __name__ == "__main__":
    unittest.main()
