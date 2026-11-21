#!/usr/bin/env python3
"""pr_ticket_lint.py — does a PR cite a Linear ticket? (SSSF-72; stdlib only)

Usage:
  gh pr view N -R BJGLLC/REPO --json title,body,headRefName,labels,author | pr_ticket_lint.py
  jq .pull_request "$GITHUB_EVENT_PATH" | pr_ticket_lint.py --annotate warning
  pr_ticket_lint.py --title T [--body B] [--branch BR] [--label L ...] [--author LOGIN]

Exit 0 = a Linear ID (KEY-123, any case, KEY one of the workspace's team keys) appears in the title, body
or head branch, or the PR is exempt (label `hotfix`; dependabot/renovate). Exit 1 = none, with one
::error (or ::warning) line naming what is missing. Exit 2 = unreadable input. An allowlist of real team
keys, not [A-Z]+-\\d+, is what keeps UTF-8, SHA-256, ISO-8601 or CVE-2024-1 from counting as a cite.
Janitor PRs need no exemption: janitor_run.publish() puts the ticket ID in the body.
"""
import argparse
import json
import re
import sys

# Every Linear team key in the workspace (list_teams, 2026-11-21). A superset of deploy_notify.TEAM_KEYS,
# which test_pr_ticket_lint pins; HDS, FIN and YOU are real teams whose PRs may cite them.
LINEAR_KEYS = ("tool", "ops", "cd", "rnd", "auto", "data", "sssf", "hds", "bjg", "fin", "you")
TICKET_RE = re.compile(r"(?<![A-Za-z0-9])(" + "|".join(LINEAR_KEYS) + r")-([1-9][0-9]*)(?![0-9])", re.IGNORECASE)
EXEMPT_LABELS = {"hotfix"}
# Dependency bots open PRs no ticket drives. gh's --json author reports app/<name>; the event payload <name>[bot].
# Not every bot: agents open PRs as bjg-agent[bot], and those must cite a ticket like anyone else.
EXEMPT_AUTHORS = {"dependabot[bot]", "app/dependabot", "renovate[bot]", "app/renovate"}


def _s(v, what):
    if v is None:
        return ""
    if not isinstance(v, str):
        raise ValueError(f"{what} must be a string")
    return v


def normalize(pr):
    """gh --json shape or pull_request event payload -> {title, body, branch, labels, author}."""
    if not isinstance(pr, dict):
        raise ValueError("input must be a JSON object")
    head = pr.get("head") if isinstance(pr.get("head"), dict) else {}
    who = pr.get("author") if isinstance(pr.get("author"), dict) else pr.get("user")
    labels = pr.get("labels") or []
    if not isinstance(labels, list):
        raise ValueError("labels must be a list")
    names = [_s(x.get("name") if isinstance(x, dict) else x, "a label") for x in labels]
    return {"title": _s(pr.get("title"), "title"), "body": _s(pr.get("body"), "body"),
            "branch": _s(pr.get("headRefName", head.get("ref")), "branch"), "labels": names,
            "author": _s(who.get("login") if isinstance(who, dict) else who, "author")}


def lint(pr):
    """-> (exit code, one output line)."""
    hot = sorted(EXEMPT_LABELS & {n.lower() for n in pr["labels"]})
    if hot:
        return 0, f"pr_ticket_lint: exempt (label {hot[0]})"
    if pr["author"].lower() in EXEMPT_AUTHORS:
        return 0, f"pr_ticket_lint: exempt (author {pr['author']})"
    for where in ("title", "body", "branch"):
        m = TICKET_RE.search(pr[where])
        if m:
            return 0, f"pr_ticket_lint: ok ({m.group(1).upper()}-{m.group(2)} in {where})"
    return 1, ("title=PR cites no Linear ticket::No Linear ID (e.g. SSSF-72) in the PR title, body or branch: "
               "cite the ticket by bare ID in the body (never Fixes), or label hotfix")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--annotate", choices=("error", "warning"), default="error")
    ap.add_argument("--title")
    ap.add_argument("--body", default="")
    ap.add_argument("--branch", default="")
    ap.add_argument("--label", action="append", default=[])
    ap.add_argument("--author", default="")
    a = ap.parse_args(argv)
    try:
        if a.title is not None:
            pr = {"title": a.title, "body": a.body, "branch": a.branch, "labels": a.label, "author": a.author}
        else:
            pr = normalize(json.loads(sys.stdin.read()))
    except (ValueError, AttributeError) as e:   # json.JSONDecodeError is a ValueError
        print(f"::{a.annotate} title=pr_ticket_lint::unreadable PR input: {' '.join(str(e).split())[:200]}")
        return 2
    rc, line = lint(pr)
    print(f"::{a.annotate} {line}" if rc else line)
    return rc


if __name__ == "__main__":
    sys.exit(main())
