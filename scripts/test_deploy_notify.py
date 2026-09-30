# scripts/test_deploy_notify.py
import contextlib
import io
import subprocess
import unittest
from unittest import mock

import deploy_notify
from deploy_notify import STATES, TEAM_KEYS, body, final_state, tickets

A, B = "1234567" + "0" * 33, "abcdef0" + "0" * 33


class Tickets(unittest.TestCase):
    def test_spec_key_list_is_exact(self):
        self.assertEqual(set(TEAM_KEYS), {"cd", "tool", "auto", "sssf", "data", "ops", "rnd", "bjg"})

    def test_every_live_team_key_matches(self):
        msg = " ".join(f"{k}-{i}" for i, k in enumerate(TEAM_KEYS, 1))
        self.assertEqual(tickets([msg], cap=20),
                         ["CD-1", "TOOL-2", "AUTO-3", "SSSF-4", "DATA-5", "OPS-6", "RND-7", "BJG-8"])

    def test_dead_keys_and_lookalikes_do_not_match(self):
        self.assertEqual(tickets(["ENG-1 DES-2 SEC-3 MAR-4 sha-256 utf-8 iso-8601 abcd-12 cd-home x-cd-9y"]), [])

    def test_subject_ticket_outranks_body_mentions(self):
        msgs = ["SSSF-40: deploy smoke (#12)\n\nsee also OPS-350", "CD-7: copy fix"]
        self.assertEqual(tickets(msgs), ["SSSF-40", "CD-7", "OPS-350"])

    def test_dedupes_uppercases_and_caps_at_five(self):
        self.assertEqual(tickets(["cd-1 cd-1 CD-2 cd-3 cd-4 cd-5 cd-6"]), ["CD-1", "CD-2", "CD-3", "CD-4", "CD-5"])

    def test_merge_commit_branch_name_counts(self):
        self.assertEqual(tickets(["Merge pull request #12 from BJGLLC/claude/cd-441-tunnel-receipt"]), ["CD-441"])


class State(unittest.TestCase):
    def test_matrix(self):
        cases = [
            (("deploy", "success", "true", "skipped", ""), "deployed"),
            (("rollback", "success", "true", "skipped", ""), "rolled-back"),
            (("deploy", "failure", "", "skipped", ""), "ship-failed"),
            (("deploy", "success", "false", "success", "true"), "auto-rolled-back"),
            (("deploy", "success", "false", "success", "false"), "rollback-failed"),
            (("deploy", "success", "false", "failure", ""), "rollback-failed"),
            (("deploy", "success", "false", "skipped", ""), "smoke-failed-no-target"),
            (("deploy", "success", "false", "", ""), "smoke-failed-no-target"),
            (("rollback", "success", "false", "skipped", ""), "rollback-failed"),
        ]
        for args, want in cases:
            with self.subTest(args=args):
                self.assertEqual(final_state(*args), want)


class Body(unittest.TestCase):
    def test_deployed_names_sha_and_rollback_command(self):
        t = body("deployed", "example.pages.dev (CF Pages)", "BJGLLC/example-pages", A, B, "https://run/1")
        self.assertIn("`1234567`", t)
        self.assertIn("(was `abcdef0`)", t)
        self.assertIn("Rollback: `bin/rollback example-pages abcdef0`", t)
        self.assertIn("[run](https://run/1)", t)

    def test_first_deploy_says_no_target(self):
        self.assertIn("no earlier green deploy on record", body("deployed", "s", "BJGLLC/x", A))

    def test_auto_rollback_names_freeze(self):
        t = body("auto-rolled-back", "s", "BJGLLC/cd-pages", A, B, freeze=42)
        self.assertIn("back on `abcdef0`", t)
        self.assertIn("BJGLLC/cd-pages#42", t)

    def test_noop_rollback_reads_as_redeploy(self):
        self.assertIn("Redeployed", body("rolled-back", "s", "BJGLLC/x", A, A))

    def test_no_params_body_never_claims_a_rollback_or_an_unknown_state(self):
        hint = "Nothing was rolled back: rollback target `abcdef0` has no trusted recorded build params."
        text = body("smoke-failed-no-target", "site", "BJGLLC/r", A, B, "https://run", 9, rollback_hint=hint)
        self.assertIn("NOT rolled back", text)
        self.assertIn("still live", text)
        self.assertIn(hint, text)
        self.assertNotIn("UNKNOWN", text)
        self.assertNotIn("Rolled back", text)
        self.assertIn("UNKNOWN", body("smoke-failed-no-target", "site", "BJGLLC/r", A, B))  # no hint: unchanged

    def test_unknown_outcome_says_unknown(self):
        self.assertIn("UNKNOWN", body("rollback-failed", "s", "BJGLLC/x", A, B))

    def test_drill_is_tagged_and_log_paths_are_not_links(self):
        t = body("deployed", "s", "BJGLLC/x", A, B, "journalctl --user -u intranet-deploy.service", env="drill")
        self.assertTrue(t.startswith("[drill] "))
        self.assertIn("log: `journalctl", t)
        self.assertNotIn("[run](journalctl", t)


class Hardening(unittest.TestCase):
    def test_every_state_final_state_can_return_is_in_the_vocabulary(self):
        outs = {final_state(r, sh, sm, rs, rsm) for r in ("deploy", "rollback")
                for sh in ("success", "failure", "") for sm in ("true", "false", "")
                for rs in ("success", "failure", "skipped", "") for rsm in ("true", "false", "")}
        self.assertEqual(outs, set(STATES))

    def test_unknown_state_is_rejected_by_argparse(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as cm:
            deploy_notify.main(["notify", "--repo", "o/r", "--state", "bogus", "--surface", "s", "--attempted", A, "--dry-run"])
        self.assertEqual(cm.exception.code, 2)

    def test_all_zero_live_before_is_treated_as_no_prior_deploy(self):
        seen = []
        with mock.patch.object(deploy_notify, "range_messages", lambda repo, o, n: seen.append((o, n)) or []), \
                contextlib.redirect_stdout(io.StringIO()):
            deploy_notify.main(["notify", "--repo", "o/r", "--state", "deployed", "--surface", "s",
                                "--attempted", A, "--live-before", "0" * 40, "--dry-run"])
        self.assertEqual(seen, [("", A)])

    def test_gh_failure_prints_stderr_and_reraises(self):
        err = subprocess.CalledProcessError(1, ["gh"], output="", stderr="HTTP 403: nope")
        with mock.patch.object(deploy_notify.subprocess, "run", side_effect=err), \
                contextlib.redirect_stderr(io.StringIO()) as se, self.assertRaises(subprocess.CalledProcessError):
            deploy_notify.gh("api", "x")
        self.assertIn("HTTP 403: nope", se.getvalue())


if __name__ == "__main__":
    unittest.main()
