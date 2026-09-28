`review-verdict` turns Codex's advisory PR review into one deterministic commit status (`review-verdict`), a required merge check on the gated repos (since 2026-09-28).
Public on purpose: reusable workflows must be readable by every BJGLLC repo; nothing secret lives here.
Spec: `claude-dotfiles/docs/superpowers/specs/2026-09-26-review-system-v4-design.md` §6.

The gate never asks Codex: it only replies `queued-for-janitor` on the P2/P3 threads it resolves. (Codex's summary edit is flaky (R35); a bot's mention did get a review on #72.) Agents request Codex reviews themselves by commenting `@codex review`: after a fix push (re-review), whenever the status reads `no Codex review of <sha> yet: comment @codex review`, and after merging a `hotfix`-labelled PR (the post-hoc review).

`workflow_dispatch -f drill=p1-open` is the red-proof drill: it posts a `failure` status on the PR head. A drill can only ever post failure; there is no passing drill (R39).
