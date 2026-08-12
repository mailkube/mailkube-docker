# shellcheck shell=dash
# Environment loading: *_FILE indirection, defaults, and derived values.
#
# Loading is separated from validation (validate.sh) so that every value is
# present before any rule runs. That lets error messages reference other
# variables, e.g. checking BOUNCE_RECIPIENT against the domain from
# SMTP_USERNAME.

# The runtime config directory. Postfix's own multi-instance mechanism, used
# here so the whole of / can stay read-only and the SMTP credential lands on
# tmpfs instead of a disk-backed layer.
# shellcheck disable=SC2034  # consumed by render.sh and lifecycle.sh
MAIL_CONFIG=/run/postfix

# Read a secret from <VAR>_FILE into <VAR> when the _FILE form is used.
#
# Three fixes over the lineage script this replaces:
#   1. A missing file is FATAL. The old behaviour printed "skipping" and carried
#      on, turning a broken secret mount into a confusing 535 half a minute later.
#   2. Trailing whitespace is stripped. `kubectl create secret --from-file` with
#      a file made by `echo` (not `echo -n`) embeds a trailing newline, which is
#      the single most common silent 535 in this ecosystem.
#   3. Tracing is suppressed for the duration, so RELAY_DEBUG=yes can never echo
#      a credential into the log.
load_secret_file() {
	_name="$1"
	_file_var="${_name}_FILE"
	eval "_file=\${${_file_var}:-}"
	[ -n "$_file" ] || return 0

	set +x
	if [ ! -f "$_file" ]; then
		die_hint "${_file_var} is set to '${_file}' but that file does not exist." \
			"Check the secret is mounted at that path, or set ${_name} directly."
	fi
	if [ ! -r "$_file" ]; then
		die_hint "${_file_var} is set to '${_file}' but that file is not readable." \
			"Check the mount's file mode and the container's user."
	fi

	# Strip trailing CR/LF/space/tab. Deliberately not `$(cat)`: command
	# substitution alone would strip newlines but keep other trailing whitespace.
	_val="$(tr -d '\r' <"$_file" | sed -e 's/[[:space:]]*$//' | head -n 1)"
	[ -n "$_val" ] || die "${_file_var} points at '${_file}', which is empty."
	eval "${_name}=\$_val"
	_restore_trace
}

# Re-enable tracing only if the operator asked for it.
_restore_trace() {
	[ "${RELAY_DEBUG:-no}" = "yes" ] && set -x
	return 0
}

load_env() {
	# --- credentials -------------------------------------------------------
	SMTP_USERNAME="${SMTP_USERNAME:-}"
	SMTP_PASSWORD="${SMTP_PASSWORD:-}"
	load_secret_file SMTP_USERNAME
	load_secret_file SMTP_PASSWORD

	# --- upstream ----------------------------------------------------------
	# The HOST is not configurable. It is a literal in main.cf. Only the port
	# is selectable, and only between the two submission ports.
	SMTP_PORT="${SMTP_PORT:-587}"

	# --- ingress -----------------------------------------------------------
	LISTEN_PORT="${LISTEN_PORT:-25}"
	RELAY_NETWORKS="${RELAY_NETWORKS:-}"
	RELAY_NETWORKS_MODE="${RELAY_NETWORKS_MODE:-append}"

	# --- limits ------------------------------------------------------------
	MESSAGE_SIZE_LIMIT="${MESSAGE_SIZE_LIMIT:-20971520}"
	RECIPIENT_LIMIT="${RECIPIENT_LIMIT:-50}"
	MAX_QUEUE_LIFETIME="${MAX_QUEUE_LIFETIME:-1d}"
	RELAY_CONCURRENCY="${RELAY_CONCURRENCY:-2}"
	RELAY_MSG_RATE="${RELAY_MSG_RATE:-}"

	# --- inbound authentication (optional) ---------------------------------
	# Off unless RELAY_AUTH_USERS or RELAY_AUTH_USERS_FILE is set. When on, the
	# relay stops being an unauthenticated endpoint and clients may authenticate
	# with AUTH PLAIN or LOGIN. Combined with RELAY_AUTH_REQUIRED=yes and
	# RELAY_NETWORKS_MODE=replace this closes the "any pod in the namespace can
	# send as your domain" gap that a NetworkPolicy is otherwise the only
	# defence against.
	RELAY_AUTH_USERS="${RELAY_AUTH_USERS:-}"
	RELAY_AUTH_USERS_FILE="${RELAY_AUTH_USERS_FILE:-}"
	RELAY_AUTH_REQUIRED="${RELAY_AUTH_REQUIRED:-no}"

	# --- listener TLS (optional) -------------------------------------------
	# Certificates are MOUNTED, never generated. A self-signed certificate made
	# at startup would force every client to disable verification, which trains
	# people to accept any certificate and is worse than plaintext on a
	# controlled network.
	RELAY_TLS_CERT_FILE="${RELAY_TLS_CERT_FILE:-}"
	RELAY_TLS_KEY_FILE="${RELAY_TLS_KEY_FILE:-}"

	# --- behaviour ---------------------------------------------------------
	BOUNCE_RECIPIENT="${BOUNCE_RECIPIENT:-}"
	ENFORCE_FROM_DOMAIN="${ENFORCE_FROM_DOMAIN:-no}"
	SMTPUTF8_ENABLE="${SMTPUTF8_ENABLE:-no}"
	INET_PROTOCOLS="${INET_PROTOCOLS:-all}"
	RELAY_HOSTNAME="${RELAY_HOSTNAME:-}"

	# --- lifecycle ---------------------------------------------------------
	SHUTDOWN_DRAIN_TIMEOUT="${SHUTDOWN_DRAIN_TIMEOUT:-20}"
	# Decorrelates synchronized cold starts across replicas. Without it, N pods
	# restarting together open N connections in the same second and can trip the
	# per-domain AUTH limit, which emits RiskSignals against the shared egress IP.
	RELAY_START_JITTER="${RELAY_START_JITTER:-15}"

	# --- diagnostics -------------------------------------------------------
	RELAY_DEBUG="${RELAY_DEBUG:-no}"
	RELAY_IGNORE_LEGACY_ENV="${RELAY_IGNORE_LEGACY_ENV:-no}"
	TZ="${TZ:-UTC}"
	export TZ
}

# The authenticated domain is, by definition, the part of SMTP_USERNAME after
# the '@'. Deriving it from the hostname (as the lineage script did with
# `awk 'BEGIN{FS=OFS="."}{print $(NF-1),$NF}'`) is wrong: it yields "co.uk" for
# mail.example.co.uk and emits a leading-comma garbage value for a single-label
# hostname.
# shellcheck disable=SC2034  # AUTH_LOCALPART and MYHOSTNAME are read by render.sh
derive_identity() {
	AUTH_DOMAIN="${SMTP_USERNAME#*@}"
	AUTH_LOCALPART="${SMTP_USERNAME%@*}"
	MYHOSTNAME="${RELAY_HOSTNAME:-mailkube-relay.${AUTH_DOMAIN}}"
}
