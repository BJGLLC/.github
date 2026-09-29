#!/usr/bin/env python3
"""deploy_smoke.py: did the live URL pick up this SHA, and is it healthy? (Review v4 §8, phase 4)

Polls VERSION_URL until its JSON `sha` equals the expected full SHA or TIMEOUT (180 s) passes,
then GETs HEALTH_URL once. A 200 that is not JSON is a mismatch, never a pass: Cloudflare
Pages serves index.html for unknown paths, which is what crawldaddyrepairs.com/version.json
returns today. Redirects are not followed. A 302 to a Cloudflare Access login means the smoke
is unauthenticated; that is a failure to report, not a page to parse.
"""
import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

FORCE_FAIL_SHA = "drill-force-fail"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def fetch(url, headers, timeout=10):
    """-> (status, content_type, body). Network errors -> (0, '', 'ErrType: msg')."""
    req = urllib.request.Request(url, headers={"Cache-Control": "no-cache", "User-Agent": "review-v4-smoke", **headers})
    try:
        with urllib.request.build_opener(_NoRedirect).open(req, timeout=timeout) as r:
            return r.status, r.headers.get("Content-Type", ""), r.read(65536).decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, (e.headers.get("Content-Type", "") if e.headers else ""), ""
    except Exception as e:  # noqa: BLE001 — DNS, TLS, timeout
        return 0, "", f"{type(e).__name__}: {e}"


def read_sha(status, ctype, body):
    if status in (301, 302, 303, 307, 308):
        return None, f"redirect {status} (auth wall?)"
    if status != 200:
        return None, f"http {status}" + (f" {body[:80]}" if status == 0 else "")
    try:
        doc = json.loads(body)
    except ValueError:
        return None, f"not json ({ctype or 'no content-type'}; index.html fallback?)"
    sha = doc.get("sha") if isinstance(doc, dict) else None
    return (sha, "ok") if isinstance(sha, str) and sha else (None, "json without sha")


def run(version_url, health_url, expected, timeout=180, interval=10, headers=None,
        fetch=fetch, sleep=time.sleep, clock=time.monotonic):
    headers = headers or {}
    start = clock()

    def result(ok, stage, observed, reason):
        return {"ok": ok, "stage": stage, "expected": expected, "observed": observed,
                "reason": reason, "elapsed": round(clock() - start)}

    while True:
        sep = "&" if "?" in version_url else "?"
        observed, reason = read_sha(*fetch(f"{version_url}{sep}_={int(time.time())}", headers))
        if observed == expected:
            break
        if clock() - start >= timeout:
            return result(False, "version", observed, reason)
        sleep(interval)
    if health_url:
        status, _, _ = fetch(health_url, headers)
        if not 200 <= status < 300:
            return result(False, "health", observed, f"health http {status}")
    return result(True, "done", observed, "ok")


def full_sha(s):
    if not re.fullmatch(r"[0-9a-f]{40}", s):
        raise argparse.ArgumentTypeError("need the full 40-char lowercase SHA")
    return s


def main(argv=None, env=None):
    env = os.environ if env is None else env
    p = argparse.ArgumentParser(prog="deploy_smoke.py")
    p.add_argument("--version-url", required=True)
    p.add_argument("--health-url", default="")
    p.add_argument("--sha", required=True, type=full_sha)
    p.add_argument("--timeout", type=int, default=180)
    p.add_argument("--interval", type=int, default=10)
    p.add_argument("--access", action="store_true")
    p.add_argument("--force-fail", action="store_true")
    a = p.parse_args(argv)
    headers = {}
    if a.access:
        cid, sec = env.get("CF_ACCESS_CLIENT_ID", ""), env.get("CF_ACCESS_CLIENT_SECRET", "")
        if not (cid and sec):
            print(json.dumps({"ok": False, "stage": "config", "reason": "CF Access service token missing"}))
            return 1
        headers = {"CF-Access-Client-Id": cid, "CF-Access-Client-Secret": sec}
    r = run(a.version_url, a.health_url, FORCE_FAIL_SHA if a.force_fail else a.sha, a.timeout, a.interval, headers)
    print(json.dumps(r))
    return 0 if r["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
