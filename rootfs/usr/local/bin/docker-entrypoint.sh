#!/bin/sh
# mailkube/smtp-relay entrypoint.
#
# Orchestration only. Each step lives in a single-responsibility library under
# /usr/local/lib/mailkube-relay/. See .rules/ENTRYPOINT.md before changing the
# order: several steps depend on values derived by an earlier one.
set -eu
# BusyBox ash supports pipefail; dash does not. This tree is linted as dash
# (see .shellcheckrc), so the directive below suppresses the resulting SC3040.
# Guarded with `|| true` so the entrypoint still starts on a shell without it.
# shellcheck disable=SC3040
set -o pipefail 2>/dev/null || true

LIB=/usr/local/lib/mailkube-relay
# shellcheck source=/dev/null
. "$LIB/log.sh"
# shellcheck source=/dev/null
. "$LIB/env.sh"
# shellcheck source=/dev/null
. "$LIB/validate.sh"
# shellcheck source=/dev/null
. "$LIB/render.sh"
# shellcheck source=/dev/null
. "$LIB/lifecycle.sh"

main() {
	load_env
	[ "$RELAY_DEBUG" = "yes" ] && set -x

	validate_legacy_env   # before validate_env, so a renamed variable is named as such
	derive_identity       # AUTH_DOMAIN is needed by BOUNCE_RECIPIENT validation
	validate_env
	build_networks
	collect_auth_accounts # apply_config branches on whether any account exists

	prepare_config_dir
	normalize_mounts
	apply_config
	write_credentials
	write_auth_db # after prepare_config_dir: the database lives in $MAIL_CONFIG
	write_maps

	preflight
	warn_if_publicly_reachable
	start_and_supervise
}

main "$@"
