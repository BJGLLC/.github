#!/usr/bin/env python3
"""review-verdict: turn Codex's advisory review into one deterministic commit status.

Pure part: compute(pr, now), parse_summary(body), pushed_at(commit_node), is_nudged(comments, head_pushed_at),
poll_done(pr, verdict, now), clock_start(pr), derive_asked_at(...).
Fetch/act part: fetch(repo, number) via `gh`; run_once = fetch -> compute -> post status ->
queue_threads (marker reply, then resolve); poll = run_once every 60 s until poll_done.
The gate never asks Codex (R32): it only replies `queued-for-janitor` on the P2/P3 threads it
resolves. When a review is due, the pending status description asks a human or agent to
comment `@codex review`; agents also request re-reviews and the post-hoc review of a hotfix
merge. (Codex's summary edit is flaky (R35); a bot's mention did get a review on #72.)
The reusable workflow (.github/workflows/review-verdict.yml) runs `poll` after a push and the
one-shot `<repo> <n>` for every other event. Spec: claude-dotfiles
docs/superpowers/specs/2026-09-26-review-system-v4-design.md §6.
No secrets here: this repo is public and the token comes from the caller's GITHUB_TOKEN env.
"""
import json
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone

CODEX = "chatgpt-codex-connector"
NUDGE_AFTER = timedelta(minutes=10)    # after this with no verdict and nobody asking, the status asks
UNAVAILABLE_AFTER = timedelta(minutes=30)
POLL_DEADLINE = timedelta(minutes=31)  # a poller never waits past clock_start + 31 min
POLL_INTERVAL = 60                     # seconds between poll iterations
POLL_MAX_ERRORS = 3                    # consecutive failed iterations before the poller gives up
DRAFT_DESC = "draft: Codex reviews when marked ready"
HOTFIX_DESC = "hotfix: review skipped; comment @codex review after merge for the post-hoc review"
REVIEW_REQUEST = "@codex review"
BADGE = re.compile(r"P([0-3]) Badge\]")
NO_ISSUES = re.compile(r"didn'?t find any major issues", re.I)
REVIEWED_COMMIT = re.compile(r"reviewed\s+commit\W*?([0-9a-f]+)(?![0-9a-z])", re.I)
# Codex's real error comment (intranet #59, file-mine #14-#17): 'Codex Review: Something went
# wrong. Try again later by commenting "@codex review"'. The older phrasings stay as fallbacks.
ERROR = re.compile(r"codex review:\s*something went wrong|codex (encountered an error|was unable|could not|failed)", re.I)
QUEUE_MARK = "queued-for-janitor"
CODEX_UNAVAILABLE_DESC = "codex-unavailable: Codex errored. Retry `@codex review`, or label `hotfix` if urgent."
SUMMARY_MARKER = "<!-- codex-pull-request-review-summary -->"


def ts(s):
    s = s.strip()
    fmt = "%Y-%m-%dT%H:%M:%S.%fZ" if "." in s else "%Y-%m-%dT%H:%M:%SZ"
    return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)


def _normalize_login(name):
    """Strip a trailing '[bot]' suffix so review/comment/thread authors compare correctly
    against CODEX regardless of whether GitHub reported the App identity (GraphQL:
    'chatgpt-codex-connector') or the bot identity (REST, e.g. the summary scan:
    'chatgpt-codex-connector[bot]')."""
    if not name:
        return name
    return name[:-5] if name.endswith("[bot]") else name


def is_codex(name):
    return _normalize_login(name) == CODEX


def priority(thread):
    first = thread["comments"][0]["body"]
    m = BADGE.search(first)
    return int(m.group(1)) if m else 2


def reviewed_commit(body):
    """The SHA in a Codex comment's `**Reviewed commit:** `5cf2a667d3`` line, lowercase, or
    None when absent or shorter than 7 hex chars (too short to name a commit safely)."""
    m = REVIEWED_COMMIT.search(body)
    return m.group(1).lower() if m and len(m.group(1)) >= 7 else None


def _summary_status_word(status_cell):
    m = re.search(r"\*\*(.+?)\*\*", status_cell)
    word = m.group(1) if m else status_cell
    if re.search(r"completed", word, re.I):
        return "completed"
    if re.search(r"progress|running|queued|pending|reviewing|started", word, re.I):
        return "running"
    if re.search(r"fail|error|cancel|unable", word, re.I):
        return "error"
    return "unknown"


def parse_summary(body):
    """Parse Codex's sticky per-PR summary comment (marked by SUMMARY_MARKER) into
    {"status": ..., "commit": ...}, or None if the marker is absent. Reads the
    'Code Review' table row: commit = the backticked short sha in the Commit column,
    status = the bold word(s) in the Status column, mapped per amendment A2."""
    if SUMMARY_MARKER not in body:
        return None
    for line in body.splitlines():
        line = line.strip()
        if not (line.startswith("|") and "Code Review" in line):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) < 3:
            continue
        status = _summary_status_word(cells[1])
        m = re.search(r"`([0-9a-fA-F]+)`", cells[2])
        commit = m.group(1) if m else None
        return {"status": status, "commit": commit}
    return None


def is_open(pr):
    """fetch() reports GraphQL PullRequest.state (OPEN|CLOSED|MERGED); fixtures without it are open."""
    return pr.get("state", "OPEN") == "OPEN"


def clock_start(pr):
    """R23: when the wait for Codex began -- the later of the head push and `asked_at`
    (derived by fetch() from PR state, see derive_asked_at; absent in older fixtures). It
    drives only the nudge / codex-unavailable ages and the poll deadline; after() filtering
    and the thread `cleared` rule stay on head_pushed_at, so a later asked_at can never
    clear a thread that was resolved without a push."""
    pushed = ts(pr["head_pushed_at"])
    asked = pr.get("asked_at")
    return max(pushed, ts(asked)) if asked else pushed


def compute(pr, now):
    """The verdict {state, description, queue} for the PR's head. Pure: every mode (poll,
    one-shot, dispatch) feeds it the same fetch() output, so two runs on the same SHA post
    the same status. The ask (10 min) and codex-unavailable (30 min) ages run from
    clock_start(pr). The gate never asks Codex (R32; it only replies `queued-for-janitor` on
    P2/P3 threads it resolves): once a review is due and nobody has asked, the pending
    description asks for `@codex review` instead. An error comment counts only when no head
    verdict exists, so a successful retry wins over an earlier error. A retry does NOT
    restart the window: the status stays codex-unavailable until Codex's summary edit (an
    issue_comment event -> one-shot run) recomputes it once the review lands (spec §6)."""
    if "hotfix" in pr.get("labels", []):
        return {"state": "success", "description": HOTFIX_DESC, "queue": []}

    # Codex does not review drafts (it reviews on draft -> ready), so a draft has no
    # verdict to wait for: pending (drafts cannot merge anyway), never codex-unavailable,
    # never asking, nothing queued. ready_for_review starts a fresh poller.
    if pr.get("draft"):
        return {"state": "pending", "description": DRAFT_DESC, "queue": []}

    head_sha = pr["head_sha"]
    pushed = ts(pr["head_pushed_at"])
    after = lambda s: ts(s) > pushed

    reviews = [r for r in pr["reviews"] if is_codex(r["author"])]
    # The summary comment itself (marker text) never counts toward the error /
    # no-major-issues comment channels -- it's Codex's own status board, not a finding.
    head_comments = [c for c in pr["comments"]
                      if is_codex(c["author"]) and after(c["created_at"]) and SUMMARY_MARKER not in c["body"]]

    summary = pr.get("summary")
    summary_for_head = bool(summary and summary.get("commit") and head_sha.startswith(summary["commit"]))
    summary_status = summary.get("status") if summary_for_head else None

    # Checked only AFTER the verdict (final review, Minor 1): an error comment followed by a
    # retry that lands a head verdict must not stay red forever. No verdict -> fail at once.
    errored = any(ERROR.search(c["body"]) for c in head_comments) or summary_status == "error"

    # Stale-verdict race fix (review focus 2): a Codex review only counts as a verdict
    # for THIS head when its commit_sha actually matches -- being merely "submitted
    # after the push" is not enough (that let a stale review on an old commit pass an
    # unreviewed new head). When Codex's sticky summary exists it is authoritative, except
    # (R35) a no-issues comment naming the head as its Reviewed commit also counts, since
    # Codex's summary edit is flaky (E8: stuck "Running" after a clean review). Without a
    # summary (none yet, or -- before R46 -- one outside the fetched window) the same SHA-named
    # comment, a same-SHA review or an empty-sha-after-push review are the fallback. A 👍 or a
    # SHA-less no-issues comment is never a verdict (R46): after a push it may still be about
    # the previous head -- the stale-verdict race again, on PRs with 100+ comments.
    has_head_sha_review = any(r.get("commit_sha") == head_sha for r in reviews)
    head_named_no_issues = any(
        NO_ISSUES.search(c["body"]) and (rc := reviewed_commit(c["body"])) and head_sha.lower().startswith(rc)
        for c in head_comments)
    if summary is not None:
        has_verdict = (summary_status == "completed") or has_head_sha_review or head_named_no_issues
    else:
        empty_sha_review_after_push = any(
            not r.get("commit_sha") and after(r["submitted_at"]) for r in reviews)
        has_verdict = has_head_sha_review or empty_sha_review_after_push or head_named_no_issues

    if not has_verdict:
        if errored:
            return {"state": "failure", "description": CODEX_UNAVAILABLE_DESC, "queue": []}
        age = now - clock_start(pr)
        if age >= UNAVAILABLE_AFTER:
            return {"state": "failure", "description": "codex-unavailable: no verdict in 30 min. Retry `@codex review`, or label `hotfix` if urgent.", "queue": []}
        # R32: the old nudge condition now picks the description instead of posting a comment.
        # Not when Codex is already working this SHA, someone already asked after the push, or
        # the PR is closed.
        ask = (age >= NUDGE_AFTER and not pr.get("nudged", False) and is_open(pr)
               and summary_status != "running")
        # No minute count in either description: it must be stable across polls of the same
        # SHA, or every poll posts a "changed" status (post_status compares description too).
        desc = (f"no Codex review of {head_sha[:7]} yet: comment {REVIEW_REQUEST}" if ask
                else f"waiting for Codex review of {head_sha[:7]}")
        return {"state": "pending", "description": desc, "queue": []}

    # R22: a round is one reviewed SHA -- several Codex reviews of the same commit (e.g. a
    # duplicate request) are one round; a review without a commit_sha cannot be matched to
    # a SHA, so it counts as a round of its own.
    round_no = (len({r["commit_sha"] for r in reviews if r.get("commit_sha")})
                + sum(1 for r in reviews if not r.get("commit_sha")))
    blocking_max = 1 if round_no <= 2 else 0  # P0/P1 block in rounds 1-2, P0 only after
    # A clean first pass is a comment, not a review submission (round_no 0); label it round 1.
    label = f"round {max(round_no, 1)}"
    blocking, queue = [], []
    for t in pr["threads"]:
        if not t["comments"] or not is_codex(t["comments"][0]["author"]):
            continue
        if any(QUEUE_MARK in c["body"] for c in t["comments"]):
            continue  # already handed to the janitor
        p = priority(t)
        cleared = t["is_resolved"] and ts(t["comments"][0]["created_at"]) < pushed
        if p <= blocking_max:
            if not cleared:
                blocking.append(t["id"])
        else:
            if not t["is_resolved"]:
                queue.append(t["id"])
    if blocking:
        return {"state": "failure", "description": f"{label}: {len(blocking)} unresolved P0/P1 (push a fix): {', '.join(blocking)}"[:140], "queue": queue}
    return {"state": "success", "description": f"{label}: no blocking findings" + (f"; {len(queue)} queued for janitor" if queue else ""), "queue": queue}


def poll_done(pr, verdict, now):
    """Loop control for `poll`: True once there is nothing left to wait for.
    - a verdict landed (success/failure), or
    - the PR is a draft (Codex skips drafts; ready_for_review starts a new poller) -- so a
      draft push bills ~1 minute, not 31, or
    - the PR was closed/merged mid-poll (nobody will review it), or
    - clock_start (later of head push / asked_at) + 31 min has passed, so a re-run long
      after the push and the ask does not wait again."""
    if verdict["state"] != "pending":
        return True
    if pr.get("draft") or not is_open(pr):
        return True
    return now >= clock_start(pr) + POLL_DEADLINE


# ---- fetch / act (thin; every decision is in the pure functions above) ------------
def gh(*args, stdin=None):
    return subprocess.run(["gh", *args], input=stdin, text=True, capture_output=True, check=True).stdout


def pushed_at(commit_node):
    """The real push time for a commit, not just when it was authored/committed:
    committedDate can lag the actual push by minutes (observed on PR #61: committed
    02:37:02Z, pushed 02:39:58Z). Prefer the earliest GitHub Actions check-suite
    createdAt (fired by the push webhook), falling back to committedDate."""
    commit = commit_node["commit"]
    suites = (commit.get("checkSuites") or {}).get("nodes") or []
    candidates = [s["createdAt"] for s in suites if ((s.get("app") or {}).get("slug")) == "github-actions"]
    if candidates:
        return min(candidates, key=ts)
    return commit["committedDate"]


def is_nudged(comments, head_pushed_at):
    """True if any comment (any author) whose stripped body starts with
    '@codex review' was created after head_pushed_at -- i.e. someone already asked Codex
    about this SHA, so the pending status stops asking for it (R32)."""
    pushed = ts(head_pushed_at)
    return any(c["body"].strip().startswith(REVIEW_REQUEST) and ts(c["created_at"]) > pushed for c in comments)


def derive_asked_at(head_pushed_at, event_times):
    """R23b/d/e: when Codex was asked about this head, from PR state alone so every mode
    (poll, one-shot, dispatch) computes the same clock: the latest of head_pushed_at and
    `event_times` -- the PR's createdAt and its READY_FOR_REVIEW / REOPENED timeline events
    (a branch can sit long before its PR opens; Codex reviews on open and on draft -> ready).
    Returns the winning ISO timestamp string.
    `@codex review` comments (a human or agent request or retry) never move it -- they only
    count for is_nudged(). Each clock start is an
    event that starts its own poller (push -> synchronize, PR created -> opened, draft ->
    ready -> ready_for_review, reopen -> reopened), so clock_start + 31 min always falls
    inside that poller's 35-min job timeout; a comment-moved clock (R23b/c) could push the
    deadline past it, and Actions killed the poller while compute still said pending."""
    return max([head_pushed_at, *event_times], key=ts)


GQL = """
query($owner:String!,$name:String!,$n:Int!){ repository(owner:$owner,name:$name){ pullRequest(number:$n){
  headRefOid isDraft state createdAt labels(first:20){nodes{name}}
  timelineItems(last:5, itemTypes:[READY_FOR_REVIEW_EVENT, REOPENED_EVENT]){nodes{... on ReadyForReviewEvent{createdAt} ... on ReopenedEvent{createdAt}}}
  commits(last:1){nodes{commit{committedDate checkSuites(first:20){nodes{createdAt app{slug}}}}}}
  reviews(first:100){nodes{author{login} submittedAt commit{oid}}}
  comments(last:100){totalCount nodes{author{login} body createdAt}}
  reviewThreads(first:100){nodes{id isResolved comments(first:20){nodes{author{login} body createdAt commit{oid}}}}}
}}}"""


def _codex_summaries(comments):
    return [c for c in comments if is_codex(c["author"]) and SUMMARY_MARKER in c["body"]]


def _newest_summary(comments):
    codex_marked = _codex_summaries(comments)
    if not codex_marked:
        return None
    newest = max(codex_marked, key=lambda c: ts(c["created_at"]))
    return parse_summary(newest["body"])


def needs_summary_scan(window_comments, total_count):
    """R46: the GraphQL query sees only comments(last:100). Scan the full history for Codex's
    summary only when it isn't in that window AND older comments exist -- so a normal PR
    costs no extra call and a long one costs ceil(N/100) REST pages."""
    return not _codex_summaries(window_comments) and total_count > len(window_comments)


def _scan_for_summaries(repo, number):
    """Every Codex-summary-marked comment on the PR, however old. REST + --paginate + a --jq
    filter that emits one compact JSON object per match (a plain --paginate would print
    '[..][..]' page arrays, which json.loads can't parse). Authors come back as ...[bot];
    is_codex normalizes that."""
    jq = (f'.[] | select(.body | contains("{SUMMARY_MARKER}")) '
          '| {author: .user.login, body: .body, created_at: .created_at} | tojson')
    out = gh("api", "--paginate", f"repos/{repo}/issues/{number}/comments?per_page=100", "--jq", jq)
    return [json.loads(line) for line in out.splitlines() if line.strip()]


def fetch(repo, number):
    owner, name = repo.split("/")
    d = json.loads(gh("api", "graphql", "-f", f"query={GQL}", "-F", f"owner={owner}", "-F", f"name={name}", "-F", f"n={number}"))
    p = d["data"]["repository"]["pullRequest"]
    login = lambda x: (x or {}).get("login", "")
    comments = [{"author": login(c["author"]), "body": c["body"], "created_at": c["createdAt"]} for c in p["comments"]["nodes"]]
    head_pushed_at = pushed_at(p["commits"]["nodes"][0])
    summary_sources = comments
    if needs_summary_scan(comments, p["comments"]["totalCount"]):
        summary_sources = comments + _scan_for_summaries(repo, number)
    return {
        "head_sha": p["headRefOid"], "head_pushed_at": head_pushed_at,
        "draft": p["isDraft"], "state": p["state"],
        "labels": [l["name"] for l in p["labels"]["nodes"]],
        "reviews": [{"author": login(r["author"]), "submitted_at": r["submittedAt"], "commit_sha": (r["commit"] or {}).get("oid", "")} for r in p["reviews"]["nodes"]],
        "comments": comments,
        "threads": [{"id": t["id"], "is_resolved": t["isResolved"], "comments": [
            {"author": login(c["author"]), "body": c["body"], "created_at": c["createdAt"], "commit_sha": (c["commit"] or {}).get("oid", "")} for c in t["comments"]["nodes"]]} for t in p["reviewThreads"]["nodes"]],
        "nudged": is_nudged(comments, head_pushed_at),
        "asked_at": derive_asked_at(head_pushed_at, [p["createdAt"], *(e["createdAt"] for e in p["timelineItems"]["nodes"] if e.get("createdAt"))]),
        "summary": _newest_summary(summary_sources),
    }


def post_status(repo, sha, verdict):
    cur = json.loads(gh("api", f"repos/{repo}/commits/{sha}/status"))
    for s in cur.get("statuses", []):
        if s["context"] == "review-verdict" and s["state"] == verdict["state"] and s["description"] == verdict["description"]:
            return "unchanged"
    gh("api", "-X", "POST", f"repos/{repo}/statuses/{sha}", "-f", f"state={verdict['state']}", "-f", "context=review-verdict",
       "-f", f"description={verdict['description']}")
    return "posted"


QUEUE_REPLY = f"{QUEUE_MARK}: advisory finding, handed to the weekly janitor (review v4 §7)."
FORBIDDEN = "Resource not accessible by integration"


class QueueError(Exception):
    """Some queued threads did not get their `queued-for-janitor` marker reply, so nothing
    records them for the janitor. Raised only after every thread was tried. Those threads stay
    unmarked and unresolved, so the next run (a poll retry or the next event) queues them again."""
    def __init__(self, thread_ids):
        self.thread_ids = list(thread_ids)
        super().__init__(f"not queued for the janitor (marker reply failed): {', '.join(self.thread_ids)}")


def annotation(level, message):
    """One GitHub Actions workflow-command line (a warning/error annotation on the run)."""
    msg = message.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    return f"::{level} title=review-verdict janitor queue::{msg}"


def reply_thread(thread_id, body):
    gh("api", "graphql", "-f", "query=mutation($id:ID!,$b:String!){ addPullRequestReviewThreadReply(input:{pullRequestReviewThreadId:$id, body:$b}){ comment{id} } }", "-F", f"id={thread_id}", "-F", f"b={body}")


def resolve_thread(thread_id):
    gh("api", "graphql", "-f", "query=mutation($id:ID!){ resolveReviewThread(input:{threadId:$id}){ thread{id} } }", "-F", f"id={thread_id}")


def queue_threads(queue):
    """Hand each queued P2/P3 thread to the janitor, trying every thread even when one fails
    (SSSF-25: one failed resolve aborted the loop, and the later threads never got a marker).
    The marker reply IS the queue entry: compute() skips marked threads and the janitor finds
    them by it. Resolving only collapses the thread on the PR.
    - reply fails   -> ::error:: annotation; the thread id is returned (run_once withholds
                       success and fails the run; the unmarked thread is queued again next run).
    - resolve fails -> ::warning:: annotation only: the finding is queued; the thread stays
                       open. resolveReviewThread needs `contents: write` (SSSF-25 drill,
                       BJGLLC/.github#5); the callers grant `contents: read`.
    Returns the ids that did NOT get their marker."""
    not_queued = []
    for tid in queue:
        try:
            reply_thread(tid, QUEUE_REPLY)
        except subprocess.CalledProcessError as e:
            not_queued.append(tid)
            print(annotation("error", f"{tid} NOT queued for the janitor: the marker reply failed "
                                      f"({(e.stderr or '').strip()}). The next run retries it."), flush=True)
            continue
        try:
            resolve_thread(tid)
        except subprocess.CalledProcessError as e:
            why = (e.stderr or "").strip()
            hint = (" resolveReviewThread needs contents: write, which the caller's token does not grant (SSSF-25)."
                    if FORBIDDEN in why else "")
            print(annotation("warning", f"{tid} is queued for the janitor (marker reply posted) but stays "
                                        f"open: resolve failed ({why}).{hint}"), flush=True)
    return not_queued


def withhold_success(v, not_queued, number):
    """Pure: the verdict to publish once the queue ran. A lost marker must never publish
    success (Codex P1 on BJGLLC/.github#6: auto-merge would take the PR while the finding was
    never recorded, and a failed reply creates no follow-up event). An existing failure keeps
    its own description: it names the finding to fix, and the fix push re-runs the queue."""
    if not not_queued or v["state"] != "success":
        return v
    desc = (f"janitor hand-off failed for {len(not_queued)} P2/P3 thread(s); "
            f"re-run the review-verdict workflow with -f pr={number}")
    return {**v, "state": "failure", "description": desc[:140]}


def run_once(repo, number, now=None):
    """The one-shot path, shared by `<repo> <n>` and every `poll` iteration:
    fetch -> compute -> queue P2/P3 threads for the janitor -> post status. Queue BEFORE
    publishing, so success is only ever posted once every finding is durable (a lost marker
    posts failure, then raises QueueError). No mode-specific input (asked_at comes from
    fetch(), R23b) and no PR comments (R32).
    Returns (pr, verdict, now) so the poller can decide whether to keep going."""
    pr = fetch(repo, number)
    now = now or datetime.now(timezone.utc)
    v = compute(pr, now)
    print(json.dumps({"pr": number, "sha": pr["head_sha"], **v}), flush=True)
    not_queued = queue_threads(v["queue"])
    v = withhold_success(v, not_queued, number)
    print(post_status(repo, pr["head_sha"], v), flush=True)
    if not_queued:
        raise QueueError(not_queued)
    return pr, v, now


def poll(repo, number, *, step=None, sleep=time.sleep,
         interval=POLL_INTERVAL, max_errors=POLL_MAX_ERRORS):
    """run_once every `interval` s until poll_done. A failed iteration (gh/API hiccup) is
    retried; only `max_errors` consecutive failures end the poller, because a dead poller
    leaves the PR pending with no ask and no 30-minute escalation until some other event
    arrives."""
    step = step or run_once
    errors = 0
    while True:
        try:
            pr, v, now = step(repo, number)
        except Exception as e:  # noqa: BLE001 -- bounded retry, re-raised below
            errors += 1
            detail = getattr(e, "stderr", "") or ""
            print(f"poll: iteration failed ({errors}/{max_errors}): {e!r} {detail}".rstrip(), file=sys.stderr, flush=True)
            if errors >= max_errors:
                raise
            sleep(interval)
            continue
        errors = 0
        if poll_done(pr, v, now):
            return v
        sleep(interval)


USAGE = ("usage: review_verdict.py compute <pr.json> [now-iso]\n"
         "       review_verdict.py poll <owner/repo> <number>\n"
         "       review_verdict.py <owner/repo> <number>")


def main(argv):
    args = argv[1:]
    if args[:1] == ["compute"]:  # compute <pr.json> [now-iso]  (drills + local runs)
        pr = json.load(open(args[1])); now = ts(args[2]) if len(args) > 2 else datetime.now(timezone.utc)
        print(json.dumps(compute(pr, now))); return 0
    try:
        if args[:1] == ["poll"]:  # poll <repo> <n>  (after a push / open / ready / reopen)
            if len(args) != 3:
                print(USAGE, file=sys.stderr); return 2
            poll(args[1], int(args[2])); return 0
        if len(args) != 2:
            print(USAGE, file=sys.stderr); return 2
        run_once(args[0], int(args[1])); return 0
    except subprocess.CalledProcessError as e:
        print(f"gh failed (exit {e.returncode}): {e.stderr}", file=sys.stderr); return 1
    except QueueError as e:
        print(f"review-verdict: {e}", file=sys.stderr); return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
