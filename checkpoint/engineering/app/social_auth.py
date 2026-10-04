"""Backend social login. No provider credentials or tokens enter model context.

Google uses the official google-auth signature verifier. WeChat website OAuth
has a separate adapter and subject namespace; it is not WeChat Pay permission.
Missing configuration disables login. The current HTTP app remains loopback
only: WeChat's public callback deployment is deliberately not enabled here.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import base64
import hashlib
import hmac
import importlib.util
import json
import os
import re
import secrets
from types import SimpleNamespace
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, HTTPRedirectHandler, build_opener

from app.auth import APIError, Auth, hashed

SCHEMA = """
CREATE TABLE IF NOT EXISTS app_login_attempts (
 state_hash TEXT PRIMARY KEY, browser_hash TEXT NOT NULL, provider TEXT NOT NULL,
 client_id TEXT NOT NULL, nonce TEXT NOT NULL, verifier TEXT NOT NULL,
 redirect_uri TEXT NOT NULL, expires_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS app_social_identities (
 provider TEXT NOT NULL, client_id TEXT NOT NULL, subject TEXT NOT NULL,
 code_hash TEXT NOT NULL UNIQUE REFERENCES app_access_codes(code_hash),
 PRIMARY KEY(provider, client_id, subject)
);
"""
GOOGLE_AUTH = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN = "https://oauth2.googleapis.com/token"
GOOGLE_CERTS = "https://www.googleapis.com/oauth2/v1/certs"
WECHAT_AUTH = "https://open.weixin.qq.com/connect/qrconnect"
WECHAT_TOKEN = "https://api.weixin.qq.com/sns/oauth2/access_token"
TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9_-]{32,128}$")


@dataclass(frozen=True)
class ProviderConfig:
    client_id: str = ""
    client_secret: str = field(default="", repr=False)
    enabled: bool = False


def load_configs(environ=None):
    env = os.environ if environ is None else environ
    return {name: ProviderConfig(
        client_id=env.get(f"MAIYEBANG_{name.upper()}_CLIENT_ID", "").strip(),
        client_secret=env.get(f"MAIYEBANG_{name.upper()}_CLIENT_SECRET", "").strip(),
        enabled=env.get(f"MAIYEBANG_{name.upper()}_LOGIN_ENABLED") == "1",
    ) for name in ("google", "wechat")}


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def provider_request(url, *, method="GET", body=None, headers=None):
    """Only the three documented back-channel endpoints, verified TLS, no retry.

    WeChat's documented token exchange puts the secret in its query. Neither
    request URLs nor upstream bodies/errors are logged or returned to clients.
    """
    parsed = urlsplit(url)
    endpoint = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
    if endpoint not in {GOOGLE_TOKEN, GOOGLE_CERTS, WECHAT_TOKEN} or parsed.fragment:
        raise APIError(502, "LOGIN_ENDPOINT_REJECTED")
    request = Request(url, data=body, headers=headers or {}, method=method)
    try:
        with build_opener(_NoRedirect()).open(request, timeout=10) as response:
            data = response.read(262145)
            if response.status != 200 or len(data) > 262144:
                raise APIError(502, "LOGIN_PROVIDER_UNAVAILABLE")
            return SimpleNamespace(status=200, data=data)
    except (HTTPError, URLError, OSError, ValueError):
        raise APIError(502, "LOGIN_PROVIDER_UNAVAILABLE") from None


def _json(response):
    try:
        data = json.loads(response.data)
        if response.status != 200 or not isinstance(data, dict):
            raise ValueError()
        return data
    except (ValueError, UnicodeError):
        raise APIError(502, "LOGIN_PROVIDER_RESPONSE_INVALID") from None


def exchange_identity(provider, config, *, code, redirect_uri, nonce, verifier,
                      transport=provider_request):
    """Return a provider-verified subject only; never trust browser profile data."""
    if provider == "google":
        from google.oauth2 import id_token
        payload = urlencode({"code": code, "client_id": config.client_id,
                             "client_secret": config.client_secret,
                             "redirect_uri": redirect_uri, "grant_type": "authorization_code",
                             "code_verifier": verifier}).encode()
        result = _json(transport(GOOGLE_TOKEN, method="POST", body=payload,
                                headers={"Content-Type": "application/x-www-form-urlencoded"}))
        token = result.get("id_token")
        if not isinstance(token, str) or not 1 <= len(token) <= 32768:
            raise APIError(401, "LOGIN_IDENTITY_REJECTED")
        def certificate_request(url, method="GET", **kwargs):
            if url != GOOGLE_CERTS or method != "GET":
                raise APIError(502, "LOGIN_ENDPOINT_REJECTED")
            return transport(url, method="GET")
        try:
            claims = id_token.verify_oauth2_token(token, certificate_request, config.client_id)
            if (not isinstance(claims.get("nonce"), str)
                    or not hmac.compare_digest(claims["nonce"], nonce)
                    or claims.get("azp", config.client_id) != config.client_id):
                raise ValueError()
            subject = claims["sub"]
        except APIError:
            raise
        except Exception:
            raise APIError(401, "LOGIN_IDENTITY_REJECTED") from None
    elif provider == "wechat":
        result = _json(transport(WECHAT_TOKEN + "?" + urlencode({
            "appid": config.client_id, "secret": config.client_secret,
            "code": code, "grant_type": "authorization_code"})))
        if (result.get("errcode") or not isinstance(result.get("access_token"), str)
                or not result["access_token"] or result.get("scope") != "snsapi_login"):
            raise APIError(401, "LOGIN_IDENTITY_REJECTED")
        # Code was exchanged server-to-server for this app. Do not accept an
        # openid/unionid supplied by the browser; do not auto-link by email/name.
        subject = result.get("openid")
    else:
        raise APIError(404, "LOGIN_PROVIDER_UNKNOWN")
    if not isinstance(subject, str) or not 1 <= len(subject) <= 255 or any(ord(c) < 33 for c in subject):
        raise APIError(401, "LOGIN_IDENTITY_REJECTED")
    return subject


class SocialAuth:
    def __init__(self, store, *, origin, configs=None, exchange=exchange_identity):
        parsed = urlsplit(origin)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                or parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment
                or (parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1"})):
            raise ValueError("Invalid trusted login origin")
        self.store, self.origin = store, origin
        self.configs = load_configs() if configs is None else dict(configs)
        self.exchange = exchange
        with store.connection() as conn:
            conn.executescript(SCHEMA)

    def status(self):
        items = []
        for name in ("google", "wechat"):
            config = self.configs.get(name, ProviderConfig())
            reason = None
            if not config.enabled or not config.client_id or not config.client_secret:
                reason = "configuration_required"
            elif name == "wechat" and urlsplit(self.origin).scheme != "https":
                reason = "public_https_callback_required"
            elif name == "google":
                try:
                    if importlib.util.find_spec("google.oauth2.id_token") is None:
                        reason = "dependency_required"
                except ModuleNotFoundError:
                    reason = "dependency_required"
            items.append({"provider": name, "available": reason is None,
                          "reason": reason, "start_path": f"/api/v1/auth/{name}/start"})
        return items

    def _config(self, provider):
        status = next((s for s in self.status() if s["provider"] == provider), None)
        if status is None:
            raise APIError(404, "LOGIN_PROVIDER_UNKNOWN")
        if not status["available"]:
            raise APIError(503, "LOGIN_NOT_CONFIGURED", "此登入方式尚未開通。")
        return self.configs[provider]

    def start(self, provider):
        config = self._config(provider)
        state, browser, nonce, verifier = (secrets.token_urlsafe(32) for _ in range(4))
        redirect = self.origin + f"/api/v1/auth/{provider}/callback"
        with self.store.transaction() as conn:
            conn.execute("DELETE FROM app_login_attempts WHERE expires_at<=?", (self.store.now(),))
            # Bound pending state storage even under unauthenticated requests.
            if conn.execute("SELECT count(*) FROM app_login_attempts").fetchone()[0] >= 200:
                raise APIError(429, "LOGIN_BUSY")
            conn.execute("INSERT INTO app_login_attempts VALUES(?,?,?,?,?,?,?,?)",
                         (hashed(state), hashed(browser), provider, config.client_id,
                          nonce, verifier, redirect, self.store.now() + 300))
        if provider == "google":
            challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
            url = GOOGLE_AUTH + "?" + urlencode({"client_id": config.client_id,
                "redirect_uri": redirect, "response_type": "code", "scope": "openid",
                "state": state, "nonce": nonce, "code_challenge": challenge,
                "code_challenge_method": "S256"})
        else:
            url = WECHAT_AUTH + "?" + urlencode({"appid": config.client_id,
                "redirect_uri": redirect, "response_type": "code", "scope": "snsapi_login",
                "state": state}) + "#wechat_redirect"
        return {"authorization_url": url}, browser

    def finish(self, provider, *, params, browser):
        config = self._config(provider)
        state = params.get("state", "")
        if (not isinstance(state, str) or not TOKEN_PATTERN.fullmatch(state)
                or not isinstance(browser, str) or not TOKEN_PATTERN.fullmatch(browser)):
            raise APIError(400, "LOGIN_STATE_REJECTED")
        with self.store.transaction() as conn:
            row = conn.execute("SELECT * FROM app_login_attempts WHERE state_hash=?", (hashed(state),)).fetchone()
            if (row is None or row["expires_at"] <= self.store.now()
                    or row["provider"] != provider or row["client_id"] != config.client_id
                    or not hmac.compare_digest(row["browser_hash"], hashed(browser))):
                raise APIError(400, "LOGIN_STATE_REJECTED")
            # Consume before exchange. Errors/timeouts require a new login; never
            # replay an authorization code. Browser-binding prevents login CSRF.
            conn.execute("DELETE FROM app_login_attempts WHERE state_hash=?", (hashed(state),))
        if "error" in params:
            raise APIError(401, "LOGIN_CANCELLED")
        code = params.get("code")
        if not isinstance(code, str) or not 1 <= len(code) <= 2048 or any(ord(c) < 33 for c in code):
            raise APIError(400, "LOGIN_CODE_REJECTED")
        subject = self.exchange(provider, config, code=code, redirect_uri=row["redirect_uri"],
                                nonce=row["nonce"], verifier=row["verifier"])
        # Only this verified subject is permitted to create a buyer identity.
        # The provider cannot issue an operator/merchant role or inherit another
        # buyer's history. Email, nickname and unionid are not account-link keys.
        with self.store.transaction() as conn:
            identity = conn.execute("SELECT code_hash FROM app_social_identities WHERE provider=? AND client_id=? AND subject=?",
                                    (provider, config.client_id, subject)).fetchone()
            if identity is None:
                binding = "social:" + secrets.token_hex(32)
                conn.execute("INSERT INTO app_access_codes(code_hash,tenant_id,actor_id,role,merchant_id) VALUES(?,?,?,'buyer',NULL)",
                             (binding, "local-hk", "buyer-" + secrets.token_hex(16)))
                conn.execute("INSERT INTO app_social_identities VALUES(?,?,?,?)", (provider, config.client_id, subject, binding))
            else:
                binding = identity["code_hash"]
            actor = conn.execute("SELECT * FROM app_access_codes WHERE code_hash=? AND active=1", (binding,)).fetchone()
            if actor is None:
                raise APIError(403, "LOGIN_ACCOUNT_DISABLED")
            token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(24)
            conn.execute("INSERT INTO app_sessions VALUES(?,?,?,?)", (hashed(token), binding, csrf, self.store.now() + 8 * 3600))
        return token, {"actor": Auth._actor(actor), "csrf_token": csrf}
