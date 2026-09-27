import os
import unittest
from datetime import datetime, timedelta, timezone

from review_verdict import compute, parse_summary, pushed_at, is_nudged, ts, reactions_from_rest

CODEX = "chatgpt-codex-connector"
T0 = datetime(2026, 9, 26, 12, 0, tzinfo=timezone.utc)
iso = lambda dt: dt.strftime("%Y-%m-%dT%H:%M:%SZ")
BADGE = "<sub>![P{p} Badge](https://img.shields.io/badge/P{p}-orange?style=flat)</sub> finding"
FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")


def pr(**over):
    base = {"head_sha": "new", "head_pushed_at": iso(T0), "labels": [], "reviews": [], "comments": [],
            "reactions": [], "threads": [], "nudged": False}
    base.update(over); return base


def review(minutes, sha="new"):
    return {"author": CODEX, "submitted_at": iso(T0 + timedelta(minutes=minutes)), "commit_sha": sha}


def thread(tid, p, minutes, resolved=False, sha="new"):
    return {"id": tid, "is_resolved": resolved, "comments": [
        {"author": CODEX, "body": BADGE.format(p=p), "created_at": iso(T0 + timedelta(minutes=minutes)), "commit_sha": sha}]}


def read_fixture(name):
    with open(os.path.join(FIXTURES, name)) as f:
        return f.read()


class Hotfix(unittest.TestCase):
    def test_label_short_circuits(self):
        v = compute(pr(labels=["hotfix"]), T0 + timedelta(minutes=1))
        self.assertEqual(v["state"], "success")
        self.assertEqual(v["description"], "hotfix: review skipped, post-hoc queued")


class NoVerdictYet(unittest.TestCase):
    def test_pending_in_first_ten_minutes(self):
        v = compute(pr(), T0 + timedelta(minutes=5))
        self.assertEqual(v["state"], "pending"); self.assertFalse(v["nudge"])

    def test_nudge_once_after_ten_minutes(self):
        self.assertTrue(compute(pr(), T0 + timedelta(minutes=11))["nudge"])
        self.assertFalse(compute(pr(nudged=True), T0 + timedelta(minutes=11))["nudge"])

    def test_unavailable_after_thirty_minutes(self):
        v = compute(pr(), T0 + timedelta(minutes=31))
        self.assertEqual(v["state"], "failure"); self.assertIn("codex-unavailable", v["description"])

    def test_error_comment_is_unavailable(self):
        c = {"author": CODEX, "body": "Codex encountered an error while reviewing", "created_at": iso(T0 + timedelta(minutes=2))}
        v = compute(pr(comments=[c]), T0 + timedelta(minutes=3))
        self.assertEqual(v["state"], "failure"); self.assertIn("codex-unavailable", v["description"])

    def test_old_review_is_not_a_verdict_for_new_head(self):
        v = compute(pr(reviews=[review(-30, sha="old")]), T0 + timedelta(minutes=5))
        self.assertEqual(v["state"], "pending")


class VerdictShapes(unittest.TestCase):
    def test_thumbs_up_reaction_is_success(self):
        r = {"user": CODEX, "content": "+1", "created_at": iso(T0 + timedelta(minutes=3))}
        self.assertEqual(compute(pr(reactions=[r]), T0 + timedelta(minutes=4))["state"], "success")

    def test_no_major_issues_comment_is_success(self):
        c = {"author": CODEX, "body": "Didn't find any major issues. Nice work!", "created_at": iso(T0 + timedelta(minutes=3))}
        self.assertEqual(compute(pr(comments=[c]), T0 + timedelta(minutes=4))["state"], "success")


class Blocking(unittest.TestCase):
    def test_p1_round1_blocks(self):
        v = compute(pr(reviews=[review(3)], threads=[thread("T1", 1, 3)]), T0 + timedelta(minutes=4))
        self.assertEqual(v["state"], "failure"); self.assertIn("T1", v["description"])

    def test_resolved_without_push_still_blocks(self):
        v = compute(pr(reviews=[review(3)], threads=[thread("T1", 1, 3, resolved=True)]), T0 + timedelta(minutes=4))
        self.assertEqual(v["state"], "failure")

    def test_resolved_with_newer_push_clears(self):
        p = pr(head_sha="new2", head_pushed_at=iso(T0 + timedelta(minutes=10)),
               reviews=[review(3, sha="new"), review(14, sha="new2")], threads=[thread("T1", 1, 3, resolved=True, sha="new")])
        self.assertEqual(compute(p, T0 + timedelta(minutes=15))["state"], "success")

    def test_p2_never_blocks_and_is_queued(self):
        v = compute(pr(reviews=[review(3)], threads=[thread("T2", 2, 3)]), T0 + timedelta(minutes=4))
        self.assertEqual(v["state"], "success"); self.assertEqual(v["queue"], ["T2"])

    def test_round3_p1_is_queued_not_blocking(self):
        p = pr(reviews=[review(3), review(6), review(9)], threads=[thread("T9", 1, 9)])
        v = compute(p, T0 + timedelta(minutes=10))
        self.assertEqual(v["state"], "success"); self.assertEqual(v["queue"], ["T9"])

    def test_round3_p0_still_blocks(self):
        p = pr(reviews=[review(3), review(6), review(9)], threads=[thread("T9", 0, 9)])
        self.assertEqual(compute(p, T0 + timedelta(minutes=10))["state"], "failure")

    def test_unbadged_codex_thread_counts_as_p2(self):
        t = {"id": "TX", "is_resolved": False, "comments": [{"author": CODEX, "body": "nit: rename", "created_at": iso(T0 + timedelta(minutes=3)), "commit_sha": "new"}]}
        v = compute(pr(reviews=[review(3)], threads=[t]), T0 + timedelta(minutes=4))
        self.assertEqual(v["state"], "success"); self.assertEqual(v["queue"], ["TX"])

    def test_human_threads_are_ignored(self):
        t = {"id": "TH", "is_resolved": False, "comments": [{"author": "blakejgruber", "body": BADGE.format(p=0), "created_at": iso(T0 + timedelta(minutes=3)), "commit_sha": "new"}]}
        self.assertEqual(compute(pr(reviews=[review(3)], threads=[t]), T0 + timedelta(minutes=4))["state"], "success")


# ---- Amendment A1: [bot] suffix normalization -------------------------------------
class BotSuffix(unittest.TestCase):
    def test_bot_suffixed_reaction_is_success(self):
        r = {"user": "chatgpt-codex-connector[bot]", "content": "+1", "created_at": iso(T0 + timedelta(minutes=3))}
        self.assertEqual(compute(pr(reactions=[r]), T0 + timedelta(minutes=4))["state"], "success")

    def test_bot_suffixed_review_author_counts_as_head_verdict(self):
        r = {"author": "chatgpt-codex-connector[bot]", "submitted_at": iso(T0 + timedelta(minutes=3)), "commit_sha": "new"}
        v = compute(pr(reviews=[r]), T0 + timedelta(minutes=4))
        self.assertEqual(v["state"], "success")

    def test_bot_suffixed_thread_comment_author_still_blocks_as_p1(self):
        t = {"id": "TB", "is_resolved": False, "comments": [
            {"author": "chatgpt-codex-connector[bot]", "body": BADGE.format(p=1), "created_at": iso(T0 + timedelta(minutes=3)), "commit_sha": "new"}]}
        v = compute(pr(reviews=[review(3)], threads=[t]), T0 + timedelta(minutes=4))
        self.assertEqual(v["state"], "failure"); self.assertIn("TB", v["description"])


# ---- Fix round 1: stale-verdict race (review focus 2) -----------------------------
class StaleVerdictRace(unittest.TestCase):
    def test_thumbs_up_after_push_with_completed_summary_for_old_commit_is_pending(self):
        # Summary exists and says "completed", but for a DIFFERENT (old) commit than
        # the current head -- the 👍 channel must be ignored once a summary exists.
        r = {"user": CODEX, "content": "+1", "created_at": iso(T0 + timedelta(minutes=3))}
        p = pr(summary={"status": "completed", "commit": "old"}, reactions=[r])
        self.assertEqual(compute(p, T0 + timedelta(minutes=4))["state"], "pending")

    def test_thumbs_up_after_push_with_running_summary_for_head_is_pending(self):
        # Summary exists for the head SHA but says "running" -- 👍 must still be ignored.
        r = {"user": CODEX, "content": "+1", "created_at": iso(T0 + timedelta(minutes=3))}
        p = pr(summary={"status": "running", "commit": "new"}, reactions=[r])
        self.assertEqual(compute(p, T0 + timedelta(minutes=4))["state"], "pending")

    def test_stale_review_submitted_after_push_is_not_a_verdict(self):
        # commit_sha="old" != head_sha "new"; submitted AFTER the push must NOT count
        # just because it's chronologically after the push (that was the bug).
        v = compute(pr(reviews=[review(3, sha="old")]), T0 + timedelta(minutes=4))
        self.assertEqual(v["state"], "pending")

    def test_no_summary_fallback_still_succeeds_on_thumbs_up(self):
        r = {"user": CODEX, "content": "+1", "created_at": iso(T0 + timedelta(minutes=3))}
        v = compute(pr(reactions=[r]), T0 + timedelta(minutes=4))
        self.assertEqual(v["state"], "success")


# ---- Minor (a): running summary for head still escalates at 30 min ----------------
class RunningSummaryEscalation(unittest.TestCase):
    def test_running_summary_for_head_still_escalates_to_failure_at_31_min(self):
        p = pr(summary={"status": "running", "commit": "new"})
        v = compute(p, T0 + timedelta(minutes=31))
        self.assertEqual(v["state"], "failure"); self.assertIn("codex-unavailable", v["description"])


# ---- Minor (c): pending description carries no minute count -----------------------
class PendingDescription(unittest.TestCase):
    def test_pending_description_is_stable_across_polls_and_has_no_minute_count(self):
        d1 = compute(pr(), T0 + timedelta(minutes=2))["description"]
        d2 = compute(pr(), T0 + timedelta(minutes=7))["description"]
        self.assertEqual(d1, d2)
        self.assertNotIn("min", d1)
        self.assertIn("new", d1)  # head_sha[:7]


# ---- Minor (f): blocking description puts count/remedy before the thread ids ------
class BlockingDescriptionFormat(unittest.TestCase):
    def test_count_and_remedy_precede_ids(self):
        v = compute(pr(reviews=[review(3)], threads=[thread("T1", 1, 3)]), T0 + timedelta(minutes=4))
        self.assertTrue(v["description"].startswith("round 1: 1 unresolved P0/P1 (push a fix): T1"))


# ---- Minor (d): the sticky summary comment itself never trips comment channels ----
class SummaryCommentExcludedFromChannels(unittest.TestCase):
    def test_full_summary_comment_body_does_not_trip_codex_unavailable(self):
        body = read_fixture("summary-completed.md")
        c = {"author": CODEX, "body": body, "created_at": iso(T0 + timedelta(minutes=3))}
        v = compute(pr(comments=[c], summary=None), T0 + timedelta(minutes=4))
        self.assertNotIn("codex-unavailable", v["description"])


# ---- Amendment (review focus 2, part 2): REST reactions mapping -------------------
class ReactionsFromRest(unittest.TestCase):
    def test_maps_realistic_rest_payload(self):
        sample = [
            {"id": 111, "node_id": "MDg6UmVhY3Rpb24x",
             "user": {"login": "chatgpt-codex-connector[bot]", "id": 1, "type": "Bot"},
             "content": "+1", "created_at": "2026-09-27T22:10:00Z"},
            {"id": 112, "node_id": "MDg6UmVhY3Rpb24y",
             "user": {"login": "blakejgruber", "id": 2, "type": "User"},
             "content": "heart", "created_at": "2026-09-27T22:11:00Z"},
        ]
        expected = [
            {"user": "chatgpt-codex-connector[bot]", "content": "+1", "created_at": "2026-09-27T22:10:00Z"},
            {"user": "blakejgruber", "content": "heart", "created_at": "2026-09-27T22:11:00Z"},
        ]
        self.assertEqual(reactions_from_rest(sample), expected)

    def test_handles_null_user(self):
        sample = [{"id": 1, "user": None, "content": "+1", "created_at": "2026-09-27T22:10:00Z"}]
        self.assertEqual(reactions_from_rest(sample), [{"user": "", "content": "+1", "created_at": "2026-09-27T22:10:00Z"}])


# ---- Amendment A2: sticky summary comment -----------------------------------------
class SummaryParsing(unittest.TestCase):
    def test_completed_fixture(self):
        s = parse_summary(read_fixture("summary-completed.md"))
        self.assertEqual(s, {"status": "completed", "commit": "e176ac7"})

    def test_in_progress(self):
        body = (
            "<!-- codex-pull-request-review-summary -->\n\n"
            "| Review | Status | Commit | Review trigger |\n"
            "| --- | --- | --- | --- |\n"
            "| \U0001f4dd **Code Review** | \U0001f504 **In Progress** | `abc1234` | Manual request |\n"
        )
        self.assertEqual(parse_summary(body), {"status": "running", "commit": "abc1234"})

    def test_error(self):
        body = (
            "<!-- codex-pull-request-review-summary -->\n\n"
            "| Review | Status | Commit | Review trigger |\n"
            "| --- | --- | --- | --- |\n"
            "| \U0001f4dd **Code Review** | ❌ **Failed** | `deadbee` | PR opened |\n"
        )
        self.assertEqual(parse_summary(body), {"status": "error", "commit": "deadbee"})

    def test_missing_marker_is_none(self):
        body = "Just a regular comment, no marker here, with a `deadbee` commit mention."
        self.assertIsNone(parse_summary(body))


class SummaryVerdict(unittest.TestCase):
    def test_completed_summary_for_head_is_success_with_no_threads(self):
        p = pr(summary={"status": "completed", "commit": "new"})
        self.assertEqual(compute(p, T0 + timedelta(minutes=4))["state"], "success")

    def test_completed_summary_for_old_commit_is_still_pending(self):
        p = pr(summary={"status": "completed", "commit": "old"})
        v = compute(p, T0 + timedelta(minutes=5))
        self.assertEqual(v["state"], "pending")

    def test_running_summary_for_head_is_pending_with_no_nudge(self):
        p = pr(summary={"status": "running", "commit": "new"})
        v = compute(p, T0 + timedelta(minutes=11))
        self.assertEqual(v["state"], "pending"); self.assertFalse(v["nudge"])

    def test_error_summary_for_head_is_codex_unavailable_failure(self):
        p = pr(summary={"status": "error", "commit": "new"})
        v = compute(p, T0 + timedelta(minutes=4))
        self.assertEqual(v["state"], "failure"); self.assertIn("codex-unavailable", v["description"])

    def test_head_sha_review_counts_as_verdict_despite_clock_skew(self):
        # submitted_at is BEFORE head_pushed_at (clock skew), but commit_sha == head_sha
        r = {"author": CODEX, "submitted_at": iso(T0 - timedelta(minutes=1)), "commit_sha": "new"}
        v = compute(pr(reviews=[r]), T0 + timedelta(minutes=2))
        self.assertEqual(v["state"], "success")


# ---- Amendment A3: head_pushed_at / pushed_at() / ts() fractional seconds ---------
class PushedAtHelper(unittest.TestCase):
    def test_earliest_github_actions_check_suite_wins(self):
        node = {"commit": {"committedDate": "2026-09-27T02:37:02Z", "checkSuites": {"nodes": [
            {"createdAt": "2026-09-27T02:39:58Z", "app": {"slug": "github-actions"}},
            {"createdAt": "2026-09-27T02:40:10Z", "app": {"slug": "github-actions"}},
            {"createdAt": "2026-09-27T02:38:00Z", "app": {"slug": "some-other-app"}},
        ]}}}
        self.assertEqual(pushed_at(node), "2026-09-27T02:39:58Z")

    def test_falls_back_to_committed_date_without_matching_suites(self):
        node = {"commit": {"committedDate": "2026-09-27T02:37:02Z", "checkSuites": {"nodes": [
            {"createdAt": "2026-09-27T02:38:00Z", "app": {"slug": "some-other-app"}},
        ]}}}
        self.assertEqual(pushed_at(node), "2026-09-27T02:37:02Z")

    def test_falls_back_with_no_check_suites_at_all(self):
        node = {"commit": {"committedDate": "2026-09-27T02:37:02Z", "checkSuites": {"nodes": []}}}
        self.assertEqual(pushed_at(node), "2026-09-27T02:37:02Z")

    def test_ts_tolerates_fractional_seconds(self):
        parsed = ts("2026-09-27T22:04:49.277256Z")
        self.assertEqual(parsed.replace(microsecond=0), ts("2026-09-27T22:04:49Z"))
        self.assertEqual(parsed.microsecond, 277256)


# ---- Amendment A4: nudged is per-SHA ------------------------------------------------
class NudgedHelper(unittest.TestCase):
    def test_nudge_comment_after_push_is_true(self):
        comments = [{"author": "github-actions", "body": "@codex review", "created_at": iso(T0 + timedelta(minutes=1))}]
        self.assertTrue(is_nudged(comments, iso(T0)))

    def test_nudge_comment_by_any_author_counts(self):
        comments = [{"author": "blakejgruber", "body": "@codex review please", "created_at": iso(T0 + timedelta(minutes=1))}]
        self.assertTrue(is_nudged(comments, iso(T0)))

    def test_nudge_comment_before_push_does_not_count(self):
        comments = [{"author": "github-actions", "body": "@codex review", "created_at": iso(T0 - timedelta(minutes=1))}]
        self.assertFalse(is_nudged(comments, iso(T0)))

    def test_unrelated_comment_does_not_count(self):
        comments = [{"author": "github-actions", "body": "unrelated", "created_at": iso(T0 + timedelta(minutes=1))}]
        self.assertFalse(is_nudged(comments, iso(T0)))


if __name__ == "__main__":
    unittest.main()
