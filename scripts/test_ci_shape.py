# scripts/test_ci_shape.py
"""Review v4 phase 3 (SSSF-30): the single-job `ci` caller shape (decision 01M3N14TGN, Q2). Every rule
has a passing and a failing case; a validator that cannot return false is theater."""
import copy
import pathlib
import subprocess
import sys
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

    # --- fix round 1 (SSSF-30 review)
    def test_name_must_be_ci(self):
        wf = good(); wf["name"] = "CI"
        self.hit(wf, "name must be 'ci'")

    def test_dispatch_inputs_rejected(self):
        wf = good(); wf["on"]["workflow_dispatch"] = {"inputs": {"x": {}}}
        self.hit(wf, "workflow_dispatch must take no inputs")

    def test_top_level_keys_allowlisted(self):
        # workflow-level env (BASH_ENV, NODE_OPTIONS, ...) is inherited by every step of the action
        for key, val in (("env", {"BASH_ENV": "${{ github.workspace }}/.neuter.sh"}),
                         ("defaults", {"run": {"shell": "bash {0} || true"}}), ("run-name", "x")):
            with self.subTest(key=key):
                wf = good(); wf[key] = val
                self.hit(wf, f"{key} is not allowed")

    def test_notifier_with_secrets_allowed(self):
        wf = good(); n = notifier(); n["secrets"] = {"LINEAR_API_KEY": "${{ secrets.LINEAR_API_KEY }}"}
        n["needs"] = ["ci"]; wf["jobs"]["notify-linear"] = n
        self.assertEqual(self.v(wf), [])

    def test_notifier_is_only_a_call_to_the_notify_workflow(self):
        for extra in ({"name": "review-verdict"}, {"permissions": {"statuses": "write"}},
                      {"runs-on": "ubuntu-latest", "steps": [{"run": "true"}]},
                      {"strategy": {"matrix": {"a": [1]}}}, {"uses": "./.github/workflows/other.yml"}):
            with self.subTest(extra=sorted(extra)):
                wf = good(); n = notifier(); n.update(extra); wf["jobs"]["notify-linear"] = n
                self.hit(wf, "jobs.notify-linear")

    def test_malformed_shapes_do_not_traceback(self):
        for wf in ({"name": "ci", "on": ["push", "pull_request"], "jobs": {}},
                   {"name": "ci", "on": {}, "jobs": ["ci"]}, ["a"], None):
            with self.subTest(wf=wf):
                self.assertTrue(self.v(wf))
        wf = good(); wf["jobs"]["ci"]["steps"][1]["with"] = ["a"]
        self.assertTrue(self.v(wf))
        wf = good(); wf["jobs"]["ci"]["steps"] = {"0": "a", "1": "b"}
        self.assertTrue(self.v(wf))


class Loader(unittest.TestCase):
    def run_text(self, text, extra=None):
        with tempfile.TemporaryDirectory() as d:
            p = pathlib.Path(d)
            (p / "ci.yml").write_text(text)
            for n, t in (extra or {}).items():
                (p / n).write_text(t)
            out = subprocess.run([sys.executable, cs.__file__, "--repo", "BJGLLC/cd-home", d], capture_output=True, text=True)
            return out.returncode, out.stdout, out.stderr

    def base(self):
        return yaml.safe_dump(good(), sort_keys=False)

    def test_duplicate_key_rejected(self):
        text = self.base().replace("jobs:\n", "jobs:\n  ci:\n    if: false\n", 1)
        rc, out, err = self.run_text(text)
        self.assertEqual(rc, 1, out)
        self.assertIn("duplicate", out)
        self.assertNotIn("Traceback", err)

    def test_merge_key_rejected(self):
        text = ("name: ci\non: {push: {branches: [main]}, pull_request: {}, workflow_dispatch: {}}\n"
                "x: &b {runs-on: ubuntu-latest}\njobs:\n  ci:\n    <<: *b\n")
        rc, out, err = self.run_text(text)
        self.assertEqual(rc, 1, out)
        self.assertIn("merge", out)
        self.assertNotIn("Traceback", err)

    def test_unparseable_unrelated_workflow_is_one_line(self):
        rc, out, err = self.run_text(self.base(), {"broken.yml": "on: {push: [\n"})
        self.assertEqual(rc, 1)
        self.assertIn("ci_shape: broken.yml:", out)
        self.assertNotIn("Traceback", err)

    def test_unparseable_ci_yml_is_one_line(self):
        rc, out, err = self.run_text("name: ci\njobs: {ci: [\n")
        self.assertEqual(rc, 1)
        self.assertIn("ci_shape: ci.yml:", out)
        self.assertNotIn("Traceback", err)

    def crashless(self, text, extra=None, ci_extra=None):
        rc, out, err = self.run_text(text, extra)
        self.assertNotIn("Traceback", err)
        self.assertEqual(rc, 1, out)
        self.assertIn("ci_shape:", out)

    def test_bad_scalars_do_not_traceback(self):
        for bad in ("!!int abc", "!!bool maybe", "2026-13-45"):
            with self.subTest(bad=bad):
                self.crashless(self.base().replace("timeout-minutes: 20", f"timeout-minutes: {bad}"))
                self.crashless(self.base(), {"o.yml": f"x: {bad}\n"})

    def test_complex_key_does_not_traceback(self):
        self.crashless(self.base() + "? [a]\n: 1\n")

    def test_mixed_type_keys_do_not_traceback(self):
        wf = good(); wf["jobs"]["ci"]["steps"][1]["with"][1] = "x"; wf["jobs"]["ci"]["steps"][1]["with"]["node-version"] = "20"
        wf["jobs"]["ci"][True] = "a"; wf["jobs"]["ci"]["if"] = "b"
        self.crashless(yaml.safe_dump(wf, sort_keys=False))
        self.crashless(self.base().replace("jobs:", "yes: 1\nif: 2\njobs:", 1))

    def test_push_as_list_does_not_traceback(self):
        wf = good(); wf["on"]["push"] = ["main"]
        self.crashless(yaml.safe_dump(wf, sort_keys=False))

    def test_directory_named_yml_does_not_traceback(self):
        with tempfile.TemporaryDirectory() as d:
            pathlib.Path(d, "ci.yml").write_text(self.base())
            pathlib.Path(d, "z.yml").mkdir()
            out = subprocess.run([sys.executable, cs.__file__, "--repo", "BJGLLC/cd-home", d], capture_output=True, text=True)
            self.assertNotIn("Traceback", out.stderr)
            self.assertEqual(out.returncode, 0, out.stdout)

    def test_top_level_list_workflow_does_not_traceback(self):
        rc, out, err = self.run_text(self.base(), {"weird.yml": "- a\n"})
        self.assertNotIn("Traceback", err)


class Reserved(unittest.TestCase):
    """SSSF-27 scope note: any other job named ci / review-verdict emits a context that satisfies the ruleset."""

    def run_dir(self, extra, repo="BJGLLC/cd-home"):
        with tempfile.TemporaryDirectory() as d:
            p = pathlib.Path(d)
            (p / "ci.yml").write_text(yaml.safe_dump(good(repo), sort_keys=False))
            for name, body in extra.items():
                (p / name).write_text(yaml.safe_dump(body))
            out = subprocess.run([sys.executable, cs.__file__, "--repo", repo, d], capture_output=True, text=True)
            return out.returncode, out.stdout

    def test_workflow_named_review_verdict_is_fine(self):
        # review-verdict is a commit status posted via the API; a workflow's name emits nothing by that name.
        rc, out = self.run_dir({"x.yml": {"name": "review-verdict", "on": {"push": {}}, "jobs": {"verdict": {"runs-on": "ubuntu-latest"}}}})
        self.assertEqual(rc, 0, out)

    def test_real_review_verdict_caller_passes(self):
        # The byte-identical caller every gated repo carries (job `verdict`, name: review-verdict).
        real = (pathlib.Path(__file__).parent / "fixtures" / "caller-review-verdict.yml").read_text()
        with tempfile.TemporaryDirectory() as d:
            p = pathlib.Path(d)
            (p / "ci.yml").write_text(yaml.safe_dump(good(), sort_keys=False))
            (p / "review-verdict.yml").write_text(real)
            out = subprocess.run([sys.executable, cs.__file__, "--repo", "BJGLLC/cd-home", d], capture_output=True, text=True)
            self.assertEqual(out.returncode, 0, out.stdout)
            self.assertIn("ci_shape: ok", out.stdout)

    def test_uppercase_suffix_scanned(self):
        rc, out = self.run_dir({"Other.YML": {"on": {"pull_request": {}}, "jobs": {"ci": {"runs-on": "ubuntu-latest"}}}})
        self.assertEqual(rc, 1)
        self.assertIn("Other.YML", out)

    def test_bare_dot_yml_name_scanned(self):
        # Path('.yml').suffix == '' so a suffix match skipped a file named exactly `.yml`.
        rc, out = self.run_dir({".yml": {"on": {"pull_request": {}}, "jobs": {"ci": {"runs-on": "ubuntu-latest"}}}})
        self.assertEqual(rc, 1)
        self.assertIn(".yml", out)

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

    def test_expression_job_names_rejected(self):
        for job in ({"name": "${{ 'ci' }}", "runs-on": "ubuntu-latest"},
                    {"strategy": {"matrix": {"n": ["ci"]}}, "name": "${{ matrix.n }}", "runs-on": "ubuntu-latest"}):
            with self.subTest(job=job):
                rc, out = self.run_dir({"x.yml": {"on": {"pull_request": {}}, "jobs": {"x": job}}})
                self.assertEqual(rc, 1)
                self.assertIn("x.yml", out)

    def test_case_variants_rejected(self):
        rc, out = self.run_dir({"x.yml": {"on": {"pull_request": {}}, "jobs": {"CI": {"runs-on": "ubuntu-latest"}}}})
        self.assertEqual(rc, 1)
        rc, out = self.run_dir({"y.yml": {"on": {"pull_request": {}}, "jobs": {"v": {"name": "Review-Verdict"}}}})
        self.assertEqual(rc, 1)

    def test_other_workflow_named_ci_rejected(self):
        for n in ("ci", "CI", " ci "):
            with self.subTest(name=n):
                rc, out = self.run_dir({"x.yml": {"name": n, "on": {"push": {}}, "jobs": {"build": {"runs-on": "ubuntu-latest"}}}})
                self.assertEqual(rc, 1)
                self.assertIn("x.yml", out)

    def test_notifier_named_ci_in_ci_yml_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            wf = good(); n = notifier(); n["name"] = "ci"; wf["jobs"]["notify"] = n
            pathlib.Path(d, "ci.yml").write_text(yaml.safe_dump(wf, sort_keys=False))
            out = subprocess.run([sys.executable, cs.__file__, "--repo", "BJGLLC/cd-home", d], capture_output=True, text=True)
            self.assertEqual(out.returncode, 1)

    def test_missing_ci_yml_is_red(self):
        with tempfile.TemporaryDirectory() as d:
            out = subprocess.run([sys.executable, cs.__file__, "--repo", "BJGLLC/x", d], capture_output=True, text=True)
            self.assertEqual(out.returncode, 1)
            self.assertIn("missing", out.stdout)


if __name__ == "__main__":
    unittest.main()
