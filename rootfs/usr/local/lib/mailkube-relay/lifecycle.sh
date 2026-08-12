# shellcheck shell=dash
# Boot gate, startup diagnostics, and the supervisor.

# Convert a Postfix time value to whole seconds.
_to_seconds() {
	_v="$1"
	_n="${_v%[smhdw]}"
	case "$_v" in
	*s | "$_n") echo "$_n" ;;
	*m) echo $((_n * 60)) ;;
	*h) echo $((_n * 3600)) ;;
	*d) echo $((_n * 86400)) ;;
	*w) echo $((_n * 604800)) ;;
	*) echo "$_n" ;;
	esac
}

# The boot gate.
#
# `postfix check` EXITS 0 EVEN WHEN IT WARNS: postfix-script's check) branch ends
# in `exit 0` after check-warn. So an exit-code assertion alone is vacuous, and
# the output predicate below is what actually does the work.
#
# What this catches, verified: misspelled parameter NAMES ("unknown parameter",
# "unused parameter") and ownership/permission problems on the queue and data
# directories. What it does NOT catch: bad VALUES. `postconf -e
# smtp_destination_concurrency_limit=not-a-number` passes silently, which is why
# validate.sh carries the grammar allowlist and T-12b exists as a positive
# control against this gate silently becoming a no-op.
preflight() {
	_out="$(postfix -c "$MAIL_CONFIG" check 2>&1)" || {
		log_error "postfix check failed:"
		printf '%s\n' "$_out" >&2
		die "configuration is not valid"
	}
	if printf '%s' "$_out" | grep -qiE 'unknown parameter|unused parameter|bad numerical|unsupported dictionary type|no such file or directory|group or other writable|not owned by'; then
		log_error "postfix check reported a fatal-class warning:"
		printf '%s\n' "$_out" >&2
		die "refusing to start with a suspect configuration"
	fi
	# Anything else on stderr is surfaced but does not block the boot.
	[ -n "$_out" ] && printf '%s\n' "$_out" >&2
	log_info "preflight passed"
	return 0
}

# State the security model on every start, and warn when the container's own
# address is outside private space.
#
# Be honest about the limit of this check: inside a Kubernetes pod the address is
# always a private pod IP, so the warning cannot fire there even when a
# LoadBalancer Service has exposed the port. Network isolation is the real
# control, not this line. See examples/kubernetes/30-networkpolicy.yaml.
warn_if_publicly_reachable() {
	log_info "port ${LISTEN_PORT} accepts UNAUTHENTICATED SMTP from: ${MYNETWORKS}"

	_ip="$(hostname -i 2>/dev/null | tr ' ' '\n' | grep -E '^[0-9]+\.' | head -n 1)" || _ip=''
	[ -n "$_ip" ] || return 0
	case "$_ip" in
	10.* | 127.* | 192.168.* | 169.254.*) return 0 ;;
	172.1[6-9].* | 172.2[0-9].* | 172.3[01].*) return 0 ;;
	100.6[4-9].* | 100.[7-9][0-9].* | 100.1[01][0-9].* | 100.12[0-7].*) return 0 ;;
	esac
	log_warn "this container's address ${_ip} is outside private ranges."
	log_warn "Anyone who can reach port ${LISTEN_PORT} can send mail as ${AUTH_DOMAIN}."
	log_warn "Do not publish this port to the internet."
}

_queue_count() {
	_out="$(postqueue -c "$MAIL_CONFIG" -p 2>/dev/null | tail -n 1)"
	case "$_out" in
	*'is empty'*) echo 0 ;;
	*Request*) printf '%s' "$_out" | sed -n 's/.*-- .* in \([0-9][0-9]*\) Request.*/\1/p' ;;
	*) echo 0 ;;
	esac
}

_STOPPING=0

# Flush the queue, wait for it to drain, then stop cleanly.
#
# Postfix has no "stop accepting but keep delivering" mode, so a message can
# still arrive mid-drain. The Kubernetes-side fix is a preStop hook that lets
# endpoint removal win the race; see the manifests.
graceful_stop() {
	[ "$_STOPPING" -eq 1 ] && return 0
	_STOPPING=1
	_budget="$(_to_seconds "$SHUTDOWN_DRAIN_TIMEOUT")"
	log_info "shutdown requested, draining queue (budget ${_budget}s)"

	postqueue -c "$MAIL_CONFIG" -f 2>/dev/null || true

	_left="$(_queue_count)"
	_waited=0
	while [ "${_left:-0}" -gt 0 ] && [ "$_waited" -lt "$_budget" ]; do
		sleep 1
		_waited=$((_waited + 1))
		_left="$(_queue_count)"
	done

	if [ "${_left:-0}" -gt 0 ]; then
		# Fixed-shape line: the README alert expressions match on it. A sidecar
		# is not guaranteed a drain window, because a pod has ONE
		# terminationGracePeriodSeconds spent sequentially across containers.
		printf 'mailkube-relay: drain-incomplete undelivered=%s timeout=%ss\n' "$_left" "$_budget" >&2
		postfix -c "$MAIL_CONFIG" stop 2>/dev/null || true
		exit 75
	fi

	log_info "queue drained, stopping"
	postfix -c "$MAIL_CONFIG" stop 2>/dev/null || true
	exit 0
}

# Supervisor.
#
# We deliberately do NOT exec, because a custom drain is needed on SIGTERM.
# The trap is mandatory rather than stylistic: PID 1 has no default signal
# dispositions, so an untrapped SIGTERM to a PID-1 shell is IGNORED and the
# container would sit until SIGKILL. No init shim is needed: there is exactly one
# child, and master reaps its own children.
start_and_supervise() {
	# A remounted spool carries a stale pid file from the previous container.
	rm -f /var/spool/postfix/pid/master.pid

	if [ "$RELAY_START_JITTER" -gt 0 ]; then
		_j=$(awk -v m="$RELAY_START_JITTER" 'BEGIN{srand();print int(rand()*m)}')
		[ "$_j" -gt 0 ] && log_info "start jitter ${_j}s (decorrelates multi-replica cold starts)"
		sleep "$_j"
	fi

	trap graceful_stop TERM INT

	postfix -c "$MAIL_CONFIG" start-fg &
	MASTER_PID=$!
	log_info "relay ready: [smtp.mailkube.com]:${SMTP_PORT} as ${SMTP_USERNAME}"

	# POSIX `wait` returns 128+signum when a trap fires, at which point
	# graceful_stop has already run and exited. Reaching past this loop means
	# master died on its own, which is always an error.
	while [ "$_STOPPING" -eq 0 ]; do
		if wait "$MASTER_PID"; then _rc=0; else _rc=$?; fi
		[ "$_STOPPING" -eq 1 ] && break
		[ "$_rc" -gt 128 ] && continue
		log_error "postfix master exited unexpectedly with status ${_rc}"
		exit "$_rc"
	done
}
