#!/usr/bin/env bash
# Docker Hub README portability gate.
#
# docs/DOCKERHUB.md is rendered on the Docker Hub image page, which is a
# different environment from GitHub:
#
#   * relative links resolve against docker.com and 404
#   * mermaid is NOT rendered; a ```mermaid fence shows as raw source
#   * relative image paths do not resolve
#
# README.md is the GitHub document and is deliberately exempt: it may use
# mermaid and relative links freely.
#
# Deliberately avoids `grep -P`. BSD/macOS grep has no PCRE support, so a
# lookahead-based implementation exits non-zero, and with stderr suppressed the
# gate silently passes while checking nothing. A gate that cannot fail is worse
# than no gate.
set -euo pipefail

cd "$(dirname "$0")/.."
F=docs/DOCKERHUB.md

if [[ ! -f $F ]]; then
  echo "FAIL: $F is missing. It is what the release workflow pushes to Docker Hub."
  exit 1
fi

fail=0
report() {
  echo "FAIL: $1"
  printf '%s\n' "$2" | sed 's/^/    /'
  fail=1
}

# Markdown link targets, as "<line>:<target>", excluding fenced code blocks.
targets="$(
  awk '
    /^```/ { infence = !infence; next }
    infence { next }
    {
      line = $0
      while (match(line, /\]\([^)]*\)/)) {
        t = substr(line, RSTART + 2, RLENGTH - 3)
        print NR ":" t
        line = substr(line, RSTART + RLENGTH)
      }
    }
  ' "$F"
)"

bad_links=""
bad_images=""
while IFS= read -r entry; do
  [[ -z $entry ]] && continue
  target="${entry#*:}"
  case "$target" in
    http://* | https://* | \#* | mailto:*) continue ;;
  esac
  bad_links+="${entry}"$'\n'
done <<<"$targets"

[[ -n $bad_links ]] && report \
  "relative link target(s) in $F. Docker Hub resolves these against docker.com." "$bad_links"

# Images: markdown ![...](target) and any HTML src=
while IFS= read -r entry; do
  [[ -z $entry ]] && continue
  case "$entry" in
    *'](http'* | *'src="http'*) continue ;;
  esac
  bad_images+="${entry}"$'\n'
done <<<"$(grep -nE '!\[[^]]*\]\(|src="' "$F" || true)"

[[ -n $bad_images ]] && report \
  "relative image path(s) in $F. Use absolute raw.githubusercontent.com URLs." "$bad_images"

if hits="$(grep -n '```mermaid' "$F" || true)" && [[ -n $hits ]]; then
  report "mermaid fence(s) in $F. Docker Hub renders them as raw source; keep diagrams in README.md." "$hits"
fi

if hits="$(grep -nE '<(div|table|img|details|picture|iframe)[ >]' "$F" || true)" && [[ -n $hits ]]; then
  report "raw HTML in $F. Docker Hub's renderer strips most of it." "$hits"
fi

[[ $fail -ne 0 ]] && exit 1
echo "OK: $F is Docker Hub portable."
