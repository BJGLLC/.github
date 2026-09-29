`review-verdict` turns Codex's advisory PR review into one deterministic commit status (`review-verdict`), a required merge check on the gated repos (since 2026-09-28).
Public on purpose: reusable workflows must be readable by every BJGLLC repo; nothing secret lives here.
Spec: `claude-dotfiles/docs/superpowers/specs/2026-09-26-review-system-v4-design.md` §6.

**The gate asks Codex on every push** (SSSF-25, 2026-09-28; this reverses R32, "the gate never comments"). On `pull_request` `synchronize` it posts one `@codex review` comment with a hidden `<!-- review-verdict:nudge <sha> -->` marker. It skips drafts, `hotfix` PRs, closed PRs and any head someone already asked about: a `@codex review` comment after the push, by anyone except Codex, or its own marker for that SHA. Codex reviews opened and ready-for-review PRs by itself, so those get no nudge. Agents don't need to ask after a push. Asking anyway is harmless: if you ask first, the gate skips; if you ask after it, Codex may review the same head twice (rounds count distinct SHAs, R22). Agents still ask in two cases: after merging a `hotfix`-labelled PR (the post-hoc review), and when the status reads `no Codex review of <sha> yet: comment @codex review`.

**Short poll.** After a push, open, ready or reopen, the poll job waits at most 8 min for Codex, then stops. It was 31 min, and every minute of that is billed on private repos. Codex answered the gate's nudge in 1m19s–1m56s. Its reply (a review or a comment) fires its own event, and that event recomputes the status, so a longer hold buys nothing.

**Pending past 30 min?** If the status still says `waiting for Codex review of <sha>` 30 min after the push, recompute it by hand:

```
gh workflow run review-verdict.yml -R BJGLLC/<repo> -f pr=<n>        # this repo: review-verdict-self.yml
```

With no Codex response by then, the recompute posts the `codex-unavailable` failure, exactly as the old 31-min poll did: retry `@codex review`, or label `hotfix` if urgent. Any later gate event (a review, a thread reply, Codex's comment) recomputes the same way. **If Codex never answers and nobody recomputes, the status stays pending, and a pending required check blocks the merge.** That fails safe: nothing merges unreviewed, it just waits.

**Janitor queue.** P2/P3 findings get a `queued-for-janitor` thread reply, and that reply is the queue entry. The gate no longer resolves those threads, because `resolveReviewThread` needs `contents: write` and the callers grant `read` (drill #5), so the threads stay open. A thread that already has the reply is never queued twice: the gate checks the thread again right before replying, and one-shot runs for a PR are serialized (cd-marketing #165 got two replies 1 s apart from racing runs). If a reply fails, the run posts `failure` with the re-run remedy instead of success.

`workflow_dispatch -f drill=p1-open` is the red-proof drill: it posts a `failure` status on the PR head. A drill can only ever post failure; there is no passing drill (R39).


This repo also hosts the shared deploy library (SSSF-31, Review v4 phase 4): `scripts/deploy_*.py`, `.github/workflows/linear-deploy-notify.yml`, and later `cf-deploy.yml` / `cf-ship.yml`. Callers pin `BJGLLC/.github@main`; team keys live in `scripts/deploy_notify.py` only.
