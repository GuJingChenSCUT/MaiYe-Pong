# HacKU engineering collaboration rules

This package specifies work; it is not a deployed application or a tested payment integration.

1. Read the engineering handoff and contracts before implementation. Keep the first slice to one selected merchant and one order after comparing two eligible quotes.
2. Contract owner approves API, event and state-machine changes before dependent work. Other agents propose changes rather than silently fork schemas.
3. Product agents cannot directly query databases, hold payment credentials, approve mandates, adjudicate disputes or publish policies. Enforce this in server code, not prompts.
4. Derive actor and tenant context from authenticated server sessions. Apply resource and field authorization to APIs, files, SSE, search, logs and model inputs.
5. Money uses integer minor units and explicit currency. Cash limits include required fees and never subtract unposted rewards.
6. Use durable workflow state, atomic reservations, idempotency, signed provider callbacks and reconciliation. Never retry an unknown payment under a new key.
7. Revocation controls future dispatch. A request already claimed/sent may complete: show the actual state and use provider-supported cancel/refund flows.
8. Preserve buyer and seller statements independently. Human-authorized financial actions and contested outcomes remain outside model autonomy.
9. Sources and resolved cases enter quarantine, review, evaluation and versioned publication. No autonomous model training or live policy changes.
10. Every external dependency states one of: designed, mocked, sandbox_verified, production_verified. Do not present mocks as real partners or money transfers.
11. Use directory ownership. Reviewers should inspect sensitive financial and authorization changes independently of their authors.
12. Do not contact users, merchants or providers, publish changes, create accounts, or spend real funds solely because this file suggests a workflow; follow the active user's authorization.
13. Deliver changed files, contract deviations, verification evidence and remaining risks. Log concise decision reasons and evidence, not private model reasoning.
14. Do not reuse pre-competition application code in violation of competition rules. Credit dependencies and explain architecture.
