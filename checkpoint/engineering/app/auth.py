"""Local development identity: server-provisioned bearer codes -> durable sessions.

This is deliberately not production identity federation. Roles and merchant scopes
are provisioned only by the local CLI, never accepted from browser request data.
"""
from __future__ import annotations
import hashlib
import hmac
import secrets


class APIError(Exception):
    def __init__(self, status, code, message=None):
        self.status, self.code, self.message = status, code, message or code
        super().__init__(self.message)


def hashed(value):
    return hashlib.sha256(value.encode()).hexdigest()


class Auth:
    def __init__(self, store):
        self.store = store

    def provision(self, *, actor_id, tenant_id="local-hk", role="buyer", merchant_id=None):
        if role not in {"buyer", "merchant", "operator"} or (role == "merchant" and not merchant_id):
            raise ValueError("Invalid server role binding")
        code = secrets.token_urlsafe(24)
        with self.store.transaction() as conn:
            conn.execute("INSERT INTO app_access_codes(code_hash,tenant_id,actor_id,role,merchant_id) VALUES(?,?,?,?,?)",
                         (hashed(code), tenant_id, actor_id, role, merchant_id))
        return code

    def login(self, code):
        if not isinstance(code, str) or not 16 <= len(code) <= 200:
            raise APIError(401, "INVALID_ACCESS_CODE")
        with self.store.transaction() as conn:
            row = conn.execute("SELECT * FROM app_access_codes WHERE code_hash=? AND active=1", (hashed(code),)).fetchone()
            if row is None:
                raise APIError(401, "INVALID_ACCESS_CODE")
            token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(24)
            conn.execute("INSERT INTO app_sessions VALUES(?,?,?,?)",
                         (hashed(token), row["code_hash"], csrf, self.store.now() + 8 * 3600))
            return token, {"actor": self._actor(row), "csrf_token": csrf}

    @staticmethod
    def _actor(row):
        actor = {key: row[key] for key in ("tenant_id", "actor_id", "role")}
        if row["merchant_id"]:
            actor["merchant_id"] = row["merchant_id"]
        return actor

    def session(self, token, *, csrf=None, mutate=False):
        if not isinstance(token, str) or not token:
            raise APIError(401, "SESSION_REQUIRED")
        with self.store.connection() as conn:
            row = conn.execute("""SELECT c.*,s.csrf,s.expires_at FROM app_sessions s
             JOIN app_access_codes c ON c.code_hash=s.code_hash
             WHERE s.session_hash=? AND c.active=1""", (hashed(token),)).fetchone()
        if row is None or row["expires_at"] <= self.store.now():
            raise APIError(401, "SESSION_EXPIRED")
        if mutate and (not isinstance(csrf, str) or not hmac.compare_digest(csrf, row["csrf"])):
            raise APIError(403, "CSRF_REJECTED")
        return {"actor": self._actor(row), "csrf_token": row["csrf"]}

    def logout(self, token):
        with self.store.transaction() as conn:
            conn.execute("DELETE FROM app_sessions WHERE session_hash=?", (hashed(token),))
