BJGLLC/.github holds the org-wide review gate: `scripts/review_verdict.py` and the reusable `.github/workflows/review-verdict.yml` that every gated BJGLLC repo calls to post its `review-verdict` status.

## Code Review Rules

Codex reads this section when it reviews a PR here. Review v4 (2026-09-26): only P0/P1 block; everything else is queued for a weekly janitor PR.

- **P0/P1** = would break something a user, Blake, or a scheduled job touches: data loss, wrong money, secrets, auth, a deploy that cannot roll back, a crash on the happy path.
- **P2/P3** = everything else: unlikely inputs, hardening, naming, docs wording, test suggestions.
- **Skip entirely:** adversarial or malformed input to internal-only CLI tools under `bin/` and `claude/hooks/` unless the input can arrive from outside the box; alternative spellings or quoting of shell constructs; style.
- **Re-review of a PR you already reviewed:** report only regressions on earlier P0/P1 items and new P0/P1 introduced by the latest push. Do not repeat queued P2/P3 findings.
- A new or changed test that restates a constant, reads source text instead of running code, or mocks the unit under test is **P1** when it is the only test covering a change on a deploy or customer-facing path, and **P2** otherwise.
