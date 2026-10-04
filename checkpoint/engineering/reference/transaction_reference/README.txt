HacKU transaction reference — local executable proof, version 0.3
================================================================

STATUS
This directory is a bounded, runnable reference using Python standard-library SQLite.
It is NOT a bank/PSP sandbox, a real payment integration, a web identity provider,
a complete Agent application, or a production financial ledger. No external API is
called. All merchants, balances, prices and payment facts in tests are synthetic.
There is no claimed real fee, cashback rate, earning rate or point redemption value.
reward_units=7 is an arbitrary synthetic_fixture ledger quantity, NOT a percentage.
The additional amount in the cap test is synthetic, not a sourced merchant fee.

RUN
From this directory:
    python -m unittest discover -s tests -v
    python verify.py
No pip packages, API keys, account creation or external network are needed.
Tests create disposable databases in a temporary directory and remove them afterward.
Current verification: 33 local tests pass; see verification.json and test_output.txt.

FILES / BOUNDARIES
kernel.py           Business state and transactions, trusted internal server ports.
local_psp.py        Separate durable simulator; synthetic callback signing and lookup.
runtime_adapter.py  Minimal DurableWorkflow port for request_execution(plan_id).
tests/              Local verification, including separate-connection concurrency and
                    a fresh Python process for restart/reconciliation.
verify.py           Reproducible test runner, writes verification.json + test_output.txt.

SERVER-ISSUED IDENTITY
TestPrincipal is a trusted fixture, explicitly NOT a bearer-token verifier.
The real server must verify issuer/audience/signature/expiry, authenticated session,
tenant, role and delegated resources; then construct its internal principal.
Test identity scopes are an enforcement illustration, not a production identity stack.
Principal expiry is runtime-checked as a bounded integer epoch; adapter context expiry
may be a finite integer or float epoch. NaN, infinity, booleans and strings are rejected
before comparisons, so invalid expiry cannot turn into an accidentally unlimited grant.
The model has only request_execution with plan_id; it cannot set the amount, payee,
currency, approval kind, identity or command key. Human approval is a separate port.
The actual approval HTTP endpoint still needs a single-use challenge bound to the
human session, immutable plan, limits and expiry, plus suitable fresh authentication.
The reference binds immutable plan digest/version/merchant/currency/amount cap and
expiry, but does not implement that HTTP/session challenge or credential verification.

STATE / BUDGET
An immutable plan stores an integer-minor-unit total including additional amounts.
Human approval binds it. Atomic BEGIN IMMEDIATE validates grant and ownership,
reserves cash, creates one logical operation and appends audit + outbox entries.
Any failure rolls back all four. A UNIQUE plan_id prevents a second payment command
for the same approved plan; a UNIQUE command_key detects a changed command payload.
Keys are durable logical workflow IDs, never model tool_call_id values.
QUEUED -> DISPATCH_COMMITTED -> SUCCEEDED | FAILED | UNKNOWN.
QUEUED may become STOPPED. UNKNOWN retains the entire reservation.
Success moves reserved -> spent; a terminal decline releases reserved cash.
This reference uses a single net-cash budget bucket. Full confirmed refunds restore
that bucket. It does not implement daily/rolling-period limits or gross-spend rules;
their refund treatment must be independently specified before production use.

REVOCATION CUT-OFF
The serialized dispatch claim is the explicit cut-off. Stop before that claim blocks
submission and releases reservation. A worker that already won its claim may send its
one in-flight operation even if stop follows; the UI must show that truth.
This does NOT guarantee that every revoke before the network packet leaves wins.
Stale UI view versions do not defeat revocation, but identity and ownership still apply.
Stopping a model run is not revocation; a direct trusted stop endpoint is required.

RECOVERY / REPLAY
After claim, a timeout or crash can hide success. query(operation_id) uses the original
ID in the separately persisted simulator. A new process can settle the existing
operation without causing another debit. Reconciliation works after mandate revocation.
An absent provider record stays UNKNOWN and reserved here: no automatic release or
new payment, because a real provider could be temporarily inconsistent.
A production recovery policy may clear this only with provider-specific guarantees
or a reviewed terminal resolution. There is no generalized exactly-once claim.
Callback IDs are deduplicated; conflicting same-ID payloads or contradictory payment
facts create alerts and cannot change settled money. A new callback ID with the same
settlement fact also cannot double-spend. The test HMAC is synthetic, not a provider's
real callback scheme; replace with the provider's verified protocol and replay rules.

REFUNDS / REWARDS
Full refund only, single merchant and single order. A trusted pre-approved settlement
port queues a refund against the original provider reference; its amount is fixed.
The simulator rejects partial amounts, another payment reference and duplicate refund
keys for the same original payment. Confirmed reconciliation marks the full refund
and reverses the fixture reward state atomically. No new payout beneficiary exists.
The human scope approved_settlement_refund represents an ALREADY authorized settlement;
this reference does not determine refund entitlement, merchant acceptance or liability.
finish_refund is an internal verified-fact port; it is not a public unverified webhook.
The retained reward_units record is historical; only reward_state='posted' counts as
active. No money conversion is implemented.

CONNECTION TO runtime_reference/agent_runtime.py
1. The composition root verifies a real session and workflow task (not supplied here).
2. Its principal_resolver maps TrustedContext to scoped internal identity.
3. ExecutionWorkflowAdapter is supplied as DurableWorkflow. It implements ONLY
   request_execution; other tools explicitly fail rather than silently succeed.
4. A real Authorizer still checks scope, task, versions and output projection. The
   kernel repeats sensitive checks atomically; prompts/skills cannot bypass it.
5. The runtime sees a contract-shaped queued/already_exists receipt. A payment worker
   later claims and processes the durable operation. No model holds PSP credentials.
6. The production error adapter maps Rejected codes into the established tool result
   envelope. Uncertain write outcomes must still be queried by operation key.

TRUSTED ADAPTER SHAPE (illustrative; no permissive production resolver provided)
    workflow = ExecutionWorkflowAdapter(kernel, principal_resolver)
    # Existing runtime calls:
    workflow.enqueue_once(context=trusted_context,
        tool_name='request_execution', arguments={'plan_id': saved_plan_id},
        operation_key=trusted_context.operation_key)

WHAT REMAINS
Real DeepSeek API and end-to-end Agent wiring; login/token verification; challenge UI;
three-role HTTP/object/SSE authorization; actual merchants and sourced fee/reward rules;
licensed PSP sandbox and verified callbacks; merchant acceptance and refund approval;
PostgreSQL schema/locking tests; outbox delivery, consumer dedupe, retry leases and
queue monitoring; backups, privacy controls, production observability and recovery.
SQLite tests demonstrate LOCAL semantics, not PostgreSQL isolation or distributed
deployment behavior. The outbox is atomically persisted but not delivered by a broker.

No facts, model performance, commercial API availability or real payment claims are
inferred from these offline tests.
