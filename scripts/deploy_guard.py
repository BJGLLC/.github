#!/usr/bin/env python3
"""deploy_guard.py: may this SHA ship now, and what is live? Review v4 phase 4 (spec §8).

decide() is pure and unit-tested. Rules, in order:
  1. --force-smoke-fail is drill-only; a target without a preview alias cannot drill.
  2. The SHA must be on main (ancestor-or-equal of main's tip). A dispatch can name any commit.
  3. reason=deploy needs a `ci` check run (GitHub Actions app) that succeeded on the SHA.
     reason=rollback needs that OR an earlier successful deploy of the SHA to this env
     (commits from before phase 3 have no `ci`, but they were live and healthy).
  4. reason=deploy skips a SHA that is already live or not newer than live. CI runs finish out
     of order, and the older one must not overwrite the newer deploy.
  5. reason=deploy skips while a `deploy-freeze` issue is open for the env, unless the SHA's PR
     has the `hotfix` label (spec §8: rollback first, hotfix second).
The ledger is GitHub Deployments. A deploy's auto-rollback target is whatever was live.
Each successful deploy records the build params it shipped with (payload.build); a rollback rebuilds
the target with THOSE, never the failed deploy's inputs. No record -> no rollback ship, loudly.
"""
import argparse
import datetime
import json
import subprocess
import sys

FREEZE_LABEL, HOTFIX_LABEL, ACTIONS_APP = "deploy-freeze", "hotfix", "github-actions"
# Every cf-ship input that can differ per revision (sha and cf_branch are per-run, not build params).
BUILD_KEYS = ("kind", "node", "install", "build", "out_dir", "cf_project")
FREEZE_BODY = ("{env} was rolled back from `{frm}` to `{to}` ({run}).\n\n"
               "Forward deploys are held: the deploy guard skips every SHA whose PR is not labelled `hotfix` "
               "until one deploys green. Ship the fix, or the revert that `bin/rollback {name} --revert` opens, "
               "as a `hotfix` PR; its deploy closes this issue. Review v4 spec §8.")


def decide(c):
    live = c.get("live_sha") or ""
    base = {"go": False, "sha": c["sha"], "prev_sha": "", "from_sha": live, "pr": c.get("pr") or "",
            "decided_at": c.get("now", ""), "prev_build": "", "message": ""}
    s7 = c["sha"][:7]

    def no(msg):
        return {**base, "message": msg}

    if c.get("force_smoke_fail") and c["env"] != "drill":
        return no("refused: --force-smoke-fail is drill-only and never reaches a live environment")
    if c["env"] == "drill" and not c.get("has_drill_alias", True):
        return no("refused: no preview alias for this target; drill it with a no-op rollback (bin/rollback <repo> <live sha>)")
    if not c["on_main"]:
        return no(f"refused: {s7} is not on main")
    green = c["ci"] == "success"
    if c["reason"] == "rollback":
        if not green and c["sha"] not in c.get("ledger_success_shas", []):
            return no(f"refused: {s7} has no green ci and never deployed successfully to {c['env']}")
        return {**base, "go": True, "message": f"go: rollback to {s7}" + (" (no-op redeploy)" if c["sha"] == live else "")}
    if c["reason"] != "deploy":
        return no(f"refused: unknown reason {c['reason']!r}")
    if not green:
        return no(f"skip: ci is {c['ci']} on {s7}")
    if live == c["sha"]:
        return no(f"skip: {s7} is already live")
    if live and not c.get("newer_than_live"):
        return no(f"skip: {s7} is not newer than live {live[:7]} (CI finished out of order)")
    if c.get("freezes") and HOTFIX_LABEL not in c.get("pr_labels", []):
        return no(f"skip: forward deploys frozen by #{c['freezes'][0]}; ship the fix as a `hotfix` PR")
    tail = f", auto-rollback target {live[:7]}" if live else ", no rollback target on record (first v4 deploy)"
    lb = c.get("live_build")
    return {**base, "go": True, "prev_sha": live, "prev_build": json.dumps(lb, sort_keys=True) if lb else "",
            "message": f"go: {s7}{tail}"}


def pick_live(deps):
    """deps: newest first, each {sha, state, created_at, payload}. Live = newest success."""
    for d in deps:
        if d["state"] == "success":
            return d["sha"]
    return ""


def pick_previous(deps):
    """Newest success that is not live and was never rolled away from."""
    live = pick_live(deps)
    bad = {(d.get("payload") or {}).get("rolled_back_from") for d in deps} - {None, ""}
    for d in deps:
        if d["state"] == "success" and d["sha"] != live and d["sha"] not in bad:
            return d["sha"]
    return ""


def valid_build(b):
    return isinstance(b, dict) and all(k in b and isinstance(b[k], str) for k in BUILD_KEYS)


def recorded_build(deps, sha):
    """The build params the newest successful deploy of `sha` shipped with, or None (deployed before
    params were recorded, or the record is malformed). Never guessed: None means no rollback ship."""
    for d in deps:
        if d["sha"] == sha and d["state"] == "success":
            b = (d.get("payload") or {}).get("build")
            return {k: b[k] for k in BUILD_KEYS} if valid_build(b) else None
    return None


def deployment_request(sha, env, reason, run_url, rolled_back_from="", build=None):
    payload = {"reason": reason, "run_url": run_url}
    if rolled_back_from:
        payload["rolled_back_from"] = rolled_back_from
    if build:
        payload["build"] = build
    return {"ref": sha, "environment": env, "auto_merge": False, "required_contexts": [],
            "production_environment": env.startswith("production"), "transient_environment": env == "drill",
            "description": f"review-v4 {reason}"[:140], "payload": payload}


def freezes_to_close(freezes, decided_at):
    """Close only freezes that existed when the guard said go, so a freeze opened meanwhile by a
    concurrent bin/rollback survives this deploy. Strict <: a freeze from the same second stays open."""
    return [f["number"] for f in freezes if f["created_at"] < decided_at]


# ---- I/O (thin; exercised by the Task 7 live run and the drills) -----------------------------

def gh(*args, stdin=None):
    try:
        return subprocess.run(["gh", *args], input=stdin, capture_output=True, text=True, check=True).stdout.strip()
    except subprocess.CalledProcessError as e:
        print(e.stderr, file=sys.stderr)
        raise


def gh_json(path):
    return json.loads(gh("api", path))


def ledger(repo, env, n=20):
    out = []
    for d in gh_json(f"repos/{repo}/deployments?environment={env}&per_page={n}"):
        st = gh_json(f"repos/{repo}/deployments/{d['id']}/statuses?per_page=1")
        payload = d.get("payload") or {}
        if isinstance(payload, str):
            payload = json.loads(payload or "{}")
        out.append({"sha": d["sha"], "state": st[0]["state"] if st else "pending",
                    "created_at": d["created_at"], "payload": payload})
    return out


def ci_state(repo, sha):
    runs = [r for r in gh_json(f"repos/{repo}/commits/{sha}/check-runs?check_name=ci&per_page=20")["check_runs"]
            if (r.get("app") or {}).get("slug") == ACTIONS_APP]
    if not runs:
        return "missing"
    r = max(runs, key=lambda x: x.get("started_at") or "")
    return r.get("conclusion") or r.get("status") or "missing"


def ancestor_or_equal(repo, base, head):
    return gh("api", f"repos/{repo}/compare/{base}...{head}", "--jq", ".status") in ("ahead", "identical")


def open_freezes(repo, env):
    issues = gh_json(f"repos/{repo}/issues?labels={FREEZE_LABEL}&state=open&per_page=30")
    return [{"number": i["number"], "created_at": i["created_at"]}
            for i in issues if f"[{env}]" in i["title"] and "pull_request" not in i]


def pr_of(repo, sha):
    prs = gh_json(f"repos/{repo}/commits/{sha}/pulls")
    return (prs[0]["number"], [lbl["name"] for lbl in prs[0].get("labels", [])]) if prs else (None, [])


def now():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def gather(repo, sha, reason, env, force, has_alias):
    full = gh("api", f"repos/{repo}/commits/{sha}", "--jq", ".sha")  # expands a short SHA; unknown SHA -> error
    tip = gh("api", f"repos/{repo}/commits/main", "--jq", ".sha")
    decided_at = now()  # before the freezes are read, so a freeze opened later is never older than this
    deps = ledger(repo, env)
    live = pick_live(deps)
    pr, labels = pr_of(repo, full)
    return {"sha": full, "reason": reason, "env": env, "force_smoke_fail": force, "has_drill_alias": has_alias,
            "on_main": ancestor_or_equal(repo, full, tip), "ci": ci_state(repo, full), "live_sha": live, "live_build": recorded_build(deps, live) if live else None,
            "newer_than_live": bool(live) and live != full and ancestor_or_equal(repo, live, full),
            "ledger_success_shas": [d["sha"] for d in deps if d["state"] == "success"],
            "freezes": [f["number"] for f in open_freezes(repo, env)], "pr": pr, "pr_labels": labels, "now": decided_at}


def record(a):
    frm = a.from_sha if a.from_sha and a.from_sha != a.sha else ""
    build = json.loads(a.build_json) if a.build_json else None
    dep = json.loads(gh("api", "-X", "POST", f"repos/{a.repo}/deployments", "--input", "-",
                        stdin=json.dumps(deployment_request(a.sha, a.env, a.reason, a.run_url, frm, build))))
    status = {"state": a.state, "description": f"{a.reason}: {a.state}", "auto_inactive": False}
    if a.url.startswith("http"):
        status["environment_url"] = a.url
    if a.run_url.startswith("http"):
        status["log_url"] = a.run_url
    gh("api", "-X", "POST", f"repos/{a.repo}/deployments/{dep['id']}/statuses", "--input", "-", stdin=json.dumps(status))
    return dep["id"]


def freeze_open(a):
    # One issue per rollback event, never reused: a hotfix deploy closes only the freezes that existed
    # when it decided, so a rollback that lands mid-deploy keeps its own freeze.
    gh("label", "create", FREEZE_LABEL, "-R", a.repo, "--color", "B60205",
       "--description", "Review v4: forward deploys held after a rollback", "--force")
    url = gh("issue", "create", "-R", a.repo, "--label", FREEZE_LABEL,
             "--title", f"deploy-freeze [{a.env}]: rolled back from {a.frm[:7]}",
             "--body", FREEZE_BODY.format(env=a.env, frm=a.frm[:7], to=a.to[:7], run=a.run_url, name=a.repo.split("/")[-1]))
    return int(url.rstrip("/").rsplit("/", 1)[1])


def freeze_close(a):
    for n in freezes_to_close(open_freezes(a.repo, a.env), a.decided_at):
        gh("issue", "close", str(n), "-R", a.repo, "--comment", f"Cleared: `{a.sha[:7]}` deployed green (#{a.pr}).")


def main(argv=None):
    p = argparse.ArgumentParser(prog="deploy_guard.py")
    sub = p.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("decide")
    d.add_argument("--repo", required=True)
    d.add_argument("--sha", required=True)
    d.add_argument("--reason", required=True, choices=["deploy", "rollback"])
    d.add_argument("--env", required=True)
    d.add_argument("--force-smoke-fail", action="store_true")
    d.add_argument("--no-drill-alias", action="store_true")
    r = sub.add_parser("record")
    for f in ("--repo", "--sha", "--env", "--reason", "--url", "--run-url"):
        r.add_argument(f, required=True)
    r.add_argument("--state", required=True, choices=["success", "failure"])
    r.add_argument("--from", dest="from_sha", default="")
    r.add_argument("--build-json", default="", help="the build params this deploy shipped with (a rollback reuses them)")
    for name in ("live", "previous"):
        q = sub.add_parser(name)
        q.add_argument("--repo", required=True)
        q.add_argument("--env", required=True)
    fo = sub.add_parser("freeze-open")
    for f in ("--repo", "--env", "--to", "--run-url"):
        fo.add_argument(f, required=True)
    fo.add_argument("--from", dest="frm", required=True)
    fc = sub.add_parser("freeze-close")
    for f in ("--repo", "--env", "--sha", "--decided-at"):
        fc.add_argument(f, required=True)
    fc.add_argument("--pr", default="")
    a = p.parse_args(argv)
    if a.cmd == "record" and a.build_json:
        try:
            ok = valid_build(json.loads(a.build_json))
        except ValueError:
            ok = False
        if not ok:
            p.error(f"--build-json must be a JSON object of strings with keys {', '.join(BUILD_KEYS)}")
    if a.cmd == "decide":
        print(json.dumps(decide(gather(a.repo, a.sha, a.reason, a.env, a.force_smoke_fail, not a.no_drill_alias))))
    elif a.cmd == "record":
        print(record(a))
    elif a.cmd == "live":
        print(pick_live(ledger(a.repo, a.env)))
    elif a.cmd == "previous":
        print(pick_previous(ledger(a.repo, a.env)))
    elif a.cmd == "freeze-open":
        print(freeze_open(a))
    elif a.cmd == "freeze-close":
        freeze_close(a)
    return 0


if __name__ == "__main__":
    sys.exit(main())
