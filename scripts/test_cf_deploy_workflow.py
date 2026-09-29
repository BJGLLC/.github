# scripts/test_cf_deploy_workflow.py — pins the pipeline's safety structure (Review Focus 2–4).
import pathlib
import unittest

import yaml

WF = pathlib.Path(__file__).resolve().parents[1] / ".github" / "workflows"


def load(name):
    d = yaml.safe_load((WF / name).read_text())
    d["on"] = d.pop(True, d.get("on"))  # PyYAML reads the key `on` as True
    return d


class CfDeploy(unittest.TestCase):
    def setUp(self):
        self.j = load("cf-deploy.yml")["jobs"]

    def test_pipeline_order(self):
        self.assertEqual(self.j["ship"]["needs"], "guard")
        self.assertEqual(self.j["verify"]["needs"], ["guard", "ship"])
        self.assertEqual(self.j["rollback-ship"]["needs"], ["guard", "verify"])
        self.assertEqual(self.j["rollback-verify"]["needs"], ["guard", "rollback-ship"])
        self.assertEqual(sorted(self.j["report"]["needs"]),
                         sorted(["guard", "ship", "verify", "rollback-ship", "rollback-verify"]))

    def test_auto_rollback_only_for_deploys_with_a_target(self):
        cond = self.j["rollback-ship"]["if"]
        for s in ("needs.verify.outputs.ok == 'false'", "inputs.reason == 'deploy'", "needs.guard.outputs.prev_sha != ''"):
            self.assertIn(s, cond)

    def test_rollback_verify_never_forces_failure(self):
        run = "\n".join(s.get("run", "") for s in self.j["rollback-verify"]["steps"])
        self.assertNotIn("--force-fail", run)

    def test_ship_uses_the_guarded_sha_not_the_event_sha(self):
        self.assertEqual(self.j["ship"]["with"]["sha"], "${{ needs.guard.outputs.sha }}")
        self.assertEqual(self.j["rollback-ship"]["with"]["sha"], "${{ needs.guard.outputs.prev_sha }}")

    def test_report_always_runs_once_guard_said_go(self):
        self.assertEqual(self.j["report"]["if"], "always() && needs.guard.outputs.go == 'true'")

    def test_drill_branch_main_is_refused(self):
        run = next(s["run"] for s in self.j["guard"]["steps"] if s.get("id") == "u")
        self.assertIn('[ "$DB" != main ]', run)

    def test_no_job_can_dispatch_workflows(self):
        for name, job in self.j.items():
            self.assertNotEqual((job.get("permissions") or {}).get("actions"), "write", name)

    def test_inline_jobs_have_bounded_timeouts(self):
        # Actions minutes are a hard cost constraint; the default timeout is 360 min.
        for name, mins in {"guard": 5, "verify": 8, "rollback-verify": 8, "report": 5}.items():
            self.assertEqual(self.j[name]["timeout-minutes"], mins, name)

    def test_smoke_output_fails_closed(self):
        # Empty/missing smoke output must yield ok=false (triggering rollback), never ok="".
        for name in ("verify", "rollback-verify"):
            run = next(s["run"] for s in self.j[name]["steps"] if s.get("id") == "s")
            self.assertIn('echo "ok=${ok:-false}" >> "$GITHUB_OUTPUT"', run, name)
            self.assertIn('if .ok == true then "true" else "false" end', run, name)

    def test_freeze_open_failure_is_not_swallowed(self):
        run = next(s["run"] for s in self.j["report"]["steps"] if s.get("id") == "fz")
        self.assertIn('f="$(python3 .deploylib/scripts/deploy_guard.py freeze-open', run)
        self.assertIn('echo "freeze=$f" >> "$GITHUB_OUTPUT"', run)
        for line in run.splitlines():
            if "freeze-open" in line:
                self.assertFalse(line.lstrip().startswith("echo"), line)
                self.assertNotIn("echo", line.split("freeze-open")[0].replace('f="$(', ""), line)

    # --- fix round 1 ---
    def _step(self, job, key, val):
        return next(s for s in self.j[job]["steps"] if s.get(key) == val)

    def test_drill_ledger_env_requires_drill_env(self):
        run = self._step("guard", "id", "u")["run"]
        self.assertIn('[ "$LE" != drill ] || { echo "::error::ledger_env drill requires env: drill"; exit 1; }', run)
        smoke = self._step("verify", "id", "s")
        self.assertIn("ENVN: ${{ inputs.env }}", "\n".join(f"{k}: {v}" for k, v in smoke["env"].items()))
        self.assertIn('[ "$FORCE" = true ] && [ "$ENVN" = drill ]', smoke["run"])

    def test_guard_refusals_fail_the_run(self):
        run = self._step("guard", "id", "d")["run"]
        self.assertIn("refused:*)", run)
        refuse = run.split("refused:*)", 1)[1].split(";;", 1)[0]
        self.assertIn("exit 1", refuse)
        self.assertIn("skip:*)", run)
        self.assertIn("::notice::", run)

    def test_outcome_is_posted_even_if_freeze_step_failed(self):
        steps = self.j["report"]["steps"]
        notify = next(s for s in steps if s.get("name") == "PR comment + Linear")
        final = steps[-1]
        self.assertEqual(notify["if"], "${{ !cancelled() && steps.st.outputs.state != '' }}")
        self.assertEqual(final["if"], "${{ !cancelled() }}")
        self.assertNotIn("|| true", notify["run"])
        self.assertIn("::warning::deploy notify failed", notify["run"])

    def test_state_failure_is_not_swallowed(self):
        run = self._step("report", "id", "st")["run"]
        self.assertIn('s="$(python3 .deploylib/scripts/deploy_notify.py state', run)
        self.assertIn('echo "state=$s" >> "$GITHUB_OUTPUT"', run)

    def test_rollback_fires_when_verify_itself_failed(self):
        cond = self.j["rollback-ship"]["if"]
        for s in ("!cancelled()", "needs.guard.result == 'success'",
                  "needs.verify.result == 'failure'"):
            self.assertIn(s, cond)
        # ship is not a direct need (test_pipeline_order pins it); verify only runs after ship succeeded.
        self.assertNotIn("needs.ship", cond)

    def test_rollback_verify_runs_after_a_crashed_verify(self):
        # No status function = implicit success() over the whole chain, which a failed verify breaks.
        self.assertEqual(self.j["rollback-verify"]["if"],
                         "${{ !cancelled() && needs.rollback-ship.result == 'success' }}")

    def test_passed_smoke_never_triggers_rollback(self):
        cond = self.j["rollback-ship"]["if"]
        self.assertIn("(needs.verify.outputs.ok == 'false' || (needs.verify.result == 'failure' && needs.verify.outputs.ok != 'true'))", cond)

    def test_smoke_jq_failure_falls_back_to_false(self):
        for name in ("verify", "rollback-verify"):
            run = next(s["run"] for s in self.j[name]["steps"] if s.get("id") == "s")
            self.assertIn('smoke.json 2>/dev/null || echo false)"', run, name)

    def test_report_keeps_always(self):
        self.assertIn("always()", self.j["report"]["if"])


    def test_env_and_kind_are_validated(self):
        run = next(s["run"] for s in self.j["guard"]["steps"] if s.get("id") == "u")
        self.assertIn('case "$ENVN" in production|drill) ;; *) echo "::error::env must be production or drill"; exit 1 ;; esac', run)
        self.assertIn('case "$KIND" in pages|worker) ;; *) echo "::error::kind must be pages or worker"; exit 1 ;; esac', run)
        env = next(s["env"] for s in self.j["guard"]["steps"] if s.get("id") == "u")
        self.assertEqual(env["KIND"], "${{ inputs.kind }}")


class CfShip(unittest.TestCase):
    def setUp(self):
        self.job = load("cf-ship.yml")["jobs"]["ship"]
        self.steps = self.job["steps"]

    def test_checks_out_the_input_sha(self):
        self.assertEqual(self.steps[0]["with"]["ref"], "${{ inputs.sha }}")

    def test_refuses_git_integrated_pages_projects(self):
        self.assertTrue(any(".result.source.type" in s.get("run", "") for s in self.steps))

    def test_cloudflare_creds_only_on_cloudflare_steps(self):
        self.assertFalse([k for k in (self.job.get("env") or {}) if k.startswith("CLOUDFLARE_")])
        for s in self.steps:
            has = any(k.startswith("CLOUDFLARE_") for k in (s.get("env") or {}))
            if s.get("name") in ("Install + build", "Stamp version.json (pages)") or "uses" in s:
                self.assertFalse(has, s.get("name"))
            if s.get("name", "").startswith(("Refuse", "Upload")):
                self.assertTrue(has, s.get("name"))

    def test_cred_isolation_is_documented_as_best_effort(self):
        self.assertIn("best-effort", (WF / "cf-ship.yml").read_text())

    def test_ship_job_is_time_bounded(self):
        self.assertEqual(self.job["timeout-minutes"], 15)


if __name__ == "__main__":
    unittest.main()
