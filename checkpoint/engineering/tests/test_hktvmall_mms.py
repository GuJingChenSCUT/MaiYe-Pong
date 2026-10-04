"""Offline only: generated ephemeral RSA keys, invented UUID/store and no account.

Run after installing requirements-platform.txt. No private keys or JWTs are
written to disk or printed. A skipped optional suite is not a signing pass.
"""
import base64
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta, timezone
import hashlib
import json
import socket
import unittest
from unittest.mock import patch

from integrations.contracts import BackendContext, CallerKind
from integrations.hktvmall_mms import (
    MMS_BASE_URL, MMS_READ_ENDPOINTS, MmsCredentials, MmsPreparationError,
    MmsReadApproval, prepare_mms_read,
)

try:
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa
    CRYPTO_AVAILABLE = True
except ImportError:
    CRYPTO_AVAILABLE = False


def decode_segment(segment):
    return base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))


@unittest.skipUnless(CRYPTO_AVAILABLE, "Optional cryptography dependency missing; install requirements-platform.txt")
class MmsPreparationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cls.pem = cls.key.private_bytes(serialization.Encoding.PEM,
                                        serialization.PrivateFormat.PKCS8,
                                        serialization.NoEncryption())
        cls.fingerprint = hashlib.sha256(cls.key.public_key().public_bytes(
            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)).hexdigest()

    def setUp(self):
        self.now = datetime(2026, 10, 4, 3, 0, tzinfo=timezone.utc)
        self.epoch = int(self.now.timestamp())
        self.credentials = MmsCredentials("11111111-2222-4333-8444-555555555555", self.pem, "H8888881")
        self.context = BackendContext("tenant-test", "backend-user-test", CallerKind.BACKEND)
        self.approval = MmsReadApproval(
            tenant_id=self.context.tenant_id, principal_id=self.context.user_id,
            store_code=self.credentials.store_code, api_key=self.credentials.api_key,
            public_key_sha256=self.fingerprint,
            approval_evidence_ref="fixture://reviewed-grant-not-real",
            credential_binding_evidence_ref="fixture://reviewed-key-not-real",
            valid_from=self.epoch - 60, expires_at=self.epoch + 3600,
            granted_methods=frozenset(MMS_READ_ENDPOINTS), warehouse_ids=frozenset({"H888888101"}),
        )

    def prepare(self, **kwargs):
        values = dict(method="store.details", credentials=self.credentials,
                      context=self.context, approval=self.approval, now=self.now)
        values.update(kwargs)
        return prepare_mms_read(**values)

    def rejected(self, code, **kwargs):
        with self.assertRaises(MmsPreparationError) as caught:
            self.prepare(**kwargs)
        self.assertEqual(str(caught.exception), code)

    def test_rs256_signature_and_exact_tutorial_claims(self):
        plan = self.prepare()
        token = plan.wire_headers()["x-auth-token"]
        head, payload, signature = token.split(".")
        self.assertEqual(json.loads(decode_segment(head)), {"alg": "RS256", "typ": "JWT"})
        claims = json.loads(decode_segment(payload))
        self.assertEqual(claims, {"sub": "shoalter", "name": "shoalter", "iat": self.epoch,
                                  "x-api-key": self.credentials.api_key})
        self.assertNotIn("exp", claims)
        self.key.public_key().verify(decode_segment(signature), (head + "." + payload).encode(),
                                     padding.PKCS1v15(), hashes.SHA256())
        with self.assertRaises(InvalidSignature):
            self.key.public_key().verify(decode_segment(signature), (head + "." + payload + "x").encode(),
                                         padding.PKCS1v15(), hashes.SHA256())

    def test_production_store_get_has_no_body_query_or_bearer(self):
        plan = self.prepare()
        self.assertEqual(plan.endpoint, MMS_BASE_URL + MMS_READ_ENDPOINTS["store.details"])
        self.assertEqual(plan.http_method, "GET")
        self.assertEqual(plan.environment, "production")
        self.assertFalse(plan.network_dispatched)
        self.assertIsNone(plan.body)
        self.assertEqual(plan.query, b"")
        headers = plan.wire_headers()
        self.assertEqual(set(headers), {"Content-Type", "x-auth-token", "storeCode", "platformCode", "businessType"})
        self.assertEqual(headers["Content-Type"], "application/json")
        self.assertEqual(headers["storeCode"], "H8888881")
        self.assertEqual(headers["platformCode"], "HKTV")
        self.assertEqual(headers["businessType"], "eCommerce")
        self.assertFalse(headers["x-auth-token"].startswith("Bearer "))
        self.assertIsNone(plan.rate_limit_hint)

    def test_product_codes_default_and_bounded_pagination(self):
        plan = self.prepare(method="product.codes")
        self.assertEqual(plan.query, b"page=1&pageSize=10")
        self.assertIsNone(plan.body)
        self.assertEqual(plan.rate_limit_hint, (1, 100))
        self.assertEqual(self.prepare(method="product.codes", query={"page": 2, "pageSize": 100}).query,
                         b"page=2&pageSize=100")

    def test_pagination_rejects_bool_float_string_negative_and_oversized(self):
        for value in (True, 1.5, "1", 0, -1, 2**31):
            with self.subTest(page=value):
                self.rejected("MMS_PAGE_INVALID", method="product.codes", query={"page": value})
        for value in (True, 1.5, "10", 0, -1, 101):
            with self.subTest(page_size=value):
                self.rejected("MMS_PAGE_SIZE_INVALID", method="product.codes", query={"pageSize": value})

    def test_product_details_get_keeps_array_body(self):
        body = [{"skuCode": "H8888881_S_a1"}, {"skuCode": "H8888881_S_a2"}]
        plan = self.prepare(method="product.details", body=body)
        self.assertEqual(plan.http_method, "GET")
        self.assertEqual(plan.query, b"")
        self.assertEqual(json.loads(plan.body), body)

    def test_stock_get_preserves_warehouse_omitted_null_and_approved(self):
        body = [{"productId": "H8888881_S_a1"},
                {"productId": "H8888881_S_a2", "warehouseId": None},
                {"productId": "H8888881_S_a3", "warehouseId": "H888888101"}]
        plan = self.prepare(method="inventory.details", body=body)
        self.assertEqual(plan.http_method, "GET")
        self.assertEqual(plan.query, b"")
        self.assertEqual(json.loads(plan.body), body)
        self.assertEqual(plan.rate_limit_hint, (3, 100))

    def test_arrays_exactly_100_allowed_0_and_101_rejected(self):
        for method, key in (("product.details", "skuCode"), ("inventory.details", "productId")):
            with self.subTest(method=method):
                self.assertEqual(len(json.loads(self.prepare(method=method, body=[{key: f"H8888881_S_{i}"} for i in range(100)]).body)), 100)
                for body in (None, [], [{key: "H8888881_S_a"}] * 101, {key: "H8888881_S_a"}, "[]"):
                    self.rejected("MMS_BODY_ARRAY_1_TO_100_REQUIRED", method=method, body=body)

    def test_cross_store_and_partial_sku_rejected_for_both_body_apis(self):
        for method, key in (("product.details", "skuCode"), ("inventory.details", "productId")):
            for sku in ("a1", "H8888882_S_a1", "H88888810_S_a1", "H8888881_S_", " H8888881_S_a1", "H8888881_S_a\n"):
                with self.subTest(method=method, sku=sku):
                    self.rejected("MMS_FULL_SKU_STORE_BINDING_REQUIRED", method=method, body=[{key: sku}])

    def test_warehouse_needs_explicit_store_approval(self):
        self.rejected("MMS_WAREHOUSE_APPROVAL_REQUIRED", method="inventory.details",
                      body=[{"productId": "H8888881_S_a1", "warehouseId": "OTHER-WAREHOUSE"}])
        self.rejected("MMS_WAREHOUSE_APPROVAL_REQUIRED", method="inventory.details",
                      approval=replace(self.approval, warehouse_ids=frozenset()),
                      body=[{"productId": "H8888881_S_a1", "warehouseId": "H888888101"}])

    def test_query_cannot_override_fixed_headers_claims_or_path(self):
        for parameter in ("storeCode", "x-auth-token", "x-api-key", "platformCode", "businessType", "alg", "iat", "exp", "endpoint", "method", "access_token"):
            with self.subTest(parameter=parameter):
                self.rejected("MMS_REQUEST_PARAMETER_NOT_ALLOWED", method="product.codes", query={parameter: "untrusted"})

    def test_body_cannot_override_scope_or_include_customer_order_fields(self):
        for parameter in ("storeCode", "x-auth-token", "platformCode", "orderId", "buyerId", "address", "customerName"):
            with self.subTest(parameter=parameter):
                self.rejected("MMS_REQUEST_PARAMETER_NOT_ALLOWED", method="product.details",
                              body=[{"skuCode": "H8888881_S_a1", parameter: "untrusted"}])

    def test_unexpected_body_and_query_rejected(self):
        self.rejected("MMS_REQUEST_PARAMETER_NOT_ALLOWED", body=[])
        self.rejected("MMS_REQUEST_PARAMETER_NOT_ALLOWED", query={"page": 1})
        self.rejected("MMS_REQUEST_PARAMETER_NOT_ALLOWED", method="product.codes", body=[])
        self.rejected("MMS_REQUEST_PARAMETER_NOT_ALLOWED", method="product.details", query={"skuCode": "H8888881_S_a1"}, body=[{"skuCode": "H8888881_S_a1"}])
        self.rejected("MMS_BODY_ITEM_INVALID", method="product.details", body=["H8888881_S_a1"])
        self.rejected("MMS_QUERY_PARAMETERS_INVALID", query=[("page", 1)])

    def test_default_absent_empty_and_wrong_method_grants_deny(self):
        for approval in (None, MmsReadApproval(), replace(self.approval, granted_methods=frozenset({"product.codes"})),
                         replace(self.approval, granted_methods={"store.details"})):
            with self.subTest(approval_type=type(approval).__name__):
                self.rejected("MMS_METHOD_APPROVAL_REQUIRED", approval=approval)

    def test_model_or_untyped_context_is_rejected(self):
        for context in ({"kind": "authenticated_backend"}, replace(self.context, kind=CallerKind.MODEL),
                        replace(self.context, kind="authenticated_backend"), replace(self.context, user_id="")):
            self.rejected("MMS_AUTHENTICATED_BACKEND_REQUIRED", context=context)

    def test_tenant_and_principal_cannot_borrow_store_grant(self):
        self.rejected("MMS_APPROVAL_PRINCIPAL_MISMATCH", context=replace(self.context, tenant_id="other-tenant"))
        self.rejected("MMS_APPROVAL_PRINCIPAL_MISMATCH", context=replace(self.context, user_id="other-principal"))

    def test_store_and_api_uuid_must_match_reviewed_binding(self):
        self.rejected("MMS_APPROVAL_STORE_OR_ACCOUNT_MISMATCH", credentials=replace(self.credentials, store_code="H8888882"))
        self.rejected("MMS_APPROVAL_STORE_OR_ACCOUNT_MISMATCH", credentials=replace(self.credentials, api_key="99999999-2222-4333-8444-555555555555"))

    def test_different_signing_key_not_allowed(self):
        self.rejected("MMS_APPROVED_SIGNING_KEY_MISMATCH", approval=replace(self.approval, public_key_sha256="0" * 64))

    def test_evidence_refs_and_key_binding_are_required(self):
        for change in ({"approval_evidence_ref": ""}, {"credential_binding_evidence_ref": ""}, {"public_key_sha256": ""}):
            self.rejected("MMS_APPROVAL_EVIDENCE_REQUIRED", approval=replace(self.approval, **change))

    def test_revoked_expired_future_and_millisecond_grants_rejected(self):
        for change in ({"revoked": True}, {"revoked": 0}, {"expires_at": self.epoch},
                       {"valid_from": self.epoch + 1}, {"valid_from": self.epoch * 1000},
                       {"expires_at": self.epoch * 1000}, {"valid_from": True}):
            self.rejected("MMS_APPROVAL_REVOKED_OR_EXPIRED", approval=replace(self.approval, **change))

    def test_iat_refresh_boundary_and_future_rejected(self):
        plan = self.prepare(issued_at=self.epoch - 1799)
        self.assertEqual(plan.token_refresh_at, self.epoch + 1)
        for iat in (self.epoch - 1800, self.epoch - 1801, self.epoch + 1):
            self.rejected("MMS_TOKEN_REFRESH_REQUIRED", issued_at=iat)

    def test_iat_milliseconds_bool_float_and_text_rejected(self):
        for iat in (self.epoch * 1000, True, float(self.epoch), str(self.epoch), 0, -1):
            self.rejected("MMS_IAT_EPOCH_SECONDS_REQUIRED", issued_at=iat)

    def test_epoch_uses_utc_not_local_clock_string(self):
        plan = self.prepare(now=self.now.astimezone(timezone(timedelta(hours=8))))
        self.assertEqual(plan.token_issued_at, self.epoch)
        self.rejected("MMS_AWARE_BACKEND_CLOCK_REQUIRED", now=self.now.replace(tzinfo=None))

    def test_all_write_order_and_arbitrary_endpoints_rejected(self):
        for method in ("order.details", "inventory.update", "product.create", "POST", "https://evil.invalid/x",
                       "/oapi/api/store/details", "https://merchant-oapi.shoalter.com/oapi/api/store/details"):
            self.rejected("MMS_READ_METHOD_NOT_ALLOWED", method=method)

    def test_no_arbitrary_url_header_claim_or_verb_argument(self):
        for extra in ({"endpoint": "https://evil.invalid"}, {"headers": {}}, {"claims": {"alg": "none"}}, {"http_method": "POST"}):
            with self.assertRaises(TypeError):
                self.prepare(**extra)

    def test_credentials_token_and_nested_headers_reprs_are_redacted(self):
        plan = self.prepare()
        raw_token = plan.wire_headers()["x-auth-token"]
        for value in (self.credentials, self.approval, plan, plan.headers, plan.headers["x-auth-token"]):
            rendered = repr(value) + str(value)
            self.assertNotIn(self.credentials.api_key, rendered)
            self.assertNotIn(self.pem.decode(), rendered)
            self.assertNotIn(raw_token, rendered)
        self.assertIn("redacted", repr(plan.headers))

    def test_input_mutation_cannot_change_prepared_wire_bytes(self):
        body = [{"skuCode": "H8888881_S_a1"}]
        plan = self.prepare(method="product.details", body=body)
        body[0]["skuCode"] = "H8888882_S_hijack"
        body.append({"skuCode": "H8888882_S_hijack2"})
        self.assertEqual(json.loads(plan.body), [{"skuCode": "H8888881_S_a1"}])
        with self.assertRaises(TypeError):
            plan.headers["storeCode"] = "H8888882"
        with self.assertRaises(FrozenInstanceError):
            plan.endpoint = "https://evil.invalid"
        exported = plan.wire_headers()
        exported["storeCode"] = "H8888882"
        self.assertEqual(plan.wire_headers()["storeCode"], "H8888881")

    def test_all_four_preparations_work_while_network_and_key_files_forbidden(self):
        # Patch concrete network/file boundaries, not just a declared boolean.
        with patch("socket.socket.connect", side_effect=AssertionError("network forbidden")), \
             patch("socket.create_connection", side_effect=AssertionError("network forbidden")), \
             patch("urllib.request.urlopen", side_effect=AssertionError("network forbidden")), \
             patch("builtins.open", side_effect=AssertionError("file credential loading forbidden")):
            plans = [self.prepare(), self.prepare(method="product.codes"),
                     self.prepare(method="product.details", body=[{"skuCode": "H8888881_S_a1"}]),
                     self.prepare(method="inventory.details", body=[{"productId": "H8888881_S_a1"}])]
        self.assertEqual(len(plans), 4)
        self.assertTrue(all(not plan.network_dispatched for plan in plans))

    def test_key_uuid_and_frontstore_inputs_are_validated_without_echo(self):
        for change, code in (({"api_key": "secret-not-uuid"}, "MMS_UUID_REQUIRED"),
                             ({"store_code": "MMS123"}, "MMS_STOREFRONT_STORE_CODE_REQUIRED"),
                             ({"store_code": "H8888881\r\nx-evil: yes"}, "MMS_STOREFRONT_STORE_CODE_REQUIRED"),
                             ({"private_key_pem": "secret-key"}, "MMS_PRIVATE_KEY_INPUT_INVALID")):
            with self.assertRaises(MmsPreparationError) as caught:
                replace(self.credentials, **change)
            self.assertEqual(str(caught.exception), code)
        self.rejected("MMS_RSA_PRIVATE_KEY_INVALID", credentials=replace(self.credentials, private_key_pem=b"not-a-key"))

    def test_non_rsa_key_is_rejected(self):
        key = ec.generate_private_key(ec.SECP256R1())
        pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
        self.rejected("MMS_RSA_KEY_SIZE_OR_TYPE_UNSUPPORTED", credentials=replace(self.credentials, private_key_pem=pem))


class MmsDefaultDenyWithoutDependencyTests(unittest.TestCase):
    def test_empty_grant_rejected_before_key_loading_or_signing(self):
        credentials = MmsCredentials("11111111-2222-4333-8444-555555555555", b"unloaded-test-key", "H8888881")
        with patch("integrations.hktvmall_mms._load_key", side_effect=AssertionError("must not load key")):
            with self.assertRaisesRegex(MmsPreparationError, "^MMS_METHOD_APPROVAL_REQUIRED$"):
                prepare_mms_read(method="store.details", credentials=credentials,
                                 context=BackendContext("tenant-test", "user-test", CallerKind.BACKEND),
                                 now=datetime(2026, 10, 4, tzinfo=timezone.utc))


if __name__ == "__main__":
    unittest.main()
