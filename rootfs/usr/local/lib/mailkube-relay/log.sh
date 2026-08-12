# shellcheck shell=dash
# Leveled logging for the mailkube-relay entrypoint.
#
# Everything goes to stderr so it can never be confused with Postfix's own
# stdout maillog stream, which operators grep for delivery status lines.
#
# Message shapes here are a contract: README "Troubleshooting" and the alert
# expressions match on them. Do not reword an existing line without updating
# both, and see .rules/ENTRYPOINT.md.

_log() {
  _lvl="$1"
  shift
  printf 'mailkube-relay: %s: %s\n' "$_lvl" "$*" >&2
}

log_info() { _log info "$@"; }
log_warn() { _log warn "$@"; }
log_error() { _log error "$@"; }

# Fatal, with an actionable message. Never exits 0.
die() {
  _log fatal "$@"
  exit 1
}

# Fatal for a configuration mistake, with a "how to fix" second line. Used for
# everything a user can correct by changing an environment variable, because a
# bare rejection sends people to the source and a hint does not.
die_hint() {
  _msg="$1"
  _hint="$2"
  _log fatal "$_msg"
  printf 'mailkube-relay:        fix: %s\n' "$_hint" >&2
  exit 1
}
