#!/usr/bin/env python3
"""review-verdict: turn Codex's advisory review into one deterministic commit status.

Pure part: compute(pr, now), parse_summary(body), pushed_at(commit_node), is_nudged(comments, head_pushed_at),
nudge_decision(pr), nudge_due(pr, verdict, now), poll_done(pr, verdict, now), clock_start(pr), derive_asked_at(...).
Fetch/act part: fetch(repo, number) via `gh`; run_nudge = fetch -> nudge_decision -> one
`@codex review` comment; run_once = fetch -> compute -> [poller only: nudge_due -> one
`@codex review`] -> queue_threads (fresh marker check, then the marker reply) -> post status;
poll = run_once every 60 s until poll_done (a verdict, or codex-unavailable at 30 min).
SSSF-25 (Blake, 2026-09-28; reverses R32 "the gate never comments"): on a new head (pull_request
synchronize) the gate asks Codex itself, once per head: one `@codex review` comment carrying a
hidden per-SHA marker, skipped for drafts, `hotfix`, closed PRs and heads someone already asked
about. SSSF-46: Codex's review on open can be a bare 👍 (R46: never a verdict) or nothing at all
(it reviews by itself only PRs its users authored), and a 👍 fires no event, so the poller also
asks once nobody has: at ASK_AFTER, or at once for a bot-authored PR. It then holds until a
verdict or the 30-min codex-unavailable failure, so an unanswered PR goes red, never hangs.
P2/P3 threads get a `queued-for-janitor` reply, which IS the queue entry; the gate no longer
resolves them (resolveReviewThread needs contents: write; the callers grant read).
The reusable workflow (.github/workflows/review-verdict.yml) runs `nudge` (synchronize only) and
then `poll` after a push / open / ready / reopen, and the one-shot `<repo> <n>` for every other
event. Spec: claude-dotfiles docs/superpowers/specs/2026-09-26-review-system-v4-design.md §6.
No secrets here: this repo is public and the token comes from the caller's GITHUB_TOKEN env.
"""
import json
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone

CODEX = "chatgpt-codex-connector"
ASK_AFTER = timedelta(minutes=10)      # after this with no verdict and nobody asking, the poller asks
                                       # (SSSF-46; a one-shot run's status asks a human instead)
# The poller holds until a verdict or this failure (SSSF-46). SSSF-25 stopped it at +8 on the
# theory that Codex's reply fires its own event, but a 👍 fires none and an unanswered request
# fires nothing, so the PR hung pending for good. When Codex answers, the hold ends with the
# verdict (~2 min after a request); only a request nobody answers holds the full 30 min.
UNAVAILABLE_AFTER = timedelta(minutes=30)
POLL_INTERVAL = 60                     # seconds between poll iterations
POLL_MAX_ERRORS = 3                    # consecutive failed iterations before the poller gives up
DRAFT_DESC = "draft: Codex reviews when marked ready"
HOTFIX_DESC = "hotfix: review skipped; comment @codex review after merge for the post-hoc review"
REVIEW_REQUEST = "@codex review"
NUDGE_MARK = "review-verdict:nudge"    # hidden in the gate's own request: <!-- review-verdict:nudge <sha> -->
BADGE = re.compile(r"P([0-3]) Badge\]")
NO_ISSUES = re.compile(r"didn'?t find any major issues", re.I)
REVIEWED_COMMIT = re.compile(r"reviewed\s+commit\W*?([0-9a-f]+)(?![0-9a-z])", re.I)
# Codex's real error comment (intranet #59, file-mine #14-#17): 'Codex Review: Something went
# wrong. Try again later by commenting "@codex review"'. The older phrasings stay as fallbacks.
ERROR = re.compile(r"codex review:\s*something went wrong|codex (encountered an error|was unable|could not|failed)", re.I)
QUEUE_MARK = "queued-for-janitor"
# The gate replies through addPullRequestReviewThreadReply with the Actions token (GraphQL login
# `github-actions`, __typename Bot). Compared after _normalize_login. SSSF-29's cutover adds its
# bot login here once that account exists: an unowned login must never be honoured (SSSF-38).
GATE_LOGINS = {"github-actions"}
QUEUE_REPLY = f"{QUEUE_MARK}: advisory finding, handed to the weekly janitor (review v4 §7)."
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


def is_gate_marker(comment):
    """SSSF-38: the janitor marker counts only when the gate wrote it. Anyone can reply on a
    review thread, so a marker from any other author (or one that merely mentions the text)
    is ignored. All hold: the author is a gate login, that author is a Bot (a User can own a
    look-alike login), and the body STARTS with the exact QUEUE_REPLY the gate posts.
    It only ever de-duplicates the P2/P3 queue; it never unblocks a P0/P1 (see compute)."""
    if _normalize_login(comment.get("author")) not in GATE_LOGINS:
        return False
    if comment.get("author_type") != "Bot":
        return False
    return (comment.get("body") or "").lstrip().startswith(QUEUE_REPLY)


def edited_by_other(c):
    """SSSF-38: a Codex comment or review body edited by someone other than Codex, so its badge
    cannot be trusted (P1 -> P3 downgrade). A null editor with an edit time is a deleted
    account: fail closed."""
    ed = c.get("editor")
    return bool(c.get("last_edited_at") or ed) and not is_codex(ed)


def tampered(thread):
    """The thread's first (Codex) comment was edited by someone other than Codex."""
    return edited_by_other(thread["comments"][0])


def priority(thread):
    if tampered(thread):
        return 0
    first = thread["comments"][0]["body"]
    m = BADGE.search(first)
    return int(m.group(1)) if m else 2


def review_priority(review):
    """The most severe badge in a Codex review's BODY (0-3), or None when it carries none.
    Codex can put a finding only there, with no inline thread: its review of one head was a
    COMMENTED review whose body held the PR's only P1, and the thread-only count passed it. A
    body edited by anyone but Codex counts as P0 (SSSF-38, as for threads)."""
    if edited_by_other(review):
        return 0
    found = [int(p) for p in BADGE.findall(review.get("body") or "")]
    return min(found) if found else None


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
    clock_start(pr). compute() never comments: after a push the `nudge` step already asked
    Codex (SSSF-25), and at ASK_AFTER the poller asks (SSSF-46); when nobody has asked anyway
    (no poller running, or its ask failed), the pending description asks for `@codex review`.
    An error comment counts only when no head
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
        # Ask in the description only when nobody (the gate's nudge included) asked after the
        # push, Codex is not already working this SHA, and the PR is open.
        ask = (age >= ASK_AFTER and not pr.get("nudged", False) and is_open(pr)
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
        p = priority(t)
        cleared = t["is_resolved"] and ts(t["comments"][0]["created_at"]) < pushed
        if p <= blocking_max:
            if not cleared:
                blocking.append(t["id"])
        elif not t["is_resolved"] and not any(is_gate_marker(c) for c in t["comments"]):
            # already handed to the janitor -> skipped. The marker is honoured only here: the gate
            # replies only to queued (non-blocking) threads, so a marker never unblocks a P0/P1
            # (a PR-added workflow can post as github-actions: SSSF-38).
            queue.append(t["id"])
    # Body-only findings block like threads. A review counts for the head it reviewed (its
    # commit, or a SHA-less one after the push, as has_verdict reads them), so only a push
    # clears it: a later clean review of the same head does not, as a resolved thread doesn't
    # (R23). Body P2/P3 have no thread to hand to the janitor, so they are left alone.
    for r in reviews:
        about_head = r.get("commit_sha") == head_sha or (not r.get("commit_sha") and after(r["submitted_at"]))
        p = review_priority(r) if about_head else None
        if p is not None and p <= blocking_max:
            blocking.append(r.get("id") or f"review@{r['submitted_at']}")
    if blocking:
        return {"state": "failure", "description": f"{label}: {len(blocking)} unresolved P0/P1 (push a fix): {', '.join(blocking)}"[:140], "queue": queue}
    return {"state": "success", "description": f"{label}: no blocking findings" + (f"; {len(queue)} queued for janitor" if queue else ""), "queue": queue}


def poll_done(pr, verdict, now):
    """Loop control for `poll`: True once there is nothing left to wait for.
    - a verdict landed (success/failure, codex-unavailable included), or
    - the PR is a draft (Codex skips drafts; ready_for_review starts a new poller) -- so a
      draft push bills ~1 minute, not 31, or
    - the PR was closed/merged mid-poll (nobody will review it), or
    - clock_start (later of head push / asked_at) + UNAVAILABLE_AFTER has passed: compute()
      has posted codex-unavailable by then, so this is only the backstop that keeps a re-run
      long after the push from waiting again. SSSF-46: there is no earlier stop. A poller that
      quits while the PR is pending leaves it pending for good when Codex answers with only a
      👍 or not at all, since neither fires an event that would recompute it."""
    if verdict["state"] != "pending":
        return True
    if pr.get("draft") or not is_open(pr):
        return True
    return now >= clock_start(pr) + UNAVAILABLE_AFTER


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


# Every Markdown code form: ``` and ~~~ fences closed only by the same character at the same
# length (a ```` fence can show a ``` example; an unclosed one runs to the end, as GitHub
# renders it), <pre>/<code>, inline spans, then indented blocks (4 spaces or a tab).
CODE = re.compile(r"(?P<fence>`{3,}|~{3,}).*?(?:(?P=fence)|\Z)|<pre\b.*?(?:</pre>|\Z)|<code\b.*?(?:</code>|\Z)"
                  r"|``[^\n]*?``|`[^`\n]*`", re.S | re.I)
INDENTED_CODE = re.compile(r"^(?: {4}|\t).*$", re.M)


def asks_codex(body):
    """True when the body issues '@codex review' outside code. Inside code it is text about the
    command, not a request (GitHub makes no mention of it): a tracker's link-back bot quoted a
    ticket that described `@codex review`, and the gate took it for a request and never asked.
    Erring toward code is the safe side: a missed request costs one extra, harmless ask from
    the gate; a false one suppresses its only ask."""
    return REVIEW_REQUEST in INDENTED_CODE.sub("", CODE.sub("", body))


def is_nudged(comments, head_pushed_at):
    """True if a comment asking '@codex review' (outside code, see asks_codex), by anyone except
    Codex, was created after head_pushed_at: someone (a human, an agent, or the gate's own
    nudge) already asked Codex about this SHA, so the gate neither nudges nor asks in the
    status. Codex's own comments never count: its summary, clean-review and error comments all
    say 'comment "@codex review"'."""
    pushed = ts(head_pushed_at)
    return any(asks_codex(c["body"]) and not is_codex(c["author"]) and ts(c["created_at"]) > pushed
               for c in comments)


def nudge_marker(sha):
    return f"<!-- {NUDGE_MARK} {sha} -->"


def nudge_body(sha):
    """The gate's request: `@codex review` alone on the first line, then a hidden marker naming
    the full head SHA, so any later run (a re-run, a racing run) can tell this head was asked."""
    return f"{REVIEW_REQUEST}\n\n{nudge_marker(sha)}"


def nudge_decision(pr):
    """(post, why): whether the gate may post `@codex review` for the PR's head at all. Pure.
    On pull_request synchronize it is the whole rule (Codex never reviews a push by itself);
    the poller adds nudge_due's timing on top of it."""
    if not is_open(pr):
        return False, "closed"
    if pr.get("draft"):
        return False, "draft: Codex reviews when marked ready"
    if "hotfix" in pr.get("labels", []):
        return False, "hotfix: review skipped"
    marker = nudge_marker(pr["head_sha"])
    if any(marker in c["body"] for c in pr["comments"]):
        return False, "already nudged this head (marker found)"
    if pr.get("nudged"):
        return False, "already asked about this head (a @codex review comment after the push)"
    return True, "new head, nobody asked"


def nudge_due(pr, verdict, now):
    """(post, why): SSSF-46 -- should the poller post the one `@codex review` for this head now?
    Pure. Codex's own review on open can end in a bare 👍 (R46: never a verdict) or never come
    (Codex reviews by itself only PRs its users authored), and neither fires an event, so a
    poller that only waited left the PR pending. It asks when all hold:
    - no verdict (`verdict` is compute()'s for the same PR and time): a review, a head-named
      clean comment, an error or a codex-unavailable failure is never nudged;
    - nudge_decision allows it (open, not a draft, not `hotfix`, no marker for this head and
      nobody's request after the push), so a head is asked about at most once;
    - Codex does not have the head in hand: no sticky summary for this head (running, or a
      status word we cannot read; a completed one is a verdict). An older head's summary does
      not count, so a failed synchronize nudge is retried here;
    - the PR's author is a Bot (Codex never reviews it by itself: ask at once), or ASK_AFTER
      has passed since clock_start, the point where compute() would ask in the status."""
    if verdict["state"] != "pending":
        return False, f"verdict: {verdict['state']}"
    post, why = nudge_decision(pr)
    if not post:
        return post, why
    summary = pr.get("summary")
    if summary and summary.get("commit") and pr["head_sha"].startswith(summary["commit"]):
        return False, f"Codex's summary for this head says {summary.get('status')}: it is answering"
    if pr.get("author_type") == "Bot":
        return True, "bot-authored PR: Codex reviews it only when asked"
    if now - clock_start(pr) >= ASK_AFTER:
        return True, f"no verdict and nobody asked {int(ASK_AFTER.total_seconds() // 60)} min after the clock start"
    return False, "Codex may still review this head by itself"


def derive_asked_at(head_pushed_at, event_times):
    """R23b/d/e: when Codex was asked about this head, from PR state alone so every mode
    (poll, one-shot, dispatch) computes the same clock: the latest of head_pushed_at and
    `event_times` -- the PR's createdAt and its READY_FOR_REVIEW / REOPENED timeline events
    (a branch can sit long before its PR opens; Codex reviews on open and on draft -> ready).
    Returns the winning ISO timestamp string.
    `@codex review` comments (a human or agent request or retry, or the gate's nudge) never
    move it -- they only count for is_nudged(). Each clock start is an
    event that starts its own poller (push -> synchronize, PR created -> opened, draft ->
    ready -> ready_for_review, reopen -> reopened), so clock_start + POLL_WINDOW always falls
    inside that poller's job timeout; a comment-moved clock (R23b/c) could push the
    deadline past it, and Actions killed the poller while compute still said pending."""
    return max([head_pushed_at, *event_times], key=ts)


# GitHub Actions' own app id. Only its check suites are read (the slug filter below is a second check).
GITHUB_ACTIONS_APP_ID = 15368

GQL = """
query($owner:String!,$name:String!,$n:Int!){ repository(owner:$owner,name:$name){ pullRequest(number:$n){
  headRefOid isDraft state createdAt author{__typename} labels(first:20){nodes{name}}
  timelineItems(last:5, itemTypes:[READY_FOR_REVIEW_EVENT, REOPENED_EVENT]){nodes{... on ReadyForReviewEvent{createdAt} ... on ReopenedEvent{createdAt}}}
  commits(last:1){nodes{commit{committedDate checkSuites(first:20, filterBy:{appId:__ACTIONS_APP_ID__}){nodes{createdAt app{slug}}}}}}
  reviews(first:100){nodes{id author{login} body submittedAt lastEditedAt editor{login} commit{oid}}}
  comments(last:100){totalCount nodes{author{login} body createdAt}}
  reviewThreads(first:100){nodes{id isResolved comments(first:20){nodes{author{login __typename} editor{login __typename} lastEditedAt body createdAt commit{oid}}}}}
}}}""".replace("__ACTIONS_APP_ID__", str(GITHUB_ACTIONS_APP_ID))


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
        "author_type": (p.get("author") or {}).get("__typename", ""),   # "Bot" | "User"; "" = deleted
        "labels": [l["name"] for l in p["labels"]["nodes"]],
        "reviews": [{"id": r.get("id"), "author": login(r["author"]), "submitted_at": r["submittedAt"],
                     "commit_sha": (r["commit"] or {}).get("oid", ""), "body": r.get("body") or "",
                     "editor": login(r.get("editor")) or None, "last_edited_at": r.get("lastEditedAt")}
                    for r in p["reviews"]["nodes"]],
        "comments": comments,
        "threads": [{"id": t["id"], "is_resolved": t["isResolved"], "comments": [
            {"author": login(c["author"]), "author_type": (c["author"] or {}).get("__typename", ""),
             "editor": login(c.get("editor")) or None, "last_edited_at": c.get("lastEditedAt"), "body": c["body"], "created_at": c["createdAt"],
             "commit_sha": (c["commit"] or {}).get("oid", "")} for c in t["comments"]["nodes"]]} for t in p["reviewThreads"]["nodes"]],
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


def post_comment(repo, number, body):
    """One PR (issue) comment via the caller's GITHUB_TOKEN. Needs only pull-requests: write,
    which every caller grants (probe, BJGLLC/.github#5 run 36483153709). A GITHUB_TOKEN comment
    triggers no workflow (and the callers admit issue_comment only from Codex), so the nudge
    never re-runs the gate: Codex's reply does, or the poller still running sees it."""
    gh("api", "-X", "POST", f"repos/{repo}/issues/{number}/comments", "-f", f"body={body}")


def run_nudge(repo, number):
    """The `nudge` path (pull_request synchronize only): fetch -> nudge_decision -> at most one
    `@codex review` comment. Idempotent: a later run for the same head finds the marker (and
    the request) and posts nothing. Runs for one PR never overlap: the poll job, which runs this
    step, is serialized per PR by its concurrency group. Returns whether it posted."""
    pr = fetch(repo, number)
    post, why = nudge_decision(pr)
    print(json.dumps({"pr": number, "sha": pr["head_sha"], "nudge": post, "why": why}), flush=True)
    if post:
        post_comment(repo, number, nudge_body(pr["head_sha"]))
    return post


def poller_nudge(repo, number, pr, verdict, now):
    """The poller's ask (SSSF-46): nudge_due -> at most one `@codex review` with the head's
    marker. Returns whether it posted. A failed post is loud (an ::error:: annotation naming the
    remedy) but never ends the poll: the status still gets posted, and since nothing marks the
    head as asked, the next iteration tries again."""
    post, why = nudge_due(pr, verdict, now)
    print(json.dumps({"pr": number, "sha": pr["head_sha"], "nudge": post, "why": why}), flush=True)
    if not post:
        return False
    try:
        post_comment(repo, number, nudge_body(pr["head_sha"]))
    except subprocess.CalledProcessError as e:
        print(annotation("error", f"the gate could not ask Codex about PR #{number} "
                                  f"(gh exit {e.returncode}: {(e.stderr or '').strip()}); retrying next poll. "
                                  f"Or comment {REVIEW_REQUEST} on the PR yourself.",
                         title="review-verdict nudge"), flush=True)
        return False
    return True



class QueueError(Exception):
    """Some queued threads did not get their `queued-for-janitor` marker reply, so nothing
    records them for the janitor. Raised only after every thread was tried. Those threads stay
    unmarked, so the next run (a poll retry or the next event) queues them again."""
    def __init__(self, thread_ids):
        self.thread_ids = list(thread_ids)
        super().__init__(f"not queued for the janitor (marker reply failed): {', '.join(self.thread_ids)}")


def annotation(level, message, title="review-verdict janitor queue"):
    """One GitHub Actions workflow-command line (a warning/error annotation on the run)."""
    msg = message.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    return f"::{level} title={title}::{msg}"


def reply_thread(thread_id, body):
    gh("api", "graphql", "-f", "query=mutation($id:ID!,$b:String!){ addPullRequestReviewThreadReply(input:{pullRequestReviewThreadId:$id, body:$b}){ comment{id} } }", "-F", f"id={thread_id}", "-F", f"b={body}")


THREAD_COMMENTS = "query($id:ID!){ node(id:$id){ ... on PullRequestReviewThread{ comments(last:100){ nodes{ body author{ login __typename } } } } } }"


def thread_is_queued(thread_id):
    """Fresh read, right before replying: does the thread already carry a gate-authored
    queued-for-janitor marker (SSSF-38: a marker from anyone else is absent)? Another run may have replied after this run's fetch (cd-marketing #165: the poller
    and a pull_request_review run replied 1 s apart)."""
    d = json.loads(gh("api", "graphql", "-f", f"query={THREAD_COMMENTS}", "-F", f"id={thread_id}"))
    nodes = ((d.get("data") or {}).get("node") or {}).get("comments", {}).get("nodes", [])
    return any(is_gate_marker({"author": (c.get("author") or {}).get("login"),
                           "author_type": (c.get("author") or {}).get("__typename"), "body": c.get("body")})
               for c in nodes)


def queue_threads(queue):
    """Hand each queued P2/P3 thread to the janitor, trying every thread even when one fails
    (SSSF-25: one failure aborted the loop, and the later threads never got a marker).
    The marker reply IS the queue entry: compute() skips marked threads and the janitor finds
    them by it. The thread stays open: the gate never resolves (resolveReviewThread needs
    `contents: write`, drill BJGLLC/.github#5; the callers grant `contents: read`).
    - already marked (fresh check) -> skipped: another run queued it after our fetch.
    - fresh check fails            -> ::warning::, reply anyway (a duplicate beats a lost finding).
    - reply fails                  -> ::error:: annotation; the thread id is returned (run_once
                                      withholds success and fails the run; the unmarked thread
                                      is queued again next run).
    Returns the ids that did NOT get their marker."""
    not_queued = []
    for tid in queue:
        try:
            if thread_is_queued(tid):
                print(f"{tid}: already queued for the janitor by another run; no second reply", flush=True)
                continue
        except subprocess.CalledProcessError as e:
            print(annotation("warning", f"{tid}: the fresh marker check failed ({(e.stderr or '').strip()}); "
                                        "replying anyway (a duplicate reply beats a lost finding)."), flush=True)
        try:
            reply_thread(tid, QUEUE_REPLY)
        except subprocess.CalledProcessError as e:
            not_queued.append(tid)
            print(annotation("error", f"{tid} NOT queued for the janitor: the marker reply failed "
                                      f"({(e.stderr or '').strip()}). The next run retries it."), flush=True)
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


def run_once(repo, number, now=None, nudge=False):
    """The one-shot path, shared by `<repo> <n>` and every `poll` iteration:
    fetch -> compute -> queue P2/P3 threads for the janitor -> post status. Queue BEFORE
    publishing, so success is only ever posted once every finding is durable (a lost marker
    posts failure, then raises QueueError). No mode-specific input to the verdict (asked_at
    comes from fetch(), R23b). nudge=True (the poller only, SSSF-46) may post the one
    `@codex review` for the head (poller_nudge) before the status, which is then computed as
    asked, so it says "waiting" rather than flapping to "comment @codex review" and back. The
    one-shot path never comments: one-shot runs are not serialized with the poller.
    Returns (pr, verdict, now) so the poller can decide whether to keep going."""
    pr = fetch(repo, number)
    now = now or datetime.now(timezone.utc)
    v = compute(pr, now)
    if nudge and poller_nudge(repo, number, pr, v, now):
        pr = {**pr, "nudged": True}
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
    """run_once (with the poller's nudge) every `interval` s until poll_done. A failed
    iteration (gh/API hiccup) is retried; only `max_errors` consecutive failures end the
    poller, because a dead poller leaves the PR pending with no ask and no 30-minute
    escalation until some other event arrives."""
    step = step or (lambda r, n: run_once(r, n, nudge=True))
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
         "       review_verdict.py nudge <owner/repo> <number>\n"
         "       review_verdict.py poll <owner/repo> <number>\n"
         "       review_verdict.py <owner/repo> <number>")


def main(argv):
    args = argv[1:]
    if args[:1] == ["compute"]:  # compute <pr.json> [now-iso]  (drills + local runs)
        pr = json.load(open(args[1])); now = ts(args[2]) if len(args) > 2 else datetime.now(timezone.utc)
        print(json.dumps(compute(pr, now))); return 0
    if args[:1] == ["nudge"]:  # nudge <repo> <n>  (pull_request synchronize only)
        if len(args) != 3:
            print(USAGE, file=sys.stderr); return 2
        try:
            run_nudge(args[1], int(args[2])); return 0
        except subprocess.CalledProcessError as e:
            # Loud (the step fails; the poll step still runs), never silent: without the nudge
            # nobody asked Codex about this head, so the PR waits until someone does.
            print(annotation("error", f"the gate could not ask Codex about PR #{args[2]} "
                                      f"(gh exit {e.returncode}: {(e.stderr or '').strip()}). "
                                      f"Comment {REVIEW_REQUEST} on the PR yourself.",
                             title="review-verdict nudge"), flush=True)
            return 1
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
