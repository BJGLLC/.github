# scripts/test_deploy_notify.py
import contextlib
import hashlib
import io
import json
import os
import subprocess
import unittest
from unittest import mock

import deploy_notify
from deploy_notify import STATES, TEAM_KEYS, body, final_state, ping_text, probe, tickets

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


class ClientPing(unittest.TestCase):
    """Decision 01M46ESRWR (10/5): every client production deploy pings Blake on Moshi, with live before/after."""

    def notify(self, *extra, env=None, token="tok"):
        sent = []
        environ = {k: v for k, v in os.environ.items() if k not in ("MOSHI_TOKEN", "LINEAR_API_KEY")}
        if token:
            environ["MOSHI_TOKEN"] = token
        with mock.patch.object(deploy_notify, "range_messages", lambda *a: []), \
                mock.patch.object(deploy_notify, "post_moshi", lambda tok, t, m: sent.append((tok, t, m)) or True), \
                mock.patch.dict(os.environ, environ, clear=True), contextlib.redirect_stdout(io.StringIO()) as out:
            rc = deploy_notify.main(["notify", "--repo", "o/r", "--state", "deployed", "--surface", "site", "--attempted", A,
                                     "--live-before", B, "--run-url", "https://run/1", *extra])
        return rc, sent, out.getvalue()

    def test_rolled_back_with_no_live_before_has_no_empty_was(self):
        t = body("rolled-back", "s", "BJGLLC/x", A, "")
        self.assertNotIn("(was ``)", t)
        self.assertIn("is on `1234567`", t)

    def test_ping_text(self):
        title, msg = ping_text("deployed", "site", A, B, "HTTP 200, sha256 aaa", "HTTP 200, sha256 bbb", "https://run/1")
        self.assertEqual(title, "Client deploy deployed: site")
        self.assertEqual(msg, "1234567 (was abcdef0) · live before: HTTP 200, sha256 aaa · live after: HTTP 200, sha256 bbb"
                              " · https://run/1")
        self.assertEqual(ping_text("auto-rolled-back", "site", A)[0], "Client deploy AUTO-ROLLED-BACK: site")
        self.assertEqual(ping_text("refused", "site", detail="refused: x")[1], "refused: x")
        self.assertIn("(was none)", ping_text("deployed", "site", A)[1])

    def test_client_production_notify_pings_even_without_linear(self):
        rc, sent, _ = self.notify("--client", "--before", "HTTP 200", "--after", "HTTP 200")
        self.assertEqual(rc, 0)
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0][0], "tok")
        self.assertIn("live before: HTTP 200", sent[0][2])

    def test_no_ping_for_internal_targets_drills_or_dry_runs(self):
        for extra in ((), ("--client", "--env", "drill"), ("--client", "--dry-run")):
            with self.subTest(extra=extra):
                self.assertEqual(self.notify(*extra)[1], [])

    def test_a_missing_token_warns_and_never_fails(self):
        rc, sent, out = self.notify("--client", token="")
        self.assertEqual((rc, sent), (0, []))
        self.assertIn("NOT pinged", out)

    def test_a_failed_push_never_fails_and_never_prints_the_token(self):
        def boom(tok, t, m):
            raise OSError("network down")
        with mock.patch.object(deploy_notify, "post_moshi", boom), mock.patch.dict(os.environ, {"MOSHI_TOKEN": "s3cret"}), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertFalse(deploy_notify.ping("t", "m"))
        self.assertIn("::warning", out.getvalue())
        self.assertNotIn("s3cret", out.getvalue())

    def test_post_moshi_sends_one_unified_push(self):
        seen = []

        class Resp:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False
        with mock.patch.object(deploy_notify.urllib.request, "urlopen", lambda req, timeout: seen.append(req) or Resp()):
            self.assertTrue(deploy_notify.post_moshi("tok", "title", "msg"))
        self.assertEqual(seen[0].full_url, "https://api.getmoshi.app/api/webhook")
        self.assertEqual(json.loads(seen[0].data), {"token": "tok", "title": "title", "message": "msg", "unified": True})

    def test_ping_cli_for_guard_refusals(self):
        sent = []
        with mock.patch.object(deploy_notify, "post_moshi", lambda tok, t, m: sent.append((t, m)) or True), \
                mock.patch.dict(os.environ, {"MOSHI_TOKEN": "tok"}), contextlib.redirect_stdout(io.StringIO()):
            rc = deploy_notify.main(["ping", "--surface", "site", "--detail", "refused: no drill", "--run-url", "https://run/2"])
        self.assertEqual(rc, 0)
        self.assertEqual(sent, [("Client deploy REFUSED: site", "refused: no drill · https://run/2")])


class Probe(unittest.TestCase):
    PAGE = b"<html>live</html>"

    def fake(self, pages):
        def fetch(url):
            for prefix, resp in pages.items():
                if url.startswith(prefix):
                    return resp
            return 0, b""
        return fetch

    def test_status_body_hash_and_version(self):
        f = self.fake({"https://x/version.json": (200, json.dumps({"sha": A}).encode()), "https://x/": (200, self.PAGE)})
        want = f"HTTP 200, sha256 {hashlib.sha256(self.PAGE).hexdigest()[:12]}, version 1234567"
        self.assertEqual(probe("https://x/", "https://x/version.json", fetch=f), want)

    def test_a_site_without_a_version_says_so(self):
        for vresp in ((200, b"<html>fallback</html>"), (404, b""), (200, b"[1]")):
            with self.subTest(vresp=vresp):
                f = self.fake({"https://x/version.json": vresp, "https://x/": (200, self.PAGE)})
                self.assertTrue(probe("https://x/", "https://x/version.json", fetch=f).endswith(f"no version (HTTP {vresp[0]})"))

    def test_unreachable_and_errors_are_reported_not_raised(self):
        self.assertEqual(probe("https://down/", fetch=self.fake({})), "unreachable")
        self.assertEqual(probe("https://x/", fetch=self.fake({"https://x/": (503, b"")})), "HTTP 503")

    def test_probe_cli_always_exits_0(self):
        with mock.patch.object(deploy_notify, "fetch_raw", lambda url: (0, b"")), contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(deploy_notify.main(["probe", "--url", "https://down/", "--version-url", "https://down/v.json"]), 0)
        self.assertEqual(out.getvalue().strip(), "unreachable, no version (HTTP 0)")


if __name__ == "__main__":
    unittest.main()
