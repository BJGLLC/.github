#!/usr/bin/env python3
"""ci_shape.py — assert a repo's .github/workflows/ci.yml is the Review v4 single-job `ci` caller.

Usage: ci_shape.py --repo OWNER/NAME WORKFLOWS_DIR   exit 0 = conforms, 1 = violations (printed)

Spec: claude-dotfiles docs/superpowers/specs/2026-09-26-review-system-v4-design.md §5 (SSSF-30).
Decision 01M3N14TGN (Blake, 2026-09-28, Q2): one job named `ci` that calls the shared composite
action, no reusable workflow, no fan-in. The ruleset requires ONE context, `ci`. It is only
trustworthy if (1) it reports on every PR (no path filter on pull_request) and can never be
skipped (no `needs`, no job-level `if`), (2) it is red whenever a step failed (no continue-on-error,
no matrix or name that renames the check, steps are exactly checkout + the action), and (3) nothing
else in the repo can emit a check named `ci` or `review-verdict` (SSSF-27 scope note). The only
other jobs ci.yml may hold are failure notifiers, so the workflow's conclusion always equals the
check's (phase 4's workflow_run guard reads the workflow conclusion).
Runs inside every ci run (the ci-check action's `shape` step) and in `repo-parity.sh ci-ready`.
"""
import argparse
import pathlib
import sys

import yaml

ORG_ACTION = "BJGLLC/.github/actions/ci-check@main"   # @main on purpose: plan D7 (cost, drift, safety)
LOCAL_ACTION = "./actions/ci-check"                   # BJGLLC/.github tests the action it is changing
SELF_REPO = "BJGLLC/.github"
CHECKOUT = {"uses": "actions/checkout@v4", "with": {"fetch-depth": 0, "persist-credentials": False}}
INPUTS = {"node-version", "node-cache", "python-version", "python-cache", "cache-dependency-path",
          "install", "ci-setup"}   # == actions/ci-check/action.yml inputs (test_ci_check pins it)
OUTPUTS = {"failing_step": "${{ steps.check.outputs.failing_step }}"}
CONCURRENCY = {"group": "ci-${{ github.event_name }}-${{ github.event.pull_request.number || github.sha }}",
               "cancel-in-progress": True}
RUNS_ON = "ubuntu-latest"
MAX_TIMEOUT = 30
# Every key a job named `ci` may carry. What each banned key would do to the required check:
BANNED = {
    "needs": "a failed or skipped dependency skips ci, and a skipped required check passes",
    "if": "a false if: skips ci, and a skipped required check passes",
    "continue-on-error": "a failing ci would still read as green",
    "strategy": "a matrix renames the check to `ci (...)`, so `ci` never reports",
    "name": "a job name renames the check",
    "uses": "a reusable-workflow job reports as `ci / <job>`, so `ci` never reports",
}
CI_JOB_KEYS = {"runs-on", "timeout-minutes", "outputs", "steps"}
RESERVED = {"ci", "review-verdict"}


def _on(wf):
    """PyYAML (YAML 1.1) reads a bare `on:` key as boolean True."""
    return wf.get("on", wf.get(True)) or {}


def _needs(job):
    n = job.get("needs") or []
    return [n] if isinstance(n, str) else list(n)


def is_notifier(job):
    return isinstance(job, dict) and _needs(job) == ["ci"] and job.get("if") == "failure()"


def check_ci(wf, repo):
    v = []
    if wf.get("name") != "ci":
        v.append("name must be 'ci'")
    on = _on(wf)
    if set(map(str, on)) != {"push", "pull_request", "workflow_dispatch"}:
        v.append(f"on: must be exactly push, pull_request, workflow_dispatch (got {sorted(map(str, on))})")
    push = dict(on.get("push") or {})
    ignore = push.pop("paths-ignore", None)   # SSSF-25: push-only, machine-lane paths; the push run gates nothing
    if push != {"branches": ["main"]}:
        v.append("on.push must be {branches: [main]} plus at most paths-ignore: no `paths` allowlist on a required check")
    if ignore is not None and not (isinstance(ignore, list) and ignore and all(isinstance(p, str) and p for p in ignore)):
        v.append("on.push.paths-ignore must be a non-empty list of globs (mirror claude/policy/machine-paths.txt)")
    if on.get("pull_request") not in (None, {}):
        v.append("on.pull_request must have no filters: a filtered required check never reports and the PR hangs")
    if on.get("workflow_dispatch") not in (None, {}):
        v.append("on.workflow_dispatch must take no inputs")
    if wf.get("permissions") != {"contents": "read"}:
        v.append("permissions must be exactly {contents: read}")
    if wf.get("concurrency") != CONCURRENCY:
        v.append(f"concurrency must be {CONCURRENCY}")
    jobs = wf.get("jobs") or {}
    for name, job in jobs.items():
        if name != "ci" and not is_notifier(job):
            v.append(f"jobs.{name}: ci.yml holds only job `ci` plus failure notifiers (needs: ci, if: failure()); "
                     "move anything else to its own workflow")
    ci = jobs.get("ci")
    if not isinstance(ci, dict):
        v.append("jobs.ci (the one job the ruleset requires) is missing")
        return v
    for key in sorted(set(ci) - CI_JOB_KEYS):
        v.append(f"jobs.ci.{key} is not allowed: {BANNED.get(key, 'not part of the uniform shape')}")
    if ci.get("runs-on") != RUNS_ON:
        v.append(f"jobs.ci.runs-on must be {RUNS_ON}")
    t = ci.get("timeout-minutes")
    if not (isinstance(t, int) and not isinstance(t, bool) and 1 <= t <= MAX_TIMEOUT):
        v.append(f"jobs.ci.timeout-minutes must be an integer from 1 to {MAX_TIMEOUT}")
    if ci.get("outputs") not in (None, OUTPUTS):
        v.append(f"jobs.ci.outputs must be absent or exactly {OUTPUTS} (for a failure notifier)")
    steps = ci.get("steps") or []
    if len(steps) != 2:
        v.append("jobs.ci.steps must be exactly two: actions/checkout, then the ci-check action")
        return v
    if steps[0] != CHECKOUT:
        v.append(f"jobs.ci.steps[0] must be exactly {CHECKOUT} (gitleaks reads this change's commits; no token left on disk)")
    step = steps[1] if isinstance(steps[1], dict) else {}
    want = LOCAL_ACTION if repo == SELF_REPO else ORG_ACTION
    if step.get("uses") != want:
        v.append(f"jobs.ci.steps[1].uses must be {want}")
    if step.get("id") != "check":
        v.append("jobs.ci.steps[1].id must be `check` (the outputs and failure notifiers read steps.check)")
    for key in sorted(set(step) - {"id", "uses", "with"}):
        v.append(f"jobs.ci.steps[1].{key} is not allowed (continue-on-error or if would let a red check pass)")
    given = step.get("with") or {}
    for key in sorted(set(given) - INPUTS):
        v.append(f"jobs.ci.steps[1].with.{key} is not an input of the ci-check action ({sorted(INPUTS)})")
    for key, val in sorted(given.items()):
        if not isinstance(val, str):
            v.append(f"jobs.ci.steps[1].with.{key} must be a quoted string")
    return v


def check_reserved(wdir):
    v = []
    for f in sorted(list(wdir.glob("*.yml")) + list(wdir.glob("*.yaml"))):
        if f.name == "ci.yml":
            continue
        wf = yaml.safe_load(f.read_text()) or {}
        for key, job in (wf.get("jobs") or {}).items():
            names = {str(key), str((job or {}).get("name", ""))}
            hit = sorted(names & RESERVED)
            if hit:
                v.append(f"{f.name}: job {key!r} would emit a check named {hit[0]!r}; only ci.yml's job ci may")
    return v


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--repo", required=True)
    ap.add_argument("workflows")
    a = ap.parse_args(argv)
    wdir = pathlib.Path(a.workflows)
    ci = wdir / "ci.yml"
    if not ci.is_file():
        print(f"ci_shape: {ci} missing")
        return 1
    v = check_ci(yaml.safe_load(ci.read_text()) or {}, a.repo) + check_reserved(wdir)
    for line in v:
        print(f"ci_shape: {line}")
    print("ci_shape: ok" if not v else f"ci_shape: {len(v)} violation(s)")
    return 1 if v else 0


if __name__ == "__main__":
    sys.exit(main())
