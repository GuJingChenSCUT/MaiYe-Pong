"""Isolated OAuth security checks; synthetic RSA keys and remote responses only.

HTTP tests use a real loopback server and disposable SQLite. Google ID tokens
are verified by google-auth, not by a mocked identity verifier. No account or
merchant credentials are loaded, and public network access is disabled.
"""
from __future__ import annotations

import base64
import copy
import concurrent.futures
import datetime
import functools
import hashlib
import http.client
from http.cookies import SimpleCookie
import json
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlencode, urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.x509.oid import NameOID

from app.auth import APIError, Auth, hashed
from app.social_auth import (GOOGLE_AUTH, GOOGLE_CERTS, GOOGLE_TOKEN, WECHAT_TOKEN,
                             ProviderConfig, SocialAuth, exchange_identity,
                             load_configs, provider_request)
from app.task_api import ApplicationServer
from app.task_store import TaskStore
from slice04.fixtures import V1_PRODUCT

GOOGLE = ProviderConfig("test-google-client", "TEST-ONLY-google-secret", True)
WECHAT = ProviderConfig("test-wechat-app", "TEST-ONLY-wechat-secret", True)
CONFIGS = {"google": GOOGLE, "wechat": WECHAT}


def b64(value):
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode()


class TestIssuer:
    """An ephemeral signer, never a real Google or merchant key."""
    def __init__(self):
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "offline-test-only")])
        now = datetime.datetime.now(datetime.timezone.utc)
        cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
                .public_key(self.key.public_key()).serial_number(x509.random_serial_number())
                .not_valid_before(now - datetime.timedelta(days=1))
                .not_valid_after(now + datetime.timedelta(days=1)).sign(self.key, hashes.SHA256()))
        self.certificates = {"test-key": cert.public_bytes(serialization.Encoding.PEM).decode()}

    def claims(self, nonce, *, subject="test-subject", **updates):
        now = int(time.time())
        result = {"iss": "https://accounts.google.com", "aud": GOOGLE.client_id,
                  "sub": subject, "nonce": nonce, "iat": now - 2, "exp": now + 600}
        result.update(updates)
        return result

    def token(self, claims):
        header = b64(json.dumps({"alg": "RS256", "kid": "test-key"}).encode())
        payload = b64(json.dumps(claims).encode())
        unsigned = f"{header}.{payload}".encode()
        signature = self.key.sign(unsigned, padding.PKCS1v15(), hashes.SHA256())
        return unsigned.decode() + "." + b64(signature)


_ISSUER = None


def issuer():
    global _ISSUER
    if _ISSUER is None:
        _ISSUER = TestIssuer()
    return _ISSUER


class RemoteResponses:
    def __init__(self):
        self.tokens = {}
        self.calls = []
        self.wechat = {"access_token": "TEST-ONLY-token", "openid": "verified-wechat-subject",
                       "scope": "snsapi_login"}

    def __call__(self, url, *, method="GET", body=None, headers=None):
        self.calls.append((url, method, body, headers))
        if url == GOOGLE_TOKEN:
            params = parse_qs(body.decode())
            data = {"id_token": self.tokens[params["code"][0]], "access_token": "DO-NOT-EXPOSE"}
        elif url == GOOGLE_CERTS:
            data = issuer().certificates
        elif urlsplit(url)._replace(query="").geturl() == WECHAT_TOKEN:
            data = self.wechat
        else:
            raise AssertionError("Unexpected provider endpoint: " + urlsplit(url).path)
        return SimpleNamespace(status=200, data=json.dumps(data).encode())


class OfflineCase(unittest.TestCase):
    def setUp(self):
        # No upstream request is allowed to escape an explicitly supplied fixture.
        self.addCleanup(patch.stopall)
        patch("urllib.request.OpenerDirector.open", side_effect=AssertionError("Public network disabled in OAuth tests")).start()
        self.temp = tempfile.TemporaryDirectory(prefix="maiyebang-oauth-test-")
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / "oauth.sqlite3"
        self.now = int(time.time())
        self.store = TaskStore(self.db, clock=lambda: self.now)
        self.remote = RemoteResponses()
        self.exchange = functools.partial(exchange_identity, transport=self.remote)
        self.social = SocialAuth(self.store, origin="https://login.example.test", configs=CONFIGS,
                                 exchange=self.exchange)

    def start(self, social=None, provider="google", subject="test-subject", **claims):
        social = social or self.social
        result, browser = social.start(provider)
        query = parse_qs(urlsplit(result["authorization_url"]).query)
        state = query["state"][0]
        code = "test-code-" + secrets.token_hex(8)
        if provider == "google":
            self.remote.tokens[code] = issuer().token(issuer().claims(query["nonce"][0], subject=subject, **claims))
        return state, browser, code, result

    def finish(self, started, social=None, provider="google", **extra):
        state, browser, code, _ = started
        return (social or self.social).finish(provider, params={"state": state, "code": code, **extra}, browser=browser)

    def assert_api(self, code, call, status=None):
        with self.assertRaises(APIError) as caught:
            call()
        self.assertEqual(caught.exception.code, code)
        if status is not None:
            self.assertEqual(caught.exception.status, status)
        return caught.exception


class GoogleAndWeChatVerificationTests(OfflineCase):
    def verify_claims(self, claims, *, expected_nonce="nonce-test"):
        self.remote.tokens["verify-code"] = issuer().token(claims)
        return exchange_identity("google", GOOGLE, code="verify-code", redirect_uri="https://login.example.test/callback",
                                 nonce=expected_nonce, verifier="test-pkce-verifier", transport=self.remote)

    def test_google_official_verifier_accepts_real_rsa_signature_and_pkce_exchange(self):
        subject = self.verify_claims(issuer().claims("nonce-test", subject="verified-subject"))
        self.assertEqual(subject, "verified-subject")
        self.assertEqual([call[0] for call in self.remote.calls], [GOOGLE_TOKEN, GOOGLE_CERTS])
        _, method, body, headers = self.remote.calls[0]
        payload = parse_qs(body.decode())
        self.assertEqual(method, "POST")
        self.assertEqual(payload["client_id"], [GOOGLE.client_id])
        self.assertEqual(payload["code_verifier"], ["test-pkce-verifier"])
        self.assertEqual(headers["Content-Type"], "application/x-www-form-urlencoded")

    def test_google_rejects_wrong_issuer(self):
        self.assert_api("LOGIN_IDENTITY_REJECTED", lambda: self.verify_claims(issuer().claims("nonce-test", iss="https://evil.example")))

    def test_google_rejects_wrong_audience(self):
        self.assert_api("LOGIN_IDENTITY_REJECTED", lambda: self.verify_claims(issuer().claims("nonce-test", aud="other-app")))

    def test_google_rejects_wrong_nonce(self):
        self.assert_api("LOGIN_IDENTITY_REJECTED", lambda: self.verify_claims(issuer().claims("another-nonce")))

    def test_google_rejects_expired_token(self):
        self.assert_api("LOGIN_IDENTITY_REJECTED", lambda: self.verify_claims(issuer().claims("nonce-test", exp=int(time.time()) - 30)))

    def test_google_rejects_wrong_authorized_party(self):
        self.assert_api("LOGIN_IDENTITY_REJECTED", lambda: self.verify_claims(issuer().claims("nonce-test", azp="other-app")))

    def test_google_rejects_tampered_signed_payload(self):
        token = issuer().token(issuer().claims("nonce-test"))
        header, payload, signature = token.split(".")
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        claims["sub"] = "attacker-subject"
        self.remote.tokens["tampered"] = header + "." + b64(json.dumps(claims).encode()) + "." + signature
        self.assert_api("LOGIN_IDENTITY_REJECTED", lambda: exchange_identity(
            "google", GOOGLE, code="tampered", redirect_uri="https://login.example.test/callback",
            nonce="nonce-test", verifier="test", transport=self.remote))

    def test_google_rejects_unsigned_token(self):
        self.remote.tokens["unsigned"] = b64(b'{"alg":"none"}') + "." + b64(json.dumps(issuer().claims("nonce-test")).encode()) + "."
        self.assert_api("LOGIN_IDENTITY_REJECTED", lambda: exchange_identity(
            "google", GOOGLE, code="unsigned", redirect_uri="https://login.example.test/callback",
            nonce="nonce-test", verifier="test", transport=self.remote))

    def test_wechat_raw_exchange_uses_server_app_scope_and_subject(self):
        result = exchange_identity("wechat", WECHAT, code="provider-code", redirect_uri="https://login.example.test/callback",
                                   nonce="irrelevant", verifier="irrelevant", transport=self.remote)
        self.assertEqual(result, "verified-wechat-subject")
        query = parse_qs(urlsplit(self.remote.calls[0][0]).query)
        self.assertEqual(query, {"appid": [WECHAT.client_id], "secret": [WECHAT.client_secret],
                                 "code": ["provider-code"], "grant_type": ["authorization_code"]})

    def test_wechat_scope_error_and_missing_subject_are_rejected(self):
        for updates in ({"scope": "snsapi_userinfo"}, {"scope": "snsapi_login,snsapi_userinfo"},
                        {"openid": ""}, {"openid": "bad subject"}, {"openid": None},
                        {"errcode": 40029}, {"access_token": ""}):
            with self.subTest(updates=updates):
                self.remote.wechat = {"access_token": "test", "openid": "subject", "scope": "snsapi_login", **updates}
                self.assert_api("LOGIN_IDENTITY_REJECTED", lambda: exchange_identity(
                    "wechat", WECHAT, code="test", redirect_uri="https://login.example.test/callback",
                    nonce="test", verifier="test", transport=self.remote))

    def test_provider_request_rejects_arbitrary_endpoints_before_network(self):
        for url in ("https://evil.example/token", GOOGLE_TOKEN + "/other", GOOGLE_TOKEN + "#secret",
                    "http://oauth2.googleapis.com/token", "https://oauth2.googleapis.com.evil.example/token"):
            with self.subTest(url=url):
                self.assert_api("LOGIN_ENDPOINT_REJECTED", lambda: provider_request(url))


class SocialStateTests(OfflineCase):
    def test_missing_configuration_is_closed_and_status_does_not_expose_secrets(self):
        configs = load_configs({})
        social = SocialAuth(self.store, origin="https://login.example.test", configs=configs)
        self.assertTrue(all(not item["available"] for item in social.status()))
        self.assert_api("LOGIN_NOT_CONFIGURED", lambda: social.start("google"), 503)
        self.assert_api("LOGIN_NOT_CONFIGURED", lambda: social.start("wechat"), 503)
        output = json.dumps(self.social.status()) + repr(GOOGLE) + repr(WECHAT)
        self.assertNotIn(GOOGLE.client_secret, output)
        self.assertNotIn(WECHAT.client_secret, output)
        self.assertEqual(self.remote.calls, [])

    def test_state_browser_nonce_are_random_and_pkce_bound_to_persisted_verifier(self):
        a, b = self.start(), self.start()
        self.assertNotEqual(a[0], b[0]); self.assertNotEqual(a[1], b[1])
        with self.store.connection() as conn:
            rows = conn.execute("SELECT * FROM app_login_attempts ORDER BY state_hash").fetchall()
        self.assertEqual(len(rows), 2)
        row = next(row for row in rows if row["state_hash"] == hashed(a[0]))
        query = parse_qs(urlsplit(a[3]["authorization_url"]).query)
        self.assertEqual(urlsplit(a[3]["authorization_url"])._replace(query="").geturl(), GOOGLE_AUTH)
        self.assertEqual(query["code_challenge_method"], ["S256"])
        self.assertEqual(query["code_challenge"], [b64(hashlib.sha256(row["verifier"].encode()).digest())])
        self.assertEqual(query["nonce"], [row["nonce"]])
        self.assertEqual(row["browser_hash"], hashed(a[1]))
        self.assertNotEqual(row["browser_hash"], a[1])
        self.assertEqual(row["expires_at"], self.now + 300)
        self.assertNotIn(GOOGLE.client_secret, a[3]["authorization_url"])

    def test_wrong_browser_cookie_rejected_without_consuming_valid_state(self):
        started = self.start()
        self.assert_api("LOGIN_STATE_REJECTED", lambda: self.social.finish("google", params={"state": started[0], "code": started[2]}, browser=secrets.token_urlsafe(32)))
        self.assertEqual(self.remote.calls, [])
        self.assertEqual(self.finish(started)[1]["actor"]["role"], "buyer")

    def test_expiry_boundary_rejects_before_exchange(self):
        started = self.start(); self.now += 300
        self.assert_api("LOGIN_STATE_REJECTED", lambda: self.finish(started))
        self.assertEqual(self.remote.calls, [])

    def test_successful_state_cannot_be_replayed(self):
        started = self.start(); self.finish(started); count = len(self.remote.calls)
        self.assert_api("LOGIN_STATE_REJECTED", lambda: self.finish(started))
        self.assertEqual(len(self.remote.calls), count)

    def test_provider_or_client_id_change_cannot_reuse_state(self):
        started = self.start()
        self.assert_api("LOGIN_STATE_REJECTED", lambda: self.finish(started, provider="wechat"))
        self.social.configs["google"] = ProviderConfig("other-client", "test-other-secret", True)
        self.assert_api("LOGIN_STATE_REJECTED", lambda: self.finish(started))
        self.assertEqual(self.remote.calls, [])

    def test_failed_exchange_and_cancelled_login_consume_state(self):
        started = self.start(); self.remote.tokens[started[2]] = "invalid-id-token"
        self.assert_api("LOGIN_IDENTITY_REJECTED", lambda: self.finish(started))
        self.assert_api("LOGIN_STATE_REJECTED", lambda: self.finish(started))
        cancelled = self.start()
        self.assert_api("LOGIN_CANCELLED", lambda: self.finish(cancelled, error="access_denied"))
        self.assert_api("LOGIN_STATE_REJECTED", lambda: self.finish(cancelled))

    def test_malformed_state_and_authorization_code_are_rejected(self):
        for bad in ("", "short", "x" * 129, "x" * 31 + " "):
            with self.subTest(state=bad):
                self.assert_api("LOGIN_STATE_REJECTED", lambda: self.social.finish("google", params={"state": bad, "code": "test"}, browser=secrets.token_urlsafe(32)))
        for bad in ("", "x" * 2049, "bad code", "bad\ncode"):
            with self.subTest(code_length=len(bad)):
                started = self.start()
                self.assert_api("LOGIN_CODE_REJECTED", lambda: self.social.finish("google", params={"state": started[0], "code": bad}, browser=started[1]))
        self.assertEqual(self.remote.calls, [])

    def test_pending_state_limit_bounds_unauthenticated_storage(self):
        for _ in range(200):
            self.social.start("google")
        self.assert_api("LOGIN_BUSY", lambda: self.social.start("google"), 429)
        self.now += 300
        self.social.start("google")
        with self.store.connection() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM app_login_attempts").fetchone()[0], 1)

    def test_same_verified_subject_is_stable_and_distinct_subject_is_isolated(self):
        token_a, a = self.finish(self.start(subject="subject-a"))
        _, again = self.finish(self.start(subject="subject-a"))
        _, other = self.finish(self.start(subject="subject-b"))
        self.assertEqual(a["actor"], again["actor"])
        self.assertNotEqual(a["actor"]["actor_id"], other["actor"]["actor_id"])
        self.assertEqual(Auth(self.store).session(token_a)["actor"], a["actor"])

    def test_same_subject_is_namespaced_by_provider_and_app_without_profile_linking(self):
        _, google = self.finish(self.start(subject="same-subject", email="same@example.test"))
        self.remote.wechat["openid"] = "same-subject"
        _, wechat = self.finish(self.start(provider="wechat"), provider="wechat", email="same@example.test")
        self.social.configs["google"] = ProviderConfig("second-google-app", "test-other-secret", True)
        _, other_app = self.finish(self.start(subject="same-subject", aud="second-google-app", email="same@example.test"))
        actors = {result["actor"]["actor_id"] for result in (google, wechat, other_app)}
        self.assertEqual(len(actors), 3)

    def test_wechat_browser_profile_cannot_choose_subject_or_role(self):
        started = self.start(provider="wechat")
        _, actor = self.finish(started, provider="wechat", openid="attacker", unionid="attacker", role="operator", actor_id="local-operator")
        self.assertEqual(actor["actor"]["role"], "buyer")
        with self.store.connection() as conn:
            subject = conn.execute("SELECT subject FROM app_social_identities WHERE provider='wechat'").fetchone()[0]
        self.assertEqual(subject, "verified-wechat-subject")

    def test_existing_login_identity_can_be_disabled_server_side(self):
        _, result = self.finish(self.start())
        with self.store.transaction() as conn:
            conn.execute("UPDATE app_access_codes SET active=0 WHERE actor_id=?", (result["actor"]["actor_id"],))
        self.assert_api("LOGIN_ACCOUNT_DISABLED", lambda: self.finish(self.start()), 403)

    def test_http_loopback_cannot_enable_wechat_even_with_config(self):
        social = SocialAuth(self.store, origin="http://127.0.0.1:1234", configs=CONFIGS, exchange=self.exchange)
        status = next(item for item in social.status() if item["provider"] == "wechat")
        self.assertFalse(status["available"])
        self.assertEqual(status["reason"], "public_https_callback_required")
        self.assert_api("LOGIN_NOT_CONFIGURED", lambda: social.start("wechat"), 503)
        self.assertEqual(self.remote.calls, [])

    def test_oauth_state_survives_real_subprocess_restart_with_real_signature_verifier(self):
        started = self.start(subject="restart-subject")
        child = r'''
import functools,json,sys
from types import SimpleNamespace
from unittest.mock import patch
from app.auth import Auth
from app.social_auth import SocialAuth,ProviderConfig,exchange_identity,GOOGLE_TOKEN,GOOGLE_CERTS
from app.task_store import TaskStore
data=json.loads(sys.stdin.read())
def transport(url,**kwargs):
 if url==GOOGLE_TOKEN: body={"id_token":data["jwt"]}
 elif url==GOOGLE_CERTS: body=data["certs"]
 else: raise AssertionError("unexpected endpoint")
 return SimpleNamespace(status=200,data=json.dumps(body).encode())
with patch("urllib.request.OpenerDirector.open",side_effect=AssertionError("network forbidden")):
 store=TaskStore(data["path"])
 social=SocialAuth(store,origin="https://login.example.test",configs={"google":ProviderConfig("test-google-client","TEST-ONLY-google-secret",True)},exchange=functools.partial(exchange_identity,transport=transport))
 token,result=social.finish("google",params={"state":data["state"],"code":data["code"]},browser=data["browser"])
 print(json.dumps({"token":token,"actor":result["actor"]}))
'''
        payload = {"path": str(self.db), "state": started[0], "browser": started[1], "code": started[2],
                   "jwt": self.remote.tokens[started[2]], "certs": issuer().certificates}
        process = subprocess.run([sys.executable, "-c", child], input=json.dumps(payload), text=True,
                                 capture_output=True, cwd=ROOT, timeout=30)
        self.assertEqual(process.returncode, 0, process.stderr)
        result = json.loads(process.stdout)
        reopened = TaskStore(self.db)
        self.assertEqual(Auth(reopened).session(result["token"])["actor"], result["actor"])
        self.assertEqual(result["actor"]["role"], "buyer")
        self.assert_api("LOGIN_STATE_REJECTED", lambda: self.finish(started))


class SocialHTTPTests(OfflineCase):
    def setUp(self):
        super().setUp()
        self.server = ApplicationServer(("127.0.0.1", 0), self.store, start_worker=False,
                                        social_configs=CONFIGS, social_exchange=self.exchange)
        self.thread = threading.Thread(target=lambda: self.server.serve_forever(poll_interval=.01), daemon=True)
        self.thread.start()
        self.addCleanup(self.close_server)

    def close_server(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join(timeout=3)

    def request(self, method, path, *, body=None, cookie=None, headers=None):
        headers = dict(headers or {})
        if cookie: headers["Cookie"] = cookie
        if body is not None:
            headers["Content-Type"] = "application/json"
            body = json.dumps(body).encode()
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=5)
        try:
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            data = response.read()
            return response.status, response.getheaders(), data
        finally:
            connection.close()

    @staticmethod
    def cookie(headers, name):
        for key, value in headers:
            if key.lower() == "set-cookie":
                parsed = SimpleCookie(); parsed.load(value)
                if name in parsed and parsed[name].value:
                    return name + "=" + parsed[name].value
        return None

    def begin(self, subject="http-subject"):
        status, headers, raw = self.request("POST", "/api/v1/auth/google/start", body={})
        self.assertEqual(status, 200, raw)
        query = parse_qs(urlsplit(json.loads(raw)["authorization_url"]).query)
        code = "http-code-" + secrets.token_hex(8)
        self.remote.tokens[code] = issuer().token(issuer().claims(query["nonce"][0], subject=subject))
        return query["state"][0], self.cookie(headers, "hacku_oauth_google"), code, headers

    def callback(self, started, *, extra=None, cookie=None, headers=None):
        state, binding, code, _ = started
        query = urlencode({"state": state, "code": code, **(extra or {})})
        return self.request("GET", "/api/v1/auth/google/callback?" + query,
                            cookie=cookie or binding, headers=headers)

    def login(self, subject):
        status, headers, raw = self.callback(self.begin(subject))
        self.assertEqual(status, 303, raw)
        self.assertEqual(dict(headers)["Location"], "/?login=success")
        cookie = self.cookie(headers, "hacku_session")
        status, _, raw = self.request("GET", "/api/v1/session", cookie=cookie)
        self.assertEqual(status, 200, raw)
        return cookie, json.loads(raw)

    def test_providers_endpoint_is_safe_and_wechat_disabled(self):
        status, _, raw = self.request("GET", "/api/v1/auth/providers")
        self.assertEqual(status, 200)
        providers = {item["provider"]: item for item in json.loads(raw)["providers"]}
        self.assertTrue(providers["google"]["available"])
        self.assertFalse(providers["wechat"]["available"])
        self.assertNotIn(GOOGLE.client_secret.encode(), raw)
        self.assertNotIn(WECHAT.client_secret.encode(), raw)
        self.assertEqual(self.request("POST", "/api/v1/auth/wechat/start", body={})[0], 503)

    def test_unconfigured_http_providers_fail_closed_without_any_exchange(self):
        self.server.social.configs = load_configs({})
        status, _, raw = self.request("GET", "/api/v1/auth/providers")
        self.assertEqual(status, 200)
        self.assertTrue(all(not item["available"] for item in json.loads(raw)["providers"]))
        for provider in ("google", "wechat"):
            status, headers, raw = self.request("POST", f"/api/v1/auth/{provider}/start", body={})
            self.assertEqual(status, 503)
            self.assertEqual(json.loads(raw)["error"], "LOGIN_NOT_CONFIGURED")
            self.assertFalse(any(key.lower() == "set-cookie" for key, _ in headers))
        self.assertEqual(self.remote.calls, [])

    def test_login_start_cookie_is_http_only_lax_and_path_scoped(self):
        started = self.begin()
        cookies = [value for key, value in started[3] if key.lower() == "set-cookie"]
        self.assertEqual(len(cookies), 1)
        self.assertIn("HttpOnly", cookies[0]); self.assertIn("SameSite=Lax", cookies[0])
        self.assertIn("Path=/api/v1/auth/google", cookies[0]); self.assertIn("Max-Age=300", cookies[0])

    def test_only_exact_get_callback_allows_cross_site_navigation(self):
        cross = {"Origin": "https://accounts.google.com", "Sec-Fetch-Site": "cross-site"}
        status, headers, _ = self.callback(self.begin(), headers=cross)
        self.assertEqual(status, 303)
        self.assertEqual(dict(headers)["Location"], "/?login=success")
        for method, path, body in (("GET", "/api/v1/config", None), ("GET", "/api/v1/auth/providers", None),
                                   ("POST", "/api/v1/auth/google/start", {}), ("POST", "/api/v1/auth/google/callback", {}),
                                   ("GET", "/api/v1/auth/unknown/callback", None), ("GET", "/api/v1/tasks", None)):
            with self.subTest(method=method, path=path):
                self.assertEqual(self.request(method, path, body=body, headers=cross)[0], 403)

    def test_callback_exception_does_not_relax_host_check(self):
        started = self.begin()
        status, _, raw = self.callback(started, headers={"Host": "evil.example", "Sec-Fetch-Site": "cross-site"})
        self.assertEqual(status, 403)
        self.assertEqual(json.loads(raw)["error"], "HOST_REJECTED")
        self.assertEqual(self.remote.calls, [])

    def test_duplicate_and_excess_query_parameters_are_rejected_without_exchange(self):
        started = self.begin()
        base = "/api/v1/auth/google/callback?" + urlencode({"state": started[0], "code": started[2]})
        for suffix in ("&state=duplicate", "&code=duplicate", "&role=buyer&role=operator", "".join(f"&k{i}=v" for i in range(12))):
            with self.subTest(suffix=suffix):
                status, headers, raw = self.request("GET", base + suffix, cookie=started[1])
                self.assertEqual(status, 303)
                self.assertEqual(dict(headers)["Location"], "/?login=retry")
                self.assertFalse(any(key.lower() == "set-cookie" for key, _ in headers))
                self.assertEqual(raw, b"")
        self.assertEqual(self.remote.calls, [])
        self.assertEqual(dict(self.callback(started)[1])["Location"], "/?login=success")

    def test_invalid_callback_does_not_clear_legitimate_in_progress_binding(self):
        started = self.begin()
        status, headers, raw = self.request("GET", "/api/v1/auth/google/callback?state=bad&code=bad", cookie=started[1])
        self.assertEqual(status, 303)
        self.assertEqual(dict(headers)["Location"], "/?login=retry")
        self.assertFalse(any(key.lower() == "set-cookie" for key, _ in headers))
        self.assertEqual(self.remote.calls, [])
        self.assertEqual(dict(self.callback(started)[1])["Location"], "/?login=success")

    def test_upstream_failure_does_not_reflect_token_error_or_profile(self):
        started = self.begin()
        sentinel = "SECRET-TOKEN-DO-NOT-REFLECT"
        self.remote.tokens[started[2]] = sentinel
        status, headers, raw = self.callback(started, extra={"openid": sentinel, "role": "operator"})
        self.assertEqual(status, 303)
        self.assertEqual(dict(headers)["Location"], "/?login=retry")
        self.assertNotIn(sentinel, str(headers) + raw.decode())
        self.assertNotIn(GOOGLE.client_secret, str(headers) + raw.decode())
        self.assertEqual(dict(self.callback(started)[1])["Location"], "/?login=retry")

    def test_cancelled_provider_message_is_not_reflected(self):
        started = self.begin()
        status, headers, raw = self.callback(started, extra={"error": "<script>secret</script>", "error_description": "provider-token"})
        self.assertEqual(status, 303)
        self.assertEqual(dict(headers)["Location"], "/?login=retry")
        self.assertNotIn("<script>secret</script>", str(headers) + raw.decode())
        self.assertNotIn("provider-token", str(headers) + raw.decode())
        self.assertEqual(self.remote.calls, [])

    def test_http_buyers_are_isolated_and_same_subject_retains_own_tasks(self):
        cookie_a, a = self.login("subject-a")
        body = {"text": "日用品", "mode": "scripted", "scenario": "normal", "fields": {
            "product": copy.deepcopy(V1_PRODUCT), "purchase_quantity": 1, "cash_cap_minor": 6000,
            "destination_ref": "HK-DEMO-KOWLOON", "preference": "lowest_cost"}}
        status, _, raw = self.request("POST", "/api/v1/intents", body=body, cookie=cookie_a,
                                     headers={"X-CSRF-Token": a["csrf_token"], "Idempotency-Key": "oauth-create-owned-task"})
        self.assertEqual(status, 200, raw); task_id = json.loads(raw)["task_id"]
        cookie_b, b = self.login("subject-b")
        self.assertNotEqual(a["actor"]["actor_id"], b["actor"]["actor_id"])
        self.assertEqual(self.request("GET", "/api/v1/tasks/" + task_id, cookie=cookie_b)[0], 404)
        self.assertEqual(json.loads(self.request("GET", "/api/v1/tasks", cookie=cookie_b)[2])["tasks"], [])
        cookie_again, again = self.login("subject-a")
        self.assertEqual(a["actor"], again["actor"])
        self.assertEqual(self.request("GET", "/api/v1/tasks/" + task_id, cookie=cookie_again)[0], 200)

    def test_social_login_rotates_operator_session_and_never_inherits_privilege(self):
        access = Auth(self.store).provision(actor_id="local-operator", role="operator")
        old_token, _ = Auth(self.store).login(access)
        started = self.begin()
        status, headers, raw = self.callback(started, cookie=started[1] + "; hacku_session=" + old_token,
                                             extra={"role": "operator", "actor_id": "local-operator"})
        self.assertEqual(status, 303, raw)
        session_cookie = self.cookie(headers, "hacku_session")
        status, _, raw = self.request("GET", "/api/v1/session", cookie=session_cookie)
        actor = json.loads(raw)["actor"]
        self.assertEqual(status, 200)
        self.assertEqual(actor["role"], "buyer")
        self.assertNotEqual(actor["actor_id"], "local-operator")
        self.assert_api("SESSION_EXPIRED", lambda: Auth(self.store).session(old_token))
        session_header = next(value for key, value in headers if key.lower() == "set-cookie" and value.startswith("hacku_session="))
        self.assertIn("HttpOnly", session_header); self.assertIn("SameSite=Strict", session_header)

    def test_simultaneous_callbacks_consume_state_once_and_create_one_session(self):
        started = self.begin()
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _: self.callback(started), range(2)))
        self.assertEqual(sorted(dict(headers)["Location"] for _, headers, _ in results),
                         ["/?login=retry", "/?login=success"])
        self.assertEqual([call[0] for call in self.remote.calls], [GOOGLE_TOKEN, GOOGLE_CERTS])
        with self.store.connection() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM app_sessions").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT count(*) FROM app_login_attempts").fetchone()[0], 0)

    def test_start_rejects_browser_supplied_profile_and_wrong_canonical_host(self):
        self.assertEqual(self.request("POST", "/api/v1/auth/google/start", body={"role": "operator", "openid": "attacker"})[0], 400)
        port = self.server.server_address[1]
        self.assertEqual(self.request("POST", "/api/v1/auth/google/start", body={}, headers={"Host": f"localhost:{port}"})[0], 400)
        self.assertEqual(self.remote.calls, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
