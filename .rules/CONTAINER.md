# Container Rules

Load this before editing the `Dockerfile`, the base-image pin, the package list, the build assertions,
the OCI labels, the healthcheck, or the runtime security posture.

## Base image

```
FROM alpine:3.24@sha256:28bd5fe8b56d1bd048e5babf5b10710ebe0bae67db86916198a6eec434943f8b
```

- **Alpine, not Debian.** Debian would cost roughly 4x the image size and 4x the CVE surface. This is a
  sidecar: pulled on every pod start, in every pod, on every node. That is a real operational cost for
  no functional gain here.
- **musl is a non-issue for this workload.** The usual objection is about MX lookups.
  `relayhost = [smtp.mailkube.com]:587` brackets the nexthop, which suppresses the MX lookup entirely,
  so this is a plain A/AAAA resolution that musl handles correctly. This is a null client: no local
  delivery, no virtual maps, no LDAP or SQL, no chroot. The exercised path is
  `smtpd -> cleanup -> qmgr -> smtp`.
- **Single stage.** Nothing is compiled, so a builder stage would only produce a second copy of the
  same apk closure.
- **Always pinned by digest.** Dependabot bumps the digest; a bump PR must be re-tested, not
  auto-merged, because it can move the Postfix version and therefore every default in
  `.rules/POSTFIX_TUNING.md`.

### Why DL3018 is waived

`.hadolint.yaml` ignores DL3018 ("pin versions in apk add"). The digest pin on the base image already
fixes the Alpine release and therefore the package repository snapshot. Pinning individual apk versions
**on top of that** makes the build fail the moment Alpine supersedes a package on a security update,
because apk repositories do not retain superseded versions. Digest-pinned base plus floating patch
versions is the combination that gets reproducible builds **and** security updates. This waiver is
scoped in config with that reasoning; do not extend it to other rules and do not remove the
`# hadolint ignore=DL3018` comment at the `apk add`.

## The package list

```
postfix postfix-pcre cyrus-sasl ca-certificates tzdata
```

| Package | Why |
|---|---|
| `postfix` | the MTA |
| `postfix-pcre` | required by `sender_dependent_default_transport_maps` (the null-sender `discard:` map) and by `smtp_header_checks` (`ENFORCE_FROM_DOMAIN`) |
| `cyrus-sasl-login` | keep | server-side `AUTH LOGIN` for optional inbound authentication. `AUTH PLAIN` needs no extra package. Also supplies `saslpasswd2`'s companion tooling. |
| `cyrus-sasl` | ships `libplain.so`, the SASL PLAIN **client** plugin |
| `ca-certificates` | the trust store `smtp_tls_security_level = secure` verifies against |
| `tzdata` | so `TZ` produces correct timestamps in the mail log |

**There is no `cyrus-sasl-plain` package on Alpine.** `libplain.so` ships in the base `cyrus-sasl`
package, and `postfix` pulls `cyrus-sasl` transitively. Adding `cyrus-sasl-plain` to the list fails the
build with "unsatisfiable constraints". The build assertion checks for the **plugin file**, not the
package name, so it stays correct if Alpine ever splits the package.

Adding a package needs a stated reason in the PR. Every one is pulled on every pod start.

## The 7 build assertions

They turn otherwise-untestable assumptions into **build** failures rather than production failures.
Four run before `COPY rootfs/ /` (they test the distribution), three after (they test our config).

Before the copy, against Alpine's package layout and Postfix's compiled-in map types:

1. `postconf -m | grep -qx pcre`: `sender_transport` and `smtp_header_checks` are PCRE maps. Without
   pcre support they fail at delivery time, not at boot.
2. `postconf -m | grep -qx texthash`: `smtp_sasl_password_maps` is `texthash:`, chosen so there is no
   `postmap` step and no `.lmdb` sidecar next to the credential.
3. `postconf -m | grep -qx lmdb`: `smtp_tls_session_cache_database` is `lmdb:`.
4. `ls /usr/lib/sasl2/ | grep -q '^libplain\.so$'`: the AUTH PLAIN client plugin, see above.

After the copy, against **our** `master.cf` rather than the stock one:

5. `postconf -M tlsproxy/unix`: the load-bearing entry. Without `tlsproxy`,
   `smtp_tls_connection_reuse` silently does nothing and every message pays a fresh AUTH against a
   2/sec per-domain budget. Stock Alpine ships this line commented out, so a careless merge from
   upstream reintroduces the failure with no other symptom.
6. `postconf -M scache/unix`: the shared connection cache that holds the reused authenticated sessions.
7. `postconf -M discard/unix`: the target of the null-sender transport map that suppresses DSNs when
   `BOUNCE_RECIPIENT` is unset. Without it, a DSN would be relayed upstream, rejected 550 and loop.

The same `RUN` also `chmod +x` the two entrypoint scripts and runs `postfix -c /etc/postfix check`.

**Never delete an assertion to make a build pass.** Each one exists because the failure it catches is
silent at runtime. If an assertion legitimately no longer applies, remove the feature that depends on
it in the same PR.

## OCI labels

All eleven `org.opencontainers.image.*` labels stay populated. `version`, `revision` and `created` come
from build args and are overridden by `docker/metadata-action` at push time; keep the `ARG` defaults
(`dev`, `unknown`, the epoch) so a local build still produces a valid label set.
`base.name` must be updated whenever the base image line changes.

**`source` is load-bearing, not decorative.** GHCR uses it to link the package to this repository,
which is what makes the package page render `README.md`. Drop it and the page goes blank with no
error anywhere in the build or the publish. See `.rules/RELEASE.md`.

## Multi-arch

The image is built and published for `linux/amd64` and `linux/arm64`. Nothing in the tree is
architecture-specific (no compilation, no binary downloads), so the constraint is only that any new
dependency must exist for both. A package that is amd64-only silently breaks the arm64 leg of the
manifest, so check before adding one.

## Runtime security posture

Verified in spike S2 with:

```
--read-only --tmpfs /run --tmpfs /tmp
-v spool:/var/spool/postfix -v data:/var/lib/postfix
--cap-drop ALL --cap-add CHOWN,DAC_OVERRIDE,FOWNER,SETGID,SETUID,NET_BIND_SERVICE
--security-opt no-new-privileges
```

### Capabilities, one justification each

| Capability | Why it is required |
|---|---|
| `CHOWN` | `normalize_mounts()` sets the owner of `/var/spool/postfix` and `/var/lib/postfix` on a fresh volume, and `write_credentials()` chowns `sasl_passwd` to `mail_owner` |
| `DAC_OVERRIDE` | Postfix's own privilege split reads and writes across the `postfix`/`postdrop` boundary; it is also what makes `postqueue -p` work under `no-new-privileges` (see below) |
| `FOWNER` | `chmod` on directories the process does not own after a volume is mounted with a foreign owner |
| `SETGID` | `master` drops to `mail_owner` and to `setgid_group` (`postdrop`, gid 102) when spawning services |
| `SETUID` | `master` drops to the `postfix` uid (100) for `smtpd`, `smtp`, `qmgr` and friends |
| `NET_BIND_SERVICE` | binds the default `LISTEN_PORT=25`. Only droppable when `LISTEN_PORT` is above 1023 |

`CAP_SYS_CHROOT` is **not** in the set, and must not be added: `master.cf` keeps `chroot = n` on every
service for the reasons in `.rules/POSTFIX_TUNING.md`.

`no-new-privileges` is safe here even though `postdrop` and `postqueue` are setgid `postdrop`: the
entrypoint runs as root with `DAC_OVERRIDE`, so the ignored setgid bit does not matter and
`postqueue -p` still works. This was the audit's largest open container risk and is now verified, which
is what makes the graceful-drain design in `lifecycle.sh` safe under the hardened securityContext.

### `readOnlyRootFilesystem: true` requirements

The image is designed for it. Three things are required and must stay true:

- **`/run` must be writable (tmpfs).** `MAIL_CONFIG=/run/postfix` is the runtime config directory, via
  Postfix's own multi-instance mechanism (`postfix -c /run/postfix start-fg` with
  `config_directory = /run/postfix`). That is what lets the whole of `/` stay read-only and, more
  importantly, puts the SMTP credential on tmpfs instead of a disk-backed layer.
- **`/var/spool/postfix` must be a writable volume**, or the queue is not durable and the product has
  no reason to exist.
- **`/var/lib/postfix` must be a writable volume** for `smtp_scache` (the TLS session cache).

`normalize_mounts()` exists because Kubernetes creates emptyDir volumes 0777 (hardcoded in
`empty_dir.go`) and most CSI provisioners create PV roots 0777. Postfix's `check-warn` then warns
"group or other writable" and the fatal-warning preflight refuses to start. Docker named volumes hide
this completely (they seed from the image directory with mode and owner preserved), so CI passes green
while every shipped Kubernetes manifest fails. That asymmetry is why test T-23 mounts tmpfs at
`mode=1777`. The chmod is deliberately **non-recursive**: `post-install create-missing` already applies
the correct modes to the queue subdirectories, and a recursive chmod would destroy the setgid bits
`public/` and `maildrop/` require.

## `EXPOSE`, `CMD` and `HEALTHCHECK`

- `EXPOSE 25` is documentation only. The actual bind follows `LISTEN_PORT`.
- **There is deliberately no `CMD`.** With one, `docker run <image> sh` would silently skip all setup
  and start an unconfigured shell, which is how the lineage image behaved. `ENTRYPOINT` is the only
  entry point.
- `HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3` runs
  `/usr/local/bin/healthcheck.sh`. `start-period` covers the config copy, mount normalization,
  preflight, master startup and `RELAY_START_JITTER` (up to 15s by default).

Three facts about the healthcheck that must not be lost:

1. **kubelet ignores the OCI `HEALTHCHECK` directive entirely.** The Kubernetes manifests in
   `examples/kubernetes/` define their own readiness and liveness probes that invoke the *same script*.
   A change to probe semantics has to be made in both places or the two environments diverge silently.
2. **The probe must wait for the banner.** Probing with an immediate `QUIT` trips Postfix's protocol
   synchronization guard and returns `554 5.5.0 Error: SMTP protocol synchronization`, so a perfectly
   healthy relay reports unhealthy and every Kubernetes deployment flaps. `healthcheck.sh` sleeps 0.5s
   before sending `QUIT` and accepts only a `220*` first line. Waiting also keeps the server-side log
   clean (`quit=1 commands=1` instead of a pipelining error every 30 seconds). This was a real bug
   found during implementation; do not "simplify" the sleep away.
3. **Compose files must not restate it.** `depends_on: condition: service_healthy` reads the
   container's health status, which the image's directive already supplies, so a restated block adds
   a second definition to keep in step and nothing else. The image is the single definition
   everywhere except Kubernetes, where point 1 forces a second one.

The probe reads `LISTEN_PORT` rather than hardcoding 25, so it follows a non-default ingress port.

## Optional inbound authentication

Three build assertions cover it, alongside the client-side ones:

- `[ -e /usr/lib/sasl2/libsasldb.so ]` — the `auxprop` backend the server config uses.
- `command -v saslpasswd2` — builds the password database at startup.
- `[ -e /usr/lib/sasl2/libplain.so ]` — shared with the client path.

`rootfs/etc/sasl2/smtpd.conf` is baked and static so `/etc` can stay read-only. Only the database
itself is written, to `$MAIL_CONFIG/sasldb2` on tmpfs, so inbound passwords never reach a
disk-backed layer either.

Listener certificates are **mounted, never generated**. Generating a self-signed certificate at
startup would force every client to disable verification, which is a worse security posture than
plaintext on a controlled network because it normalises accepting any certificate.
