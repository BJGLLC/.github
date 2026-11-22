#!/usr/bin/env python3
"""one_way_door.py — is this change one-way (can't be undone)? SSSF-48; stdlib only; NOT wired into any gate.

Pure part:
  classify(paths, diff, repo, patterns) -> {"door": "one-way"|"two-way", "reasons": [...], "blast": [...]}
  parse_danger_block(body) -> {"door", "blast", "approval", "errors"}
  decide(body, paths, diff, repo, patterns) -> classify + the body's self-report, which can raise to
      one-way but never lower: the body is written by the agent making the change.
The pattern list is data, not code: one-way-patterns.json at the repo root, next to AGENTS.md's Code
Review Rules. A path pattern fires on any changed path, both sides of a rename and deleted files
included (the diff headers are read, because `gh pr view --json files` names only the new path). A
content pattern fires on an ADDED line of a file outside content_exempt_paths (tests, docs, *.md):
removing a send or a DROP is not the danger.

Report-only CLI (the dry run; nothing posts a status):
  one_way_door.py --repo BJGLLC/X --git-dir PATH --last 100 [--ref main]   # first-parent commits; git log/diff only, no fetch
Design: docs/specs/2026-11-21-one-way-door-lane.md.
"""
import argparse
import json
import os
import re
import subprocess
import sys

BLASTS = ("local", "repo", "customer", "money")
DOORS = ("one-way", "two-way")
DEFAULT_PATTERNS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "one-way-patterns.json")
BLOCK_HELP = "door: one-way | two-way, blast: local | repo | customer | money"
COURT_CODE = r"\d{4}-q\d+"
EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"

_glob_cache = {}


def glob_match(glob, path):
    """Path glob: `**/` = zero or more directories, `**` = anything, `*` and `?` stay inside one segment."""
    rx = _glob_cache.get(glob)
    if rx is None:
        out, i = [], 0
        while i < len(glob):
            if glob.startswith("**/", i):
                out.append("(?:.*/)?"); i += 3
            elif glob.startswith("**", i):
                out.append(".*"); i += 2
            elif glob[i] == "*":
                out.append("[^/]*"); i += 1
            elif glob[i] == "?":
                out.append("[^/]"); i += 1
            else:
                out.append(re.escape(glob[i])); i += 1
        rx = _glob_cache[glob] = re.compile("".join(out) + r"\Z")
    return bool(rx.match(path))


def load_patterns(path=DEFAULT_PATTERNS):
    with open(path) as f:
        p = json.load(f)
    for entry in p["content_patterns"]:
        entry["_rx"] = re.compile(entry["regex"])
    return p


def _repo_name(repo):
    return (repo or "").rsplit("/", 1)[-1].lower()


def _applies(entry, repo):
    repos = entry.get("repos")
    return not repos or _repo_name(repo) in {_repo_name(r) for r in repos}


def _unquote(p):
    """git quotes a path with unusual bytes as "a/caf\\303\\251"; undo that."""
    p = p.rstrip("\t")
    if len(p) >= 2 and p[0] == p[-1] == '"':
        p = p[1:-1].encode("latin-1", "backslashreplace").decode("unicode_escape").encode("latin-1").decode("utf-8", "replace")
    return p


def _strip_side(p):
    p = _unquote(p)
    return None if p == "/dev/null" else (p[2:] if p[:2] in ("a/", "b/") else p)


def _sections(diff):
    """Yield (old_path, new_path, added_lines) per file section of a unified `git diff`."""
    old = new = None
    added, started, in_hunk = [], False, False
    for line in (diff or "").splitlines():
        if line.startswith("diff --git "):
            if started:
                yield old, new, added
            started, in_hunk, added = True, False, []
            old = new = None
            # Unquoted "a/X b/X": split where both halves name the same file (paths may hold spaces).
            m = re.match(r"a/(.*) b/(.*)\Z", line[len("diff --git "):])
            if m and m.group(1) == m.group(2):
                old = new = m.group(1)
            elif m and " b/" not in m.group(1):
                old, new = m.group(1), m.group(2)
            continue
        if not started:
            continue
        if line.startswith("@@"):
            in_hunk = True
        elif not in_hunk and line.startswith("rename from "):
            old = _unquote(line[len("rename from "):])
        elif not in_hunk and line.startswith("rename to "):
            new = _unquote(line[len("rename to "):])
        elif not in_hunk and line.startswith("--- "):
            old = _strip_side(line[4:])
        elif not in_hunk and line.startswith("+++ "):
            new = _strip_side(line[4:])
        elif in_hunk and line.startswith("+"):
            added.append(line[1:])
    if started:
        yield old, new, added


def diff_paths(diff):
    """Every path a diff touches: both sides of a rename, deleted and created files. Sorted, unique."""
    out = set()
    for old, new, _ in _sections(diff):
        out.update(p for p in (old, new) if p)
    return sorted(out)


def classify(paths, diff, repo, patterns=None):
    """One-way if any path pattern matches a touched path or any content pattern an added line."""
    patterns = patterns if patterns is not None else load_patterns()
    for entry in patterns.get("content_patterns", []):
        if "_rx" not in entry:
            entry["_rx"] = re.compile(entry["regex"])
    touched = sorted(set(paths or []) | set(diff_paths(diff)))
    reasons = []
    for entry in patterns.get("path_patterns", []):
        if not _applies(entry, repo):
            continue
        for path in touched:
            if any(glob_match(g, path) for g in entry["globs"]) and not any(
                    glob_match(g, path) for g in entry.get("exclude", [])):
                reasons.append({"id": entry["id"], "kind": entry["kind"], "blast": entry["blast"],
                                "path": path, "match": "path"})
    exempt = patterns.get("content_exempt_paths", [])
    for old, new, added in _sections(diff):
        path = new or old
        if not added or not path or any(glob_match(g, path) for g in exempt):
            continue
        for entry in patterns.get("content_patterns", []):
            if not _applies(entry, repo):
                continue
            hit = next((line for line in added if entry["_rx"].search(line)), None)
            if hit is not None:
                reasons.append({"id": entry["id"], "kind": entry["kind"], "blast": entry["blast"],
                                "path": path, "match": hit.strip()[:120]})
    blast = sorted({r["blast"] for r in reasons}, key=BLASTS.index)
    return {"door": "one-way" if reasons else "two-way", "reasons": reasons, "blast": blast}


def _unfenced(body):
    out, fenced = [], False
    for line in body.splitlines():
        if line.lstrip().startswith(("```", "~~~")):
            fenced = not fenced
        elif not fenced:
            out.append(line)
    return out


def _field(lines, name, value=r"`?([^`\s]*)`?"):
    rx = re.compile(r"^\s*(?:[-*]\s+)?(?:\*\*|__)?" + name + r"\s*:\s*(?:\*\*|__)?\s*" + value + r"\s*$", re.I)
    return [m.group(1).strip().lower() for m in map(rx.match, lines) if m]


def parse_danger_block(body):
    """The merge-danger block: `door:` and `blast:` lines anywhere outside a code fence, plus an
    optional `approval: court <code>`. Case-insensitive; a repeat with a different value is an error."""
    lines = _unfenced(body or "")
    out = {"door": None, "blast": None, "approval": None, "errors": []}
    seen = {}
    for name, allowed in (("door", DOORS), ("blast", BLASTS)):
        vals = seen[name] = _field(lines, name)
        if not vals:
            continue
        if len(set(vals)) > 1:
            out["errors"].append(f"{name}: conflicting values {sorted(set(vals))}")
        elif vals[0] not in allowed:
            out["errors"].append(f"{name}: {vals[0]!r} is not one of {' | '.join(allowed)}")
        else:
            out[name] = vals[0]
    if not any(seen.values()):
        out["errors"].append(f"missing merge-danger block ({BLOCK_HELP})")
    else:
        out["errors"] += [f"{n}: missing" for n in ("door", "blast") if not seen[n]]
    codes = set(_field(lines, "approval", r"court\s+`?(" + COURT_CODE + r")`?"))
    if len(codes) == 1:
        out["approval"] = codes.pop()
    elif codes:
        out["errors"].append(f"approval: more than one court code {sorted(codes)}")
    return out


def decide(body, paths, diff, repo, patterns=None):
    """The classifier's verdict, raised (never lowered) by what the PR body declares."""
    c = classify(paths, diff, repo, patterns)
    declared = parse_danger_block(body)
    notes = list(declared["errors"])
    forced = c["door"] == "one-way"
    if forced and declared["door"] == "two-way":
        notes.append("body says two-way; ignored: " + ", ".join(sorted({r["id"] for r in c["reasons"]})) + " forces one-way")
    door = "one-way" if forced or declared["door"] == "one-way" else "two-way"
    return {"door": door, "forced": forced, "reasons": c["reasons"], "blast": c["blast"],
            "declared": declared, "notes": notes}


def _git(git_dir, *args):
    return subprocess.run(["git", "-C", git_dir, "-c", "core.quotePath=false", *args], check=True,
                          capture_output=True, text=True, errors="replace").stdout


def dry_run(repo, git_dir, last, patterns, ref="main"):
    """Classify the last N first-parent commits of ref. Read-only: git log and git diff, no fetch."""
    rows = []
    for line in _git(git_dir, "log", "--first-parent", f"-{last}", "--format=%H %P", ref).splitlines():
        sha, *parents = line.split()
        subject = _git(git_dir, "log", "-1", "--format=%s", sha).strip()
        diff = _git(git_dir, "diff", "-M", "--no-color", "--no-ext-diff", parents[0] if parents else EMPTY_TREE, sha)
        rows.append({"sha": sha[:10], "subject": subject, **classify([], diff, repo, patterns)})
    return rows


def main(argv=None):
    ap = argparse.ArgumentParser(description="one-way-door dry run (report only)")
    ap.add_argument("--repo", required=True)
    ap.add_argument("--git-dir", required=True)
    ap.add_argument("--last", type=int, default=100)
    ap.add_argument("--ref", default="main")
    ap.add_argument("--patterns", default=DEFAULT_PATTERNS)
    a = ap.parse_args(argv)
    json.dump(dry_run(a.repo, a.git_dir, a.last, load_patterns(a.patterns), a.ref), sys.stdout, indent=1)
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
