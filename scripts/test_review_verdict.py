import os
import unittest
from datetime import datetime, timedelta, timezone

from review_verdict import compute, parse_summary, pushed_at, is_nudged, ts

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


HEX_HEAD = "abc1234def" + "0" * 30  # head for tests that need a Reviewed-commit SHA to prefix it


def named_clean(sha, minutes, author=CODEX):
    """Codex's real clean-review comment, naming the commit it reviewed (R35 shape)."""
    return {"author": author, "created_at": iso(T0 + timedelta(minutes=minutes)),
            "body": f"Codex Review: Didn't find any major issues. Nice work!\n\n**Reviewed commit:** `{sha}`"}


class VerdictShapes(unittest.TestCase):
    # R46: without a summary, only verdicts that name the head count -- a 👍 or a SHA-less
    # no-issues comment after the push may belong to an older head (the stale-verdict race).
    def test_thumbs_up_alone_is_not_a_verdict(self):
        r = {"user": CODEX, "content": "+1", "created_at": iso(T0 + timedelta(minutes=3))}
        self.assertEqual(compute(pr(reactions=[r]), T0 + timedelta(minutes=4))["state"], "pending")

    def test_sha_less_no_major_issues_comment_is_not_a_verdict(self):
        c = {"author": CODEX, "body": "Didn't find any major issues. Nice work!", "created_at": iso(T0 + timedelta(minutes=3))}
        self.assertEqual(compute(pr(comments=[c]), T0 + timedelta(minutes=4))["state"], "pending")

    def test_head_named_no_major_issues_comment_is_success_without_summary(self):
        v = compute(pr(head_sha=HEX_HEAD, comments=[named_clean("abc1234def", 3)]), T0 + timedelta(minutes=4))
        self.assertEqual(v["state"], "success")


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
    def test_bot_suffixed_comment_author_counts(self):  # REST reports Codex as ...[bot]
        c = named_clean("abc1234def", 3, author="chatgpt-codex-connector[bot]")
        self.assertEqual(compute(pr(head_sha=HEX_HEAD, comments=[c]), T0 + timedelta(minutes=4))["state"], "success")

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

    def test_no_summary_fallback_ignores_thumbs_up(self):  # R46 (was: succeeded on 👍)
        r = {"user": CODEX, "content": "+1", "created_at": iso(T0 + timedelta(minutes=3))}
        v = compute(pr(reactions=[r]), T0 + timedelta(minutes=4))
        self.assertEqual(v["state"], "pending")

    def test_no_summary_stale_thumbs_up_and_old_sha_no_issues_is_pending(self):  # R46
        # Summary out of the fetched window: a 👍 and a no-issues comment after the push,
        # but the comment reviewed the previous head. Neither is a verdict for this head.
        r = {"user": "chatgpt-codex-connector[bot]", "content": "+1", "created_at": iso(T0 + timedelta(minutes=2))}
        old = named_clean("0dd5a00000", 2)
        self.assertEqual(rv.reviewed_commit(old["body"]), "0dd5a00000")  # parses: rejected for the SHA, not the format
        p = pr(head_sha=HEX_HEAD, reactions=[r], comments=[old], summary=None)
        v = compute(p, T0 + timedelta(minutes=3))
        self.assertEqual(v["state"], "pending"); self.assertEqual(v["description"], "waiting for Codex review of abc1234")


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


# ---- R46: 👍 is no longer an input anywhere, so its REST fetch + mapper are gone -----
class ReactionsNotFetched(unittest.TestCase):
    def test_reactions_mapper_is_gone(self):
        import review_verdict
        self.assertFalse(hasattr(review_verdict, "reactions_from_rest"))


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
import contextlib
import io
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

    def test_stops_8_minutes_after_the_clock_start(self):  # SSSF-25 short poll (was 31)
        self.assertFalse(rv.poll_done(pr(), pending(), T0 + timedelta(minutes=7, seconds=59)))
        self.assertTrue(rv.poll_done(pr(), pending(), T0 + timedelta(minutes=8)))

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


class NoRereviewFlag(unittest.TestCase):  # R32's re-review flag stays gone; the nudge is its own subcommand
    def test_rereview_path_is_gone(self):
        self.assertFalse(hasattr(rv, "wants_rereview"))


class FakeGitHub:
    """Records the side effects run_once performs; hands back a fixed PR from fetch. Any
    other gh call (e.g. posting a comment) fails the test: run_once never comments (only the
    `nudge` subcommand does, on synchronize).
    fail_reply / fail_check: thread ids whose marker reply / fresh marker check raises like a
    failed gh call. queued_now: thread ids the fresh check finds already marked (another run
    replied after this run's fetch). A thread this fake replied to reads as marked from then on."""
    def __init__(self, prdict, fail_reply=(), fail_check=(), queued_now=()):
        self.prdict, self.calls, self.posted = prdict, [], []
        self.fail_reply, self.fail_check = set(fail_reply), set(fail_check)
        self.queued_now = set(queued_now)

    def install(self, test):
        def no_other_gh(*args, **kw):
            test.fail(f"unexpected gh call from run_once: {args[:4]}")
        for name, fn in (("fetch", self.fetch), ("post_status", self.post_status),
                         ("reply_thread", self.reply_thread), ("thread_is_queued", self.thread_is_queued),
                         ("post_comment", lambda *a, **k: test.fail("run_once must never comment")),
                         ("gh", no_other_gh)):
            orig = getattr(rv, name)
            setattr(rv, name, fn)
            test.addCleanup(setattr, rv, name, orig)
        return self

    def fetch(self, repo, n):
        self.calls.append(("fetch", repo, n)); return json.loads(json.dumps(self.prdict))

    def post_status(self, repo, sha, v):
        self.calls.append(("status", sha, v["state"])); self.posted.append(dict(v)); return "posted"

    def thread_is_queued(self, tid):
        self.calls.append(("check", tid))
        if tid in self.fail_check:
            raise subprocess.CalledProcessError(1, ["gh", "api", "graphql"], stderr="HTTP 502: Bad Gateway\n")
        return tid in self.queued_now

    def reply_thread(self, tid, body):
        self.calls.append(("reply", tid))
        if tid in self.fail_reply:
            raise subprocess.CalledProcessError(1, ["gh", "api", "graphql"], stderr="HTTP 502: Bad Gateway\n")
        self.queued_now.add(tid)


def run_once_capturing(g, now):
    """run_once with stdout captured (the annotations are workflow commands on stdout).
    Returns (run_once's result or the exception it raised, captured stdout)."""
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        try:
            result = rv.run_once("o/r", 7, now=now)
        except Exception as e:  # noqa: BLE001 -- the test inspects it
            return e, out.getvalue()
    return result, out.getvalue()


class RunOnce(unittest.TestCase):
    def test_posts_status_and_queues_threads_without_resolving_them(self):
        g = FakeGitHub(pr(reviews=[review(3)], threads=[thread("T2", 2, 3)])).install(self)
        _, v, _ = rv.run_once("o/r", 7, now=T0 + timedelta(minutes=4))
        self.assertEqual(v["state"], "success")
        # queue first, then publish (Codex P1 on BJGLLC/.github#6): success only once it is durable.
        # SSSF-25: fresh marker check, then the reply; no resolve (it needs contents: write).
        self.assertEqual(g.calls, [("fetch", "o/r", 7), ("check", "T2"), ("reply", "T2"), ("status", "new", "success")])

    def test_queue_reply_carries_the_janitor_marker(self):  # the reply IS the durable queue entry
        bodies = []
        g = FakeGitHub(pr(reviews=[review(3)], threads=[thread("T2", 2, 3)])).install(self)
        rv.reply_thread = lambda tid, body: bodies.append(body) or g.reply_thread(tid, body)
        rv.run_once("o/r", 7, now=T0 + timedelta(minutes=4))
        self.assertEqual(len(bodies), 1); self.assertIn(rv.QUEUE_MARK, bodies[0])

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


class QueueFailures(unittest.TestCase):  # SSSF-25: every thread is tried; a lost marker never passes
    NOW = T0 + timedelta(minutes=4)

    def two_queued(self, **fail):
        return FakeGitHub(pr(reviews=[review(3)], threads=[thread("T2", 2, 3), thread("T3", 3, 3)]), **fail).install(self)

    def test_the_gate_never_resolves_a_thread(self):
        # 9/28 decision "Drop the resolve": resolveReviewThread needs contents: write, callers
        # grant read (drill BJGLLC/.github#5). The marker reply is the queue entry; threads stay open.
        self.assertFalse(hasattr(rv, "resolve_thread"))
        with open(rv.__file__) as f:
            self.assertNotIn("resolveReviewThread(input", f.read())  # the mutation itself
        g = self.two_queued()
        result, out = run_once_capturing(g, self.NOW)
        self.assertEqual(result[1]["state"], "success", out)
        self.assertEqual([c for c in g.calls if c[0] == "reply"], [("reply", "T2"), ("reply", "T3")])

    def test_one_failed_thread_never_stops_the_others(self):
        # Live 9/28 (dotfiles #71): one failure aborted the loop, so threads 2 and 3 never got
        # their marker. A failed fresh check on T2 still leaves T2 replied (see QueueDedupe) and T3 tried.
        g = self.two_queued(fail_check={"T2"})
        run_once_capturing(g, self.NOW)
        self.assertEqual([c for c in g.calls if c[0] == "reply"], [("reply", "T2"), ("reply", "T3")])

    def test_failed_marker_reply_fails_the_run_after_every_thread_is_tried(self):
        # No marker = not queued = lost for the janitor unless someone notices: fail loudly,
        # but only after every other thread got its marker and the status is posted.
        g = self.two_queued(fail_reply={"T2"})
        result, out = run_once_capturing(g, self.NOW)
        self.assertIsInstance(result, rv.QueueError)
        self.assertIn("T2", str(result))
        self.assertEqual([c for c in g.calls if c[0] in ("status", "reply")],
                         [("reply", "T2"), ("reply", "T3"), ("status", "new", "failure")])
        err = [l for l in out.splitlines() if l.startswith("::error")]
        self.assertEqual(len(err), 1, out); self.assertIn("T2", err[0])

    def test_a_lost_marker_never_publishes_success(self):
        # Codex P1 on BJGLLC/.github#6: posting success BEFORE queueing let a PR auto-merge
        # while a finding was never recorded (a failed reply creates no follow-up event).
        # Queue first; a lost marker turns would-be success into failure with the remedy.
        g = self.two_queued(fail_reply={"T2"})
        run_once_capturing(g, self.NOW)
        self.assertEqual(len(g.posted), 1)
        self.assertEqual(g.posted[0]["state"], "failure")
        self.assertIn("-f pr=7", g.posted[0]["description"])
        self.assertLessEqual(len(g.posted[0]["description"]), 140)  # the statuses API limit

    def test_a_lost_marker_keeps_an_existing_blocking_failure(self):
        # Already failing on a P1: that description (the finding to fix) stays; the push that
        # fixes it re-runs the queue anyway.
        g = FakeGitHub(pr(reviews=[review(3)], threads=[thread("T1", 1, 3), thread("T2", 2, 3)]),
                       fail_reply={"T2"}).install(self)
        run_once_capturing(g, self.NOW)
        self.assertEqual(g.posted[0]["state"], "failure")
        self.assertIn("unresolved P0/P1", g.posted[0]["description"])

    def test_annotation_text_is_escaped_to_one_line(self):
        # A workflow command is one stdout line; a raw newline in gh's stderr would cut it.
        self.assertEqual(rv.annotation("warning", "a%b\r\nc"),
                         "::warning title=review-verdict janitor queue::a%25b%0D%0Ac")

    def test_the_poller_retries_a_failed_marker(self):
        # QueueError is an iteration failure like any gh error: the poller tries again, and the
        # next fetch still lists the unmarked thread in the queue.
        v, seen, _ = PollLoop.run_poll(self, [rv.QueueError(["T2"]), ("success", {})])
        self.assertEqual(v["state"], "success"); self.assertEqual(len(seen), 2)

    def test_cli_exits_nonzero_on_queue_error(self):
        def boom(repo, n):
            raise rv.QueueError(["T2"])
        orig = rv.run_once; rv.run_once = boom; self.addCleanup(setattr, rv, "run_once", orig)
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(rv.main(["rv", "o/r", "9"]), 1)


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


class NeedsSummaryScan(unittest.TestCase):  # R46: pure decision behind the marker scan
    SUMMARY = {"author": CODEX, "body": "<!-- codex-pull-request-review-summary -->\n| x |", "created_at": iso(T0)}
    OTHER = {"author": "blakejgruber", "body": "hi", "created_at": iso(T0)}

    def test_window_is_complete(self):
        self.assertFalse(rv.needs_summary_scan([self.OTHER], 1))

    def test_summary_in_window(self):
        self.assertFalse(rv.needs_summary_scan([self.SUMMARY, self.OTHER], 250))

    def test_older_comments_exist_and_no_summary_in_window(self):
        self.assertTrue(rv.needs_summary_scan([self.OTHER], 250))

    def test_a_human_quoting_the_marker_does_not_count_as_the_summary(self):
        quoted = dict(self.SUMMARY, author="blakejgruber")
        self.assertTrue(rv.needs_summary_scan([quoted], 250))


class FetchWiring(unittest.TestCase):
    def fetch_with(self, ready=(), comments=(), created=None, total=None, scanned=()):
        """ready = createdAt of READY_FOR_REVIEW / REOPENED timeline events; created = PR createdAt
        (default: an hour before the push, i.e. the PR predates its head); total = the PR's
        comments.totalCount (default: all of them are in the fetched window); scanned = what
        the full-history REST marker scan returns. self.calls records every gh call."""
        gql = {"data": {"repository": {"pullRequest": {
            "headRefOid": "abc", "isDraft": True, "state": "OPEN", "labels": {"nodes": [{"name": "x"}]},
            "createdAt": created or iso(T0 - timedelta(hours=1)),
            "commits": {"nodes": [{"commit": {"committedDate": iso(T0), "checkSuites": {"nodes": []}}}]},
            "timelineItems": {"nodes": [{"createdAt": r} for r in ready]},
            "reviews": {"nodes": []}, "reviewThreads": {"nodes": []},
            "comments": {"totalCount": len(comments) if total is None else total,
                         "nodes": [{"author": {"login": a}, "body": b, "createdAt": t} for a, b, t in comments]}}}}}
        self.calls = []

        def fake_gh(*args, stdin=None):
            self.calls.append(args)
            if "graphql" in args:
                return json.dumps(gql)
            if any("reactions" in a for a in args):
                self.fail("fetch must not read reactions any more (R46)")
            if any(a.endswith("/comments?per_page=100") for a in args):
                return "".join(json.dumps(s) + "\n" for s in scanned)
            self.fail(f"unexpected gh call: {args}")
        orig = rv.gh; rv.gh = fake_gh; self.addCleanup(setattr, rv, "gh", orig)
        return rv.fetch("o/r", 7)

    def rest_calls(self):
        return [c for c in self.calls if "graphql" not in c]

    # ---- R46: find Codex's summary even when it sits outside comments(last:100) ----
    def test_no_scan_when_the_window_holds_every_comment(self):
        got = self.fetch_with(comments=[("blakejgruber", "hi", iso(T0))])
        self.assertIsNone(got["summary"]); self.assertEqual(self.rest_calls(), [])

    def test_no_scan_when_the_summary_is_in_the_window(self):
        body = read_fixture("summary-completed.md")
        got = self.fetch_with(comments=[(CODEX, body, iso(T0))], total=150)
        self.assertEqual(got["summary"], {"status": "completed", "commit": "e176ac7"}); self.assertEqual(self.rest_calls(), [])

    def test_scan_finds_a_summary_outside_the_window(self):
        body = read_fixture("summary-completed.md")
        scanned = [{"author": "chatgpt-codex-connector[bot]", "body": body, "created_at": iso(T0 - timedelta(days=2))}]
        got = self.fetch_with(comments=[("blakejgruber", "hi", iso(T0))], total=150, scanned=scanned)
        self.assertEqual(got["summary"], {"status": "completed", "commit": "e176ac7"})
        (call,) = self.rest_calls()
        self.assertIn("--paginate", call); self.assertIn("repos/o/r/issues/7/comments?per_page=100", call)

    def test_scan_finding_nothing_leaves_no_summary(self):
        got = self.fetch_with(comments=[("blakejgruber", "hi", iso(T0))], total=150, scanned=[])
        self.assertIsNone(got["summary"]); self.assertEqual(len(self.rest_calls()), 1)

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
        # a head-named clean comment between the push and asked_at is still a verdict (no summary)
        p = pr(head_sha=HEX_HEAD, asked_at=iso(ASK), comments=[named_clean("abc1234def", 5)])
        self.assertEqual(compute(p, ASK + timedelta(minutes=1))["state"], "success")

    def test_asked_at_before_push_is_ignored(self):
        v = compute(pr(asked_at=iso(T0 - timedelta(hours=1))), T0 + timedelta(minutes=30))
        self.assertEqual(v["state"], "failure"); self.assertIn("codex-unavailable", v["description"])

    def test_poll_deadline_uses_the_later_of_push_and_asked_at(self):
        p = pr(asked_at=iso(ASK))
        self.assertFalse(rv.poll_done(p, pending(), ASK + timedelta(minutes=7, seconds=59)))
        self.assertTrue(rv.poll_done(p, pending(), ASK + timedelta(minutes=8)))

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


UNAVAILABLE_30 = "codex-unavailable: no verdict in 30 min. Retry `@codex review`, or label `hotfix` if urgent."


def poll_job_timeout():
    """The reusable workflow's poll job timeout-minutes (plain text: CI's python may lack PyYAML)."""
    lines = open(REUSABLE_WF).read().splitlines()
    start = lines.index("  poll:")
    for l in lines[start + 1:]:
        if l.strip().startswith("timeout-minutes:"):
            return timedelta(minutes=int(l.split(":")[1].split("#")[0]))
        if re.match(r"^  \S", l):
            break
    raise AssertionError("poll job has no timeout-minutes")


REUSABLE_WF = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".github", "workflows", "review-verdict.yml")


class SilentCodexReplay(unittest.TestCase):  # R23c/R23d; SSSF-25: the poller stops at +8, a recompute escalates
    def setUp(self):
        self.POLL_JOB_TIMEOUT = poll_job_timeout()

    def state_at(self, minutes, comments, ready=(), summary=None):
        """Rebuild the PR exactly as fetch() would (nudged from the comments, asked_at from push/ready)."""
        p = pr(comments=comments, nudged=rv.is_nudged(comments, iso(T0)), summary=summary,
               asked_at=rv.derive_asked_at(iso(T0), list(ready)))
        now = T0 + timedelta(minutes=minutes)
        v = compute(p, now)
        return p, v, now

    def recompute_at(self, minutes, comments, ready=()):
        """What a workflow_dispatch recompute (the `once` job -> run_once) POSTS at T0+minutes."""
        p, _, now = self.state_at(minutes, comments, ready)
        g = FakeGitHub(p).install(self)
        rv.run_once("o/r", 7, now=now)
        return g.posted[-1]

    def test_opened_no_codex_poller_stops_pending_at_8_and_a_recompute_escalates(self):
        # SSSF-25: the poller holds a runner for at most 8 min, then stops with the PR pending.
        p, v, now = self.state_at(8, [])
        self.assertEqual((v["state"], v["description"]), ("pending", WAIT_NEW))
        self.assertTrue(rv.poll_done(p, v, now))
        self.assertLess(now - T0, self.POLL_JOB_TIMEOUT)
        # Recomputes (dispatch or any later event) still ask at +10 and fail at +30, exactly as today.
        self.assertEqual(self.recompute_at(10, [])["description"], ASK_NEW)
        self.assertEqual(self.recompute_at(29, [])["state"], "pending")
        self.assertEqual(self.recompute_at(30, []), {"state": "failure", "description": UNAVAILABLE_30, "queue": []})

    def test_ready_for_review_no_codex_is_unavailable_30_min_after_ready(self):
        ready = [iso(T0 + timedelta(minutes=40))]  # poller starts at +40
        p, v, now = self.state_at(48, [], ready)
        self.assertEqual(v["state"], "pending"); self.assertTrue(rv.poll_done(p, v, now))
        self.assertEqual(self.recompute_at(69, [], ready)["state"], "pending")
        self.assertEqual(self.recompute_at(70, [], ready)["description"], UNAVAILABLE_30)

    def human_retry(self, minutes):
        return {"author": "blakejgruber", "body": "@codex review", "created_at": iso(T0 + timedelta(minutes=minutes))}

    def test_human_retry_at_20_does_not_restart_the_window(self):
        # Reviewer's scenario (R23d): push t=0, human retry t=+20, Codex silent. A retry never
        # moves the clock, so a recompute at +30 is codex-unavailable, not pending until +50.
        retry = [self.human_retry(20)]
        self.assertEqual(self.recompute_at(29, retry), {"state": "pending", "description": WAIT_NEW, "queue": []})
        self.assertEqual(self.recompute_at(30, retry)["description"], UNAVAILABLE_30)

    def test_the_gates_own_nudge_never_postpones_codex_unavailable(self):
        # The gate's `@codex review` counts as asked (no ask description) but moves no clock.
        nudge = [{"author": "github-actions", "body": rv.nudge_body("new"), "created_at": iso(T0 + timedelta(seconds=20))}]
        self.assertEqual(self.recompute_at(12, nudge)["description"], WAIT_NEW)
        self.assertEqual(self.recompute_at(30, nudge), {"state": "failure", "description": UNAVAILABLE_30, "queue": []})

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
        """From a clock start at T0+start_min (its own poller starts there): waiting at +5, the
        poller done at +8 (inside its job timeout); then recomputes ask for `@codex review` at
        +10 and post codex-unavailable at +30."""
        starts = [iso(T0 + timedelta(minutes=start_min))]
        _, v, _ = self.state_at(start_min + 5, [], starts)
        self.assertEqual(v["state"], "pending"); self.assertEqual(v["description"], WAIT_NEW)
        p, v, now = self.state_at(start_min + 7, [], starts)
        self.assertFalse(rv.poll_done(p, v, now))
        p, v, now = self.state_at(start_min + 8, [], starts)
        self.assertEqual(v["state"], "pending"); self.assertTrue(rv.poll_done(p, v, now))
        self.assertLess(now - rv.ts(starts[0]), self.POLL_JOB_TIMEOUT)
        self.assertEqual(self.recompute_at(start_min + 10, [], starts)["description"], ASK_NEW)
        self.assertEqual(self.recompute_at(start_min + 29, [], starts)["state"], "pending")
        self.assertEqual(self.recompute_at(start_min + 30, [], starts)["description"], UNAVAILABLE_30)

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
        # F1: a clean first pass has no Codex review submission (round_no 0) but reads "round 1"
        self.assertEqual(v["state"], "success"); self.assertEqual(v["description"], "round 1: no blocking findings")

    def test_no_issues_comment_naming_another_sha_is_not_a_verdict(self):
        v = compute(self.e8(body=E8_BODY.replace("5cf2a667d3", "deadbeef00")), T0 + timedelta(minutes=3))
        self.assertEqual(v["state"], "pending"); self.assertEqual(v["description"], "waiting for Codex review of 5cf2a66")

    def test_thumbs_up_alone_never_overrides_a_summary(self):  # R13 guard
        v = compute(self.e8(with_comment=False), T0 + timedelta(minutes=3))
        self.assertEqual(v["state"], "pending"); self.assertEqual(v["description"], "waiting for Codex review of 5cf2a66")


# ==== F1 (final review): C1 drill bypass, the real error text, the round label ============
import shutil
import tempfile

ORG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
REUSABLE = os.path.join(ORG, ".github", "workflows", "review-verdict.yml")
SELF_CALLER = os.path.join(ORG, ".github", "workflows", "review-verdict-self.yml")


def step_run_body(workflow_path, step_name_prefix):
    """The `run: |` block of the step whose `- name:` starts with step_name_prefix, dedented.
    Plain text parsing on purpose: CI's system python may lack PyYAML (the C1 test must run)."""
    lines = open(workflow_path).read().splitlines()
    start = next(i for i, l in enumerate(lines) if l.strip().startswith(f"- name: {step_name_prefix}"))
    run_at = next(i for i in range(start + 1, len(lines)) if lines[i].strip() == "run: |")
    indent = len(lines[run_at]) - len(lines[run_at].lstrip()) + 2
    body = []
    for l in lines[run_at + 1:]:
        if l.strip() and len(l) - len(l.lstrip()) < indent:
            break
        body.append(l[indent:])
    return "\n".join(body) + "\n"


@unittest.skipUnless(shutil.which("bash") and shutil.which("jq"), "needs bash + jq (as on ubuntu-latest)")
class DrillCanOnlyFail(unittest.TestCase):  # R39 / C1: `-f drill=clean` posted a required-passing success
    def run_drill(self, drill, p1_open_fixture=None):
        """Execute the reusable workflow's real drill step body with a stub `gh` (logs, never
        calls GitHub) and the real script + fixtures. Returns (exit code, status POSTs)."""
        tmp = tempfile.mkdtemp(); self.addCleanup(shutil.rmtree, tmp)
        shutil.copytree(os.path.join(ORG, "scripts", "drills"), os.path.join(tmp, "scripts", "drills"))
        shutil.copy(os.path.join(ORG, "scripts", "review_verdict.py"), os.path.join(tmp, "scripts"))
        if p1_open_fixture:
            shutil.copy(os.path.join(ORG, "scripts", "drills", p1_open_fixture), os.path.join(tmp, "scripts", "drills", "p1-open.json"))
        os.makedirs(os.path.join(tmp, "bin"))
        log = os.path.join(tmp, "gh.log")
        stub = os.path.join(tmp, "bin", "gh")
        with open(stub, "w") as f:
            f.write('#!/usr/bin/env bash\nif [ "$1 $2" = "pr view" ]; then echo headsha123; exit 0; fi\n'
                    f'printf "%s\\n" "$*" >> "{log}"\n')
        os.chmod(stub, 0o755)
        env = {"PATH": os.path.join(tmp, "bin") + os.pathsep + os.environ["PATH"], "HOME": tmp,
               "GH_TOKEN": "stub-not-a-token", "GITHUB_REPOSITORY": "o/r", "PR": "12", "DRILL": drill}
        r = subprocess.run(["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", step_run_body(REUSABLE, "Drill")],
                           cwd=tmp, env=env, capture_output=True, text=True)
        posts = open(log).read().splitlines() if os.path.exists(log) else []
        return r.returncode, [p for p in posts if "/statuses/" in p]

    def test_clean_drill_is_refused_and_posts_nothing(self):
        rc, posts = self.run_drill("clean")
        self.assertNotEqual(rc, 0); self.assertEqual(posts, [])

    def test_p1_open_drill_posts_failure(self):
        rc, posts = self.run_drill("p1-open")
        self.assertEqual(rc, 0); self.assertEqual(len(posts), 1)
        self.assertIn("state=failure", posts[0]); self.assertIn("description=DRILL p1-open: ", posts[0])

    def test_guard_stops_a_drill_whose_fixture_computes_success(self):
        rc, posts = self.run_drill("p1-open", p1_open_fixture="clean.json")
        self.assertNotEqual(rc, 0); self.assertEqual(posts, [])

    def test_unknown_drill_is_refused(self):
        rc, posts = self.run_drill("../../etc/passwd")
        self.assertNotEqual(rc, 0); self.assertEqual(posts, [])


class SelfCallerDrillInput(unittest.TestCase):  # R39: don't advertise a drill that no longer exists
    def test_self_caller_does_not_offer_clean(self):
        line = next(l for l in open(SELF_CALLER) if l.strip().startswith("drill:"))
        self.assertIn("p1-open", line); self.assertNotIn("clean", line)


REAL_ERROR = 'Codex Review: Something went wrong. Try again later by commenting "@codex review"'
F1_HEAD = "abcdef1234" + "0" * 30


class ErrorChannel(unittest.TestCase):  # final-review Minor 1
    def c(self, body, minutes, author=CODEX):
        return {"author": author, "body": body, "created_at": iso(T0 + timedelta(minutes=minutes))}

    def test_real_error_text_matches(self):
        self.assertTrue(rv.ERROR.search(REAL_ERROR))

    def test_error_alone_is_codex_unavailable(self):
        v = compute(pr(head_sha=F1_HEAD, comments=[self.c(REAL_ERROR, 2)]), T0 + timedelta(minutes=3))
        self.assertEqual(v["state"], "failure"); self.assertIn("codex-unavailable: Codex errored", v["description"])

    def test_error_then_retry_then_head_verdict_is_success(self):
        clean = "Codex Review: Didn't find any major issues. Nice work!\n\n**Reviewed commit:** `abcdef1234`"
        comments = [self.c(REAL_ERROR, 2), self.c("@codex review", 3, author="blakejgruber"), self.c(clean, 6)]
        p = pr(head_sha=F1_HEAD, comments=comments, summary={"status": "completed", "commit": "abcdef1"})
        v = compute(p, T0 + timedelta(minutes=7))
        self.assertEqual(v["state"], "success"); self.assertEqual(v["description"], "round 1: no blocking findings")

    def test_error_then_retry_then_head_sha_review_is_success(self):
        comments = [self.c(REAL_ERROR, 2), self.c("@codex review", 3, author="blakejgruber")]
        p = pr(head_sha=F1_HEAD, comments=comments, reviews=[review(6, sha=F1_HEAD)])
        self.assertEqual(compute(p, T0 + timedelta(minutes=7))["state"], "success")

    def test_error_then_retry_still_running_is_codex_unavailable(self):
        comments = [self.c(REAL_ERROR, 2), self.c("@codex review", 3, author="blakejgruber")]
        p = pr(head_sha=F1_HEAD, comments=comments, summary={"status": "running", "commit": "abcdef1"})
        self.assertEqual(compute(p, T0 + timedelta(minutes=4))["state"], "failure")


class RoundLabel(unittest.TestCase):  # F1: the label shows round >=1; blocking uses the true count
    def test_clean_first_pass_reads_round_1(self):
        p = pr(head_sha=HEX_HEAD, comments=[named_clean("abc1234def", 3)])
        self.assertEqual(compute(p, T0 + timedelta(minutes=4))["description"], "round 1: no blocking findings")

    def test_blocking_without_a_review_submission_reads_round_1(self):
        p = pr(head_sha=HEX_HEAD, comments=[named_clean("abc1234def", 3)], threads=[thread("T1", 1, 3)])
        v = compute(p, T0 + timedelta(minutes=4))
        self.assertEqual(v["state"], "failure"); self.assertTrue(v["description"].startswith("round 1: 1 unresolved P0/P1"))


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


# ==== SSSF-25 (9/28, Blake: "Nudge + short poll", "Drop the resolve"; reverses R32) ========
HEAD = "c0ffee1234" + "0" * 30


def asked(body, minutes, author="blakejgruber"):
    return {"author": author, "body": body, "created_at": iso(T0 + timedelta(minutes=minutes))}


def fetched(comments=(), **over):
    """A PR as fetch() returns it right after the push (nudged derived from the comments)."""
    comments = list(comments)
    return pr(**{"head_sha": HEAD, "state": "OPEN", "draft": False, "comments": comments,
                 "nudged": rv.is_nudged(comments, iso(T0)), **over})


def workflow():
    try:
        import yaml
    except ImportError:
        raise unittest.SkipTest("PyYAML not installed")
    with open(REUSABLE_WF) as f:
        return yaml.safe_load(f)


class NudgeDecision(unittest.TestCase):
    """On synchronize the gate posts one `@codex review` unless nobody needs it."""
    def test_a_fresh_push_nobody_asked_about_is_nudged(self):
        self.assertTrue(rv.nudge_decision(fetched())[0])

    def test_draft_hotfix_and_closed_are_skipped(self):
        for over in ({"draft": True}, {"labels": ["hotfix"]}, {"state": "CLOSED"}, {"state": "MERGED"}):
            with self.subTest(**{k: str(v) for k, v in over.items()}):
                post, why = rv.nudge_decision(fetched(**over))
                self.assertFalse(post); self.assertTrue(why)

    def test_someone_already_asked_after_the_push(self):  # any author, anywhere in the comment
        for body, author in (("@codex review", "blakejgruber"), ("Fixed in c0ffee1. @codex review", "claude-agent"),
                             ("@codex review", "github-actions")):
            with self.subTest(body=body, author=author):
                self.assertFalse(rv.nudge_decision(fetched([asked(body, 1, author)]))[0])

    def test_a_request_before_the_push_is_about_the_old_head(self):
        self.assertTrue(rv.nudge_decision(fetched([asked("@codex review", -1)]))[0])

    def test_codexs_own_boilerplate_is_not_a_request(self):
        # Codex's summary, clean-review and error comments all say: comment "@codex review".
        for body in ('Codex Review: Something went wrong. Try again later by commenting "@codex review"',
                     '<!-- codex-pull-request-review-summary -->\n- Comment "@codex review" or "@codex security review".'):
            with self.subTest(body=body[:30]):
                self.assertTrue(rv.nudge_decision(fetched([asked(body, 1, CODEX + "[bot]")]))[0])

    def test_the_gates_marker_for_this_head_skips_whatever_the_timestamps_say(self):
        # e.g. a re-run: the marker names the SHA, so it counts even when dated before the push
        mark = asked(rv.nudge_body(HEAD), -5, "github-actions")
        post, why = rv.nudge_decision(fetched([mark]))
        self.assertFalse(post); self.assertIn("already nudged", why)

    def test_the_marker_for_a_previous_head_does_not_count(self):
        self.assertTrue(rv.nudge_decision(fetched([asked(rv.nudge_body("0ld" + "1" * 37), -5, "github-actions")]))[0])


class NudgeBody(unittest.TestCase):
    def test_first_line_is_the_request_and_the_marker_is_hidden(self):
        body = rv.nudge_body(HEAD)
        self.assertEqual(body.splitlines()[0], "@codex review")
        self.assertIn(f"<!-- review-verdict:nudge {HEAD} -->", body)  # full SHA, an HTML comment
        self.assertEqual(body.count("@codex review"), 1)


class FakeNudgeGitHub:
    """A shared PR whose comment list grows when post_comment is called, so two runs see
    each other's comments (runs are serialized per PR by the poll job's concurrency group)."""
    def __init__(self, test, prdict):
        self.pr, self.posts = prdict, []
        for name, fn in (("fetch", self.fetch), ("post_comment", self.post_comment),
                         ("gh", lambda *a, **k: test.fail(f"unexpected gh call: {a[:4]}"))):
            orig = getattr(rv, name); setattr(rv, name, fn); test.addCleanup(setattr, rv, name, orig)
        self.now = T0 + timedelta(seconds=20)

    def fetch(self, repo, n):
        p = json.loads(json.dumps(self.pr))
        p["nudged"] = rv.is_nudged(p["comments"], p["head_pushed_at"])
        return p

    def post_comment(self, repo, n, body):
        self.posts.append((repo, n, body))
        self.pr["comments"].append({"author": "github-actions", "body": body, "created_at": iso(self.now)})


class RunNudge(unittest.TestCase):
    def quiet(self, fn, *a):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            return fn(*a), out.getvalue()

    def test_posts_exactly_one_request_with_the_head_marker(self):
        g = FakeNudgeGitHub(self, fetched())
        posted, _ = self.quiet(rv.run_nudge, "o/r", 7)
        self.assertTrue(posted)
        self.assertEqual(g.posts, [("o/r", 7, rv.nudge_body(HEAD))])

    def test_two_runs_one_second_apart_post_once(self):
        g = FakeNudgeGitHub(self, fetched())
        self.quiet(rv.run_nudge, "o/r", 7)
        g.now += timedelta(seconds=1)
        posted, out = self.quiet(rv.run_nudge, "o/r", 7)
        self.assertFalse(posted); self.assertEqual(len(g.posts), 1); self.assertIn("already", out)

    def test_skips_post_nothing(self):
        for p in (fetched(draft=True), fetched(labels=["hotfix"]), fetched([asked("@codex review", 1)])):
            g = FakeNudgeGitHub(self, p)
            self.assertFalse(self.quiet(rv.run_nudge, "o/r", 7)[0]); self.assertEqual(g.posts, [])

    def test_post_comment_uses_the_issue_comments_endpoint(self):
        calls = []
        orig = rv.gh; rv.gh = lambda *a, **k: calls.append(a) or "{}"; self.addCleanup(setattr, rv, "gh", orig)
        rv.post_comment("o/r", 7, "hi")
        self.assertEqual(calls, [("api", "-X", "POST", "repos/o/r/issues/7/comments", "-f", "body=hi")])


class NudgeCli(unittest.TestCase):
    def patch(self, name, fn):
        orig = getattr(rv, name); setattr(rv, name, fn); self.addCleanup(setattr, rv, name, orig)

    def test_nudge_subcommand(self):
        calls = []
        self.patch("run_nudge", lambda repo, n: calls.append((repo, n)) or True)
        self.assertEqual(rv.main(["rv", "nudge", "o/r", "5"]), 0)
        self.assertEqual(calls, [("o/r", 5)])

    def test_nudge_rejects_bad_args(self):
        self.patch("run_nudge", lambda *a: self.fail("must not run"))
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(rv.main(["rv", "nudge", "o/r"]), 2)
            self.assertEqual(rv.main(["rv", "nudge", "o/r", "5", "x"]), 2)

    def test_a_failed_nudge_exits_1_with_an_error_annotation_naming_the_remedy(self):
        def boom(repo, n):
            raise subprocess.CalledProcessError(1, ["gh"], stderr="HTTP 403: Resource not accessible by integration\n")
        self.patch("run_nudge", boom)
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(rv.main(["rv", "nudge", "o/r", "5"]), 1)
        err = [l for l in out.getvalue().splitlines() if l.startswith("::error")]
        self.assertEqual(len(err), 1, out.getvalue()); self.assertIn("@codex review", err[0]); self.assertIn("403", err[0])


class NudgeWiring(unittest.TestCase):
    def poll_steps(self):
        return workflow()["jobs"]["poll"]["steps"]

    def test_the_nudge_runs_in_the_poll_job_on_synchronize_only_before_the_poll(self):
        steps = self.poll_steps()
        runs = [s.get("run", "").splitlines() for s in steps]
        i = next(i for i, r in enumerate(runs) if 'python3 scripts/review_verdict.py nudge "$GITHUB_REPOSITORY" "$PR"' in r)
        j = next(i for i, r in enumerate(runs) if 'python3 scripts/review_verdict.py poll "$GITHUB_REPOSITORY" "$PR"' in r)
        self.assertLess(i, j)
        self.assertEqual(steps[i]["env"]["PR"], "${{ github.event.pull_request.number }}")
        cond = steps[i]["if"]
        self.assertIn("github.event.action == 'synchronize'", cond)
        for other in ("opened", "ready_for_review", "reopened"):  # Codex auto-reviews open / ready
            self.assertNotIn(other, cond)

    def test_a_failed_nudge_never_skips_the_poll(self):
        steps = self.poll_steps()
        poll = next(s for s in steps if "review_verdict.py poll" in s.get("run", ""))
        self.assertIn("!cancelled()", poll.get("if", ""))

    def test_poll_runs_are_serialized_per_pr(self):  # what makes the marker check race-free
        c = workflow()["jobs"]["poll"]["concurrency"]
        self.assertIn("github.event.pull_request.number", c["group"]); self.assertIs(c["cancel-in-progress"], True)

    def test_permissions_are_unchanged_no_caller_edit(self):
        # Posting a PR comment needs only pull-requests: write (probe, run 36483153709); the 11
        # byte-identical callers grant exactly these, and a reusable workflow cannot elevate.
        self.assertEqual(workflow()["permissions"], {"contents": "read", "checks": "read", "pull-requests": "write",
                                                     "statuses": "write", "issues": "read"})


class ShortPoll(unittest.TestCase):
    def test_window_is_8_minutes(self):
        self.assertEqual(rv.POLL_WINDOW, timedelta(minutes=8))
        self.assertFalse(hasattr(rv, "POLL_DEADLINE"))

    def test_job_timeout_covers_the_window_and_stays_short(self):
        # window + the last sleep + slack for the nudge, checkout and a slow fetch
        t = poll_job_timeout()
        self.assertGreaterEqual(t, rv.POLL_WINDOW + timedelta(seconds=rv.POLL_INTERVAL) + timedelta(minutes=2))
        self.assertLessEqual(t, timedelta(minutes=15))

    def test_dispatch_recompute_is_the_one_shot_path(self):
        once = workflow()["jobs"]["once"]
        self.assertIn("github.event_name != 'pull_request'", once["if"])
        run = next(s["run"] for s in once["steps"] if s.get("name", "").startswith("Compute + post"))
        self.assertEqual(run.strip(), 'python3 scripts/review_verdict.py "$GITHUB_REPOSITORY" "$PR"')


class QueueDedupe(unittest.TestCase):
    """A thread that carries the queued-for-janitor reply is never queued twice. cd-marketing
    #165: the poller and a pull_request_review one-shot both fetched the unmarked thread and
    replied 1 s apart. Marker checked at fetch (compute) AND right before the reply."""
    NOW = T0 + timedelta(minutes=4)

    def test_an_open_thread_with_the_marker_is_not_queued_again(self):
        t = thread("T2", 2, 3)
        t["comments"].append({"author": "github-actions", "author_type": "Bot", "body": rv.QUEUE_REPLY, "created_at": iso(T0 + timedelta(minutes=3)), "commit_sha": "new"})
        v = compute(pr(reviews=[review(3)], threads=[t]), self.NOW)
        self.assertEqual(v, {"state": "success", "description": "round 1: no blocking findings", "queue": []})

    def test_a_reply_that_landed_after_this_runs_fetch_is_not_repeated(self):
        g = FakeGitHub(pr(reviews=[review(3)], threads=[thread("T2", 2, 3)]), queued_now={"T2"}).install(self)
        result, out = run_once_capturing(g, self.NOW)
        self.assertNotIsInstance(result, Exception, out)
        self.assertNotIn(("reply", "T2"), g.calls)
        self.assertEqual(g.posted[-1]["state"], "success")

    def test_racing_runs_reply_once(self):
        # Both runs fetched before either replied (the #165 interleaving); they share GitHub.
        stale = pr(reviews=[review(3)], threads=[thread("T2", 2, 3)])
        a = FakeGitHub(stale).install(self)
        run_once_capturing(a, self.NOW)
        b = FakeGitHub(stale, queued_now=a.queued_now).install(self)
        run_once_capturing(b, self.NOW + timedelta(seconds=1))
        replies = [c for c in a.calls + b.calls if c[0] == "reply"]
        self.assertEqual(replies, [("reply", "T2")])

    def test_a_failed_fresh_check_still_replies(self):  # a duplicate reply beats a lost finding
        g = FakeGitHub(pr(reviews=[review(3)], threads=[thread("T2", 2, 3)]), fail_check={"T2"}).install(self)
        result, out = run_once_capturing(g, self.NOW)
        self.assertIn(("reply", "T2"), g.calls); self.assertEqual(g.posted[-1]["state"], "success")
        self.assertTrue(any(l.startswith("::warning") and "T2" in l for l in out.splitlines()), out)

    def test_thread_is_queued_reads_the_thread_fresh(self):
        seen = []
        def fake_gh(*args, **kw):
            seen.append(args)
            return json.dumps({"data": {"node": {"comments": {"nodes": [{"body": "x", "author": {"login": "u", "__typename": "User"}}, {"body": rv.QUEUE_REPLY, "author": {"login": "github-actions", "__typename": "Bot"}}]}}}})
        orig = rv.gh; rv.gh = fake_gh; self.addCleanup(setattr, rv, "gh", orig)
        self.assertTrue(rv.thread_is_queued("T9"))
        self.assertIn("id=T9", seen[0]); self.assertIn("graphql", seen[0])
        rv.gh = lambda *a, **k: json.dumps({"data": {"node": {"comments": {"nodes": [{"body": "x"}]}}}})
        self.assertFalse(rv.thread_is_queued("T9"))

    def test_one_shot_runs_are_serialized_per_pr(self):
        c = workflow()["jobs"]["once"]["concurrency"]
        for key in ("github.event.pull_request.number", "github.event.issue.number", "inputs.pr"):
            self.assertIn(key, c["group"])
        self.assertIs(c["cancel-in-progress"], False)  # queue behind, never kill a half-done queue
        self.assertNotEqual(c["group"], workflow()["jobs"]["poll"]["concurrency"]["group"])


class MarkerAuthorship(unittest.TestCase):  # SSSF-38: only the gate's own reply de-duplicates the janitor queue
    NOW = T0 + timedelta(minutes=4)

    def marked(self, author, body=None, p=1, typ="Bot", reviews=None):
        t = thread("T1", p, 3)
        t["comments"].append({"author": author, "author_type": typ, "body": rv.QUEUE_REPLY if body is None else body,
                              "created_at": iso(T0 + timedelta(minutes=3)), "commit_sha": "new"})
        return compute(pr(reviews=reviews or [review(3)], threads=[t]), self.NOW)

    # -- a marker never unblocks a P0/P1 (the gate only replies to queued P2/P3 threads)
    def test_a_human_reply_with_the_marker_does_not_unblock_a_p1(self):
        v = self.marked("blakejgruber", typ="User")
        self.assertEqual(v["state"], "failure"); self.assertIn("T1", v["description"])

    def test_an_agent_reply_with_the_marker_does_not_unblock_a_p0(self):
        self.assertEqual(self.marked("some-agent", p=0)["state"], "failure")

    def test_even_a_genuine_gate_marker_never_unblocks_a_round1_p1(self):  # a PR-added workflow posts as github-actions
        v = self.marked("github-actions", p=1)
        self.assertEqual(v["state"], "failure"); self.assertIn("T1", v["description"])

    def test_a_gate_marker_on_a_round3_p1_is_not_requeued(self):  # P1 is non-blocking from round 3
        v = self.marked("github-actions", p=1, reviews=[review(1, "a"), review(2, "b"), review(3, "new")])
        self.assertEqual(v["state"], "success"); self.assertEqual(v["queue"], [])

    def test_an_unmarked_round3_p1_is_queued(self):
        t = thread("T1", 1, 3)
        v = compute(pr(reviews=[review(1, "a"), review(2, "b"), review(3, "new")], threads=[t]), self.NOW)
        self.assertEqual(v["queue"], ["T1"])

    # -- the marker de-duplicates the P2/P3 queue, from the gate only
    def test_the_gate_marker_is_honored_on_a_p2(self):
        v = self.marked("github-actions", p=2)
        self.assertEqual((v["state"], v["queue"]), ("success", []))

    def test_the_gate_marker_is_honored_on_a_p3_with_the_bot_suffix(self):
        v = self.marked("github-actions[bot]", p=3)
        self.assertEqual((v["state"], v["queue"]), ("success", []))

    def test_leading_whitespace_before_the_reply_is_tolerated(self):
        self.assertEqual(self.marked("github-actions", p=2, body="\n  " + rv.QUEUE_REPLY)["queue"], [])

    def test_the_unowned_cutover_login_is_not_honored(self):  # SSSF-29 adds it when the account exists
        self.assertEqual(self.marked("bjg-gate", p=2)["queue"], ["T1"])

    def test_a_user_typed_github_actions_login_is_not_honored(self):
        self.assertEqual(self.marked("github-actions", p=2, typ="User")["queue"], ["T1"])

    def test_a_missing_author_type_is_not_honored(self):
        self.assertEqual(self.marked("github-actions", p=2, typ=None)["queue"], ["T1"])

    def test_a_human_marker_on_a_p3_is_queued_not_skipped(self):
        self.assertEqual(self.marked("blakejgruber", p=3, typ="User")["queue"], ["T1"])

    def test_look_alike_logins_are_not_honored(self):
        for who in ("github-actions-x", "xgithub-actions", "github-actions-bot"):
            with self.subTest(who=who):
                self.assertEqual(self.marked(who, p=2)["queue"], ["T1"])

    def test_body_shapes_that_are_not_the_gate_reply_are_not_honored(self):
        for body in (rv.QUEUE_MARK, f"note: this was {rv.QUEUE_MARK} earlier", "> " + rv.QUEUE_REPLY,
                     rv.QUEUE_REPLY[:-1], "x " + rv.QUEUE_REPLY):
            with self.subTest(body=body):
                self.assertEqual(self.marked("github-actions", p=2, body=body)["queue"], ["T1"])

    # -- the fresh pre-reply check
    def _fresh(self, nodes):
        def fake(*a, **k):
            self.assertTrue(any(rv.THREAD_COMMENTS in x for x in a), a)  # only the thread-comments query is faked
            return json.dumps({"data": {"node": {"comments": {"nodes": nodes}}}})
        orig = rv.gh; self.addCleanup(setattr, rv, "gh", orig)
        rv.gh = fake
        return rv.thread_is_queued("T9")

    def test_fresh_check_sees_a_gate_marker(self):
        self.assertTrue(self._fresh([{"body": rv.QUEUE_REPLY, "author": {"login": "github-actions", "__typename": "Bot"}}]))

    def test_fresh_check_ignores_a_non_gate_marker(self):
        self.assertFalse(self._fresh([{"body": rv.QUEUE_REPLY, "author": {"login": "blakejgruber", "__typename": "User"}}]))

    def test_fresh_check_ignores_a_user_typed_github_actions(self):
        self.assertFalse(self._fresh([{"body": rv.QUEUE_REPLY, "author": {"login": "github-actions", "__typename": "User"}}]))

    def test_fresh_check_tolerates_a_deleted_author(self):
        self.assertFalse(self._fresh([{"body": rv.QUEUE_REPLY, "author": None}]))

    def test_both_queries_select_the_author_type(self):
        self.assertIn("author{ login __typename }", rv.THREAD_COMMENTS)
        self.assertIn("author{login __typename}", rv.GQL)

    def test_a_forged_marker_does_not_stop_the_gate_replying(self):
        # the real fresh check reads a non-gate marker as absent, so queue_threads still replies
        real_check = rv.thread_is_queued
        g = FakeGitHub(pr(reviews=[review(3)], threads=[thread("T2", 2, 3)])).install(self)
        rv.thread_is_queued = real_check  # install()'s cleanup restores the original
        forged = json.dumps({"data": {"node": {"comments": {"nodes": [
            {"body": rv.QUEUE_REPLY, "author": {"login": "blakejgruber", "__typename": "User"}}]}}}})

        def only_thread_query(*a, **k):
            if not any(rv.THREAD_COMMENTS in x for x in a):
                self.fail(f"unexpected gh call: {a[:4]}")
            return forged
        rv.gh = only_thread_query  # install() already registered gh's cleanup
        result, out = run_once_capturing(g, self.NOW)
        self.assertIn(("reply", "T2"), g.calls, out)


class EditedFirstComment(unittest.TestCase):  # SSSF-38: a write-access actor edits Codex's badge P1 -> P3
    NOW = T0 + timedelta(minutes=4)

    def edited(self, editor, p=3):
        t = thread("T1", p, 3)
        t["comments"][0]["editor"] = editor
        return compute(pr(reviews=[review(3)], threads=[t]), self.NOW)

    def test_a_codex_comment_edited_by_a_human_blocks_whatever_the_badge_says(self):
        v = self.edited("blakejgruber")
        self.assertEqual(v["state"], "failure"); self.assertIn("T1", v["description"])

    def test_edited_by_codex_itself_is_normal(self):
        self.assertEqual(self.edited(CODEX)["state"], "success")
        self.assertEqual(self.edited(CODEX + "[bot]")["queue"], ["T1"])

    def test_an_edit_by_a_deleted_account_fails_closed(self):  # GitHub reports a null editor
        t = thread("T1", 3, 3); t["comments"][0]["editor"] = None; t["comments"][0]["last_edited_at"] = iso(T0)
        v = compute(pr(reviews=[review(3)], threads=[t]), self.NOW)
        self.assertEqual(v["state"], "failure"); self.assertIn("T1", v["description"])

    def test_no_editor_and_no_edit_time_is_normal(self):
        t = thread("T1", 3, 3); t["comments"][0]["editor"] = None; t["comments"][0]["last_edited_at"] = None
        self.assertEqual(compute(pr(reviews=[review(3)], threads=[t]), self.NOW)["queue"], ["T1"])

    def test_codex_as_editor_with_an_edit_time_is_normal(self):
        t = thread("T1", 3, 3); t["comments"][0]["editor"] = CODEX; t["comments"][0]["last_edited_at"] = iso(T0)
        self.assertEqual(compute(pr(reviews=[review(3)], threads=[t]), self.NOW)["queue"], ["T1"])

    def test_never_edited_is_normal(self):
        self.assertEqual(self.edited(None)["queue"], ["T1"])

    def test_a_tampered_thread_still_blocks_in_a_late_round(self):  # P0 blocks in every round
        t = thread("T1", 3, 3); t["comments"][0]["editor"] = "blakejgruber"
        v = compute(pr(reviews=[review(1, "a"), review(2, "b"), review(3, "new")], threads=[t]), self.NOW)
        self.assertEqual(v["state"], "failure")

    def test_fetch_carries_the_editor_and_the_author_type(self):
        gql = {"data": {"repository": {"pullRequest": {
            "headRefOid": "abc", "isDraft": False, "state": "OPEN", "labels": {"nodes": []},
            "createdAt": iso(T0 - timedelta(hours=1)),
            "commits": {"nodes": [{"commit": {"committedDate": iso(T0), "checkSuites": {"nodes": []}}}]},
            "timelineItems": {"nodes": []}, "reviews": {"nodes": []},
            "comments": {"totalCount": 0, "nodes": []},
            "reviewThreads": {"nodes": [{"id": "T1", "isResolved": False, "comments": {"nodes": [
                {"author": {"login": CODEX, "__typename": "Bot"}, "editor": {"login": "blakejgruber", "__typename": "User"},
                 "lastEditedAt": iso(T0), "body": BADGE.format(p=3), "createdAt": iso(T0), "commit": {"oid": "abc"}},
                {"author": {"login": "github-actions", "__typename": "Bot"}, "editor": None, "lastEditedAt": None,
                 "body": rv.QUEUE_REPLY, "createdAt": iso(T0), "commit": None}]}}]}}}}}
        orig = rv.gh; rv.gh = lambda *a, **k: json.dumps(gql); self.addCleanup(setattr, rv, "gh", orig)
        first, second = rv.fetch("o/r", 7)["threads"][0]["comments"]
        self.assertEqual((first["editor"], first["author_type"]), ("blakejgruber", "Bot"))
        self.assertEqual(first["last_edited_at"], iso(T0))
        self.assertEqual((second["editor"], second["author_type"]), (None, "Bot"))
        self.assertIsNone(second["last_edited_at"])

    def test_the_fetch_query_selects_the_editor(self):
        self.assertIn("editor{login __typename}", rv.GQL); self.assertIn("lastEditedAt", rv.GQL)


if __name__ == "__main__":
    unittest.main()
