import unittest
from dataclasses import replace
from integrations.browser_research import (BrowserAction as A, BrowserDecision as D,
    BrowserResearchScope, inspect_browser_step, observe_browser_outcome)
from integrations.contracts import BackendContext, CallerKind


class BrowserResearchTests(unittest.TestCase):
    def setUp(self):
        self.context = BackendContext("tenant-a", "buyer-a", CallerKind.BACKEND)
        self.scope = BrowserResearchScope("tenant-a", "buyer-a", "task-a", "session-a",
            "approval-test", "platform-review-test", ("https://shop.example",),
            frozenset({A.OPEN, A.SEARCH, A.READ}), 100, 200, False)

    def inspect(self, **kwargs):
        args = dict(task_id="task-a", session_ref="session-a", action=A.READ,
                    url="https://shop.example/item?id=123", now=150)
        args.update(kwargs)
        return inspect_browser_step(self.scope, self.context, **args)

    def test_empty_scope_denies_without_external_access(self):
        self.scope = BrowserResearchScope()
        self.assertEqual(self.inspect().decision, D.DENY)

    def test_read_is_never_transaction_permission(self):
        result = self.inspect()
        self.assertEqual(result.decision, D.READ_ONLY_STEP)
        self.assertFalse(result.transaction_authorized)
        self.assertFalse(result.payment_verified)

    def test_model_cannot_supply_authority(self):
        self.context = replace(self.context, kind=CallerKind.MODEL)
        self.assertEqual(self.inspect().decision, D.DENY)

    def test_cross_user_task_and_session_are_denied(self):
        for change in ({"user_id": "buyer-b"}, {"tenant_id": "tenant-b"}):
            original = self.context
            self.context = replace(self.context, **change)
            self.assertEqual(self.inspect().decision, D.DENY)
            self.context = original
        self.assertEqual(self.inspect(task_id="task-b").decision, D.DENY)
        self.assertEqual(self.inspect(session_ref="session-b").decision, D.DENY)

    def test_revocation_expiry_and_stop_deny_further_steps(self):
        for now in (99, 200, 201, True, 150.0):
            self.assertEqual(self.inspect(now=now).decision, D.DENY)
        self.assertEqual(self.inspect(stopped=True).decision, D.DENY)
        self.scope = replace(self.scope, revoked=True)
        self.assertEqual(self.inspect().decision, D.DENY)

    def test_origin_confusion_and_unreviewed_redirect_are_denied(self):
        for url in ("http://shop.example", "https://shop.example.evil.test", "https://evil.test/shop.example",
                    "https://shop.example@evil.test", "https://user@shop.example", "https://shop.example:8443",
                    "https://shop.example/#pay", "https://shop.example\\@evil.test", "https://%73hop.example",
                    "https://shop.example\n.evil.test", "javascript:alert(1)"):
            with self.subTest(url=url):
                self.assertEqual(self.inspect(url=url).decision, D.DENY)

    def test_invalid_allowlist_is_not_silently_partially_accepted(self):
        self.scope = replace(self.scope, exact_origins=("https://shop.example", "https://shop.example/path"))
        self.assertEqual(self.inspect().decision, D.DENY)

    def test_login_challenges_and_mutations_require_user_takeover(self):
        for action in (A.LOGIN, A.CHALLENGE, A.CART, A.ORDER, A.PAY, A.SELECT_VARIANT):
            self.assertEqual(self.inspect(action=action).decision, D.TAKEOVER)

    def test_no_cookie_extraction_or_undocumented_api_even_if_listed(self):
        self.scope = replace(self.scope, allowed_read_actions=frozenset(A))
        for action in (A.COOKIE, A.INTERNAL_API):
            self.assertEqual(self.inspect(action=action).decision, D.DENY)

    def test_missing_evidence_and_unlisted_actions_deny(self):
        self.assertEqual(self.inspect(action="pay").decision, D.DENY)
        self.scope = replace(self.scope, allowed_read_actions=frozenset({A.OPEN}))
        self.assertEqual(self.inspect().decision, D.DENY)
        self.scope = replace(self.scope, platform_review_evidence_ref="")
        self.assertEqual(self.inspect(action=A.OPEN).decision, D.DENY)

    def test_success_page_does_not_confirm_payment_or_merchant_order(self):
        result = observe_browser_outcome("success_page_visible")
        self.assertEqual(result.payment_status, "UNVERIFIED")
        self.assertEqual(result.merchant_order_status, "UNVERIFIED")


if __name__ == "__main__":
    unittest.main()
