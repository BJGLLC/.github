#!/usr/bin/env python3
"""deploy_notify.py: deploy/rollback messages for Linear and the PR (Review v4 §8, phase 4).

Pure, unit-tested core: tickets(), final_state(), body(). Thin I/O: `notify` reads the commit
range from the GitHub compare API and posts one PR comment plus one comment per Linear ticket
(cap 5). I/O failures warn and exit 0. A deploy never goes red because Linear is down (OPS-350).

  deploy_notify.py state  --reason deploy|rollback --ship R --smoke-ok T --rb-ship R --rb-smoke-ok T
  deploy_notify.py notify --repo OWNER/NAME --state S --surface TEXT --attempted SHA
                          [--live-before SHA] [--run-url URL] [--pr N] [--freeze N]
                          [--env production|drill] [--rollback-hint TEXT] [--dry-run]
"""
import argparse
import json
import os
import re
import subprocess
import sys
import urllib.request

TEAM_KEYS = ("cd", "tool", "auto", "sssf", "data", "ops", "rnd", "bjg")  # every live team, spec §8
TICKET_RE = re.compile(r"\b(" + "|".join(TEAM_KEYS) + r")-([0-9]+)\b", re.IGNORECASE)
CAP = 5
LINEAR = "https://api.linear.app/graphql"
# Every state final_state() returns; body() has a branch for each. An unknown state would render as a
# false "UNKNOWN" alarm, so `notify --state` rejects anything else.
STATES = ("deployed", "rolled-back", "auto-rolled-back", "ship-failed", "rollback-failed", "smoke-failed-no-target")
ZERO_SHA = "0" * 40  # github.event.before on a branch's first push


def tickets(messages, cap=CAP):
    """Distinct ticket ids from commit messages (newest first). Subjects outrank bodies."""
    subjects = [m.split("\n", 1)[0] for m in messages]
    bodies = [m.split("\n", 1)[1] if "\n" in m else "" for m in messages]
    out = []
    for text in subjects + bodies:
        for key, num in TICKET_RE.findall(text):
            t = f"{key.upper()}-{num}"
            if t not in out:
                out.append(t)
                if len(out) == cap:
                    return out
    return out


def final_state(reason, ship, smoke_ok, rb_ship, rb_smoke_ok):
    """ship/rb_ship are job results (success|failure|skipped|cancelled|''); *_ok are 'true'|'false'|''."""
    if ship != "success":
        return "ship-failed"
    if smoke_ok == "true":
        return "deployed" if reason == "deploy" else "rolled-back"
    if reason != "deploy":
        return "rollback-failed"
    if rb_ship in ("", "skipped"):
        return "smoke-failed-no-target"
    if rb_ship == "success" and rb_smoke_ok == "true":
        return "auto-rolled-back"
    return "rollback-failed"


def body(state, surface, repo, attempted, live_before="", run_url="", freeze=None, env="production", rollback_hint=None):
    a, b = attempted[:7], (live_before or "")[:7]
    name = repo.split("/")[-1]
    tag = "[drill] " if env == "drill" else ""
    if run_url.startswith("http"):
        run = f" · [run]({run_url})"
    else:
        run = f" · log: `{run_url}`" if run_url else ""
    fz = f"\nForward deploys frozen until a `hotfix` PR merges: {repo}#{freeze}." if freeze else ""
    if state == "deployed":
        rb = rollback_hint or (f"`bin/rollback {name} {b}`" if b else "no earlier green deploy on record")
        was = f" (was `{b}`)" if b else ""
        return f"{tag}**Deployed** — {surface} shipped `{a}`{was}{run}\nRollback: {rb}"
    if state == "rolled-back":
        if a == b:
            return f"{tag}**Redeployed** — {surface} on `{a}` (no-op rollback drill){run}"
        return f"{tag}**Rolled back** — {surface} is on `{a}` (was `{b}`){run}{fz}"
    if state == "auto-rolled-back":
        return f"{tag}**Auto-rolled back** — `{a}` failed the smoke check; {surface} is back on `{b}`{run}{fz}"
    if state == "ship-failed":
        return f"{tag}**Deploy failed before going live** — `{a}` did not upload; {surface} unchanged at `{b or 'unknown'}`{run}"
    return (f"{tag}**Deploy {state}** — `{a}` failed the smoke check and no verified rollback followed; "
            f"{surface} state is UNKNOWN{run}{fz}\nNext: `bin/rollback {name} <sha>` from a box with gh.")


def gh(*args):
    try:
        return subprocess.run(["gh", *args], capture_output=True, text=True, check=True).stdout
    except subprocess.CalledProcessError as e:
        print(e.stderr, file=sys.stderr)
        raise


def range_messages(repo, older, newer):
    if older and older != newer:
        out = gh("api", f"repos/{repo}/compare/{older}...{newer}", "--jq", "[.commits[].commit.message] | reverse")
    else:
        out = gh("api", f"repos/{repo}/commits/{newer}", "--jq", "[.commit.message]")
    return json.loads(out)


def linear(key, query, variables):
    req = urllib.request.Request(LINEAR, data=json.dumps({"query": query, "variables": variables}).encode(),
                                 headers={"Authorization": key, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.load(r)


def post_linear(key, ticket, text):
    issue = (linear(key, "query($t: String!) { issue(id: $t) { id } }", {"t": ticket}).get("data") or {}).get("issue")
    if not issue:
        return False
    res = linear(key, "mutation($id: String!, $b: String!) { commentCreate(input: {issueId: $id, body: $b}) { success } }",
                 {"id": issue["id"], "b": text})
    return bool(((res.get("data") or {}).get("commentCreate") or {}).get("success"))


def cmd_notify(a):
    if a.live_before == ZERO_SHA:
        a.live_before = ""
    # A manual rollback reports the range it undid (target..was-live); everything else, was-live..attempted.
    older, newer = (a.attempted, a.live_before) if a.state == "rolled-back" else (a.live_before, a.attempted)
    try:
        msgs = range_messages(a.repo, older, newer or a.attempted)
    except Exception as e:  # noqa: BLE001 — reporting must not fail the deploy
        print(f"::warning::commit range unreadable: {e}")
        msgs = []
    ids = tickets(msgs)
    text = body(a.state, a.surface, a.repo, a.attempted, a.live_before, a.run_url, a.freeze, a.env, a.rollback_hint)
    print(json.dumps({"tickets": ids, "body": text}))
    if a.dry_run:
        return 0
    if a.pr:
        try:
            gh("pr", "comment", str(a.pr), "-R", a.repo, "--body", text + "\n<!-- review-v4-deploy -->")
        except Exception as e:  # noqa: BLE001
            print(f"::warning::PR comment failed: {e}")
    key = os.environ.get("LINEAR_API_KEY", "")
    if not key:
        print("::notice::LINEAR_API_KEY not set; Linear posting skipped")
        return 0
    if not ids:
        print("::warning title=OPS-350 untied deploy::no ticket ref in the deployed range")
        return 0
    for t in ids:
        try:
            print(f"{t}: {'posted' if post_linear(key, t, text) else 'not found in Linear'}")
        except Exception as e:  # noqa: BLE001
            print(f"::warning::{t}: {e}")
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(prog="deploy_notify.py")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("state")
    for f in ("--reason", "--ship", "--smoke-ok", "--rb-ship", "--rb-smoke-ok"):
        s.add_argument(f, default="")
    n = sub.add_parser("notify")
    n.add_argument("--repo", required=True)
    n.add_argument("--state", required=True, choices=STATES)
    n.add_argument("--surface", required=True)
    n.add_argument("--attempted", required=True)
    n.add_argument("--live-before", default="")
    n.add_argument("--run-url", default="")
    n.add_argument("--pr", default="")
    n.add_argument("--freeze", default=None)
    n.add_argument("--env", default="production")
    n.add_argument("--rollback-hint", default=None)
    n.add_argument("--dry-run", action="store_true")
    a = p.parse_args(argv)
    if a.cmd == "state":
        print(final_state(a.reason, a.ship, a.smoke_ok, a.rb_ship, a.rb_smoke_ok))
        return 0
    a.freeze = a.freeze or None
    return cmd_notify(a)


if __name__ == "__main__":
    sys.exit(main())
