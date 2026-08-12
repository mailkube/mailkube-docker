# syntax=docker/dockerfile:1
# =============================================================================
#  ghcr.io/mailkube/smtp-relay
#  A companion SMTP relay: plain unauthenticated SMTP in, authenticated TLS to
#  smtp.mailkube.com out, with a durable local queue in between.
# =============================================================================
#
#  Base: Alpine, pinned by digest.
#
#  The usual "musl breaks Postfix" objection does not apply to this workload.
#  main.cf uses `relayhost = [smtp.mailkube.com]:587`, and the brackets suppress
#  the MX lookup entirely, so this is a plain A/AAAA resolution that musl handles
#  correctly. This is a null client: no local delivery, no virtual maps, no
#  LDAP/SQL, no chroot. The exercised path is smtpd -> cleanup -> qmgr -> smtp.
#
#  Debian would cost roughly 4x the image size and 4x the CVE surface. This is a
#  sidecar, pulled on every pod start, in every pod, on every node, so that is a
#  real operational cost for no functional gain.
#
#  Single stage: nothing is compiled, so a builder stage would only produce a
#  second copy of the same apk closure.
# =============================================================================
FROM alpine:3.24@sha256:28bd5fe8b56d1bd048e5babf5b10710ebe0bae67db86916198a6eec434943f8b

#  Build-time shell only (this does not affect ENTRYPOINT, which uses /bin/sh).
#  pipefail matters for the assertion block below: without it, a `postconf` that
#  failed outright would still let the pipeline's exit status come from `grep`,
#  and an assertion could pass against no output at all.
SHELL ["/bin/ash", "-o", "pipefail", "-c"]

# hadolint ignore=DL3018
RUN apk add --no-cache \
    postfix \
    postfix-pcre \
    cyrus-sasl \
    cyrus-sasl-login \
    ca-certificates \
    tzdata

# --- build-time assertions ---------------------------------------------------
#  These turn three otherwise-untestable assumptions into BUILD failures rather
#  than production failures: Alpine's package layout, Postfix's compiled-in map
#  types, and the presence of the services the runtime configuration references.
#
#  Note there is no `cyrus-sasl-plain` package on Alpine; libplain.so ships in
#  the base cyrus-sasl package. The assertion checks the plugin, not the package
#  name, so it stays correct if that ever changes.
RUN set -eux; \
    postconf -m | grep -qx pcre     || { echo "FATAL: pcre map support missing (sender_transport, smtp_header_checks)"; exit 1; }; \
    postconf -m | grep -qx texthash || { echo "FATAL: texthash map support missing (sasl_passwd)"; exit 1; }; \
    postconf -m | grep -qx lmdb     || { echo "FATAL: lmdb map support missing (TLS session cache)"; exit 1; }; \
    [ -e /usr/lib/sasl2/libplain.so ] || { echo "FATAL: SASL PLAIN plugin missing"; exit 1; }; \
    [ -e /usr/lib/sasl2/libsasldb.so ] || { echo "FATAL: SASL sasldb auxprop plugin missing (inbound auth)"; exit 1; }; \
    command -v saslpasswd2 >/dev/null || { echo "FATAL: saslpasswd2 missing (inbound auth)"; exit 1; }

COPY rootfs/ /

#  Asserted AFTER the config is copied, because these check our master.cf rather
#  than the stock one. tlsproxy is the load-bearing entry: without it,
#  smtp_tls_connection_reuse silently does nothing and every message pays a fresh
#  AUTH against a 2/sec per-domain budget.
RUN set -eux; \
    postconf -M tlsproxy/unix >/dev/null || { echo "FATAL: tlsproxy service missing (required by smtp_tls_connection_reuse)"; exit 1; }; \
    postconf -M scache/unix   >/dev/null || { echo "FATAL: scache service missing (connection cache)"; exit 1; }; \
    postconf -M discard/unix  >/dev/null || { echo "FATAL: discard transport missing (DSN suppression)"; exit 1; }; \
    chmod +x /usr/local/bin/docker-entrypoint.sh /usr/local/bin/healthcheck.sh; \
    postfix -c /etc/postfix check

#  Ingress port. Documentation only; the actual bind is LISTEN_PORT.
EXPOSE 25

#  A real SMTP probe rather than a process check: master can be up while smtpd is
#  not yet accepting. start-period covers the config copy, mount normalization,
#  preflight, master startup, and RELAY_START_JITTER (up to 15s by default).
#
#  This is the single definition. The Compose files deliberately do NOT restate
#  it: `depends_on: condition: service_healthy` reads the container's health
#  status, which the image's own HEALTHCHECK already supplies.
#  Kubernetes is the exception. kubelet ignores this directive, so the manifests
#  define equivalent probes calling the same script.
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD ["/usr/local/bin/healthcheck.sh"]

#  No CMD on purpose. With one, `docker run <image> sh` would silently skip all
#  setup and start an unconfigured shell, which is how the previous image behaved.
ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]

# --- OCI metadata ------------------------------------------------------------
#  docker/metadata-action overrides version/revision/created at push time.
ARG VERSION=dev
ARG REVISION=unknown
ARG CREATED=1970-01-01T00:00:00Z
LABEL org.opencontainers.image.title="ghcr.io/mailkube/smtp-relay" \
      org.opencontainers.image.description="Companion SMTP relay that gives applications a local, durable, unauthenticated SMTP queue and forwards to smtp.mailkube.com over authenticated TLS." \
      org.opencontainers.image.url="https://github.com/mailkube/mailkube-docker" \
      org.opencontainers.image.documentation="https://github.com/mailkube/mailkube-docker#readme" \
      org.opencontainers.image.source="https://github.com/mailkube/mailkube-docker" \
      org.opencontainers.image.vendor="Mailtactic, Corp." \
      org.opencontainers.image.licenses="Apache-2.0" \
      org.opencontainers.image.authors="Walid Trabelsi <walid.trabelsi@mailkube.com>" \
      org.opencontainers.image.base.name="docker.io/library/alpine:3.24" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${REVISION}" \
      org.opencontainers.image.created="${CREATED}"
