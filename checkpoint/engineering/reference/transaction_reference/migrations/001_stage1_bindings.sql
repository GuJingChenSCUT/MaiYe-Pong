-- Stop 1 V1 is isolated from the legacy v0.4 fixture tables. No legacy row is
-- upgraded into a real identity, consent, quote or provider authorization.
CREATE TABLE IF NOT EXISTS s1_current_bindings (
 task_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, owner_id TEXT NOT NULL,
 constraints_version INTEGER NOT NULL CHECK(constraints_version > 0),
 fact_version INTEGER, quote_id TEXT, quote_digest TEXT,
 payee_ref TEXT, payee_mapping_version INTEGER,
 stopped INTEGER NOT NULL DEFAULT 0 CHECK(stopped IN (0,1)),
 snapshot_id TEXT, operation_id TEXT,
 cap_minor INTEGER NOT NULL DEFAULT 0, reserved_minor INTEGER NOT NULL DEFAULT 0,
 spent_minor INTEGER NOT NULL DEFAULT 0,
 CHECK(cap_minor >= 0 AND reserved_minor >= 0 AND spent_minor >= 0
       AND reserved_minor + spent_minor <= cap_minor));
CREATE TABLE IF NOT EXISTS s1_snapshots (
 snapshot_id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES s1_current_bindings(task_id),
 tenant_id TEXT NOT NULL, owner_id TEXT NOT NULL,
 constraints_version INTEGER NOT NULL, fact_version INTEGER NOT NULL,
 quote_id TEXT NOT NULL, quote_version INTEGER NOT NULL, quote_digest TEXT NOT NULL,
 merchant_id TEXT NOT NULL, merchant_sku TEXT NOT NULL,
 purchase_quantity INTEGER NOT NULL CHECK(purchase_quantity > 0),
 unit_price_minor INTEGER NOT NULL CHECK(unit_price_minor >= 0),
 goods_minor INTEGER NOT NULL, fees_minor INTEGER NOT NULL, discount_minor INTEGER NOT NULL,
 cash_minor INTEGER NOT NULL, currency TEXT NOT NULL CHECK(currency = 'HKD'),
 cash_cap_minor INTEGER NOT NULL, payee_ref TEXT NOT NULL, payee_mapping_version INTEGER NOT NULL,
 digest TEXT NOT NULL, payload TEXT NOT NULL, expires_at INTEGER NOT NULL, created_at INTEGER NOT NULL,
 CHECK(goods_minor = purchase_quantity * unit_price_minor),
 CHECK(goods_minor >= 0 AND fees_minor >= 0 AND discount_minor >= 0),
 CHECK(cash_minor = goods_minor + fees_minor - discount_minor AND cash_minor > 0
       AND cash_minor <= cash_cap_minor));
CREATE TRIGGER IF NOT EXISTS s1_immutable_snapshot_update BEFORE UPDATE ON s1_snapshots
 BEGIN SELECT RAISE(ABORT, 'immutable_snapshot'); END;
CREATE TRIGGER IF NOT EXISTS s1_immutable_snapshot_delete BEFORE DELETE ON s1_snapshots
 BEGIN SELECT RAISE(ABORT, 'immutable_snapshot'); END;
CREATE TABLE IF NOT EXISTS s1_challenges (
 challenge_id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES s1_current_bindings(task_id),
 snapshot_id TEXT NOT NULL REFERENCES s1_snapshots(snapshot_id),
 tenant_id TEXT NOT NULL, owner_id TEXT NOT NULL, snapshot_digest TEXT NOT NULL,
 expires_at INTEGER NOT NULL, state TEXT NOT NULL CHECK(state IN ('PENDING','CONSUMED','REVOKED')));
CREATE UNIQUE INDEX IF NOT EXISTS s1_one_pending_challenge ON s1_challenges(snapshot_id)
 WHERE state = 'PENDING';
CREATE TABLE IF NOT EXISTS s1_mandates (
 mandate_id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES s1_current_bindings(task_id),
 snapshot_id TEXT UNIQUE NOT NULL REFERENCES s1_snapshots(snapshot_id),
 challenge_id TEXT UNIQUE NOT NULL REFERENCES s1_challenges(challenge_id),
 tenant_id TEXT NOT NULL, owner_id TEXT NOT NULL, snapshot_digest TEXT NOT NULL,
 expires_at INTEGER NOT NULL, version INTEGER NOT NULL DEFAULT 1,
 state TEXT NOT NULL CHECK(state IN ('ACTIVE','REVOKED')));
CREATE TABLE IF NOT EXISTS s1_operations (
 operation_id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES s1_current_bindings(task_id),
 snapshot_id TEXT UNIQUE NOT NULL REFERENCES s1_snapshots(snapshot_id),
 mandate_id TEXT UNIQUE NOT NULL REFERENCES s1_mandates(mandate_id),
 tenant_id TEXT NOT NULL, owner_id TEXT NOT NULL,
 state TEXT NOT NULL CHECK(state IN ('QUEUED','DISPATCH_COMMITTED','UNKNOWN','SUCCEEDED','FAILED','STOPPED')),
 version INTEGER NOT NULL DEFAULT 1, created_at INTEGER NOT NULL, claimed_at INTEGER,
 provider_ref TEXT, provider_event_id TEXT UNIQUE, provider_event_digest TEXT,
 order_status TEXT NOT NULL DEFAULT 'NOT_CREATED', stop_reason TEXT);
CREATE UNIQUE INDEX IF NOT EXISTS s1_one_live_operation ON s1_operations(task_id)
 WHERE state != 'STOPPED';
CREATE UNIQUE INDEX IF NOT EXISTS s1_one_original_payment ON s1_operations(provider_ref)
 WHERE provider_ref IS NOT NULL;
CREATE TABLE IF NOT EXISTS s1_dispatch_commands (
 operation_id TEXT PRIMARY KEY REFERENCES s1_operations(operation_id),
 provider_idempotency_key TEXT UNIQUE NOT NULL, payload TEXT NOT NULL, payload_digest TEXT NOT NULL,
 state TEXT NOT NULL CHECK(state IN ('PENDING','CLAIMED','COMPLETED','CANCELLED')),
 claimed_at INTEGER);
CREATE TABLE IF NOT EXISTS s1_event_audit (
 seq INTEGER PRIMARY KEY AUTOINCREMENT, at INTEGER NOT NULL,
 task_id TEXT NOT NULL REFERENCES s1_current_bindings(task_id),
 actor_id TEXT NOT NULL, event TEXT NOT NULL, object_id TEXT NOT NULL, details TEXT NOT NULL);
