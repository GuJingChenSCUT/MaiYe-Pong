"""Explicit, loopback-only test buyers. These public demo codes are not secrets.

Provisioning never enables login by itself. A serving Auth instance must also be
explicitly enabled for a loopback binding, and check its actual socket peer on
every login and session use. No merchant/operator binding is permitted here.
"""
from __future__ import annotations

import hashlib
import ipaddress
from types import MappingProxyType


LOCAL_DEMO_TENANT = "local-hk"
LOCAL_DEMO_BINDINGS = MappingProxyType({
    "0001": "local-demo-buyer-0001",
    "0002": "local-demo-buyer-0002",
})
LOCAL_DEMO_ACTORS = frozenset(LOCAL_DEMO_BINDINGS.values())
_HASH_BINDINGS = {hashlib.sha256(code.encode()).hexdigest(): actor
                  for code, actor in LOCAL_DEMO_BINDINGS.items()}


def is_loopback_address(address):
    # No DNS lookup, Host header, X-Forwarded-For, or caller-provided role.
    try:
        return isinstance(address, str) and ipaddress.ip_address(address).is_loopback
    except ValueError:
        return False


def demo_actor_for_hash(code_hash):
    return _HASH_BINDINGS.get(code_hash)


def valid_demo_binding(row):
    expected = demo_actor_for_hash(row["code_hash"])
    return (expected is not None and row["actor_id"] == expected
            and row["tenant_id"] == LOCAL_DEMO_TENANT and row["role"] == "buyer"
            and row["merchant_id"] is None and row["active"] == 1)


def provision_short_demo_accounts(store, *, bind_address):
    """Create missing dedicated buyers atomically; never repair or reactivate.

    Existing incompatible rows or reserved actors require explicit recovery.
    Only hashes enter the existing credential table; there is no second session
    or identity implementation and no private credential file for these codes.
    """
    if not is_loopback_address(bind_address):
        raise ValueError("Local demo buyers require an actual loopback binding")
    with store.transaction() as conn:
        missing = []
        for code_hash, actor_id in _HASH_BINDINGS.items():
            row = conn.execute("SELECT * FROM app_access_codes WHERE code_hash=?", (code_hash,)).fetchone()
            if row is not None:
                if not valid_demo_binding(row):
                    raise ValueError("Existing local demo buyer is inactive or mismatched; no automatic repair")
            else:
                if conn.execute("SELECT 1 FROM app_access_codes WHERE actor_id=?", (actor_id,)).fetchone():
                    raise ValueError("Reserved demo buyer already has a different credential; no automatic replacement")
                missing.append((code_hash, actor_id))
        for code_hash, actor_id in missing:
            conn.execute("INSERT INTO app_access_codes(code_hash,tenant_id,actor_id,role) VALUES(?,?,?,'buyer')",
                         (code_hash, LOCAL_DEMO_TENANT, actor_id))
    return tuple({"tenant_id": LOCAL_DEMO_TENANT, "actor_id": actor, "role": "buyer"}
                 for actor in LOCAL_DEMO_BINDINGS.values())
