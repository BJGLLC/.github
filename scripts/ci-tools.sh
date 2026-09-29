#!/usr/bin/env bash
# ci-tools.sh DEST — put the pinned hygiene tools in DEST, sha256-verified, and print DEST (the
# caller appends it to $GITHUB_PATH). Review v4 phase 3 (P3-A). A bump = one row: URL + sha256
# (actionlint/gitleaks: from the release's checksums.txt; shellcheck publishes none, so it is the
# sha256sum of the release asset, recorded 2026-09-28). A mismatch exits non-zero before extract.
set -euo pipefail
dest="$1"; mkdir -p "$dest"
tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT
get() { # URL SHA256 → prints the verified local path
  local f="$tmp/${1##*/}"
  curl -fsSL --retry 3 --connect-timeout 10 --max-time 120 -o "$f" "$1" || return 1
  echo "$2  $f" | sha256sum -c --quiet - >&2 || return 1
  echo "$f"
}
f="$(get https://github.com/rhysd/actionlint/releases/download/v1.7.12/actionlint_1.7.12_linux_amd64.tar.gz 8aca8db96f1b94770f1b0d72b6dddcb1ebb8123cb3712530b08cc387b349a3d8)"
tar -xzf "$f" -C "$dest" actionlint
f="$(get https://github.com/gitleaks/gitleaks/releases/download/v8.30.1/gitleaks_8.30.1_linux_x64.tar.gz 551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb)"
tar -xzf "$f" -C "$dest" gitleaks
f="$(get https://github.com/koalaman/shellcheck/releases/download/v0.11.0/shellcheck-v0.11.0.linux.x86_64.tar.xz 8c3be12b05d5c177a04c29e3c78ce89ac86f1595681cab149b65b97c4e227198)"
tar -xJf "$f" -C "$tmp" shellcheck-v0.11.0/shellcheck
mv "$tmp/shellcheck-v0.11.0/shellcheck" "$dest/shellcheck"
echo "$dest"
