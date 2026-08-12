#!/usr/bin/env bash
# Behavior-coverage gate.
#
# This repo has no application code, so the house "90% line and branch coverage"
# pillar is meaningless. .rules/SOLID_DRY_KISS.md redefines Coverage as BEHAVIOR
# coverage: every environment variable the entrypoint reads must be documented
# for users and exercised by a test. This script is what makes that a gate rather
# than a promise.
#
# Without it the redefinition is unenforced and the repo effectively ships with
# no coverage gate at all.
set -euo pipefail

cd "$(dirname "$0")/.."

ENV_LIB=rootfs/usr/local/lib/mailkube-relay/env.sh
README=README.md
EXAMPLE=.env.example
TESTS="test"

fail=0
note() { printf '  %-26s %s\n' "$1" "$2"; }

# Variables the entrypoint actually consumes, as assigned in load_env().
# Matches `NAME="${NAME:-default}"` and skips internal/derived values.
#
# `mapfile` is deliberately not used: it needs bash 4, and macOS ships bash 3.2,
# so a contributor on a Mac would get "mapfile: command not found" instead of a
# result. This loop is portable.
vars=""
while IFS= read -r name; do
  [ -n "$name" ] && vars="${vars}${name} "
done << EOF
$(grep -oE '^[[:space:]]*[A-Z][A-Z0-9_]+="\$\{[A-Z][A-Z0-9_]+:-' "$ENV_LIB" |
  grep -oE '^[[:space:]]*[A-Z][A-Z0-9_]+' | sed 's/^[[:space:]]*//' | sort -u)
EOF

# Handled explicitly rather than through the default-assignment pattern.
vars="${vars} SMTP_USERNAME SMTP_PASSWORD"

# Not user-facing: derived internally or read only as a *_FILE indirection.
# A plain string rather than an associative array, which also needs bash 4.
SKIP=" MAIL_CONFIG AUTH_DOMAIN AUTH_LOCALPART MYHOSTNAME MYNETWORKS "

printf 'Checking behavior coverage for the environment surface...\n'
# shellcheck disable=SC2086  # $vars is a deliberately word-split list
for v in $(printf '%s\n' $vars | sort -u); do
  case "$SKIP" in *" $v "*) continue ;; esac

  missing=""
  grep -q "${v}" "$EXAMPLE" 2> /dev/null || missing="${missing} .env.example"
  grep -q "${v}" "$README" 2> /dev/null || missing="${missing} README.md"
  # Scoped to the test sources on purpose. A bare `grep -r test` also searches
  # the git-ignored `test/.venv`, where any three-letter name is matched by some
  # dependency's source and the gate passes locally while failing in CI on a
  # clean checkout. TZ is how that was found.
  grep -rq --include='*.py' --exclude-dir='.venv' --exclude-dir='__pycache__' \
    "${v}" "$TESTS" 2> /dev/null || missing="${missing} a-test"

  if [ -n "$missing" ]; then
    note "$v" "MISSING from:${missing}"
    fail=1
  fi
done

if [[ $fail -ne 0 ]]; then
  cat << 'EOF'

Every environment variable must appear in .env.example, README.md, and at least
one test. See .rules/SOLID_DRY_KISS.md (Coverage pillar) and .rules/TESTING.md.
EOF
  exit 1
fi

printf 'OK: every environment variable is documented and exercised.\n'
