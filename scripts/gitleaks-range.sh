#!/usr/bin/env bash
# gitleaks-range.sh CONFIG — scan only the commits this ci run introduces (Review v4 spec §5, P3-A).
# Same range logic as the global pre-push hook (claude-dotfiles git/hooks/pre-push): a new secret
# fails the check; history is the leak lane's job (op-leak-scan), so old findings never block a PR.
# Env (set by the ci-check action's gitleaks step): EVENT, BASE, HEAD_SHA, BEFORE. Exit 0 = clean.
set -euo pipefail
cfg="$1"; zero=0000000000000000000000000000000000000000
reachable() { [ -n "${1:-}" ] && [ "$1" != "$zero" ] && git cat-file -e "$1^{commit}" 2>/dev/null; }
head="${HEAD_SHA:-HEAD}"; reachable "$head" || head=HEAD
case "${EVENT:-}" in
  pull_request) if reachable "${BASE:-}"; then range="$BASE..$head"; else range="-1 $head"; fi ;;
  push)         if reachable "${BEFORE:-}"; then range="$BEFORE..$head"; else range="-1 $head"; fi ;;
  *) base="$(git merge-base origin/main "$head" 2>/dev/null || true)"
     if [ -n "$base" ] && [ "$base" != "$(git rev-parse "$head")" ]; then range="$base..$head"; else range="-1 $head"; fi ;;
esac
echo "gitleaks range: $range"
gitleaks git --no-banner --redact --config "$cfg" --log-opts "$range" .
