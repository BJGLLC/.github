# One-way-door lane: an irreversible PR can't merge on green alone

SSSF-48 · 2026-11-21 · status: **draft, nothing enforced**. Code: `scripts/one_way_door.py` (unused by any
workflow), patterns: `one-way-patterns.json`, tests: `scripts/test_one_way_door.py`.

## The problem

Every green PR merges the same way today, whether it renames a variable or changes what real customers are
texted. Some changes can't be taken back by a revert: a message that went out, a dropped table, ad money
spent, a secret pushed to GitHub. Those should need Blake's yes before they merge, and the gate should check
for that yes instead of trusting the PR to say it is safe.

## The merge-danger block

Every PR body carries two lines (anywhere outside a code fence; a bullet or bold label is fine):

    door: one-way | two-way
    blast: local | repo | customer | money

- `door: one-way` means a revert does not undo the effect. `two-way` means it does.
- `blast` is how far a mistake reaches: this box, this repo and its deploy, customers, or money.
- A one-way PR adds `approval: court <code>`, for example `approval: court 1121-q7`.

The parser (`parse_danger_block`) reports a missing block, an unknown value, a missing field, or a field
repeated with two different values. In report-only mode those are notes, never failures.

## The classifier: the body can raise the door, never lower it

The body is written by the agent making the change, so it is a claim, not evidence. `decide()` takes the
body, the changed paths, the unified diff and the repo name:

1. **Path patterns** fire on any path the change touches: both sides of a rename and deleted files count
   (they are read from the diff headers, because `gh pr view --json files` names only the new path).
2. **Content patterns** fire on an *added* line of a file that is not a test, fixture, doc or `*.md`.
   Removing a send or a `DROP` is not the danger.
3. If either fires, the PR is one-way, whatever the body says, and the result names the pattern, the path
   and the matching line. A body that says `one-way` with no pattern firing is also one-way (opt-in).

The list lives in `one-way-patterns.json` at the repo root, next to the Code Review Rules in `AGENTS.md`.
Each entry names the real repo and file it was found in. Some are scoped to one repo with `repos`.

| id | kind | fires on | grounded in |
|---|---|---|---|
| sql-migration | migration | `**/migrations/**`, `**/*.sql` (not tests) | cd-dashboard `supabase/migrations/` |
| one-off-migration-script | migration | `migration/**`, `scripts/migrate-*` | bjg-intranet |
| drop-script | delete | `**/drop-*.ts` and kin | bjg-intranet `scripts/drop-legacy-trio-tables.ts` |
| n8n-live-workflows | send | `n8n/workflows/**` | cd-lead-agent-deploy |
| bot-prompts-and-templates | send | `config/assets/prompts/**`, `config/assets/templates/**` | cd-lead-agent-deploy |
| runtime-job-flags | send | `config/eos-runtime-flags.json` | cd-lead-agent-deploy |
| marketing-flow-workflows | send | `*.workflow.js/json`, built email templates | cd-marketing |
| social-publish | send | the publish cron and runner scripts | content-engine |
| ad-platform-writers | money | Meta campaign build and client scripts | cd-marketing |
| secret-bearing-files | secret | `.env*`, `.dev.vars*` (not examples), `gitleaks.toml` | this repo |
| ghl-send-endpoint | send | `/conversations/messages"` (the send; reads end in `/{id}`) | lead-agent |
| ghl-message-type | send | `"type": "SMS"/"Email"` payloads | cd-marketing |
| social-post-call | send | Graph `photos`/`feed`/`media_publish`, LinkedIn `posts` | content-engine |
| jobber-mutation | crm-write | a named GraphQL `mutation X(` | lead-agent, cd-dashboard |
| ad-budget-field | money | `daily_budget`/`lifetime_budget`/`bid_amount` set | cd-marketing |
| schema-change-sql | migration | `ALTER TABLE` | lead-agent, bjg-intranet |
| destructive-sql | delete | `DROP TABLE/COLUMN/...`, `TRUNCATE` | bjg-intranet |
| unscoped-delete-sql | delete | `DELETE FROM t` with no `WHERE` on the line | lead-agent |

**What no pattern catches.** A change to the *logic* that decides who gets a message (a filter, a
schedule, a threshold) in code that never names the send endpoint; a send through a helper whose name is
not in the list; a data delete through an ORM call; a hand-run script whose danger is in its arguments.
The review gate's Code Review Rules (P0/P1 for "would break something a user touches") remain the net
for those. The classifier is a floor, not a fence.

## The approval: a court answer, checked on the eos runner

The court lives on the tailnet-only intranet; GitHub-hosted runners cannot reach it. So the check runs on
the eos self-hosted runner, pinned there (not routed by `BJG_RUNS_ON`, which can fall back to
`ubuntu-latest`), and runs this repo's code from `main`, never the PR's checkout.

1. The agent files a court question *before the work*, kind `question`, owner `blake`, whose `fields`
   carry `oneWay: {repo: "BJGLLC/<repo>", pr: <n or null>, ticket: "<KEY-123>"}` and whose text says what
   will happen that can't be undone.
2. Blake answers it. The agent writes `approval: court <code>` into the PR body.
3. The check reads `GET /api/court?status=answered&kind=question&stale=include` (there is no lookup by
   code today) and takes the item whose `code` matches. It passes only if **all** hold, the same rule
   `bjg-intranet/lib/loop/hold.ts` `answerOf` uses:
   - `status` is `answered` and `releasedAt` is set (a held answer is not an answer yet);
   - the client in `answeredVia` is one of Blake's lanes, `itui` or `web` (`facts/client-id.ts`);
   - `decision` is an accept: `approve` (see open decision 2 on `rec`);
   - `fields.oneWay.repo` is this repo, and `fields.oneWay.pr` is this PR or the ticket is one this PR
     cites, so an approval for one change cannot be reused for another.
4. **Court unreachable, timeout, bad JSON, or no matching code: fail closed.** The status is `failure`
   with the reason (`one-way: court unreachable, re-run when eos is up`). A one-way PR waits; a two-way PR
   never calls the court at all, so an eos outage blocks only one-way merges.

## The limit, said plainly

The court learns who answered from a self-declared `X-Facts-Client` header. Any process on the tailnet
can send `itui` and be recorded as Blake. So v1 is **convention-strength**: it stops an agent that follows
the API and makes a forged answer a deliberate act, not an accident. It is not a lock.

The path to a real lock is a signature only Blake can produce: an answer signed with a key that lives in
his hands (a passkey or a 1Password-held key that agents' lanes cannot read), verified by the check. That
is the same primitive OPS-877 needs for fresh 1Password prompts; build it once, for both.

## Rollout

1. **Now (this PR):** the classifier, patterns, tests and this doc. Nothing calls them.
2. **Report-only, one week:** a job on the eos runner computes `decide()` for each PR and writes the
   verdict and reasons into the daily digest. No status, no comment, nothing blocks.
3. **Tune:** read the week's digest; drop or narrow any pattern that fires on ordinary work; add one for
   anything one-way that slipped through. Target: under ~15% of a normal repo's PRs flagged.
4. **Enforce:** `review-verdict` fails a one-way PR without a passing court approval. Blake arms it per
   repo, starting with cd-lead-agent-deploy and cd-marketing (the customer-send repos).
5. **Lock:** swap the header check for the signature when OPS-877 lands.

## Open decisions

| # | Decision | Recommendation |
|---|---|---|
| 1 | Bind an approval to the PR, or to the PR's head SHA (like the loop hold does)? | **PR plus ticket.** A SHA binding re-asks Blake on every fix-up push; the review gate already covers what changed after the yes. Revisit when the signature lands. |
| 2 | Does `rec` (Blake took the recommendation) count as accept? | **Only `approve`.** A rec can say "don't"; an explicit approve is one tap and cannot be misread. |
| 3 | cd-lead-agent-deploy flags ~31% of commits: accept, or narrow there? | **Accept.** That repo exists to ship the customer-texting bot; its flag re-captures and prompt pins really do change what runs. Narrow only if the report-only week shows pure receipts. |
| 4 | Where does the check live: a new step in `review-verdict`, or its own required check? | **Inside `review-verdict`.** One required context per repo, already on every gated repo, and Blake already reads it. |
