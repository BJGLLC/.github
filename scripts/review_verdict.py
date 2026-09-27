#!/usr/bin/env python3
"""review-verdict: turn Codex's advisory review into one deterministic commit status.

Pure part: compute(pr, now), parse_summary(body), pushed_at(commit_node), is_nudged(comments, head_pushed_at).
Fetch part: fetch(repo, number) via `gh`. The workflow (.github/workflows/review-verdict.yml,
a later task) calls `main` which does fetch -> compute -> post status -> auto-resolve queued
threads -> nudge. Spec: claude-dotfiles docs/superpowers/specs/2026-09-26-review-system-v4-design.md §6.
No secrets here: this repo is public and the token comes from the caller's GITHUB_TOKEN env.
"""
import json
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone

CODEX = "chatgpt-codex-connector"
NUDGE_AFTER = timedelta(minutes=10)
UNAVAILABLE_AFTER = timedelta(minutes=30)
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


def compute(pr, now):
    if "hotfix" in pr.get("labels", []):
        return {"state": "success", "description": "hotfix: review skipped, post-hoc queued", "queue": [], "nudge": False}

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
        age = now - pushed
        if age >= UNAVAILABLE_AFTER:
            return {"state": "failure", "description": "codex-unavailable: no verdict in 30 min. Retry `@codex review`, or label `hotfix` if urgent.", "queue": [], "nudge": False}
        nudge = age >= NUDGE_AFTER and not pr.get("nudged", False)
        if summary_status == "running":
            nudge = False  # Codex is already working this SHA; don't ping it again
        # No minute count in the description: it must be stable across polls of the
        # same SHA, or every poll posts a "changed" status (post_status compares
        # description too).
        return {"state": "pending", "description": f"waiting for Codex review of {head_sha[:7]}", "queue": [], "nudge": nudge}

    round_no = len(reviews)  # distinct Codex review submissions on this PR, any SHA
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


# ---- fetch / act (thin, untested; every decision is in compute) -------------------
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
    for c in comments:
        if c["body"].strip().startswith("@codex review") and ts(c["created_at"]) > pushed:
            return True
    return False


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
  headRefOid labels(first:20){nodes{name}}
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
        "labels": [l["name"] for l in p["labels"]["nodes"]],
        "reviews": [{"author": login(r["author"]), "submitted_at": r["submittedAt"], "commit_sha": (r["commit"] or {}).get("oid", "")} for r in p["reviews"]["nodes"]],
        "comments": comments,
        "reactions": reactions_from_rest(reactions_raw),
        "threads": [{"id": t["id"], "is_resolved": t["isResolved"], "comments": [
            {"author": login(c["author"]), "body": c["body"], "created_at": c["createdAt"], "commit_sha": (c["commit"] or {}).get("oid", "")} for c in t["comments"]["nodes"]]} for t in p["reviewThreads"]["nodes"]],
        "nudged": is_nudged(comments, head_pushed_at),
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


def main(argv):
    if argv[1:2] == ["compute"]:  # compute <pr.json> [now-iso]  (drills + local runs)
        pr = json.load(open(argv[2])); now = ts(argv[3]) if len(argv) > 3 else datetime.now(timezone.utc)
        print(json.dumps(compute(pr, now))); return 0
    repo, number = argv[1], int(argv[2])
    pr = fetch(repo, number)
    v = compute(pr, datetime.now(timezone.utc))
    print(json.dumps({"pr": number, "sha": pr["head_sha"], **v}))
    print(post_status(repo, pr["head_sha"], v))
    for tid in v["queue"]:
        resolve_thread(tid, f"{QUEUE_MARK}: advisory finding, handed to the weekly janitor (review v4 §7).")
    if v["nudge"]:
        gh("api", "-X", "POST", f"repos/{repo}/issues/{number}/comments", "-f", "body=@codex review")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
