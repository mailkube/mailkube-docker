# shellcheck shell=dash
# Fail-fast validation.
#
# Two jobs:
#   1. Turn a misconfiguration into an actionable error at boot, rather than an
#      opaque SMTP reply minutes later.
#   2. Act as the grammar allowlist for every value that reaches `postconf -e`.
#      The config applier writes environment-derived values into main.cf, so
#      without a strict allowlist it would be the same injection surface that
#      POSTFIX_EXTRA_CONF was rejected for. Anything not matched here does not
#      reach Postfix.
#
# Every rule below must have a named row in the test matrix. See .rules/TESTING.md.

_re() { printf '%s' "$1" | grep -qE "$2"; }

# Refuse control characters and comment markers in anything destined for
# main.cf. A newline would append an arbitrary parameter; a '#' would comment
# out the remainder of a generated line.
#
# Implemented by strip-and-compare rather than a `case` glob. A glob built from
# `$(printf '\n')` is a trap: command substitution strips trailing newlines, so
# that expression is the EMPTY STRING and the pattern *""* matches every value,
# including an empty one.
assert_clean() {
	_name="$1"
	_val="$2"
	[ -n "$_val" ] || return 0
	_stripped="$(printf '%s' "$_val" | tr -d '\n\r#')"
	if [ "$_stripped" != "$_val" ]; then
		die_hint "${_name} contains a newline, carriage return, or '#'." \
			"Remove it. These characters cannot be passed through to the Postfix configuration."
	fi
}

# Postfix time value: an integer with an optional s/m/h/d/w suffix.
assert_time() {
	_re "$2" '^[0-9]+[smhdw]?$' ||
		die_hint "$1 must be a Postfix time value such as 30s, 5m, 2h or 1d (got '$2')." \
			"Use an integer with an optional s/m/h/d/w suffix."
}

assert_int_range() {
	_name="$1" _val="$2" _min="$3" _max="$4"
	_re "$_val" '^[0-9]+$' || die_hint "$_name must be an integer (got '$_val')." "Set a whole number between $_min and $_max."
	[ "$_val" -ge "$_min" ] && [ "$_val" -le "$_max" ] ||
		die_hint "$_name must be between $_min and $_max (got '$_val')." "Choose a value in range."
}

assert_yes_no() {
	case "$2" in
	yes | no) ;;
	*) die_hint "$1 must be 'yes' or 'no' (got '$2')." "Set $1=yes or $1=no." ;;
	esac
}

validate_env() {
	# --- credentials -------------------------------------------------------
	[ -n "$SMTP_USERNAME" ] ||
		die_hint "SMTP_USERNAME is not set." \
			"Set it to your SMTP credential in the form <username>@<your-verified-domain>."

	# The upstream splits the AUTH identity on '@' and rejects anything else with
	# a 535 plus a risk signal. The public docs currently say only "your SMTP
	# credential's username", which does not authenticate, so this message has to
	# carry the full contract.
	_re "$SMTP_USERNAME" '^[^@[:space:]]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$' ||
		die_hint "SMTP_USERNAME must be <username>@<verified-domain>, not a bare username (got '$SMTP_USERNAME')." \
			"Example: myapp01@example.com  -- the domain part must be the domain the credential is bound to."

	[ -n "$SMTP_PASSWORD" ] ||
		die_hint "SMTP_PASSWORD is not set." \
			"Set it to the SMTP credential's secret, or use SMTP_PASSWORD_FILE to read it from a mounted secret."

	# The marketing site tells people to use an API key here. It is wrong, and
	# the resulting 535 is indistinguishable from a typo. Warn rather than fail,
	# because the key prefix is not a guaranteed shape.
	case "$SMTP_PASSWORD" in
	mk_*)
		log_warn "SMTP_PASSWORD looks like an API key (it starts with 'mk_')."
		log_warn "SMTP authentication needs an SMTP credential secret, not an API key."
		log_warn "Create one at https://app.mailkube.com/domain/credentials#smtp"
		;;
	esac

	# --- upstream port -----------------------------------------------------
	case "$SMTP_PORT" in
	587 | 465) ;;
	25)
		die_hint "SMTP_PORT=25 is not a submission port on smtp.mailkube.com." \
			"Use 587 (STARTTLS) or 465 (implicit TLS). Upstream port 25 accepts bounce/DSN traffic only; relaying there raises a risk signal and gets your egress IP banned."
		;;
	*)
		die_hint "SMTP_PORT must be 587 or 465 (got '$SMTP_PORT')." \
			"587 is STARTTLS submission, 465 is implicit TLS. No other port is accepted upstream."
		;;
	esac

	# --- ingress -----------------------------------------------------------
	assert_int_range LISTEN_PORT "$LISTEN_PORT" 1 65535
	case "$RELAY_NETWORKS_MODE" in
	append | replace) ;;
	*) die_hint "RELAY_NETWORKS_MODE must be 'append' or 'replace' (got '$RELAY_NETWORKS_MODE')." "append adds to the private-range defaults; replace uses only RELAY_NETWORKS (loopback is always kept)." ;;
	esac
	assert_clean RELAY_NETWORKS "$RELAY_NETWORKS"
	validate_networks

	# --- limits ------------------------------------------------------------
	assert_int_range MESSAGE_SIZE_LIMIT "$MESSAGE_SIZE_LIMIT" 1024 26214400
	if [ "$MESSAGE_SIZE_LIMIT" -gt 20971520 ]; then
		log_warn "MESSAGE_SIZE_LIMIT is ${MESSAGE_SIZE_LIMIT}, above the 20971520-byte cap the upstream"
		log_warn "submission listener enforces. Larger messages will be rejected there, not here."
	fi
	assert_int_range RECIPIENT_LIMIT "$RECIPIENT_LIMIT" 1 1000
	assert_time MAX_QUEUE_LIFETIME "$MAX_QUEUE_LIFETIME"
	assert_time SHUTDOWN_DRAIN_TIMEOUT "${SHUTDOWN_DRAIN_TIMEOUT}"
	assert_int_range RELAY_START_JITTER "$RELAY_START_JITTER" 0 300

	# Upstream HAProxy rejects at 20 concurrent connections PER SOURCE IP, which
	# is per cluster egress NAT and therefore shared across every replica.
	assert_int_range RELAY_CONCURRENCY "$RELAY_CONCURRENCY" 1 10
	if [ "$RELAY_CONCURRENCY" -gt 4 ]; then
		log_warn "RELAY_CONCURRENCY=${RELAY_CONCURRENCY}. The upstream rejects at 20 concurrent connections"
		log_warn "per source IP, shared by every replica behind your cluster's egress address."
		log_warn "Keep (instances x RELAY_CONCURRENCY) at or below 18."
	fi

	if [ -n "$RELAY_MSG_RATE" ]; then
		assert_int_range RELAY_MSG_RATE "$RELAY_MSG_RATE" 1 100
		if [ "$RELAY_MSG_RATE" -le 1 ]; then
			log_warn "RELAY_MSG_RATE=${RELAY_MSG_RATE} enables a 1s per-message rate delay."
			log_warn "A non-zero rate delay collapses per-destination concurrency to 1 in Postfix."
		fi
	fi

	# --- behaviour ---------------------------------------------------------
	assert_yes_no ENFORCE_FROM_DOMAIN "$ENFORCE_FROM_DOMAIN"
	assert_yes_no SMTPUTF8_ENABLE "$SMTPUTF8_ENABLE"
	assert_yes_no RELAY_DEBUG "$RELAY_DEBUG"
	case "$INET_PROTOCOLS" in
	all | ipv4 | ipv6) ;;
	*) die_hint "INET_PROTOCOLS must be all, ipv4 or ipv6 (got '$INET_PROTOCOLS')." "Use 'ipv4' on clusters without an IPv6 route." ;;
	esac

	if [ -n "$RELAY_HOSTNAME" ]; then
		assert_clean RELAY_HOSTNAME "$RELAY_HOSTNAME"
		_re "$RELAY_HOSTNAME" '^[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?$' ||
			die_hint "RELAY_HOSTNAME is not a valid hostname (got '$RELAY_HOSTNAME')." "Use letters, digits, dots and hyphens only."
	fi

	# --- inbound authentication --------------------------------------------
	assert_yes_no RELAY_AUTH_REQUIRED "$RELAY_AUTH_REQUIRED"

	if [ -n "$RELAY_AUTH_USERS_FILE" ]; then
		[ -f "$RELAY_AUTH_USERS_FILE" ] ||
			die_hint "RELAY_AUTH_USERS_FILE is set to '${RELAY_AUTH_USERS_FILE}' but that file does not exist." \
				"Mount the file at that path, or use RELAY_AUTH_USERS instead."
		[ -r "$RELAY_AUTH_USERS_FILE" ] ||
			die_hint "RELAY_AUTH_USERS_FILE '${RELAY_AUTH_USERS_FILE}' is not readable." \
				"Check the mount's file mode."
	fi

	if [ -n "$RELAY_AUTH_USERS" ]; then
		assert_clean RELAY_AUTH_USERS "$RELAY_AUTH_USERS"
		_auth_entries="$(printf '%s' "$RELAY_AUTH_USERS" | tr ',' ' ')"
		for _e in $_auth_entries; do
			[ -n "$_e" ] || continue
			case "$_e" in
			*:*) ;;
			*) die_hint "RELAY_AUTH_USERS entry '${_e}' is not in user:password form." \
				"Use a comma-separated list, for example: app1:s3cret,app2:h0nk" ;;
			esac
			_u="${_e%%:*}"
			_re "$_u" '^[A-Za-z0-9._-]{1,64}$' ||
				die_hint "RELAY_AUTH_USERS contains an invalid username '${_u}'." \
					"Usernames may use letters, digits, dot, underscore and hyphen, up to 64 characters."
		done
	fi

	# Requiring authentication with no accounts defined would reject every
	# client including the container's own healthcheck.
	if [ "$RELAY_AUTH_REQUIRED" = "yes" ] && [ -z "$RELAY_AUTH_USERS" ] && [ -z "$RELAY_AUTH_USERS_FILE" ]; then
		die_hint "RELAY_AUTH_REQUIRED=yes but no accounts are configured." \
			"Set RELAY_AUTH_USERS or RELAY_AUTH_USERS_FILE, or leave RELAY_AUTH_REQUIRED unset."
	fi

	# --- listener TLS ------------------------------------------------------
	if [ -n "$RELAY_TLS_CERT_FILE" ] || [ -n "$RELAY_TLS_KEY_FILE" ]; then
		[ -n "$RELAY_TLS_CERT_FILE" ] && [ -n "$RELAY_TLS_KEY_FILE" ] ||
			die_hint "RELAY_TLS_CERT_FILE and RELAY_TLS_KEY_FILE must be set together." \
				"Provide both a certificate and its private key."
		[ -r "$RELAY_TLS_CERT_FILE" ] ||
			die_hint "RELAY_TLS_CERT_FILE '${RELAY_TLS_CERT_FILE}' is missing or unreadable." "Check the mount path and file mode."
		[ -r "$RELAY_TLS_KEY_FILE" ] ||
			die_hint "RELAY_TLS_KEY_FILE '${RELAY_TLS_KEY_FILE}' is missing or unreadable." "Check the mount path and file mode."
	fi

	# Credentials in the clear are better than no credentials at all, but the
	# operator must make that choice knowingly rather than by omission.
	if [ -n "$RELAY_AUTH_USERS" ] || [ -n "$RELAY_AUTH_USERS_FILE" ]; then
		if [ -z "$RELAY_TLS_CERT_FILE" ]; then
			log_warn "inbound authentication is enabled but the listener has no TLS certificate."
			log_warn "Client passwords will cross your pod network base64-encoded, not encrypted."
			log_warn "Set RELAY_TLS_CERT_FILE and RELAY_TLS_KEY_FILE to require TLS before AUTH."
		fi
	fi

	# --- bounce recipient --------------------------------------------------
	if [ -n "$BOUNCE_RECIPIENT" ]; then
		assert_clean BOUNCE_RECIPIENT "$BOUNCE_RECIPIENT"
		_re "$BOUNCE_RECIPIENT" '^[^@[:space:]]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$' ||
			die_hint "BOUNCE_RECIPIENT must be an email address (got '$BOUNCE_RECIPIENT')." "Example: ops@${AUTH_DOMAIN}"
		# A DSN sent to any other domain is rejected 550 by the upstream
		# From-must-match rule, producing a bounce loop rather than a delivery.
		[ "${BOUNCE_RECIPIENT#*@}" = "$AUTH_DOMAIN" ] ||
			die_hint "BOUNCE_RECIPIENT must be at the authenticated domain '${AUTH_DOMAIN}' (got '${BOUNCE_RECIPIENT#*@}')." \
				"Delivery reports are relayed through smtp.mailkube.com, which rejects any From outside your verified domain."
	fi
}

# Validate every CIDR and refuse an open relay outright.
validate_networks() {
	[ -n "$RELAY_NETWORKS" ] || return 0
	_nets="$(printf '%s' "$RELAY_NETWORKS" | tr ',' ' ')"
	for _n in $_nets; do
		[ -n "$_n" ] || continue
		case "$_n" in
		*/0 | */00)
			die_hint "RELAY_NETWORKS contains '${_n}', which matches every address." \
				"An open relay is never a valid configuration for this image. List only the pod or subnet CIDRs that must be allowed to send."
			;;
		esac
		_re "$_n" '^[0-9]{1,3}(\.[0-9]{1,3}){3}/[0-9]{1,2}$' && continue
		_re "$_n" '^\[?[0-9A-Fa-f:]+\]?/[0-9]{1,3}$' && continue
		die_hint "RELAY_NETWORKS entry '${_n}' is not a valid IPv4 or IPv6 CIDR." \
			"Use comma-separated CIDRs, for example: 10.42.0.0/16,192.168.5.0/24"
	done
}

# Variables from the boky/postfix lineage this image replaces.
#
# Tiered on purpose. A blanket failure is wrong because SMTP_SERVER and DEBUG are
# generic names that legitimately belong to a co-located application when a
# compose env_file is shared, which the previous docker-compose.yml did.
# Lineage-specific names have no such excuse, and silently ignoring them is how a
# migrating user loses their entire ingress allowlist at cutover.
validate_legacy_env() {
	[ "$RELAY_IGNORE_LEGACY_ENV" = "yes" ] && return 0
	_found=''

	_fail_if_set() {
		eval "_v=\${$1:-}"
		[ -n "$_v" ] || return 0
		log_error "$1 is no longer supported. $2"
		_found=1
	}

	_fail_if_set SMTP_NETWORKS "Use RELAY_NETWORKS (same comma-separated CIDR syntax)."
	_fail_if_set SERVER_HOSTNAME "Use RELAY_HOSTNAME. The mail domain now comes from SMTP_USERNAME."
	_fail_if_set OVERWRITE_FROM "Use ENFORCE_FROM_DOMAIN=yes, which rewrites only non-compliant sender domains and preserves the local part."
	_fail_if_set SMTP_HEADER_TAG "Removed. Tag messages with the X-Mailkube-Tags header, which is passed through untouched."
	_fail_if_set SMTP_HEADER_X_ENTITY_REF_ID_PREFIX "Removed. It is not a Mailkube header, and upstream regenerates Message-ID regardless."
	_fail_if_set LOG_SUBJECT "Removed. Logging subject lines puts personal data in your log aggregator; use the Mailkube sending log instead."
	_fail_if_set DESTINATION "Removed. This is a null client and never delivers locally."
	_fail_if_set ALWAYS_ADD_MISSING_HEADERS "Removed. Missing headers are always added."

	if [ -n "${SMTP_SERVER:-}" ]; then
		log_warn "SMTP_SERVER is set and will be ignored. This image always relays to smtp.mailkube.com;"
		log_warn "the host is hardcoded and has no override. Only SMTP_PORT (587 or 465) is selectable."
	fi
	if [ -n "${DEBUG:-}" ]; then
		log_warn "DEBUG is set and will be ignored. Use RELAY_DEBUG=yes for this container's diagnostics."
	fi

	[ -z "$_found" ] || die_hint "One or more removed environment variables are set (listed above)." \
		"Update them, or set RELAY_IGNORE_LEGACY_ENV=yes to start anyway (their values will be ignored)."
}
