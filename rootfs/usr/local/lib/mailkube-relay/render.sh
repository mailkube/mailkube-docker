# shellcheck shell=dash
# Config application.
#
# CONTRACT (see .rules/ENTRYPOINT.md): every environment-derived parameter is
# applied with ONE batched `postconf -e` call against $MAIL_CONFIG. Never append
# to main.cf: Postfix honours the last definition, but dict_load_fp() warns
# "overriding earlier entry: <name>=<value>" for every duplicate, and the boot
# preflight treats those warnings as fatal. `postconf -e` edits in place.

# Copy the baked configuration into a writable runtime directory. This is
# Postfix's own multi-instance mechanism and is what allows the container to run
# with readOnlyRootFilesystem: true while keeping the credential on tmpfs.
prepare_config_dir() {
  mkdir -p "$MAIL_CONFIG"
  cp -a /etc/postfix/. "$MAIL_CONFIG"/
  chmod 0755 "$MAIL_CONFIG"
}

# Kubernetes creates emptyDir volumes 0777 (hardcoded in empty_dir.go) and most
# CSI provisioners create PV roots 0777. Postfix's own `postfix-script check-warn`
# then warns "group or other writable" for the queue and data directories, which
# the fatal-warning preflight rejects, and the container CrashLoopBackOffs.
#
# Docker named volumes hide this completely: they seed from the image directory
# with mode and owner preserved, so CI passes green while every shipped
# Kubernetes manifest fails. Hence T-23, which mounts tmpfs at mode=1777.
#
# Non-recursive on purpose. `post-install create-missing` already applies the
# correct modes to the queue subdirectories, and a recursive chmod would destroy
# the setgid bits that public/ and maildrop/ require.
normalize_mounts() {
  _owner="$(postconf -c "$MAIL_CONFIG" -h mail_owner 2> /dev/null || echo postfix)"

  chown root:root /var/spool/postfix 2> /dev/null || true
  chmod 0755 /var/spool/postfix 2> /dev/null || true

  if [ -d /var/lib/postfix ]; then
    chown "${_owner}:${_owner}" /var/lib/postfix 2> /dev/null || true
    chmod 0700 /var/lib/postfix 2> /dev/null || true
  fi
  chmod 0755 "$MAIL_CONFIG" 2> /dev/null || true
}

# Escape a value for safe interpolation into a PCRE pattern.
# shellcheck disable=SC2016  # the sed script is a regex; expansion is not wanted
_pcre_quote() { printf '%s' "$1" | sed -e 's/[.[\*^$()+?{}|\\]/\\&/g'; }

apply_config() {
  # Build the argument list positionally: POSIX sh has no arrays, and `set --`
  # preserves values containing spaces correctly.
  set -- \
    "config_directory = $MAIL_CONFIG" \
    "relayhost = [smtp.mailkube.com]:${SMTP_PORT}" \
    "myhostname = ${MYHOSTNAME}" \
    "mydomain = ${AUTH_DOMAIN}" \
    "myorigin = \$mydomain" \
    "mynetworks = ${MYNETWORKS}" \
    "message_size_limit = ${MESSAGE_SIZE_LIMIT}" \
    "smtp_destination_recipient_limit = ${RECIPIENT_LIMIT}" \
    "smtp_destination_concurrency_limit = ${RELAY_CONCURRENCY}" \
    "maximal_queue_lifetime = ${MAX_QUEUE_LIFETIME}" \
    "bounce_queue_lifetime = 1h" \
    "smtputf8_enable = ${SMTPUTF8_ENABLE}" \
    "inet_protocols = ${INET_PROTOCOLS}"

  # Port 465 is implicit TLS: the connection is encrypted before the banner, so
  # there is no STARTTLS handshake to upgrade.
  if [ "$SMTP_PORT" = "465" ]; then
    set -- "$@" "smtp_tls_wrappermode = yes"
  else
    set -- "$@" "smtp_tls_wrappermode = no"
  fi

  # A non-zero rate delay implicitly collapses per-destination concurrency to 1,
  # so pacing and concurrency are not independently selectable. Postfix time
  # values are integral, making 1s the smallest usable delay.
  if [ -n "$RELAY_MSG_RATE" ] && [ "$RELAY_MSG_RATE" -le 1 ]; then
    set -- "$@" "smtp_destination_rate_delay = 1s"
  else
    set -- "$@" "smtp_destination_rate_delay = 0s"
  fi

  if [ "$RELAY_DEBUG" = "yes" ]; then
    set -- "$@" "smtp_tls_loglevel = 2"
  fi

  # --- inbound authentication ------------------------------------------
  # Note the SERVER-side default of smtpd_sasl_security_options is
  # `noanonymous` only, unlike the client side which also carries
  # `noplaintext`. PLAIN therefore works here without further coaxing.
  if [ -n "$AUTH_ACCOUNTS" ]; then
    set -- "$@" \
      "smtpd_sasl_auth_enable = yes" \
      "smtpd_sasl_type = cyrus" \
      "smtpd_sasl_path = smtpd" \
      "smtpd_sasl_security_options = noanonymous" \
      "smtpd_sasl_local_domain = ${MYHOSTNAME}" \
      "broken_sasl_auth_clients = yes"

    if [ "$RELAY_AUTH_REQUIRED" = "yes" ]; then
      # permit_mynetworks is dropped so a private-range client cannot
      # bypass authentication. Loopback is kept for the healthcheck,
      # which speaks no SMTP commands beyond QUIT.
      set -- "$@" \
        "smtpd_client_restrictions = permit_inet_interfaces, permit_sasl_authenticated, reject" \
        "smtpd_relay_restrictions = permit_sasl_authenticated, reject_unauth_destination" \
        "smtpd_recipient_restrictions = permit_sasl_authenticated, reject_unauth_destination"
    else
      set -- "$@" \
        "smtpd_client_restrictions = permit_mynetworks, permit_sasl_authenticated, reject" \
        "smtpd_relay_restrictions = permit_mynetworks, permit_sasl_authenticated, reject_unauth_destination" \
        "smtpd_recipient_restrictions = permit_mynetworks, permit_sasl_authenticated, reject_unauth_destination"
    fi
  else
    set -- "$@" "smtpd_sasl_auth_enable = no"
  fi

  # --- listener TLS ------------------------------------------------------
  if [ -n "$RELAY_TLS_CERT_FILE" ]; then
    set -- "$@" \
      "smtpd_tls_cert_file = ${RELAY_TLS_CERT_FILE}" \
      "smtpd_tls_key_file = ${RELAY_TLS_KEY_FILE}" \
      "smtpd_tls_security_level = encrypt" \
      "smtpd_tls_mandatory_protocols = >=TLSv1.2" \
      "smtpd_tls_protocols = >=TLSv1.2" \
      "smtpd_tls_loglevel = 1"
    # Refuse AUTH before STARTTLS, so a client cannot be downgraded into
    # sending its password in the clear.
    [ -n "$AUTH_ACCOUNTS" ] && set -- "$@" "smtpd_tls_auth_only = yes"
  else
    set -- "$@" "smtpd_tls_security_level ="
  fi

  # Bounce posture. The two tiers are MUTUALLY EXCLUSIVE: tier 2 discards
  # anything with a null envelope sender, which is exactly what a
  # Postfix-generated DSN carries, so it cannot be "always on" alongside a tier
  # that delivers those DSNs.
  #
  # Appended inline rather than via a helper returning text: `set -- $(helper)`
  # word-splits on spaces and would turn "k = v" into three arguments.
  if [ -n "$BOUNCE_RECIPIENT" ]; then
    # Tier 3: deliver delivery-status notifications to a local operator
    # address at the authenticated domain.
    set -- "$@" \
      "sender_dependent_default_transport_maps =" \
      "notify_classes = bounce, 2bounce" \
      "bounce_notice_recipient = ${BOUNCE_RECIPIENT}" \
      "2bounce_notice_recipient = ${BOUNCE_RECIPIENT}"
  else
    # Tier 2: a DSN must never reach smtp.mailkube.com. Its From: would be
    # MAILER-DAEMON@$myhostname, whose domain does not match the
    # authenticated domain, so upstream would reject it 550 and generate a
    # further bounce: a loop that dumps traffic on the platform.
    #
    # Logs remain the primary failure channel either way. Postfix already
    # emits a greppable line per failure and per expiry.
    set -- "$@" \
      "sender_dependent_default_transport_maps = pcre:${MAIL_CONFIG}/sender_transport" \
      "notify_classes ="
  fi

  postconf -c "$MAIL_CONFIG" -e "$@"
  _apply_listen_port
}

# LISTEN_PORT lives in master.cf, not main.cf. The port-25 entry is REPLACED,
# never supplemented: leaving it in place would fail to bind without
# NET_BIND_SERVICE and defeat the point of choosing an unprivileged port.
_apply_listen_port() {
  [ "$LISTEN_PORT" = "25" ] && return 0
  postconf -c "$MAIL_CONFIG" -MX 'smtp/inet' 2> /dev/null || true
  postconf -c "$MAIL_CONFIG" -M "${LISTEN_PORT}/inet=${LISTEN_PORT} inet n - n - - smtpd"
  log_info "listening on port ${LISTEN_PORT} (port 25 listener removed)"
}

# Write the SMTP credential. texthash is read at lookup time, so there is no
# postmap step and no .lmdb sidecar on disk.
write_credentials() {
  set +x
  _f="${MAIL_CONFIG}/sasl_passwd"
  printf '[smtp.mailkube.com]:%s %s:%s\n' "$SMTP_PORT" "$SMTP_USERNAME" "$SMTP_PASSWORD" > "$_f"
  chmod 0640 "$_f"
  # smtp(8) runs as mail_owner and must be able to read it.
  chown "root:$(postconf -c "$MAIL_CONFIG" -h mail_owner 2> /dev/null || echo postfix)" "$_f" 2> /dev/null || true
  _restore_trace
  log_info "credential written for ${SMTP_USERNAME} (relay [smtp.mailkube.com]:${SMTP_PORT})"
}

# Collect inbound accounts into AUTH_ACCOUNTS as newline-separated user:password.
#
# The file form wins over the inline form, since a mounted Secret is the shape
# Kubernetes deployments should use. Lines beginning with '#' are comments.
collect_auth_accounts() {
  set +x
  AUTH_ACCOUNTS=""
  if [ -n "$RELAY_AUTH_USERS_FILE" ]; then
    AUTH_ACCOUNTS="$(grep -vE '^[[:space:]]*(#|$)' "$RELAY_AUTH_USERS_FILE" || true)"
  elif [ -n "$RELAY_AUTH_USERS" ]; then
    AUTH_ACCOUNTS="$(printf '%s' "$RELAY_AUTH_USERS" | tr ',' '\n')"
  fi
  _restore_trace
}

# Build the SASL password database on tmpfs.
#
# saslpasswd2 writes a Berkeley DB file, so it needs a writable location; /run
# keeps it off any disk-backed layer, exactly like the upstream credential.
# Rebuilt from scratch on every start, so removing an account from the
# environment actually removes it.
write_auth_db() {
  [ -n "$AUTH_ACCOUNTS" ] || return 0
  set +x
  _db="${MAIL_CONFIG}/sasldb2"
  rm -f "$_db"

  _count=0
  printf '%s\n' "$AUTH_ACCOUNTS" | while IFS= read -r _entry; do
    [ -n "$_entry" ] || continue
    _user="${_entry%%:*}"
    _pass="${_entry#*:}"
    if [ -z "$_user" ] || [ -z "$_pass" ]; then
      continue
    fi
    # -p reads the password from stdin, -c creates, -f targets our database.
    #
    # The realm MUST be passed explicitly and MUST equal
    # smtpd_sasl_local_domain. Cyrus keys entries as user@realm, and with no
    # -u it defaults to the machine's hostname, which in a container is a
    # random ID that changes on every start. That produces a database whose
    # keys never match what Postfix looks up: AUTH is advertised, then every
    # attempt fails 535. $MYHOSTNAME is stable because it derives from the
    # authenticated domain.
    printf '%s' "$_pass" | saslpasswd2 -p -c -f "$_db" -u "$MYHOSTNAME" "$_user"
  done

  # Counted separately: the loop above runs in a subshell (pipe), so a counter
  # incremented inside it would not survive.
  _count="$(printf '%s\n' "$AUTH_ACCOUNTS" | grep -c ':' || true)"

  chmod 0640 "$_db" 2> /dev/null || true
  chown "root:$(postconf -c "$MAIL_CONFIG" -h mail_owner 2> /dev/null || echo postfix)" "$_db" 2> /dev/null || true
  _restore_trace

  log_info "inbound authentication enabled for ${_count} account(s)"
  if [ "$RELAY_AUTH_REQUIRED" = "yes" ]; then
    log_info "authentication is REQUIRED: unauthenticated clients are rejected"
  else
    log_info "authentication is optional: clients in mynetworks may still send without it"
  fi
}

write_maps() {
  # Null-sender suppression. Both key forms are written because Postfix may
  # present the null sender as an empty string or as <>.
  if [ -z "$BOUNCE_RECIPIENT" ]; then
    cat > "${MAIL_CONFIG}/sender_transport" <<- 'EOF'
			# Generated by mailkube-relay. Suppress delivery-status notifications so
			# they can never be relayed back to smtp.mailkube.com, where a
			# MAILER-DAEMON From would be rejected 550 and start a bounce loop.
			# Set BOUNCE_RECIPIENT to receive them at a local address instead.
			/^$/    discard:mailkube-relay-dsn-suppressed
			/^<>$/  discard:mailkube-relay-dsn-suppressed
		EOF
    chmod 0644 "${MAIL_CONFIG}/sender_transport"
  else
    rm -f "${MAIL_CONFIG}/sender_transport"
  fi

  _write_header_checks
}

# smtp_header_checks is applied at delivery time by smtp(8), which makes it
# topology-independent.
#
# This deliberately does NOT use sender_canonical_maps. Postfix gates header
# rewriting for remote SMTP clients behind local_header_rewrite_clients, so a
# canonical-map approach works when the application shares the relay's network
# namespace (sidecar mode) and silently does nothing when it does not (the
# cluster-Service topology), which is precisely where it is needed.
_write_header_checks() {
  _f="${MAIL_CONFIG}/smtp_header_checks"
  _dom_re="$(_pcre_quote "$AUTH_DOMAIN")"
  : > "$_f"

  {
    echo "# Generated by mailkube-relay. Do not edit; regenerated on every start."
    # Ordered first so a DSN is rewritten by this rule rather than by the
    # broader ENFORCE_FROM_DOMAIN rule below.
    if [ -n "$BOUNCE_RECIPIENT" ]; then
      echo "/^From:\\s*(?:MAILER-DAEMON|double-bounce|postmaster)@\\S+\\s*\$/ REPLACE From: Mailkube Relay <postmaster@${AUTH_DOMAIN}>"
    fi
    if [ "$ENFORCE_FROM_DOMAIN" = "yes" ]; then
      # Display-name form:  From: Name <user@other.example>
      echo "/^From:(.*)<([^<>@[:space:]]+)@(?!${_dom_re}>)[^<>@[:space:]]+>\\s*\$/ REPLACE From:\${1}<\${2}@${AUTH_DOMAIN}>"
      # Bare form:          From: user@other.example
      echo "/^From:\\s*([^<>@[:space:]]+)@(?!${_dom_re}\\s*\$)[^<>@[:space:]]+\\s*\$/ REPLACE From: \${1}@${AUTH_DOMAIN}"
    fi
  } >> "$_f"
  chmod 0644 "$_f"

  if [ -s "$_f" ] && [ "$(wc -l < "$_f")" -gt 1 ]; then
    postconf -c "$MAIL_CONFIG" -e "smtp_header_checks = pcre:${_f}"
  else
    postconf -c "$MAIL_CONFIG" -e "smtp_header_checks ="
  fi
}

# Compose mynetworks. Loopback is ALWAYS retained in both modes: the container's
# own healthcheck connects from 127.0.0.1 and smtpd_client_restrictions would
# otherwise reject it.
#
# The defaults cover RFC1918, RFC6598 CGNAT (GKE/EKS secondary pod ranges) and
# IPv6 ULA, because RFC1918 alone does not describe real cluster pod CIDRs.
build_networks() {
  _loopback='127.0.0.0/8, [::1]/128'
  _private='10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16, 100.64.0.0/10, [fc00::]/7'

  if [ "$RELAY_NETWORKS_MODE" = "replace" ] && [ -n "$RELAY_NETWORKS" ]; then
    MYNETWORKS="${_loopback}, $(printf '%s' "$RELAY_NETWORKS" | sed 's/,/, /g')"
    log_info "mynetworks: replace mode, only loopback and RELAY_NETWORKS accepted"
  elif [ -n "$RELAY_NETWORKS" ]; then
    MYNETWORKS="${_loopback}, ${_private}, $(printf '%s' "$RELAY_NETWORKS" | sed 's/,/, /g')"
  else
    MYNETWORKS="${_loopback}, ${_private}"
  fi
}
