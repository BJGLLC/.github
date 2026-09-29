import unittest

from deploy_smoke import FORCE_FAIL_SHA, main, run

SHA = "c" * 40
OLD = "a" * 40
HTML = (200, "text/html; charset=utf-8", "<!doctype html><html><body>LP</body></html>")
PAGE = (200, "text/html", "<html></html>")


def j(sha):
    return 200, "application/json", '{"sha":"%s","repo":"x"}' % sha


class Clock:
    def __init__(self):
        self.t = 0.0

    def now(self):
        return self.t

    def sleep(self, s):
        self.t += s


def seq(responses):
    it, last = iter(responses), [None]

    def fetch(url, headers, timeout=10):
        try:
            last[0] = next(it)
        except StopIteration:
            pass
        return last[0]
    return fetch


class Smoke(unittest.TestCase):
    def go(self, responses, expected=SHA, health="https://x/"):
        c = Clock()
        return run("https://x/version.json", health, expected, fetch=seq(responses), sleep=c.sleep, clock=c.now)

    def test_json_sha_match_passes(self):
        r = self.go([j(SHA), PAGE])
        self.assertTrue(r["ok"])
        self.assertEqual(r["observed"], SHA)

    def test_html_200_fallback_never_passes(self):
        r = self.go([HTML])  # crawldaddyrepairs.com/version.json today
        self.assertFalse(r["ok"])
        self.assertIn("not json", r["reason"])
        self.assertGreaterEqual(r["elapsed"], 180)

    def test_stale_then_fresh_passes(self):
        r = self.go([j(OLD), j(OLD), j(SHA), PAGE])
        self.assertTrue(r["ok"])
        self.assertEqual(r["elapsed"], 20)

    def test_timeout_reports_last_observed(self):
        r = self.go([j(OLD)])
        self.assertFalse(r["ok"])
        self.assertEqual((r["stage"], r["observed"]), ("version", OLD))

    def test_access_redirect_is_an_auth_wall(self):
        self.assertIn("auth wall", self.go([(302, "", "")])["reason"])

    def test_health_failure_fails_even_when_sha_matches(self):
        r = self.go([j(SHA), (503, "text/html", "")])
        self.assertFalse(r["ok"])
        self.assertEqual(r["stage"], "health")

    def test_force_fail_never_matches(self):
        self.assertFalse(self.go([j(SHA)], expected=FORCE_FAIL_SHA)["ok"])

    def test_cli_rejects_short_sha(self):
        with self.assertRaises(SystemExit) as e:
            main(["--version-url", "https://x/v", "--sha", "abc1234"])
        self.assertEqual(e.exception.code, 2)

    def test_access_without_token_fails_fast(self):
        self.assertEqual(main(["--version-url", "https://x/v", "--sha", SHA, "--access"], env={}), 1)


if __name__ == "__main__":
    unittest.main()
