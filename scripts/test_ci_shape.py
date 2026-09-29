# scripts/test_ci_shape.py
"""Review v4 phase 3 (SSSF-30): the single-job `ci` caller shape (decision 01M3N14TGN, Q2). Every rule
has a passing and a failing case; a validator that cannot return false is theater."""
import copy
import pathlib
import subprocess
import tempfile
import unittest

import yaml

import ci_shape as cs


def good(repo="BJGLLC/cd-home"):
    step = {"id": "check", "uses": cs.LOCAL_ACTION if repo == cs.SELF_REPO else cs.ORG_ACTION}
    if repo != cs.SELF_REPO:
        step["with"] = {"node-version": "20", "install": "npm ci"}
    return {
        "name": "ci",
        "on": {"push": {"branches": ["main"]}, "pull_request": {}, "workflow_dispatch": {}},
        "permissions": {"contents": "read"},
        "concurrency": dict(cs.CONCURRENCY),
        "jobs": {"ci": {"runs-on": "ubuntu-latest", "timeout-minutes": 20,
                        "steps": [copy.deepcopy(cs.CHECKOUT), step]}},
    }


def notifier():
    return {"needs": "ci", "if": "failure()", "uses": "./.github/workflows/ci-red-notify.yml",
            "with": {"failing_step": "${{ needs.ci.outputs.failing_step }}"}}


class Shape(unittest.TestCase):
    def v(self, wf, repo="BJGLLC/cd-home"):
        return cs.check_ci(wf, repo)

    def hit(self, wf, needle, repo="BJGLLC/cd-home"):
        out = self.v(wf, repo)
        self.assertTrue(any(needle in x for x in out), f"no violation mentioning {needle!r}: {out}")

    # --- the good shapes pass
    def test_good_passes(self):
        self.assertEqual(self.v(good()), [])

    def test_self_repo_good_passes(self):
        self.assertEqual(self.v(good(cs.SELF_REPO), cs.SELF_REPO), [])

    def test_bare_on_key_parsed_as_true_is_handled(self):
        text = yaml.safe_dump(good(), sort_keys=False).replace("'on':", "on:")
        self.assertEqual(self.v(yaml.safe_load(text)), [])

    # --- triggers (Review Focus 1)
    def test_pull_request_filters_rejected(self):
        wf = good(); wf["on"]["pull_request"] = {"paths": ["src/**"]}
        self.hit(wf, "on.pull_request")

    def test_pull_request_paths_ignore_rejected(self):
        wf = good(); wf["on"]["pull_request"] = {"paths-ignore": ["docs/**"]}
        self.hit(wf, "on.pull_request")

    def test_push_paths_allowlist_rejected(self):
        wf = good(); wf["on"]["push"] = {"branches": ["main"], "paths": ["src/**"]}
        self.hit(wf, "on.push")

    def test_push_paths_ignore_allowed(self):
        # SSSF-25 cost fix: the push run gates nothing, so machine-lane paths may skip it.
        wf = good(); wf["on"]["push"] = {"branches": ["main"], "paths-ignore": ["claude/memory/**", "claude/PRIORITY.md"]}
        self.assertEqual(self.v(wf), [])

    def test_push_paths_ignore_must_be_a_nonempty_list(self):
        wf = good(); wf["on"]["push"] = {"branches": ["main"], "paths-ignore": []}
        self.hit(wf, "paths-ignore")

    def test_missing_dispatch_rejected(self):
        wf = good(); del wf["on"]["workflow_dispatch"]
        self.hit(wf, "workflow_dispatch")

    def test_schedule_rejected(self):
        # The weekly macOS run lives in its own workflow; a schedule here would bill a whole ci run.
        wf = good(); wf["on"]["schedule"] = [{"cron": "17 9 * * 1"}]
        self.hit(wf, "on: must be exactly")

    def test_permissions_and_concurrency_pinned(self):
        wf = good(); wf["permissions"] = {"contents": "write"}; wf["concurrency"] = {"group": "x"}
        self.hit(wf, "permissions")
        self.hit(wf, "concurrency")

    # --- one job, never skipped (Review Focus 2)
    def test_missing_ci_job_rejected(self):
        wf = good(); wf["jobs"] = {"test": wf["jobs"]["ci"]}
        self.hit(wf, "jobs.ci (the one job")

    def test_job_level_if_rejected(self):
        wf = good(); wf["jobs"]["ci"]["if"] = "github.event_name != 'schedule'"
        self.hit(wf, "jobs.ci.if")

    def test_needs_rejected(self):
        wf = good(); wf["jobs"]["ci"]["needs"] = ["lint"]
        self.hit(wf, "jobs.ci.needs")

    def test_job_continue_on_error_rejected(self):
        wf = good(); wf["jobs"]["ci"]["continue-on-error"] = True
        self.hit(wf, "jobs.ci.continue-on-error")

    def test_step_continue_on_error_rejected(self):
        wf = good(); wf["jobs"]["ci"]["steps"][1]["continue-on-error"] = True
        self.hit(wf, "steps[1].continue-on-error")

    def test_matrix_rejected(self):
        wf = good(); wf["jobs"]["ci"]["strategy"] = {"matrix": {"node": ["20", "22"]}}
        self.hit(wf, "jobs.ci.strategy")

    def test_job_name_rejected(self):
        wf = good(); wf["jobs"]["ci"]["name"] = "CI"
        self.hit(wf, "jobs.ci.name")

    def test_old_reusable_plus_fan_in_shape_rejected(self):
        # The drafted shape (reusable `check` + aggregate `ci`) is exactly what Q2 retired.
        wf = good()
        wf["jobs"] = {"check": {"uses": "BJGLLC/.github/.github/workflows/ci-check.yml@main"},
                      "ci": {"needs": ["check"], "if": "always()", "runs-on": "ubuntu-latest",
                             "timeout-minutes": 5, "steps": [{"run": "true"}]}}
        out = self.v(wf)
        for needle in ("jobs.check", "jobs.ci.needs", "jobs.ci.if", "exactly two"):
            self.assertTrue(any(needle in x for x in out), f"{needle} not flagged: {out}")

    def test_extra_job_rejected(self):
        wf = good(); wf["jobs"]["bash-3-compat"] = {"runs-on": "macos-latest", "steps": [{"run": "true"}]}
        self.hit(wf, "jobs.bash-3-compat")

    def test_failure_notifier_allowed(self):
        wf = good(); wf["jobs"]["notify-linear"] = notifier()
        wf["jobs"]["ci"]["outputs"] = dict(cs.OUTPUTS)
        self.assertEqual(self.v(wf), [])

    def test_notifier_must_need_ci_and_run_only_on_failure(self):
        wf = good(); n = notifier(); n["if"] = "always()"; wf["jobs"]["notify-linear"] = n
        self.hit(wf, "jobs.notify-linear")

    def test_outputs_optional_but_exact(self):
        wf = good(); wf["jobs"]["ci"]["outputs"] = {"failing_step": "${{ steps.other.outputs.x }}"}
        self.hit(wf, "jobs.ci.outputs")

    # --- the job's body
    def test_runs_on_pinned(self):
        wf = good(); wf["jobs"]["ci"]["runs-on"] = "macos-latest"
        self.hit(wf, "runs-on")

    def test_timeout_required_and_bounded(self):
        for bad in (None, 0, 90, "20", True):
            with self.subTest(timeout=bad):
                wf = good()
                if bad is None:
                    del wf["jobs"]["ci"]["timeout-minutes"]
                else:
                    wf["jobs"]["ci"]["timeout-minutes"] = bad
                self.hit(wf, "timeout-minutes")

    def test_checkout_must_fetch_full_history_without_credentials(self):
        for key, val in (("fetch-depth", 1), ("persist-credentials", True)):
            with self.subTest(key=key):
                wf = good(); wf["jobs"]["ci"]["steps"][0]["with"][key] = val
                self.hit(wf, "steps[0]")

    def test_exactly_two_steps(self):
        wf = good(); wf["jobs"]["ci"]["steps"].append({"run": "echo extra"})
        self.hit(wf, "exactly two")

    def test_step_id_must_be_check(self):
        wf = good(); del wf["jobs"]["ci"]["steps"][1]["id"]
        self.hit(wf, "id must be `check`")

    def test_org_repo_must_use_the_org_action(self):
        wf = good(); wf["jobs"]["ci"]["steps"][1]["uses"] = cs.LOCAL_ACTION
        self.hit(wf, "steps[1].uses")

    def test_org_action_must_track_main(self):
        # D7: @main, never a tag (tags sit outside the gated-main ruleset) or a SHA (drift, bump PRs).
        for ref in ("v1", "0123456789abcdef0123456789abcdef01234567"):
            with self.subTest(ref=ref):
                wf = good(); wf["jobs"]["ci"]["steps"][1]["uses"] = cs.ORG_ACTION.replace("@main", "@" + ref)
                self.hit(wf, "steps[1].uses")

    def test_dotgithub_must_call_its_own_copy(self):
        wf = good(cs.SELF_REPO); wf["jobs"]["ci"]["steps"][1]["uses"] = cs.ORG_ACTION
        self.hit(wf, "steps[1].uses", cs.SELF_REPO)

    def test_unknown_input_rejected(self):
        wf = good(); wf["jobs"]["ci"]["steps"][1]["with"]["tools-ref"] = "main"
        self.hit(wf, "with.tools-ref")

    def test_unquoted_input_rejected(self):
        wf = good(); wf["jobs"]["ci"]["steps"][1]["with"]["python-version"] = 3.1   # YAML read 3.10 as 3.1
        self.hit(wf, "quoted string")


class Reserved(unittest.TestCase):
    """SSSF-27 scope note: any other job named ci / review-verdict emits a context that satisfies the ruleset."""

    def run_dir(self, extra, repo="BJGLLC/cd-home"):
        with tempfile.TemporaryDirectory() as d:
            p = pathlib.Path(d)
            (p / "ci.yml").write_text(yaml.safe_dump(good(repo), sort_keys=False))
            for name, body in extra.items():
                (p / name).write_text(yaml.safe_dump(body))
            out = subprocess.run(["python3", cs.__file__, "--repo", repo, d], capture_output=True, text=True)
            return out.returncode, out.stdout

    def test_clean_dir_ok(self):
        rc, out = self.run_dir({"deploy.yml": {"on": {"push": {}}, "jobs": {"deploy": {"runs-on": "ubuntu-latest"}}},
                                "bash-3-compat.yml": {"on": {"schedule": [{"cron": "17 9 * * 1"}]},
                                                      "jobs": {"bash-3-compat": {"runs-on": "macos-latest"}}}})
        self.assertEqual(rc, 0, out)
        self.assertIn("ci_shape: ok", out)

    def test_second_ci_job_elsewhere_rejected(self):
        rc, out = self.run_dir({"sneaky.yml": {"on": {"pull_request": {}}, "jobs": {"ci": {"runs-on": "ubuntu-latest"}}}})
        self.assertEqual(rc, 1)
        self.assertIn("sneaky.yml", out)

    def test_job_named_review_verdict_rejected(self):
        rc, out = self.run_dir({"x.yml": {"on": {"pull_request": {}}, "jobs": {"v": {"name": "review-verdict", "runs-on": "ubuntu-latest"}}}})
        self.assertEqual(rc, 1)
        self.assertIn("review-verdict", out)

    def test_missing_ci_yml_is_red(self):
        with tempfile.TemporaryDirectory() as d:
            out = subprocess.run(["python3", cs.__file__, "--repo", "BJGLLC/x", d], capture_output=True, text=True)
            self.assertEqual(out.returncode, 1)
            self.assertIn("missing", out.stdout)


if __name__ == "__main__":
    unittest.main()
