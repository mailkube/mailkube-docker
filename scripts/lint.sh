#!/usr/bin/env bash
# One-shot local equivalent of the `test`, `dry` and `docs` CI jobs.
# Run before opening a pull request. See CONTRIBUTING.md.
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1
rc=0
step() { printf '\n=== %s ===\n' "$1"; }

step "hadolint (Dockerfile)"
if command -v hadolint > /dev/null; then
  hadolint --config .hadolint.yaml Dockerfile || rc=1
else docker run --rm -i -v "$PWD/.hadolint.yaml:/.hadolint.yaml:ro" hadolint/hadolint hadolint --config /.hadolint.yaml - < Dockerfile || rc=1; fi

step "shellcheck (rootfs as dash, scripts as bash)"
if command -v shellcheck > /dev/null; then
  find rootfs -type f -name '*.sh' -print0 | xargs -0 shellcheck -x --shell=dash || rc=1
  shellcheck -x scripts/*.sh || rc=1
else echo "  (shellcheck not installed, skipped)"; fi

step "shfmt"
if command -v shfmt > /dev/null; then
  shfmt -d -i 2 -ci -sr rootfs scripts || rc=1
else echo "  (shfmt not installed, skipped)"; fi

step "jscpd (DRY gate)"
npx --yes jscpd@4 --config .jscpd.json . || rc=1

step "rule index"
./scripts/check-rule-index.sh || rc=1

step "environment behavior coverage"
./scripts/check-env-matrix.sh || rc=1

printf '\n'
[ $rc -eq 0 ] && echo "All local gates passed." || echo "Some gates FAILED (exit $rc)."
exit $rc
