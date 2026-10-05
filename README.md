`review-verdict` turns Codex's advisory PR review into one deterministic commit status (`review-verdict`), a required merge check on the gated repos (since 2026-09-28).
Public on purpose: reusable workflows must be readable by every BJGLLC repo; nothing secret lives here.
Spec: `claude-dotfiles/docs/superpowers/specs/2026-09-26-review-system-v4-design.md` §6.

**The gate asks Codex on every push** (SSSF-25, 2026-09-28; this reverses R32, "the gate never comments"). On `pull_request` `synchronize` it posts one `@codex review` comment with a hidden `<!-- review-verdict:nudge <sha> -->` marker. It skips drafts, `hotfix` PRs, closed PRs and any head someone already asked about: a `@codex review` comment after the push, by anyone except Codex, or its own marker for that SHA. Agents don't need to ask after a push. Asking anyway is harmless: if you ask first, the gate skips; if you ask after it, Codex may review the same head twice (rounds count distinct SHAs, R22). Agents still ask in two cases: after merging a `hotfix`-labelled PR (the post-hoc review), and when the status reads `no Codex review of <sha> yet: comment @codex review` or `codex-unavailable`.

**It also asks when nobody has** (SSSF-46). Codex reviews an opened or ready-for-review PR by itself only when one of its users authored it, and that review can end in a bare 👍, which is never a verdict (R46) and fires no event. So while the poll job runs, it posts the same one-per-head `@codex review` once nobody has asked about the head: 10 min after the clock start, or at once on a bot-authored PR. It never asks while Codex's sticky summary for the head is open, or once a verdict exists.

**The poll holds until green or red.** After a push, open, ready or reopen, the poll job polls every 60 s until a verdict or the `codex-unavailable` failure 30 min after the clock start, then stops. When Codex answers, the hold ends with the verdict, about 2 min after a request. Only a request nobody answers holds the full 30 min, and that PR turns red (`codex-unavailable`: retry `@codex review` from your own account, or label `hotfix` if urgent) instead of hanging. Before SSSF-46 the job stopped at 8 min, so a 👍-only or unanswered PR stayed pending with no event left to recompute it. No cron, and no per-repo schedule: the Actions budget can't carry one.

**Codex doesn't answer every requester.** On some repos Codex ignores the gate's `github-actions[bot]` request but answers a person's, so an unattended PR there goes red at 30 min. The fix is in Codex's code-review settings, not here: set that repo's automatic review to review all PRs (bot-authored ones included) with the trigger on every push. Then Codex reviews each head without being asked, and the gate's request is only a backstop.

**Recompute by hand** (for example, after a poller died on repeated API errors):

```
gh workflow run review-verdict.yml -R BJGLLC/<repo> -f pr=<n>        # this repo: review-verdict-self.yml
```

Any later gate event (a review, a thread reply, Codex's comment) recomputes the same way, and a late Codex verdict replaces `codex-unavailable`.

**Janitor queue.** P2/P3 findings get a `queued-for-janitor` thread reply, and that reply is the queue entry. The gate no longer resolves those threads, because `resolveReviewThread` needs `contents: write` and the callers grant `read` (drill #5), so the threads stay open. A thread that already has the reply is never queued twice: the gate checks the thread again right before replying, and one-shot runs for a PR are serialized (cd-marketing #165 got two replies 1 s apart from racing runs). If a reply fails, the run posts `failure` with the re-run remedy instead of success.

`workflow_dispatch -f drill=p1-open` is the red-proof drill: it posts a `failure` status on the PR head. A drill can only ever post failure; there is no passing drill (R39).

## Deploy library

This repo also hosts the shared deploy library (SSSF-31, Review v4 phase 4): `scripts/deploy_*.py`, `.github/workflows/linear-deploy-notify.yml`, `cf-deploy.yml` and `cf-ship.yml`. Callers pin `BJGLLC/.github@main`; team keys live in `scripts/deploy_notify.py` only.
