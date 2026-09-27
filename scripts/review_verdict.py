#!/usr/bin/env python3
"""review-verdict: turn Codex's advisory review into one deterministic commit status.

Pure part: compute(pr, now), parse_summary(body), pushed_at(commit_node), is_nudged(comments, head_pushed_at),
poll_done(pr, verdict, now), wants_rereview(pr), clock_start(pr), derive_asked_at(...).
Fetch/act part: fetch(repo, number) via `gh`; run_once = fetch -> [re-review request] -> compute ->
post status -> auto-resolve queued threads -> nudge; poll = run_once every 60 s until poll_done.
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
NUDGE_AFTER = timedelta(minutes=10)
UNAVAILABLE_AFTER = timedelta(minutes=30)
POLL_DEADLINE = timedelta(minutes=31)  # a poller never waits past clock_start + 31 min
POLL_INTERVAL = 60                     # seconds between poll iterations
POLL_MAX_ERRORS = 3                    # consecutive failed iterations before the poller gives up
DRAFT_DESC = "draft: Codex reviews when marked ready"
REVIEW_REQUEST = "@codex review"
BADGE = re.compile(r"P([0-3]) Badge\]")
NO_ISSUES = re.compile(r"didn'?t find any major issues", re.I)
ERROR = re.compile(r"codex (encountered an error|was unable|could not|failed)", re.I)
QUEUE_MARK = "queued-for-janitor"
CODEX_UNAVAILABLE_DESC = "codex-unavailable: Codex errored. Retry `@codex review`, or label `hotfix` if urgent."
SUMMARY_MARKER = "<!-- codex-pull-request-review-summary -->"


def ts(s):
    s = s.strip()
    fmt = "%Y-%m-%dT%H:%M:%S.%fZ" if "." in s else "%Y-%m-%dT%H:%M:%SZ"
    return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)


def _normalize_login(name):
    """Strip a trailing '[bot]' suffix so review/comment/reaction/thread authors compare
    correctly against CODEX regardless of whether GitHub reported the App identity
    (reviews/comments: 'chatgpt-codex-connector') or the bot identity (reactions:
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
    """The verdict for the PR's head. Pure: every mode (poll, one-shot, dispatch) feeds it
    the same fetch() output, so two runs on the same SHA post the same status.
    The nudge (10 min) and codex-unavailable (30 min) ages run from clock_start(pr) (push or
    draft -> ready). A retry `@codex review` does NOT restart the window: the status stays
    codex-unavailable until Codex's summary edit (an issue_comment event -> one-shot run)
    recomputes it once the review lands (spec §6)."""
    if "hotfix" in pr.get("labels", []):
        return {"state": "success", "description": "hotfix: review skipped, post-hoc queued", "queue": [], "nudge": False}

    # Codex does not review drafts (it reviews on draft -> ready), so a draft has no
    # verdict to wait for: pending (drafts cannot merge anyway), never codex-unavailable,
    # never nudged, nothing queued. ready_for_review starts a fresh poller.
    if pr.get("draft"):
        return {"state": "pending", "description": DRAFT_DESC, "queue": [], "nudge": False}

    head_sha = pr["head_sha"]
    pushed = ts(pr["head_pushed_at"])
    after = lambda s: ts(s) > pushed

    reviews = [r for r in pr["reviews"] if is_codex(r["author"])]
    # The summary comment itself (marker text) never counts toward the error /
    # no-major-issues comment channels -- it's Codex's own status board, not a finding.
    head_comments = [c for c in pr["comments"]
                      if is_codex(c["author"]) and after(c["created_at"]) and SUMMARY_MARKER not in c["body"]]
    head_reactions = [r for r in pr["reactions"] if is_codex(r["user"]) and r["content"] == "+1" and after(r["created_at"])]

    summary = pr.get("summary")
    summary_for_head = bool(summary and summary.get("commit") and head_sha.startswith(summary["commit"]))
    summary_status = summary.get("status") if summary_for_head else None

    if any(ERROR.search(c["body"]) for c in head_comments) or summary_status == "error":
        return {"state": "failure", "description": CODEX_UNAVAILABLE_DESC, "queue": [], "nudge": False}

    # Stale-verdict race fix (review focus 2): a Codex review only counts as a verdict
    # for THIS head when its commit_sha actually matches -- being merely "submitted
    # after the push" is not enough (that let a stale review on an old commit pass an
    # unreviewed new head). When Codex's sticky summary exists it is authoritative and
    # the 👍 / no-major-issues comment channels are ignored entirely; without a summary,
    # those channels (plus a same-SHA or empty-sha-after-push review) are the fallback.
    has_head_sha_review = any(r.get("commit_sha") == head_sha for r in reviews)
    if summary is not None:
        has_verdict = (summary_status == "completed") or has_head_sha_review
    else:
        empty_sha_review_after_push = any(
            not r.get("commit_sha") and after(r["submitted_at"]) for r in reviews)
        has_verdict = bool(
            has_head_sha_review
            or empty_sha_review_after_push
            or head_reactions
            or any(NO_ISSUES.search(c["body"]) for c in head_comments)
        )

    if not has_verdict:
        age = now - clock_start(pr)
        if age >= UNAVAILABLE_AFTER:
            return {"state": "failure", "description": "codex-unavailable: no verdict in 30 min. Retry `@codex review`, or label `hotfix` if urgent.", "queue": [], "nudge": False}
        nudge = age >= NUDGE_AFTER and not pr.get("nudged", False) and is_open(pr)
        if summary_status == "running":
            nudge = False  # Codex is already working this SHA; don't ping it again
        # No minute count in the description: it must be stable across polls of the
        # same SHA, or every poll posts a "changed" status (post_status compares
        # description too).
        return {"state": "pending", "description": f"waiting for Codex review of {head_sha[:7]}", "queue": [], "nudge": nudge}

    # R22: a round is one reviewed SHA -- several Codex reviews of the same commit (e.g. a
    # duplicate request) are one round; a review without a commit_sha cannot be matched to
    # a SHA, so it counts as a round of its own.
    round_no = (len({r["commit_sha"] for r in reviews if r.get("commit_sha")})
                + sum(1 for r in reviews if not r.get("commit_sha")))
    blocking_max = 1 if round_no <= 2 else 0  # P0/P1 block in rounds 1-2, P0 only after
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
        return {"state": "failure", "description": f"round {round_no}: {len(blocking)} unresolved P0/P1 (push a fix): {', '.join(blocking)}"[:140], "queue": queue, "nudge": False}
    return {"state": "success", "description": f"round {round_no}: no blocking findings" + (f"; {len(queue)} queued for janitor" if queue else ""), "queue": queue, "nudge": False}


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


def wants_rereview(pr):
    """R6: after a fix push, should the poller ask Codex to re-review? (Codex never
    re-reviews a push on its own.) True iff Codex's summary exists for a commit that is
    NOT the head, Codex is not mid-review (`running` for any commit -- the row may still
    name the previous commit when a review starts; compute's 10-minute nudge is the
    backstop), and nobody has already asked (`nudged`). Never for drafts (Codex skips
    them), closed PRs, or hotfixes (review skipped by design)."""
    s = pr.get("summary")
    if not s or not s.get("commit"):
        return False
    if pr["head_sha"].startswith(s["commit"]) or s.get("status") == "running":
        return False
    if pr.get("nudged") or pr.get("draft") or not is_open(pr) or "hotfix" in pr.get("labels", []):
        return False
    return True


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
    '@codex review' was created after head_pushed_at -- i.e. this SHA has already
    been nudged, so main() should not nudge it again."""
    pushed = ts(head_pushed_at)
    return any(c["body"].strip().startswith(REVIEW_REQUEST) and ts(c["created_at"]) > pushed for c in comments)


def derive_asked_at(head_pushed_at, ready_times):
    """R23b/R23d: when Codex was asked about this head, from PR state alone so every mode
    (poll, one-shot, dispatch) computes the same clock: the later of head_pushed_at and the
    latest ready_for_review event (Codex reviews on draft -> ready). Returns the winning
    ISO timestamp string.
    `@codex review` comments (any author: the gate's nudge / re-review request or a human or
    agent retry) never move it -- they only count for is_nudged(). Each clock start is an
    event that starts its own poller (push -> synchronize, draft -> ready ->
    ready_for_review), so clock_start + 31 min always falls inside that poller's 35-min job
    timeout; a comment-moved clock (R23b/c) could push the deadline past it, and Actions
    killed the poller while compute still said pending."""
    return max([head_pushed_at, *ready_times], key=ts)


def reactions_from_rest(items):
    """Map the REST 'list reactions' payload to compute()'s {"user","content","created_at"}
    shape. GraphQL's Reaction.user is typed User, so a Bot's own 👍 comes back null there
    -- the REST issue-reactions endpoint reports it correctly (as e.g.
    'chatgpt-codex-connector[bot]'), so that's what fetch() reads instead."""
    return [{
        "user": (item.get("user") or {}).get("login", ""),
        "content": item.get("content", ""),
        "created_at": item.get("created_at", ""),
    } for item in items]


GQL = """
query($owner:String!,$name:String!,$n:Int!){ repository(owner:$owner,name:$name){ pullRequest(number:$n){
  headRefOid isDraft state labels(first:20){nodes{name}}
  timelineItems(last:5, itemTypes:[READY_FOR_REVIEW_EVENT]){nodes{... on ReadyForReviewEvent{createdAt}}}
  commits(last:1){nodes{commit{committedDate checkSuites(first:20){nodes{createdAt app{slug}}}}}}
  reviews(first:100){nodes{author{login} submittedAt commit{oid}}}
  comments(last:100){nodes{author{login} body createdAt}}
  reviewThreads(first:100){nodes{id isResolved comments(first:20){nodes{author{login} body createdAt commit{oid}}}}}
}}}"""


def _newest_summary(comments):
    codex_marked = [c for c in comments if is_codex(c["author"]) and SUMMARY_MARKER in c["body"]]
    if not codex_marked:
        return None
    newest = max(codex_marked, key=lambda c: ts(c["created_at"]))
    return parse_summary(newest["body"])


def fetch(repo, number):
    owner, name = repo.split("/")
    d = json.loads(gh("api", "graphql", "-f", f"query={GQL}", "-F", f"owner={owner}", "-F", f"name={name}", "-F", f"n={number}"))
    p = d["data"]["repository"]["pullRequest"]
    login = lambda x: (x or {}).get("login", "")
    comments = [{"author": login(c["author"]), "body": c["body"], "created_at": c["createdAt"]} for c in p["comments"]["nodes"]]
    head_pushed_at = pushed_at(p["commits"]["nodes"][0])
    # Reactions come from REST, not GraphQL: GraphQL's Reaction.user is typed User, so
    # a Bot's own 👍 (Codex reacts as chatgpt-codex-connector[bot]) comes back null there.
    # Single page, no --paginate: gh concatenates array pages as separate JSON documents
    # ("[..][..]"), which json.loads can't parse; Codex adds at most a couple of
    # reactions per PR, so one page of 100 is ample.
    reactions_raw = json.loads(gh("api", f"repos/{repo}/issues/{number}/reactions?per_page=100"))
    return {
        "head_sha": p["headRefOid"], "head_pushed_at": head_pushed_at,
        "draft": p["isDraft"], "state": p["state"],
        "labels": [l["name"] for l in p["labels"]["nodes"]],
        "reviews": [{"author": login(r["author"]), "submitted_at": r["submittedAt"], "commit_sha": (r["commit"] or {}).get("oid", "")} for r in p["reviews"]["nodes"]],
        "comments": comments,
        "reactions": reactions_from_rest(reactions_raw),
        "threads": [{"id": t["id"], "is_resolved": t["isResolved"], "comments": [
            {"author": login(c["author"]), "body": c["body"], "created_at": c["createdAt"], "commit_sha": (c["commit"] or {}).get("oid", "")} for c in t["comments"]["nodes"]]} for t in p["reviewThreads"]["nodes"]],
        "nudged": is_nudged(comments, head_pushed_at),
        "asked_at": derive_asked_at(head_pushed_at, [e["createdAt"] for e in p["timelineItems"]["nodes"] if e.get("createdAt")]),
        "summary": _newest_summary(comments),
    }


def post_status(repo, sha, verdict):
    cur = json.loads(gh("api", f"repos/{repo}/commits/{sha}/status"))
    for s in cur.get("statuses", []):
        if s["context"] == "review-verdict" and s["state"] == verdict["state"] and s["description"] == verdict["description"]:
            return "unchanged"
    gh("api", "-X", "POST", f"repos/{repo}/statuses/{sha}", "-f", f"state={verdict['state']}", "-f", "context=review-verdict",
       "-f", f"description={verdict['description']}")
    return "posted"


def resolve_thread(thread_id, reply):
    gh("api", "graphql", "-f", "query=mutation($id:ID!,$b:String!){ addPullRequestReviewThreadReply(input:{pullRequestReviewThreadId:$id, body:$b}){ comment{id} } }", "-F", f"id={thread_id}", "-F", f"b={reply}")
    gh("api", "graphql", "-f", "query=mutation($id:ID!){ resolveReviewThread(input:{threadId:$id}){ thread{id} } }", "-F", f"id={thread_id}")


def comment(repo, number, body):
    gh("api", "-X", "POST", f"repos/{repo}/issues/{number}/comments", "-f", f"body={body}")


def run_once(repo, number, rereview=False, now=None):
    """The one-shot path, shared by `<repo> <n>` and every `poll` iteration:
    fetch -> [R6 re-review request] -> compute -> post status -> resolve queued -> nudge.
    No mode-specific clock input: asked_at comes from fetch() (R23b).
    Returns (pr, verdict, now) so the poller can decide whether to keep going."""
    pr = fetch(repo, number)
    now = now or datetime.now(timezone.utc)
    if rereview and wants_rereview(pr):
        comment(repo, number, REVIEW_REQUEST)
        pr["nudged"] = True  # this SHA is now asked; compute must not nudge it a second time
        print("re-review requested", flush=True)
    v = compute(pr, now)
    print(json.dumps({"pr": number, "sha": pr["head_sha"], **v}), flush=True)
    print(post_status(repo, pr["head_sha"], v), flush=True)
    for tid in v["queue"]:
        resolve_thread(tid, f"{QUEUE_MARK}: advisory finding, handed to the weekly janitor (review v4 §7).")
    if v["nudge"]:
        comment(repo, number, REVIEW_REQUEST)
    return pr, v, now


def poll(repo, number, rereview=False, *, step=None, sleep=time.sleep,
         interval=POLL_INTERVAL, max_errors=POLL_MAX_ERRORS):
    """run_once every `interval` s until poll_done. `rereview` applies to the first
    successful iteration only. A failed iteration (gh/API hiccup) is retried; only
    `max_errors` consecutive failures end the poller, because a dead poller leaves the PR
    pending with no nudge and no 30-minute escalation until some other event arrives."""
    step = step or run_once
    errors = 0
    while True:
        try:
            pr, v, now = step(repo, number, rereview=rereview)
        except Exception as e:  # noqa: BLE001 -- bounded retry, re-raised below
            errors += 1
            detail = getattr(e, "stderr", "") or ""
            print(f"poll: iteration failed ({errors}/{max_errors}): {e!r} {detail}".rstrip(), file=sys.stderr, flush=True)
            if errors >= max_errors:
                raise
            sleep(interval)
            continue
        errors, rereview = 0, False
        if poll_done(pr, v, now):
            return v
        sleep(interval)


USAGE = ("usage: review_verdict.py compute <pr.json> [now-iso]\n"
         "       review_verdict.py poll <owner/repo> <number> [--rereview]\n"
         "       review_verdict.py <owner/repo> <number>")


def main(argv):
    args = argv[1:]
    if args[:1] == ["compute"]:  # compute <pr.json> [now-iso]  (drills + local runs)
        pr = json.load(open(args[1])); now = ts(args[2]) if len(args) > 2 else datetime.now(timezone.utc)
        print(json.dumps(compute(pr, now))); return 0
    try:
        if args[:1] == ["poll"]:  # poll <repo> <n> [--rereview]  (after a push / ask)
            if len(args) not in (3, 4) or args[3:] not in ([], ["--rereview"]):
                print(USAGE, file=sys.stderr); return 2
            poll(args[1], int(args[2]), rereview=args[3:] == ["--rereview"]); return 0
        if len(args) != 2:
            print(USAGE, file=sys.stderr); return 2
        run_once(args[0], int(args[1])); return 0
    except subprocess.CalledProcessError as e:
        print(f"gh failed (exit {e.returncode}): {e.stderr}", file=sys.stderr); return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
