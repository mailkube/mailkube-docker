#!/bin/sh
# Container healthcheck: a real SMTP probe, not just "is the process alive".
#
# Postfix's master can be running while smtpd is not yet accepting, so a process
# check would report healthy during the window in which applications get
# connection refused. This opens a connection, reads the banner and quits.
#
# Reads LISTEN_PORT so it follows a non-default ingress port. Probing a hardcoded
# 25 would fail on every high-port deployment.
#
# Note for Kubernetes: kubelet does NOT consume the OCI HEALTHCHECK directive.
# The manifests define their own probes that invoke this same script.
set -eu

PORT="${LISTEN_PORT:-25}"

# The delay before QUIT is required, not cosmetic. Postfix's protocol
# synchronization guard rejects a command that arrives before the banner has
# been sent, so probing with an immediate QUIT returns
# "554 5.5.0 Error: SMTP protocol synchronization" and the healthcheck fails
# against a perfectly healthy relay. Waiting for the banner also keeps the
# server-side log line clean (quit=1 commands=1 rather than a pipelining error
# every 30 seconds).
resp="$( (sleep 0.5; printf 'QUIT\r\n') | nc -w 3 127.0.0.1 "$PORT" 2>/dev/null | head -n 1 || true)"

case "$resp" in
220*)
	exit 0
	;;
*)
	echo "healthcheck: no SMTP banner on 127.0.0.1:${PORT} (got: ${resp:-<nothing>})" >&2
	exit 1
	;;
esac
