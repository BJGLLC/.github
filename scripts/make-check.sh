#!/usr/bin/env bash
# make-check.sh — the ci-check action's `check` step: the repo's one entrypoint, `make check`, with
# spec §5's flake rule (Review v4, P3-A). A failure gets exactly one re-run; a pass on the re-run is
# green but leaves a ::warning:: so the flake is visible; two failures are red. cwd = the repo.
set -uo pipefail
for attempt in 1 2; do
  if make check; then
    [ "$attempt" = 1 ] || echo "::warning::make check passed only on attempt 2: a flake (spec §5); fix it, don't lean on the retry"
    exit 0
  fi
  echo "::warning::make check failed on attempt $attempt; spec §5 retries once for flakes"
done
exit 1
