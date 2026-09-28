import os
import unittest
from datetime import datetime, timedelta, timezone

from review_verdict import compute, parse_summary, pushed_at, is_nudged, ts, reactions_from_rest

CODEX = "chatgpt-codex-connector"
T0 = datetime(2026, 9, 26, 12, 0, tzinfo=timezone.utc)
iso = lambda dt: dt.strftime("%Y-%m-%dT%H:%M:%SZ")
BADGE = "<sub>![P{p} Badge](https://img.shields.io/badge/P{p}-orange?style=flat)</sub> finding"
FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")
# R32: pending descriptions for head "new" (the gate never comments; the description asks instead)
WAIT_NEW = "waiting for Codex review of new"
ASK_NEW = "no Codex review of new yet: comment @codex review"


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
        # R32: nothing queues the post-hoc review any more; the merging agent requests it
        self.assertEqual(v["description"], "hotfix: review skipped; comment @codex review after merge for the post-hoc review")


class NoVerdictYet(unittest.TestCase):
    def test_pending_in_first_ten_minutes(self):
        v = compute(pr(), T0 + timedelta(minutes=5))
        self.assertEqual(v["state"], "pending"); self.assertEqual(v["description"], WAIT_NEW)

    # R32: the 10-minute nudge comment became a description that asks for one
    def test_asks_for_a_review_request_after_ten_minutes(self):
        self.assertEqual(compute(pr(), T0 + timedelta(minutes=11))["description"], ASK_NEW)
        self.assertEqual(compute(pr(nudged=True), T0 + timedelta(minutes=11))["description"], WAIT_NEW)

    def test_verdict_has_no_nudge_key(self):
        self.assertEqual(set(compute(pr(), T0 + timedelta(minutes=11))), {"state", "description", "queue"})

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

    # R22: a round is a distinct reviewed SHA, so round 3 = three reviews on three SHAs.
    def test_round3_p1_is_queued_not_blocking(self):
        p = pr(reviews=[review(3, sha="a"), review(6, sha="b"), review(9)], threads=[thread("T9", 1, 9)])
        v = compute(p, T0 + timedelta(minutes=10))
        self.assertEqual(v["state"], "success"); self.assertEqual(v["queue"], ["T9"])

    def test_round3_p0_still_blocks(self):
        p = pr(reviews=[review(3, sha="a"), review(6, sha="b"), review(9)], threads=[thread("T9", 0, 9)])
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

    def test_running_summary_for_head_is_pending_and_does_not_ask(self):
        p = pr(summary={"status": "running", "commit": "new"})
        v = compute(p, T0 + timedelta(minutes=11))
        self.assertEqual(v["state"], "pending"); self.assertEqual(v["description"], WAIT_NEW)

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


# ==== Task 8 (SSSF-23): drafts, poll loop, re-review, drills ========================
# New names are reached through the module (rv.*) so the older tests above keep
# running while these are RED.
import json
import re
import subprocess
import review_verdict as rv

DRILLS = os.path.join(os.path.dirname(__file__), "drills")
DRAFT_DESC = "draft: Codex reviews when marked ready"


class Draft(unittest.TestCase):
    def test_draft_is_pending_with_draft_description(self):
        v = compute(pr(draft=True), T0 + timedelta(minutes=11))
        self.assertEqual(v, {"state": "pending", "description": DRAFT_DESC, "queue": []})

    def test_draft_is_never_codex_unavailable(self):
        v = compute(pr(draft=True), T0 + timedelta(hours=5))
        self.assertEqual(v["state"], "pending"); self.assertEqual(v["description"], DRAFT_DESC)

    def test_draft_ignores_findings_and_does_not_queue(self):
        v = compute(pr(draft=True, reviews=[review(3)], threads=[thread("T1", 1, 3), thread("T2", 2, 3)]),
                    T0 + timedelta(minutes=4))
        self.assertEqual(v["state"], "pending"); self.assertEqual(v["queue"], [])

    def test_hotfix_wins_over_draft(self):
        v = compute(pr(draft=True, labels=["hotfix"]), T0 + timedelta(minutes=1))
        self.assertEqual(v["state"], "success")

    def test_non_draft_unchanged(self):
        self.assertEqual(compute(pr(draft=False), T0 + timedelta(minutes=5))["state"], "pending")
        self.assertIn("waiting for Codex", compute(pr(draft=False), T0 + timedelta(minutes=5))["description"])


class ClosedPrNeverAsks(unittest.TestCase):
    def test_closed_or_merged_pr_does_not_ask_for_a_review(self):
        for state in ("CLOSED", "MERGED"):
            self.assertEqual(compute(pr(state=state), T0 + timedelta(minutes=11))["description"], WAIT_NEW, state)

    def test_open_pr_still_asks(self):
        self.assertEqual(compute(pr(state="OPEN"), T0 + timedelta(minutes=11))["description"], ASK_NEW)


def pending():
    return {"state": "pending", "description": "waiting", "queue": []}


class PollDone(unittest.TestCase):
    def test_keeps_polling_while_pending_before_deadline(self):
        self.assertFalse(rv.poll_done(pr(), pending(), T0 + timedelta(minutes=5)))

    def test_stops_on_any_non_pending_state(self):
        for state in ("success", "failure"):
            self.assertTrue(rv.poll_done(pr(), dict(pending(), state=state), T0 + timedelta(minutes=1)), state)

    def test_stops_at_31_minutes_after_push(self):
        self.assertFalse(rv.poll_done(pr(), pending(), T0 + timedelta(minutes=30, seconds=59)))
        self.assertTrue(rv.poll_done(pr(), pending(), T0 + timedelta(minutes=31)))

    def test_rerun_long_after_push_stops_immediately(self):
        self.assertTrue(rv.poll_done(pr(), pending(), T0 + timedelta(days=2)))

    def test_draft_stops_immediately_even_though_pending(self):
        # R16: a draft push must bill ~1 minute, not 31.
        p = pr(draft=True)
        v = compute(p, T0 + timedelta(seconds=30))
        self.assertEqual(v["state"], "pending")
        self.assertTrue(rv.poll_done(p, v, T0 + timedelta(seconds=30)))

    def test_closed_or_merged_mid_poll_stops(self):
        for state in ("CLOSED", "MERGED"):
            self.assertTrue(rv.poll_done(pr(state=state), pending(), T0 + timedelta(minutes=2)), state)


class NoBotComments(unittest.TestCase):  # R32: Codex ignores github-actions[bot] mentions
    def test_rereview_and_comment_paths_are_gone(self):
        for name in ("wants_rereview", "comment"):
            self.assertFalse(hasattr(rv, name), name)


class FakeGitHub:
    """Records the side effects run_once performs; hands back a fixed PR from fetch. Any
    other gh call (e.g. posting a comment) fails the test: the gate must never comment."""
    def __init__(self, prdict):
        self.prdict, self.calls = prdict, []

    def install(self, test):
        def no_other_gh(*args, **kw):
            test.fail(f"unexpected gh call from run_once: {args[:4]}")
        for name, fn in (("fetch", self.fetch), ("post_status", self.post_status),
                         ("resolve_thread", self.resolve_thread), ("gh", no_other_gh)):
            orig = getattr(rv, name)
            setattr(rv, name, fn)
            test.addCleanup(setattr, rv, name, orig)
        return self

    def fetch(self, repo, n):
        self.calls.append(("fetch", repo, n)); return json.loads(json.dumps(self.prdict))

    def post_status(self, repo, sha, v):
        self.calls.append(("status", sha, v["state"])); return "posted"

    def resolve_thread(self, tid, reply):
        self.calls.append(("resolve", tid))


class RunOnce(unittest.TestCase):
    def test_posts_status_and_resolves_queued_threads(self):
        g = FakeGitHub(pr(reviews=[review(3)], threads=[thread("T2", 2, 3)])).install(self)
        _, v, _ = rv.run_once("o/r", 7, now=T0 + timedelta(minutes=4))
        self.assertEqual(v["state"], "success")
        self.assertEqual(g.calls, [("fetch", "o/r", 7), ("status", "new", "success"), ("resolve", "T2")])

    def test_never_comments_even_when_a_review_is_due(self):  # R32
        # 11 min, no verdict, nobody asked: the old path posted `@codex review` here.
        g = FakeGitHub(pr()).install(self)
        _, v, _ = rv.run_once("o/r", 7, now=T0 + timedelta(minutes=11))
        self.assertEqual(v["description"], ASK_NEW)
        self.assertEqual(g.calls, [("fetch", "o/r", 7), ("status", "new", "pending")])

    def test_run_once_takes_no_rereview_flag(self):  # R32
        FakeGitHub(pr()).install(self)
        with self.assertRaises(TypeError):
            rv.run_once("o/r", 7, rereview=True, now=T0)


class PollLoop(unittest.TestCase):
    def run_poll(self, results, **kw):
        """results: list of (state, pr-overrides) or Exception; one per iteration."""
        seen, sleeps = [], []
        it = iter(results)

        def step(repo, n):  # no mode-specific input at all (R23b clock, R32 no re-review flag)
            seen.append(n)
            r = next(it)
            if isinstance(r, Exception):
                raise r
            state, over = r
            return pr(**over), dict(pending(), state=state), T0 + timedelta(minutes=len(seen))

        v = rv.poll("o/r", 7, step=step, sleep=sleeps.append, **kw)
        return v, seen, sleeps

    def test_stops_on_first_non_pending_and_sleeps_between(self):
        v, seen, sleeps = self.run_poll([("pending", {}), ("pending", {}), ("success", {})])
        self.assertEqual(v["state"], "success"); self.assertEqual(len(seen), 3)
        self.assertEqual(sleeps, [60, 60])

    def test_draft_ends_after_one_iteration(self):
        v, seen, sleeps = self.run_poll([("pending", {"draft": True})])
        self.assertEqual(len(seen), 1); self.assertEqual(sleeps, [])

    def test_transient_error_is_retried(self):
        err = subprocess.CalledProcessError(1, ["gh"], stderr="HTTP 502")
        v, seen, _ = self.run_poll([err, ("success", {})])
        self.assertEqual(v["state"], "success"); self.assertEqual(len(seen), 2)

    def test_poll_takes_no_rereview_flag(self):  # R32
        with self.assertRaises(TypeError):
            # the step accepts anything, so only poll()'s own signature can raise TypeError
            rv.poll("o/r", 7, rereview=True, step=lambda r, n, **kw: self.fail("must not run"), sleep=lambda s: None)

    def test_three_consecutive_errors_give_up(self):
        err = subprocess.CalledProcessError(1, ["gh"], stderr="HTTP 502")
        with self.assertRaises(subprocess.CalledProcessError):
            self.run_poll([("pending", {}), err, err, err, ("success", {})])

    def test_error_count_resets_after_a_good_iteration(self):
        err = subprocess.CalledProcessError(1, ["gh"], stderr="HTTP 502")
        v, seen, _ = self.run_poll([err, err, ("pending", {}), err, err, ("success", {})])
        self.assertEqual(v["state"], "success"); self.assertEqual(len(seen), 6)


class FetchWiring(unittest.TestCase):
    def fetch_with(self, ready=(), comments=(), created=None):
        """ready = createdAt of READY_FOR_REVIEW / REOPENED timeline events; created = PR createdAt
        (default: an hour before the push, i.e. the PR predates its head)."""
        gql = {"data": {"repository": {"pullRequest": {
            "headRefOid": "abc", "isDraft": True, "state": "OPEN", "labels": {"nodes": [{"name": "x"}]},
            "createdAt": created or iso(T0 - timedelta(hours=1)),
            "commits": {"nodes": [{"commit": {"committedDate": iso(T0), "checkSuites": {"nodes": []}}}]},
            "timelineItems": {"nodes": [{"createdAt": r} for r in ready]},
            "reviews": {"nodes": []}, "reviewThreads": {"nodes": []},
            "comments": {"nodes": [{"author": {"login": a}, "body": b, "createdAt": t} for a, b, t in comments]}}}}}

        def fake_gh(*args, stdin=None):
            return json.dumps(gql) if "graphql" in args else "[]"
        orig = rv.gh; rv.gh = fake_gh; self.addCleanup(setattr, rv, "gh", orig)
        return rv.fetch("o/r", 7)

    def test_fetch_reports_draft_and_state(self):
        got = self.fetch_with()
        self.assertIs(got["draft"], True); self.assertEqual(got["state"], "OPEN")
        self.assertEqual(got["head_sha"], "abc"); self.assertEqual(got["labels"], ["x"])

    def test_fetch_derives_asked_at_from_pr_state(self):  # R23b / R23d
        self.assertEqual(self.fetch_with()["asked_at"], iso(T0))
        self.assertEqual(self.fetch_with(ready=[iso(T0 + timedelta(minutes=40))])["asked_at"], iso(T0 + timedelta(minutes=40)))

    def test_review_requests_never_move_the_clock_but_still_count_as_nudged(self):  # R23d
        for login in ("blakejgruber", "github-actions", "github-actions[bot]"):
            got = self.fetch_with(ready=[iso(T0 + timedelta(minutes=40))],
                                  comments=[(login, "@codex review", iso(T0 + timedelta(minutes=75)))])
            self.assertEqual(got["asked_at"], iso(T0 + timedelta(minutes=40)), login)
            self.assertTrue(got["nudged"], login)
        got = self.fetch_with(created=iso(T0 + timedelta(minutes=45)),
                              comments=[("blakejgruber", "@codex review", iso(T0 + timedelta(minutes=60)))])
        self.assertEqual(got["asked_at"], iso(T0 + timedelta(minutes=45)))  # R23e: still only events

    def test_pr_opened_after_the_push_starts_the_clock(self):  # R23e
        self.assertEqual(self.fetch_with(created=iso(T0 + timedelta(minutes=45)))["asked_at"], iso(T0 + timedelta(minutes=45)))

    def test_reopen_event_starts_the_clock(self):  # R23e: timeline nodes carry READY_FOR_REVIEW and REOPENED alike
        self.assertEqual(self.fetch_with(ready=[iso(T0 + timedelta(hours=2))])["asked_at"], iso(T0 + timedelta(hours=2)))

    def test_query_asks_for_pr_created_at_and_both_event_types(self):  # R23e wiring
        self.assertRegex(rv.GQL, r"pullRequest\(number:\$n\)\{\s*headRefOid[^\n]*\bcreatedAt\b")
        self.assertIn("itemTypes:[READY_FOR_REVIEW_EVENT, REOPENED_EVENT]", rv.GQL)
        self.assertIn("... on ReopenedEvent{createdAt}", rv.GQL)


class CliDispatch(unittest.TestCase):
    def patch(self, name, fn):
        orig = getattr(rv, name); setattr(rv, name, fn); self.addCleanup(setattr, rv, name, orig)

    def test_poll_subcommand(self):
        calls = []
        self.patch("poll", lambda repo, n: calls.append((repo, n)) or pending())
        self.assertEqual(rv.main(["rv", "poll", "o/r", "5"]), 0)
        self.assertEqual(calls, [("o/r", 5)])

    def test_poll_rejects_extra_args_including_the_removed_rereview_flag(self):
        self.patch("poll", lambda *a, **k: self.fail("poll must not run"))
        for extra in (["--rereview"], ["--rereveiw"], ["--since", "2026-09-27T22:04:49Z"]):
            self.assertEqual(rv.main(["rv", "poll", "o/r", "5", *extra]), 2, extra)
        self.assertEqual(rv.main(["rv", "poll", "o/r"]), 2)

    def test_one_shot_subcommand_uses_run_once(self):
        calls = []
        self.patch("run_once", lambda repo, n: calls.append((repo, n)) or (pr(), pending(), T0))
        self.assertEqual(rv.main(["rv", "o/r", "9"]), 0)
        self.assertEqual(calls, [("o/r", 9)])


# ==== Fix round 1 ======================================================================
class Rounds(unittest.TestCase):  # R22
    def test_two_reviews_of_the_same_sha_are_one_round_and_p1_still_blocks(self):
        p = pr(reviews=[review(3), review(5), review(7)], threads=[thread("T1", 1, 3)])
        v = compute(p, T0 + timedelta(minutes=8))
        self.assertEqual(v["state"], "failure"); self.assertTrue(v["description"].startswith("round 1:"))

    def test_empty_commit_sha_review_is_its_own_round(self):
        # SHAs a + new = 2 rounds, plus two empty-SHA reviews = 4: P1 no longer blocks.
        p = pr(reviews=[review(1, sha="a"), review(2, sha=""), review(3, sha=""), review(4)],
               threads=[thread("T1", 1, 4)])
        v = compute(p, T0 + timedelta(minutes=5))
        self.assertEqual(v["state"], "success"); self.assertTrue(v["description"].startswith("round 4:"))

    def test_second_distinct_sha_is_round_2_and_p1_blocks(self):
        p = pr(reviews=[review(3, sha="a"), review(9)], threads=[thread("T9", 1, 9)])
        v = compute(p, T0 + timedelta(minutes=10))
        self.assertEqual(v["state"], "failure"); self.assertTrue(v["description"].startswith("round 2:"))


ASK = T0 + timedelta(minutes=40)  # e.g. draft -> ready 40 min after the push


class AskedAtClock(unittest.TestCase):  # R23
    def test_ready_40_min_after_push_is_pending_not_unavailable_and_not_asking(self):
        v = compute(pr(asked_at=iso(ASK)), ASK + timedelta(seconds=30))
        self.assertEqual(v["state"], "pending"); self.assertEqual(v["description"], WAIT_NEW)

    def test_asks_10_min_after_asked_at(self):
        self.assertEqual(compute(pr(asked_at=iso(ASK)), ASK + timedelta(minutes=9, seconds=59))["description"], WAIT_NEW)
        self.assertEqual(compute(pr(asked_at=iso(ASK)), ASK + timedelta(minutes=10))["description"], ASK_NEW)

    def test_unavailable_30_min_after_asked_at(self):
        self.assertEqual(compute(pr(asked_at=iso(ASK)), ASK + timedelta(minutes=29, seconds=59))["state"], "pending")
        v = compute(pr(asked_at=iso(ASK)), ASK + timedelta(minutes=30))
        self.assertEqual(v["state"], "failure"); self.assertIn("codex-unavailable", v["description"])

    def test_resolved_without_push_p1_still_blocks_when_asked_at_is_later(self):
        # `cleared` stays on head_pushed_at: the thread (T0+3) is after the push (T0), so
        # resolving it without a push must not clear it just because asked_at is later.
        p = pr(asked_at=iso(ASK), reviews=[review(3)], threads=[thread("T1", 1, 3, resolved=True)])
        self.assertEqual(compute(p, ASK + timedelta(minutes=1))["state"], "failure")

    def test_after_filter_stays_on_head_pushed_at(self):
        # a 👍 between the push and asked_at is still a verdict for this head (no summary)
        r = {"user": CODEX, "content": "+1", "created_at": iso(T0 + timedelta(minutes=5))}
        self.assertEqual(compute(pr(asked_at=iso(ASK), reactions=[r]), ASK + timedelta(minutes=1))["state"], "success")

    def test_asked_at_before_push_is_ignored(self):
        v = compute(pr(asked_at=iso(T0 - timedelta(hours=1))), T0 + timedelta(minutes=30))
        self.assertEqual(v["state"], "failure"); self.assertIn("codex-unavailable", v["description"])

    def test_poll_deadline_uses_the_later_of_push_and_asked_at(self):
        p = pr(asked_at=iso(ASK))
        self.assertFalse(rv.poll_done(p, pending(), ASK + timedelta(minutes=30, seconds=59)))
        self.assertTrue(rv.poll_done(p, pending(), ASK + timedelta(minutes=31)))

    def test_once_path_uses_the_fetched_asked_at(self):
        # Review Focus 4: the clock comes from fetch(), so a one-shot run (e.g. Codex's
        # "running" summary edit) right after draft -> ready posts what the poller posts.
        FakeGitHub(pr(asked_at=iso(ASK))).install(self)
        _, v, _ = rv.run_once("o/r", 7, now=ASK + timedelta(minutes=1))
        self.assertEqual(v["state"], "pending"); self.assertNotIn("codex-unavailable", v["description"])

    def test_same_pr_same_status_whichever_mode_runs(self):
        # Review Focus 4: a one-shot run and a poller pass on the same SHA at the same time post
        # the same status -- neither takes a mode-specific input (R23b clock, R32 no re-review).
        now = ASK + timedelta(minutes=11)
        FakeGitHub(pr(asked_at=iso(ASK), summary={"status": "completed", "commit": "new"})).install(self)
        _, once_v, _ = rv.run_once("o/r", 7, now=now)
        poll_v = rv.poll("o/r", 7, step=lambda r, n: rv.run_once(r, n, now=now), sleep=lambda s: None)
        self.assertEqual(once_v, poll_v); self.assertEqual(once_v["state"], "success")


class DeriveAskedAt(unittest.TestCase):  # R23d: max(push, latest draft -> ready); comments never count
    def test_none_present_is_head_pushed_at(self):
        self.assertEqual(rv.derive_asked_at(iso(T0), []), iso(T0))

    def test_push_wins_over_an_older_ready_event(self):
        self.assertEqual(rv.derive_asked_at(iso(T0), [iso(T0 - timedelta(minutes=5))]), iso(T0))

    def test_latest_ready_for_review_wins(self):
        got = rv.derive_asked_at(iso(T0), [iso(T0 + timedelta(minutes=20)), iso(T0 + timedelta(minutes=40))])
        self.assertEqual(got, iso(T0 + timedelta(minutes=40)))


class SilentCodexReplay(unittest.TestCase):  # R23c/R23d: every silent-Codex path ends inside the poll timeout
    POLL_JOB_TIMEOUT = timedelta(minutes=35)  # reusable workflow, poll job timeout-minutes

    def state_at(self, minutes, comments, ready=(), summary=None):
        """Rebuild the PR exactly as fetch() would (nudged from the comments, asked_at from push/ready)."""
        p = pr(comments=comments, nudged=rv.is_nudged(comments, iso(T0)), summary=summary,
               asked_at=rv.derive_asked_at(iso(T0), list(ready)))
        now = T0 + timedelta(minutes=minutes)
        v = compute(p, now)
        return p, v, now

    def test_opened_no_codex_asks_at_10_unavailable_at_30_inside_the_poll_timeout(self):
        # R32: nobody comments on the gate's behalf; the description asks from +10 on.
        _, v, _ = self.state_at(10, [])
        self.assertEqual(v["description"], ASK_NEW)
        _, v, _ = self.state_at(29, [])
        self.assertEqual(v["state"], "pending"); self.assertEqual(v["description"], ASK_NEW)
        p, v, now = self.state_at(30, [])
        self.assertEqual(v["state"], "failure"); self.assertIn("codex-unavailable", v["description"])
        self.assertTrue(rv.poll_done(p, v, now))
        self.assertLess(now - T0, self.POLL_JOB_TIMEOUT)

    def test_ready_for_review_no_codex_is_unavailable_30_min_after_ready(self):
        ready = [iso(T0 + timedelta(minutes=40))]  # poller starts at +40
        _, v, _ = self.state_at(69, [], ready)
        self.assertEqual(v["state"], "pending")
        p, v, now = self.state_at(70, [], ready)
        self.assertEqual(v["state"], "failure"); self.assertTrue(rv.poll_done(p, v, now))
        self.assertLess(now - (T0 + timedelta(minutes=40)), self.POLL_JOB_TIMEOUT)

    def human_retry(self, minutes):
        return {"author": "blakejgruber", "body": "@codex review", "created_at": iso(T0 + timedelta(minutes=minutes))}

    def test_human_retry_at_20_does_not_restart_the_window(self):
        # Reviewer's scenario (R23d): push t=0, human retry t=+20, Codex silent. Under R23c the
        # retry moved the deadline to +51, past the 35-min job timeout (killed while pending).
        retry = [self.human_retry(20)]
        _, v, _ = self.state_at(29, retry)
        self.assertEqual(v["state"], "pending"); self.assertEqual(v["description"], WAIT_NEW)  # someone asked
        p, v, now = self.state_at(30, retry)
        self.assertEqual(v["state"], "failure"); self.assertIn("codex-unavailable", v["description"])
        self.assertTrue(rv.poll_done(p, v, now))
        p31, v31, now31 = self.state_at(31, retry)
        self.assertTrue(rv.poll_done(p31, v31, now31))
        self.assertLess(now31 - T0, self.POLL_JOB_TIMEOUT)

    def test_retry_after_codex_unavailable_is_recomputed_by_codexs_summary_edit(self):
        # spec §6: the status stays codex-unavailable after a retry; Codex's summary edit
        # (an issue_comment event -> one-shot run) recomputes it once the review lands.
        retry = [self.human_retry(35)]
        _, v, _ = self.state_at(40, retry)
        self.assertEqual(v["state"], "failure"); self.assertIn("codex-unavailable", v["description"])
        _, v, _ = self.state_at(45, retry, summary={"status": "completed", "commit": "new"})
        self.assertEqual(v["state"], "success")

    # ---- R23e: PR creation and reopen are clock starts too -----------------------------
    def assert_relative_timings(self, start_min):
        """From a clock start at T0+start_min (its own poller starts there): waiting at +5,
        asking for `@codex review` at +10, codex-unavailable at +30 -- all inside that
        poller's 35-min timeout."""
        starts = [iso(T0 + timedelta(minutes=start_min))]
        _, v, _ = self.state_at(start_min + 5, [], starts)
        self.assertEqual(v["state"], "pending"); self.assertEqual(v["description"], WAIT_NEW)
        _, v, _ = self.state_at(start_min + 10, [], starts)
        self.assertEqual(v["state"], "pending"); self.assertEqual(v["description"], ASK_NEW)
        _, v, _ = self.state_at(start_min + 29, [], starts)
        self.assertEqual(v["state"], "pending")
        p, v, now = self.state_at(start_min + 30, [], starts)
        self.assertEqual(v["state"], "failure"); self.assertIn("codex-unavailable", v["description"])
        self.assertTrue(rv.poll_done(p, v, now))
        self.assertLess(now - rv.ts(starts[0]), self.POLL_JOB_TIMEOUT)

    def test_pr_opened_45_min_after_the_push(self):
        self.assert_relative_timings(45)   # the P1: previously codex-unavailable on the first poll

    def test_pr_reopened_2_hours_after_the_push(self):
        self.assert_relative_timings(120)

    def test_every_clock_start_ends_inside_the_poll_timeout(self):
        # push (synchronize) = 0; open / ready / reopen at arbitrary later times
        for start in (0, 7, 45, 120, 600):
            with self.subTest(start=start):
                self.assert_relative_timings(start)


class ClockStartsHaveAPoller(unittest.TestCase):  # R23e invariant
    """Every event that can set asked_at must also start a poller, or the deadline could fall
    outside any running poller: push -> synchronize, PR creation -> opened, READY_FOR_REVIEW_EVENT
    -> ready_for_review, REOPENED_EVENT -> reopened."""
    CLOCK_START_ACTIONS = {"synchronize", "opened", "ready_for_review", "reopened"}

    def test_poll_job_triggers_cover_every_clock_start(self):
        try:
            import yaml
        except ImportError:
            self.skipTest("PyYAML not installed")
        wf = os.path.join(os.path.dirname(__file__), "..", ".github", "workflows", "review-verdict.yml")
        with open(wf) as f:
            cond = yaml.safe_load(f)["jobs"]["poll"]["if"]
        m = re.search(r"fromJSON\('(\[.*?\])'\)", cond)
        self.assertIsNotNone(m, cond)
        self.assertEqual(set(json.loads(m.group(1))), self.CLOCK_START_ACTIONS)
        self.assertIn("github.event_name == 'pull_request'", cond)


# ==== Task 8d (R35): Codex's sticky summary can stay "Running" after a clean review ====
E8_HEAD = "5cf2a667d3" + "0" * 30
E8_BODY = "Codex Review: Didn't find any major issues. Nice work!\n\n**Reviewed commit:** `5cf2a667d3`"  # dotfiles #75


class ReviewedCommit(unittest.TestCase):
    def test_real_body(self):
        self.assertEqual(rv.reviewed_commit(E8_BODY), "5cf2a667d3")

    def test_no_sha(self):
        self.assertIsNone(rv.reviewed_commit("Codex Review: Didn't find any major issues. Nice work!"))

    def test_uppercase_sha_is_lowercased(self):
        self.assertEqual(rv.reviewed_commit(E8_BODY.replace("5cf2a667d3", "5CF2A667D3")), "5cf2a667d3")

    def test_sha_under_7_chars_is_none(self):
        self.assertIsNone(rv.reviewed_commit(E8_BODY.replace("5cf2a667d3", "5cf2a6")))


class StuckRunningSummary(unittest.TestCase):  # E8 replay (dotfiles #75, head 5cf2a66)
    def e8(self, body=E8_BODY, with_comment=True):
        thumbs = {"user": "chatgpt-codex-connector[bot]", "content": "+1", "created_at": iso(T0 + timedelta(minutes=2))}
        comments = [{"author": CODEX, "body": body, "created_at": iso(T0 + timedelta(minutes=2))}] if with_comment else []
        return pr(head_sha=E8_HEAD, summary={"status": "running", "commit": "5cf2a66"},
                  reactions=[thumbs], comments=comments)

    def test_head_named_no_issues_comment_is_a_verdict_despite_running_summary(self):
        v = compute(self.e8(), T0 + timedelta(minutes=3))
        self.assertEqual(v["state"], "success"); self.assertEqual(v["description"], "round 0: no blocking findings")

    def test_no_issues_comment_naming_another_sha_is_not_a_verdict(self):
        v = compute(self.e8(body=E8_BODY.replace("5cf2a667d3", "deadbeef00")), T0 + timedelta(minutes=3))
        self.assertEqual(v["state"], "pending"); self.assertEqual(v["description"], "waiting for Codex review of 5cf2a66")

    def test_thumbs_up_alone_never_overrides_a_summary(self):  # R13 guard
        v = compute(self.e8(with_comment=False), T0 + timedelta(minutes=3))
        self.assertEqual(v["state"], "pending"); self.assertEqual(v["description"], "waiting for Codex review of 5cf2a66")


class DrillFixtures(unittest.TestCase):
    def load(self, name):
        with open(os.path.join(DRILLS, name)) as f:
            return json.load(f)

    def test_p1_open_is_failure(self):
        p = self.load("p1-open.json")
        self.assertIsNone(p["summary"]); self.assertIs(p["draft"], False)
        self.assertEqual(p["reviews"][0]["commit_sha"], p["head_sha"])
        v = compute(p, datetime.now(timezone.utc))
        self.assertEqual(v["state"], "failure"); self.assertIn("unresolved P0/P1", v["description"])

    def test_clean_is_success(self):
        p = self.load("clean.json")
        self.assertIsNone(p["summary"]); self.assertIs(p["draft"], False); self.assertEqual(p["threads"], [])
        self.assertEqual(p["reviews"][0]["commit_sha"], p["head_sha"])
        self.assertEqual(compute(p, datetime.now(timezone.utc))["state"], "success")


if __name__ == "__main__":
    unittest.main()
