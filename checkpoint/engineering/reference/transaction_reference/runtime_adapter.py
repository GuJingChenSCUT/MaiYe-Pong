"""Minimal DurableWorkflow port for runtime_reference.agent_runtime.

Only request_execution is implemented. Composition root issues TestPrincipal from
trusted test session/workflow state. No permissive production Authorizer supplied.
"""
import math
from kernel import Rejected


class ExecutionWorkflowAdapter:
    def __init__(self, kernel, principal_resolver):
        self.kernel = kernel
        self.principal_resolver = principal_resolver

    def enqueue_once(self, *, context, tool_name, arguments, operation_key):
        if tool_name != "request_execution" or set(arguments) != {"plan_id"}:
            raise Rejected("UNIMPLEMENTED_OR_INVALID_TOOL")
        if operation_key != context.operation_key:
            raise Rejected("UNTRUSTED_OPERATION_KEY")
        # TrustedContext permits a numeric epoch, but never NaN/inf, booleans or
        # coercible strings. Range-check before isfinite to avoid huge-int overflow.
        expiry = context.expires_at_epoch
        if (type(expiry) not in (int, float) or not 0 < expiry <= 10**12
                or not math.isfinite(expiry)):
            raise Rejected("TRUSTED_CONTEXT_EXPIRY_INVALID")
        principal = self.principal_resolver(context)
        if (principal.actor_id != context.actor_id or principal.tenant_id != context.tenant_id
                or principal.role != context.role or expiry <= self.kernel.clock()):
            raise Rejected("TRUSTED_CONTEXT_MISMATCH")
        return self.kernel.request_execution(
            principal, arguments["plan_id"], command_key=operation_key,
            authorization_version=context.authorization_version)
