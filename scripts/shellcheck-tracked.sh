#!/usr/bin/env bash
# Script: shellcheck-tracked.sh — `shellcheck -S error` over every tracked shell file (Review v4 spec §5, P3-A).
# (Its header must not begin "# shellcheck": shellcheck parses such a line as a directive, SC1073.)
# Shell = *.sh / *.bash, or a first line naming sh/bash. Skipped: chezmoi templates (*.tmpl) and
# chezmoi symlink_ entries (their content is a target path, not a script). Error level only: the
# gate is "this will not parse or will break at runtime"; style stays a Codex P2/P3 concern.
set -euo pipefail
files=()
while IFS= read -r -d '' f; do
  case "$f" in *.tmpl|symlink_*|*/symlink_*) continue ;; esac
  [ -f "$f" ] || continue
  case "$f" in
    *.sh|*.bash) files+=("$f") ;;
    *) if head -n 1 "$f" 2>/dev/null | grep -qE '^#!.*\b(ba)?sh\b'; then files+=("$f"); fi ;;
  esac
done < <(git ls-files -z)
if [ "${#files[@]}" -eq 0 ]; then echo "shellcheck: no shell files"; exit 0; fi
echo "shellcheck -S error over ${#files[@]} files"
shellcheck -S error "${files[@]}"
