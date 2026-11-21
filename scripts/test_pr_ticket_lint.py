# scripts/test_pr_ticket_lint.py — SSSF-72: does a PR cite a Linear ticket? Every case runs the real CLI
# (JSON on stdin, the shapes `gh pr view --json` and the pull_request event payload produce) or lint().
import json
import subprocess
import sys
import unittest

import deploy_notify
import pr_ticket_lint as lint

SCRIPT = lint.__file__


def run(pr=None, args=(), stdin=None):
    data = stdin if stdin is not None else json.dumps(pr)
    out = subprocess.run([sys.executable, SCRIPT, *args], input=data, capture_output=True, text=True)
    return out.returncode, out.stdout


def gh_pr(title="tidy the feed", body="", branch="tidy-feed", labels=(), author="blakejgruber", bot=False):
    """The `gh pr view --json title,body,headRefName,labels,author` shape."""
    return {"title": title, "body": body, "headRefName": branch,
            "labels": [{"name": n} for n in labels], "author": {"login": author, "is_bot": bot}}


def event_pr(title="tidy the feed", body=None, branch="tidy-feed", labels=(), user="blakejgruber"):
    """The pull_request event payload's .pull_request shape (body is null when empty)."""
    return {"title": title, "body": body, "head": {"ref": branch}, "labels": [{"name": n} for n in labels],
            "user": {"login": user, "type": "User"}}


class Cites(unittest.TestCase):
    def test_body_only(self):
        rc, out = run(gh_pr(body="Ships the shared notifier.\n\nSSSF-72"))
        self.assertEqual(rc, 0, out)
        self.assertIn("SSSF-72", out)

    def test_title_only(self):
        self.assertEqual(run(gh_pr(title="feed: drop the stale cache (OPS-1013)"))[0], 0)

    def test_branch_only_linear_git_branch_name(self):
        # Linear's gitBranchName is lowercase: <user>/<key>-<n>-<slug>
        rc, out = run(gh_pr(branch="blake/tool-532-facts-mcp-lockfile"))
        self.assertEqual(rc, 0, out)
        self.assertIn("TOOL-532", out)

    def test_event_payload_shape(self):
        self.assertEqual(run(event_pr(branch="sssf-72-shared-notify"))[0], 0)
        self.assertEqual(run(event_pr())[0], 1)

    def test_every_live_team_key_counts(self):
        for key in ("TOOL", "OPS", "CD", "RND", "AUTO", "DATA", "SSSF", "HDS", "BJG", "FIN", "YOU"):
            with self.subTest(key=key):
                self.assertEqual(run(gh_pr(body=f"{key}-7"))[0], 0)

    def test_fixes_prefix_still_cites(self):
        self.assertEqual(run(gh_pr(body="Fixes CD-12"))[0], 0)

    def test_janitor_pr_cites_through_its_body(self):
        # janitor_run.publish(): title `janitor: N review-queue fixes (<day>)`, branch janitor/<day>, and the
        # ticket ID only in the body (N6). No exemption needed.
        pr = gh_pr(title="janitor: 2 review-queue fixes (2026-11-21)", branch="janitor/2026-11-21",
                   body="Review queue: AUTO-61\n\n- fix one", author="app/bjg-agent", bot=True)
        self.assertEqual(run(pr)[0], 0)


class Missing(unittest.TestCase):
    def test_none_fails_with_one_error_line(self):
        rc, out = run(gh_pr(body="Small cleanup."))
        self.assertEqual(rc, 1)
        lines = out.strip().splitlines()
        self.assertEqual(len(lines), 1, out)
        self.assertTrue(lines[0].startswith("::error"), out)
        self.assertIn("title, body or branch", lines[0])

    def test_false_positive_strings_fail(self):
        body = ("Reads UTF-8, hashes with SHA-256, dates are ISO-8601, see RFC-2119 and PEP-8; "
                "patches CVE-2024-3094; runs on GPT-5 and X-1; fixes PR-12 and #12; macOS-15 runner")
        for where in ("title", "body", "branch"):
            with self.subTest(where=where):
                pr = gh_pr(**{where: body})
                self.assertEqual(run(pr)[0], 1)

    def test_key_glued_to_a_word_is_not_a_cite(self):
        for s in ("mops-12", "ops-0", "ops-", "nocd-4", "sssf72", "ops_12", "tool-x1"):
            with self.subTest(s=s):
                self.assertEqual(run(gh_pr(body=s))[0], 1)

    def test_bot_that_is_not_a_dependency_bot_is_not_exempt(self):
        # Agents open PRs as bjg-agent[bot]; exempting every bot would exempt nearly every PR.
        self.assertEqual(run(gh_pr(author="app/bjg-agent", bot=True))[0], 1)
        self.assertEqual(run(event_pr(user="bjg-agent[bot]"))[0], 1)
        self.assertEqual(run(event_pr(user="github-actions[bot]"))[0], 1)

    def test_warning_annotation_level(self):
        rc, out = run(gh_pr(), args=["--annotate", "warning"])
        self.assertEqual(rc, 1)
        self.assertTrue(out.startswith("::warning"), out)


class Exempt(unittest.TestCase):
    def test_hotfix_label(self):
        rc, out = run(gh_pr(labels=["hotfix"]))
        self.assertEqual(rc, 0, out)
        self.assertIn("exempt", out)
        self.assertEqual(run(event_pr(labels=["bug", "hotfix"]))[0], 0)

    def test_other_labels_do_not_exempt(self):
        self.assertEqual(run(gh_pr(labels=["bug", "hotfix-later"]))[0], 1)

    def test_dependency_bots(self):
        self.assertEqual(run(gh_pr(author="app/dependabot", bot=True))[0], 0)
        self.assertEqual(run(event_pr(user="dependabot[bot]"))[0], 0)
        self.assertEqual(run(event_pr(user="renovate[bot]"))[0], 0)

    def test_a_human_named_like_a_bot_is_not_exempt(self):
        self.assertEqual(run(event_pr(user="dependabot"))[0], 1)


class Input(unittest.TestCase):
    def test_args_instead_of_stdin(self):
        self.assertEqual(run(args=["--title", "x", "--body", "", "--branch", "ops-9-y"], stdin="")[0], 0)
        self.assertEqual(run(args=["--title", "x", "--label", "hotfix"], stdin="")[0], 0)
        self.assertEqual(run(args=["--title", "x", "--author", "dependabot[bot]"], stdin="")[0], 0)
        self.assertEqual(run(args=["--title", "x"], stdin="")[0], 1)

    def test_bad_input_is_exit_2_not_a_traceback(self):
        for data in ("", "not json", "[]", '"a string"', '{"title": 5}', '{"labels": "hotfix"}'):
            with self.subTest(data=data):
                rc, out = run(stdin=data)
                self.assertEqual(rc, 2, out)
                self.assertIn("::error", out)

    def test_null_fields_are_empty(self):
        self.assertEqual(run(stdin='{"title": "x", "body": null, "headRefName": null, "labels": null}')[0], 1)

    def test_lint_function(self):
        self.assertEqual(lint.lint({"title": "", "body": "see DATA-3 and OPS-4", "branch": "", "labels": [], "author": ""}),
                         (0, "pr_ticket_lint: ok (DATA-3 in body)"))


class Keys(unittest.TestCase):
    def test_every_deploy_key_is_a_lint_key(self):
        # deploy_notify.TEAM_KEYS ties deploys to tickets; a PR citing one of those must pass the lint.
        self.assertLessEqual(set(deploy_notify.TEAM_KEYS), set(lint.LINEAR_KEYS))


if __name__ == "__main__":
    unittest.main()
