`review-verdict` turns Codex's advisory PR review into one deterministic commit status (`review-verdict`), which later becomes a required merge check.
Public on purpose: reusable workflows must be readable by every BJGLLC repo; nothing secret lives here.
Spec: `claude-dotfiles/docs/superpowers/specs/2026-09-26-review-system-v4-design.md` §6.

The gate never comments on a PR: Codex ignores `@codex review` from github-actions[bot]. Agents request Codex reviews themselves by commenting `@codex review`: after a fix push (re-review), whenever the status reads `no Codex review of <sha> yet: comment @codex review`, and after merging a `hotfix`-labelled PR (the post-hoc review).
