#!/usr/bin/env bash
# ci-python.sh [PYTHON...] — print an interpreter that imports PyYAML under -I, for the ci-check
# shape step (SSSF-59). Candidates default to the runner's "/usr/bin/python3", then the one on PATH.
# The first that already has PyYAML is printed as-is: no venv, no network (the ubuntu-latest path).
# None → a fresh venv under $RUNNER_TEMP with PyYAML pip-installed into it, and its interpreter is
# printed. The old fallback, `-I -m pip install` into the system python, cannot work under -I or on
# a PEP 668 python, so an image without python3-yaml turned ci red in every gated repo at once.
# Every call keeps -I. The probe runs in the PR checkout (cwd), where -I is what keeps a root
# yaml.py out; venv + pip run from $RUNNER_TEMP in a dir mktemp just made, so the PR cannot plant
# files in it, and pip runs --isolated so no PIP_* env var can swap the index.
set -euo pipefail
[ "$#" -gt 0 ] || set -- "/usr/bin/python3" "python3"
for py in "$@"; do
  if "$py" -I -c 'import yaml' 2>/dev/null; then
    command -v -- "$py"
    exit 0
  fi
done
cd "${RUNNER_TEMP:-${TMPDIR:-/tmp}}"
venv="$(mktemp -d "$PWD/bjg-py.XXXXXX")"
for py in "$@"; do
  "$py" -I -m venv "$venv" >&2 || continue
  py="$venv/bin/python"
  "$py" -I -m pip install --isolated --quiet --disable-pip-version-check pyyaml >&2
  "$py" -I -c 'import yaml'
  echo "$py"
  exit 0
done
echo "::error title=ci::no interpreter imports PyYAML, and none could build a venv to install it" >&2
exit 1
