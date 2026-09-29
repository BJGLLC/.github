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


class CfShip(unittest.TestCase):
    def setUp(self):
        self.job = load("cf-ship.yml")["jobs"]["ship"]
        self.steps = self.job["steps"]

    def test_checks_out_the_input_sha(self):
        self.assertEqual(self.steps[0]["with"]["ref"], "${{ inputs.sha }}")

    def test_refuses_git_integrated_pages_projects(self):
        self.assertTrue(any(".result.source.type" in s.get("run", "") for s in self.steps))

    def test_ship_job_is_time_bounded(self):
        self.assertEqual(self.job["timeout-minutes"], 15)


if __name__ == "__main__":
    unittest.main()
