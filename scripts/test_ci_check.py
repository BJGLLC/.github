# scripts/test_ci_check.py
"""Review v4 phase 3 (P3-A): the ci-check composite action and the scripts it runs, each driven red
and green; the check step's one-retry flake rule (spec §5); and ci_shape.INPUTS == the action's
inputs. shellcheck/gitleaks must work: in CI (ci-tools.sh put them on PATH) a missing tool FAILS;
locally it skips."""
import os
import pathlib
import random
import shutil
import string
import subprocess
import tempfile
import unittest

import yaml

import ci_shape as cs

HERE = pathlib.Path(__file__).resolve().parent
ACTION = HERE.parent / "actions" / "ci-check" / "action.yml"


def need(tool, arg="--version"):
    ok = shutil.which(tool) and subprocess.run([tool, arg], capture_output=True).returncode == 0
    if ok:
        return
    if os.environ.get("CI"):
        raise AssertionError(f"{tool} missing in CI")
    raise unittest.SkipTest(f"{tool} not usable here")


def git(d, *a):
    return subprocess.run(["git", "-C", d, *a], check=True, capture_output=True, text=True).stdout.strip()


def repo():
    d = tempfile.mkdtemp()
    git(d, "init", "-q")
    for k, val in (("user.email", "t@t"), ("user.name", "t"), ("commit.gpgsign", "false"), ("core.hooksPath", "/dev/null")):
        git(d, "config", k, val)
    return d


def commit(d, path, text, mode=None):
    p = pathlib.Path(d, path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    if mode:
        p.chmod(mode)
    git(d, "add", path)
    git(d, "commit", "-qm", path)
    return git(d, "rev-parse", "HEAD")


class Action(unittest.TestCase):
    """The composite's contract with ci_shape.py and with the caller."""

    def setUp(self):
        self.a = yaml.safe_load(ACTION.read_text())

    def test_is_composite(self):
        self.assertEqual(self.a["runs"]["using"], "composite")

    def test_inputs_match_ci_shape(self):
        self.assertEqual(set(self.a["inputs"]), cs.INPUTS)
        for name, spec in self.a["inputs"].items():
            self.assertEqual(spec.get("default"), "", f"{name}: every input defaults to empty")

    def test_failing_step_output_is_wired(self):
        self.assertEqual(self.a["outputs"]["failing_step"]["value"], "${{ steps.record.outputs.failing_step }}")
        record = next(s for s in self.a["runs"]["steps"] if s.get("id") == "record")
        self.assertEqual(record.get("if"), "failure()")

    def test_no_step_can_swallow_a_failure(self):
        for s in self.a["runs"]["steps"]:
            self.assertNotIn("continue-on-error", s, s.get("name") or s.get("uses"))

    def test_hygiene_runs_before_any_install(self):
        order = [s.get("id") or s.get("name") or s.get("uses") for s in self.a["runs"]["steps"]]
        first_setup = min(i for i, s in enumerate(order) if s in ("install", "ci-setup") or str(s).startswith("actions/setup-"))
        for gate in ("tools", "shape", "actionlint", "shellcheck", "gitleaks"):
            self.assertLess(order.index(gate), first_setup, f"{gate} should run before toolchain setup")

    def test_repo_controlled_steps_come_after_every_hygiene_step(self):
        # install / ci-setup / check run repo-controlled commands that can write BASH_ENV (or PATH)
        # into $GITHUB_ENV; anything after them would run neutered. Hygiene must all come first.
        ids = [s.get("id") or s.get("name") for s in self.a["runs"]["steps"]]
        repo_controlled = [i for i, n in enumerate(ids) if n in ("install", "ci-setup", "check")]
        self.assertEqual(len(repo_controlled), 3, ids)
        for gate in ("shape", "actionlint", "shellcheck", "gitleaks"):
            self.assertLess(ids.index(gate), min(repo_controlled), f"{gate} must precede install/ci-setup/check")

    def step_run(self, sid):
        return next(s for s in self.a["runs"]["steps"] if s.get("id") == sid)["run"]

    def test_every_python_invocation_is_isolated(self):
        # -I keeps the PR checkout (cwd) off sys.path: a root yaml.py must never run in the shape step.
        import re
        n = 0
        for s in self.a["runs"]["steps"]:
            for line in s.get("run", "").splitlines():
                for m in re.finditer(r'("\$py"|python3?)\s+(\S+)', line):
                    n += 1
                    self.assertEqual(m.group(2), "-I", f"python call without -I: {line.strip()}")
        self.assertGreaterEqual(n, 3)

    def test_hostile_root_yaml_py_does_not_run(self):
        with tempfile.TemporaryDirectory() as d:
            marker = pathlib.Path(d, "pwned")
            pathlib.Path(d, "yaml.py").write_text(f"import pathlib; pathlib.Path({str(marker)!r}).write_text('x')\n")
            env = {**os.environ, "BJG_CI": str(HERE.parent), "GITHUB_REPOSITORY": "BJGLLC/x"}
            subprocess.run(["bash", "-c", self.step_run("shape")], cwd=d, env=env, capture_output=True, text=True)
            self.assertFalse(marker.exists(), "the PR's own yaml.py ran inside the shape step")

    def test_actionlint_ignores_the_prs_own_config(self):
        need("actionlint", "-version")
        d = repo()
        commit(d, ".github/workflows/x.yml", "on: push\njobs:\n  a:\n    steps:\n      - run: echo\n")
        commit(d, ".github/actionlint.yaml", 'paths:\n  ".github/workflows/**/*.{yml,yaml}":\n    ignore: [".*"]\nself-hosted-runner:\n  labels: []\n')
        env = {**os.environ, "BJG_CI": str(HERE.parent)}
        rc = subprocess.run(["bash", "-c", self.step_run("actionlint")], cwd=d, env=env, capture_output=True, text=True).returncode
        self.assertNotEqual(rc, 0, "a PR's .github/actionlint.yaml suppressed actionlint")

    def test_every_run_step_declares_bash(self):
        for s in self.a["runs"]["steps"]:
            if "run" in s:
                self.assertEqual(s.get("shell"), "bash", s.get("name"))


class ShellcheckTracked(unittest.TestCase):
    def rc(self, d):
        return subprocess.run(["bash", str(HERE / "shellcheck-tracked.sh")], cwd=d, capture_output=True, text=True).returncode

    def test_clean_repo_is_green(self):
        need("shellcheck"); d = repo(); commit(d, "ok.sh", "#!/usr/bin/env bash\necho ok\n")
        self.assertEqual(self.rc(d), 0)

    def test_syntax_error_is_red(self):
        need("shellcheck"); d = repo(); commit(d, "bad.sh", "#!/usr/bin/env bash\nif true; then echo x\n")
        self.assertNotEqual(self.rc(d), 0)

    def test_shebang_script_without_extension_is_checked(self):
        need("shellcheck"); d = repo(); commit(d, "bin/tool", "#!/bin/sh\nif true; then echo x\n", 0o755)
        self.assertNotEqual(self.rc(d), 0)

    def test_chezmoi_symlink_and_template_entries_are_skipped(self):
        need("shellcheck"); d = repo()
        commit(d, "bin/symlink_backup.sh", "/home/x/projects/y/backup.sh\n")
        commit(d, "dot_x/run.sh.tmpl", "{{ if .x }}\nif then\n{{ end }}\n")
        self.assertEqual(self.rc(d), 0)

    def test_prs_shellcheckrc_cannot_disable_checks(self):
        need("shellcheck"); d = repo()
        commit(d, ".shellcheckrc", "disable=all\n")
        commit(d, "bad.sh", "#!/usr/bin/env bash\nif true; then echo x\n")
        self.assertNotEqual(self.rc(d), 0)

    def test_zsh_script_is_skipped(self):
        need("shellcheck"); d = repo()
        commit(d, "z", "#!/usr/bin/env zsh\nif [[ -o interactive ]] { echo i }\n", 0o755)
        self.assertEqual(self.rc(d), 0)


class GitleaksRange(unittest.TestCase):
    ZERO = "0" * 40

    def rc(self, **env):
        e = {**os.environ, "EVENT": "", "BASE": "", "HEAD_SHA": "", "BEFORE": "", **env}
        return subprocess.run(["bash", str(HERE / "gitleaks-range.sh"), str(HERE / "gitleaks.toml")],
                              cwd=self.d, env=e, capture_output=True, text=True).returncode

    def setUp(self):
        need("gitleaks", "version")
        self.d = repo()
        self.a = commit(self.d, "readme.md", "clean\n")
        fake = "ghp_" + "".join(random.choices(string.ascii_letters + string.digits, k=36))  # built at runtime
        self.b = commit(self.d, "cfg.py", f'token = "{fake}"\n')

    def test_push_range_with_a_token_is_red(self):
        self.assertNotEqual(self.rc(EVENT="push", BEFORE=self.a, HEAD_SHA=self.b), 0)

    def test_push_before_the_token_is_green(self):
        self.assertEqual(self.rc(EVENT="push", BEFORE=self.ZERO, HEAD_SHA=self.a), 0)

    def test_pull_request_range_is_red(self):
        self.assertNotEqual(self.rc(EVENT="pull_request", BASE=self.a, HEAD_SHA=self.b), 0)

    def test_unreachable_before_still_scans_the_head(self):
        self.assertNotEqual(self.rc(EVENT="push", BEFORE="deadbeef" * 5, HEAD_SHA=self.b), 0)

    def test_gitleaksignore_fingerprint_does_not_suppress(self):
        # find the real fingerprint from a report, then plant it in the PR's own .gitleaksignore
        rep = pathlib.Path(self.d, "..", "rep.json").resolve()
        subprocess.run(["gitleaks", "git", "--no-banner", "--redact", "--config", str(HERE / "gitleaks.toml"),
                        "--log-opts", f"{self.a}..{self.b}", "-r", str(rep), "-f", "json", self.d], capture_output=True)
        import json
        fps = [f["Fingerprint"] for f in json.loads(rep.read_text())]
        self.assertTrue(fps, "planted token was not detected at all")
        commit(self.d, ".gitleaksignore", "\n".join(fps) + "\n")
        self.assertNotEqual(self.rc(EVENT="push", BEFORE=self.a, HEAD_SHA=git(self.d, "rev-parse", "HEAD")), 0)

    def test_inline_gitleaks_allow_still_honoured(self):
        line = pathlib.Path(self.d, "cfg.py").read_text().rstrip("\n") + "  # gitleaks:allow\n"
        c = commit(self.d, "cfg.py", line)
        self.assertEqual(self.rc(EVENT="push", BEFORE=self.b, HEAD_SHA=c), 0)

    def test_dispatch_without_origin_scans_the_head(self):
        self.assertNotEqual(self.rc(EVENT="workflow_dispatch"), 0)


class CheckRetry(unittest.TestCase):
    """spec §5: a flake gets one re-run, not a block; a real failure is still red."""

    def run_with(self, fails):
        with tempfile.TemporaryDirectory() as d:
            fake = pathlib.Path(d, "make")
            fake.write_text('#!/usr/bin/env bash\nn=$(cat "$COUNT" 2>/dev/null || echo 0); n=$((n+1)); echo "$n" > "$COUNT"\n[ "$n" -gt "$FAILS" ]\n')
            fake.chmod(0o755)
            env = {**os.environ, "PATH": f"{d}:{os.environ['PATH']}", "COUNT": f"{d}/n", "FAILS": str(fails)}
            out = subprocess.run(["bash", str(HERE / "make-check.sh")], env=env, capture_output=True, text=True)
            return out.returncode, out.stdout, int(pathlib.Path(d, "n").read_text())

    def test_green_first_time_runs_once(self):
        rc, out, n = self.run_with(0)
        self.assertEqual((rc, n), (0, 1)); self.assertNotIn("::warning::", out)

    def test_one_flake_is_retried_to_green(self):
        rc, out, n = self.run_with(1)
        self.assertEqual((rc, n), (0, 2)); self.assertIn("::warning::", out)

    def test_two_failures_are_red(self):
        rc, _, n = self.run_with(2)
        self.assertEqual((rc, n), (1, 2))


if __name__ == "__main__":
    unittest.main()
