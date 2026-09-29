# scripts/test_deploy_guard.py
import json
import types
import unittest
from unittest import mock

import deploy_guard
from deploy_guard import decide, deployment_request, freezes_to_close, pick_live, pick_previous

A, B, C = "a" * 40, "b" * 40, "c" * 40


def ctx(**kw):
    c = {"sha": B, "reason": "deploy", "env": "production", "on_main": True, "ci": "success",
         "live_sha": A, "newer_than_live": True, "ledger_success_shas": [A], "freezes": [],
         "pr": 12, "pr_labels": [], "now": "2026-10-01T00:00:00Z", "has_drill_alias": True}
    c.update(kw)
    return c


class Decide(unittest.TestCase):
    def test_happy_deploy_goes_with_live_as_rollback_target(self):
        d = decide(ctx())
        self.assertTrue(d["go"])
        self.assertEqual((d["prev_sha"], d["from_sha"]), (A, A))

    def test_deploy_requires_green_ci(self):
        for state in ("failure", "missing", "in_progress"):
            with self.subTest(state=state):
                d = decide(ctx(ci=state))
                self.assertFalse(d["go"])
                self.assertIn(f"ci is {state}", d["message"])

    def test_unmerged_sha_refused(self):
        self.assertFalse(decide(ctx(on_main=False))["go"])
        self.assertFalse(decide(ctx(on_main=False, reason="rollback"))["go"])

    def test_already_live_is_skipped_for_deploy(self):
        self.assertIn("already live", decide(ctx(sha=A))["message"])

    def test_out_of_order_older_sha_is_skipped(self):
        d = decide(ctx(newer_than_live=False))
        self.assertFalse(d["go"])
        self.assertIn("out of order", d["message"])

    def test_freeze_blocks_deploy_unless_hotfix(self):
        self.assertIn("#42", decide(ctx(freezes=[42]))["message"])
        self.assertFalse(decide(ctx(freezes=[42]))["go"])
        self.assertTrue(decide(ctx(freezes=[42], pr_labels=["hotfix"]))["go"])

    def test_first_deploy_has_no_rollback_target(self):
        d = decide(ctx(live_sha="", ledger_success_shas=[]))
        self.assertTrue(d["go"])
        self.assertEqual(d["prev_sha"], "")
        self.assertIn("first v4 deploy", d["message"])

    def test_rollback_to_pre_ci_commit_allowed_when_on_ledger(self):
        self.assertTrue(decide(ctx(reason="rollback", sha=C, ci="missing", ledger_success_shas=[A, C]))["go"])

    def test_rollback_refused_without_ci_or_ledger(self):
        self.assertFalse(decide(ctx(reason="rollback", sha=C, ci="missing"))["go"])

    def test_rollback_to_live_is_a_noop_redeploy(self):
        d = decide(ctx(reason="rollback", sha=A))
        self.assertTrue(d["go"])
        self.assertIn("no-op", d["message"])

    def test_rollback_ignores_freeze_and_age(self):
        self.assertTrue(decide(ctx(reason="rollback", freezes=[42], newer_than_live=False))["go"])

    def test_force_smoke_fail_refused_on_production(self):
        self.assertFalse(decide(ctx(force_smoke_fail=True))["go"])
        self.assertTrue(decide(ctx(force_smoke_fail=True, env="drill"))["go"])

    def test_drill_needs_a_preview_alias(self):
        self.assertFalse(decide(ctx(env="drill", has_drill_alias=False))["go"])


class Ledger(unittest.TestCase):
    def dep(self, sha, state, payload=None):
        return {"sha": sha, "state": state, "created_at": "", "payload": payload or {}}

    def test_pick_live_ignores_failures(self):
        self.assertEqual(pick_live([self.dep(B, "failure"), self.dep(A, "success")]), A)

    def test_pick_previous_skips_live_and_rolled_back_shas(self):
        deps = [self.dep(A, "success", {"rolled_back_from": B}), self.dep(B, "success"), self.dep(C, "success")]
        self.assertEqual((pick_live(deps), pick_previous(deps)), (A, C))

    def test_deployment_request_never_auto_merges_or_waits_on_contexts(self):
        r = deployment_request(A, "production-worker", "deploy", "https://run")
        self.assertIs(r["auto_merge"], False)
        self.assertEqual(r["required_contexts"], [])
        self.assertTrue(r["production_environment"])
        self.assertTrue(deployment_request(A, "drill", "deploy", "")["transient_environment"])

    def test_freezes_to_close_ignores_freezes_newer_than_the_decision(self):
        fz = [{"number": 1, "created_at": "2026-10-01T00:00:00Z"}, {"number": 2, "created_at": "2026-10-01T00:05:00Z"}]
        self.assertEqual(freezes_to_close(fz, "2026-10-01T00:01:00Z"), [1])

    def test_freeze_opened_after_the_decision_survives(self):
        fz = [{"number": 1, "created_at": "2026-10-01T00:00:00Z"}, {"number": 2, "created_at": "2026-10-01T00:00:30Z"}]
        self.assertEqual(freezes_to_close(fz, "2026-10-01T00:00:10Z"), [1])

    def test_freeze_created_in_the_same_second_as_the_decision_survives(self):
        fz = [{"number": 1, "created_at": "2026-10-01T00:01:00Z"}]
        self.assertEqual(freezes_to_close(fz, "2026-10-01T00:01:00Z"), [])


class FreezeOpen(unittest.TestCase):
    def test_creates_a_new_issue_even_when_a_freeze_is_already_open(self):
        calls = []

        def fake_gh(*args, stdin=None):
            calls.append(args)
            if args[0] == "api":
                return json.dumps([{"number": 7, "created_at": "2026-10-01T00:00:00Z", "title": "deploy-freeze [production]: x"}])
            if args[:2] == ("issue", "create"):
                return "https://github.com/BJGLLC/r/issues/8"
            return ""

        a = types.SimpleNamespace(repo="BJGLLC/r", env="production", frm=A, to=B, run_url="https://run")
        with mock.patch.object(deploy_guard, "gh", fake_gh):
            self.assertEqual(deploy_guard.freeze_open(a), 8)
        self.assertTrue(any(c[:2] == ("issue", "create") for c in calls))
        self.assertFalse(any(c[:2] == ("issue", "comment") for c in calls))


if __name__ == "__main__":
    unittest.main()
