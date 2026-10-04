HacKU application capabilities: reference loader and rule selector
Version 0.3.0 — reference code, offline checks only

Purpose
-------
The new capability procedures are application artifacts. They are not native
DeepSeek skills uploaded to the model, and not installed ChatGPT/Codex skills.
This directory turns a server-selected procedure into a controlled prompt and
an exact subset of the existing tool registry. It does not provide identity,
durable workflow, payment, signed release distribution, or a deployed app.

Files
-----
loader.py: progressive metadata discovery and one-procedure preparation.
rules.py: deterministic reviewed-rule selection; no financial arithmetic.
stage_tool_policy.reference.json: proposed server-controlled tool ceilings.
tests/: synthetic standard-library tests; no paid inference or network access.
verify_offline.py: tests plus real draft pack loading, with explicit offline flag.

Trust and activation
--------------------
1. Construct TrustedCapabilityContext from authenticated service identity,
   task grant and authoritative current workflow state. A Python dataclass is
   not authentication. Never accept its fields from a model/user JSON payload.
2. Pin the pack version and manifest SHA-256 from a trusted deployment registry.
   A local digest proves byte consistency only; it is not a digital signature,
   reviewer approval, or protection against an attacker replacing both files and
   expected pins. Real release signing and revocation are implementation gates.
3. Current capabilities are draft. The loader refuses them by default. Explicit
   offline_test_mode exists for local tests; never enable it in production.
4. Only local pack-relative procedure paths are read. Absolute paths, traversal,
   remote URLs and symlink escapes are rejected. Mount approved release files
   immutable in production; filesystem mutation defenses are not authentication.
5. Context expiry, pack/procedure hashes, matching index metadata, role, workflow
   node, objective, phase and explicit enablement are checked before loading.
6. Effective tools = role registry policy ∩ task grant ∩ workflow-stage ceiling
   ∩ selected procedure's tool list ∩ deployed handler allowlist. Instructions,
   retrieved documents or user messages cannot enlarge that intersection.
7. metadata() does not load all instructions. prepare() loads exactly one
   procedure. The current method name is available_metadata(context).
8. task_projection must already be resource/field filtered. Only declared
   minimum_context keys are forwarded, and the combined prompt+data byte budget
   is checked. No silent truncation of evidence or authority context occurs.
9. The prompt is behavioral guidance, not a security barrier. The existing
   runtime and live business services must repeat authorization, freshness,
   projection and transaction-state checks on every tool call.

Composition with the existing DeepSeek reference runtime
--------------------------------------------------------
The deployment composition root supplies all objects below. This is illustrative
wiring, not a complete executable production composition or an auth bypass:

    prepared = capability_loader.prepare(server_capability_context,
                                         selected_capability_id,
                                         authorized_task_projection)
    registry = ToolRegistry(list(prepared.filtered_registry))
    runtime = ToolRuntime(registry, configured_transport, real_authorizer,
                          scoped_bindings, workflow=durable_workflow)
    result = runtime.run(system_prompt=prepared.system_prompt,
                         user_text=prepared.task_message,
                         context=server_runtime_context)

Construct server_capability_context and server_runtime_context from the same
identity/task snapshot. Their actor/task/role/expiry must agree; a future server
composition must enforce that binding. The loader is a library, not that server.
Each role starts a separate message history with only its authorized projection.
The same model endpoint and provider adapter may be shared across isolated jobs.
No second model API implementation exists in this directory.

Financial writes remain request-only tools accepted into the durable workflow.
The procedure authorized_execute does not take over the dispatch state owner.
At human_approval the stage policy exposes status only, not request_execution;
the service moves to dispatch only after validated human authorization.
Stop is a separate interrupt capability, and a direct authenticated UI stop API
must continue working even when the model provider is unavailable.

Four independent information classes
------------------------------------
Published source evidence: a cited finding/complaint/term. It can inform an
explanation but is not automatically an executable rate or refund entitlement.
Executable rule: reviewed, versioned, scoped parameters and eligibility. The
resolver accepts only published records in a server-approved release.
Authorization: current user's explicit, bounded, revocable authority. It is
checked by the transaction service, never inferred from a rule or a preference.
Preference: user's ranking priorities. It can affect ranking among feasible
choices but cannot lift budget/permissions or manufacture merchant obligations.

DeterministicRuleResolver is a reference SELECTOR, not a tariff calculator.
It validates record class, publication, release, synthetic flag, jurisdiction,
currency, time window, evidence freshness and exact typed eligibility conditions.
Unknown eligibility, stale candidates, conflicting candidates or missing rules
yield needs_clarification; no rate is invented. An applicable result explicitly
grants_transaction_authority=False. Parameters still require the specific
downstream rule schema and business calculation implementation before any use.
The repository should resolve superseded/conflicting versions before selection.
Eligibility facts must come from the verified server-side profile/transaction
projection. A user or model assertion of enrollment is not verified eligibility;
unknown verification should be represented as missing/null, never assumed true.
The 18 draft case seeds are never imported or activated here.
All test rules are synthetic, carry no real rate and are forbidden in live mode.

Run from the unpacked agent_handoff directory
-------------------------------------------
    python3 capability_runtime/verify_offline.py

No third-party dependency is required for these new reference tests. Existing
runtime_reference code separately depends on jsonschema. This check covers
local loader/rule behavior only, not DeepSeek accuracy, real authorization,
PostgreSQL races, payment sandbox access or complete application acceptance.
