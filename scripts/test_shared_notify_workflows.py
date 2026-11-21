# scripts/test_shared_notify_workflows.py — SSSF-72: the org-served ci-red-notify and the advisory
# pr-ticket-lint reusable workflows. The step scripts are executed with bash (in a scratch git repo / with
# a scratch event file), so these pin behavior, not text.
import json
import os
import pathlib
import subprocess
import tempfile
import unittest

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
WF = ROOT / ".github" / "workflows"


def load(name):
    d = yaml.safe_load((WF / name).read_text())
    d["on"] = d.pop(True, d.get("on"))  # PyYAML reads the key `on` as True
    return d


def bash(script, env, cwd):
    # GitHub runs a step with no `shell:` (none of these set one) as `bash -e {0}`: no pipefail.
    out = subprocess.run(["bash", "-e", "-c", script], env={**os.environ, **env}, cwd=cwd,
                         capture_output=True, text=True)
    return out.returncode, out.stdout + out.stderr


def git(cwd, *args):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=cwd, check=True,
                   capture_output=True, text=True)


class Harness(unittest.TestCase):
    def test_steps_run_under_the_default_shell(self):
        # bash() mimics GitHub's default `bash -e`; a `shell:` or `defaults:` here would make it lie.
        for name in ("ci-red-notify.yml", "pr-ticket-lint.yml"):
            wf = load(name)
            self.assertNotIn("defaults", wf, name)
            for job in wf["jobs"].values():
                self.assertNotIn("defaults", job, name)
                self.assertFalse([s for s in job["steps"] if "shell" in s], name)


class CiRedNotify(unittest.TestCase):
    def setUp(self):
        self.wf = load("ci-red-notify.yml")
        self.job = self.wf["jobs"]["notify"]
        self.steps = {s.get("id") or s.get("name"): s for s in self.job["steps"]}

    def test_interface_matches_the_per_repo_copies(self):
        # Callers keep their with/secrets stanza unchanged; only `uses:` moves.
        call = self.wf["on"]["workflow_call"]
        self.assertEqual(set(call["inputs"]), {"failing_step"})
        self.assertEqual(call["inputs"]["failing_step"]["default"], "unknown")
        self.assertEqual(call["secrets"], {"LINEAR_API_KEY": {"required": False}})

    def test_reads_only(self):
        self.assertEqual(self.wf.get("permissions"), {})
        self.assertEqual(self.job["permissions"], {"contents": "read"})
        self.assertLessEqual(self.job["timeout-minutes"], 3)

    def ticket(self, branch, *subjects):
        with tempfile.TemporaryDirectory() as d:
            git(d, "init", "-q")
            for s in subjects or ("init",):
                git(d, "commit", "-q", "--allow-empty", "-m", s)
            out = pathlib.Path(d, "out")
            out.write_text("")
            rc, log = bash(self.steps["ticket"]["run"], {"BRANCH": branch, "GITHUB_OUTPUT": str(out)}, d)
            self.assertEqual(rc, 0, log)
            kv = dict(line.split("=", 1) for line in out.read_text().splitlines())
            return kv.get("ticket", ""), log

    def test_ticket_from_branch(self):
        self.assertEqual(self.ticket("blake/tool-46-red-gate")[0], "TOOL-46")

    def test_ticket_from_recent_commits_when_branch_has_none(self):
        self.assertEqual(self.ticket("main", "feed: x (OPS-12)", "chore")[0], "OPS-12")

    def test_no_ticket_warns_and_stays_green(self):
        t, log = self.ticket("main", "chore")
        self.assertEqual(t, "")
        self.assertIn("::warning", log)

    def test_post_without_key_is_a_notice_not_a_failure(self):
        post = next(s for s in self.job["steps"] if "LINEAR_API_KEY" in (s.get("env") or {}))
        self.assertEqual(post["if"], "steps.ticket.outputs.ticket != ''")
        with tempfile.TemporaryDirectory() as d:
            rc, log = bash(post["run"], {"LINEAR_API_KEY": "", "TICKET": "OPS-1", "BRANCH": "b",
                                         "FAILING_STEP": "check", "RUN_URL": "u"}, d)
        self.assertEqual(rc, 0, log)
        self.assertIn("::notice", log)


class PrTicketLint(unittest.TestCase):
    def setUp(self):
        self.wf = load("pr-ticket-lint.yml")
        self.job = self.wf["jobs"]["lint"]
        self.step = next(s for s in self.job["steps"] if "run" in s)

    def test_advisory_by_default(self):
        adv = self.wf["on"]["workflow_call"]["inputs"]["advisory"]
        self.assertEqual((adv["type"], adv["default"]), ("boolean", True))
        self.assertEqual(self.job["continue-on-error"], "${{ inputs.advisory }}")

    def test_reads_only_and_no_secrets(self):
        self.assertEqual(self.wf.get("permissions"), {})
        self.assertEqual(self.job["permissions"], {"contents": "read"})
        self.assertNotIn("secrets", self.wf["on"]["workflow_call"])
        self.assertNotIn("secrets.", (WF / "pr-ticket-lint.yml").read_text())

    def test_no_check_it_emits_can_satisfy_a_required_context(self):
        names = {str(self.wf.get("name", "")).lower()} | {k.lower() for k in self.wf["jobs"]}
        self.assertFalse(names & {"ci", "review-verdict"})

    def lint(self, pr, advisory):
        with tempfile.TemporaryDirectory() as d:
            pathlib.Path(d, ".ticketlint").symlink_to(ROOT)
            ev = pathlib.Path(d, "event.json")
            ev.write_text(json.dumps({"pull_request": pr}))
            return bash(self.step["run"], {"GITHUB_EVENT_PATH": str(ev), "RUNNER_TEMP": d,
                                           "ADVISORY": "true" if advisory else "false"}, d)

    def test_advisory_missing_ticket_warns_and_exits_0(self):
        rc, log = self.lint({"title": "x", "body": None, "head": {"ref": "x"}, "labels": [], "user": {"login": "b"}}, True)
        self.assertEqual(rc, 0, log)
        self.assertIn("::warning", log)
        self.assertNotIn("::error", log)

    def test_blocking_missing_ticket_fails(self):
        rc, log = self.lint({"title": "x", "body": None, "head": {"ref": "x"}, "labels": [], "user": {"login": "b"}}, False)
        self.assertNotEqual(rc, 0, log)

    def test_citing_pr_passes_either_way(self):
        pr = {"title": "x", "body": "SSSF-72", "head": {"ref": "x"}, "labels": [], "user": {"login": "b"}}
        for advisory in (True, False):
            with self.subTest(advisory=advisory):
                self.assertEqual(self.lint(pr, advisory)[0], 0)

    def test_caller_fixture_calls_it_advisory(self):
        c = yaml.safe_load((ROOT / "scripts" / "fixtures" / "caller-pr-ticket-lint.yml").read_text())
        c["on"] = c.pop(True, c.get("on"))
        (job,) = c["jobs"].values()
        self.assertEqual(job["uses"], "BJGLLC/.github/.github/workflows/pr-ticket-lint.yml@main")
        self.assertNotEqual((job.get("with") or {}).get("advisory"), False)
        self.assertNotIn("secrets", job)


if __name__ == "__main__":
    unittest.main()
