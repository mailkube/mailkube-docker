# Testing

Load this before adding or changing a test, the harness, or any environment variable.

The tests are **black-box integration tests**. They start the real image against a recording SMTP sink
and assert on what arrived on the wire and on what the sink counted. There are no unit tests of shell
functions, because every interesting behaviour of this image is an interaction with Postfix.

## The sink harness

`test/sink/` is a test-only image (never published) built on `python3.13-alpine` with `aiosmtpd` and
`cryptography`.

`sink.py` stands in for `smtp.mailkube.com` and records:

| Counter | What it proves |
|---|---|
| `connections`, `auths` | the connection-reuse assertion (T-14b). A correct relay carries many messages over one authenticated connection |
| `auth_times` | AUTHs **per second**, which is what the upstream limit actually constrains, not just the total |
| `peak_concurrent` | the per-source-IP connection ceiling is respected |
| `messages`, `data_commands` | delivery vs. attempts, so a 450 retry is visible |
| `auth_failures` | AUTH rejection paths |

Every accepted message is persisted verbatim to `messages.jsonl`, decoded `latin-1` so it round-trips
every byte and header-fidelity assertions see exactly what arrived.

The sink mirrors the upstream contract where it matters: it requires TLS before AUTH
(`auth_require_tls=True`), accepts only `PLAIN`, and rejects an identity without `@`, because the real
server splits on `@` and 535s otherwise. Failures are scripted through environment variables so a test
reconfigures behaviour by restarting the container: `SINK_REPLY_DATA` (the final reply to DATA, e.g. a
550 From rejection), `SINK_FAIL_FIRST` (450 the first N DATA commands), `SINK_AUTH_FAIL_S` (reject AUTH
for N seconds after start).

`CountingController.factory()` overrides the protocol class. `Controller(server_class=…)` is **not** an
aiosmtpd API; overriding `factory` is the supported way, and the wrong form was a real bug during
implementation.

## The two seams, and why neither is an override

The relay under test is the **shipped image, unmodified**. There is no test-only code path and no
test-only environment variable inside it. That is the point: `smtp_tls_security_level = secure` and the
hardcoded relay host are security-critical, and a test that weakens them tests a different product.

1. **Docker network alias.** The sink container is given `--network-alias smtp.mailkube.com` on a user
   defined Docker network. The relay resolves the hardcoded name through `smtp_host_lookup = native`
   and reaches the sink. Ordinary DNS, no relay-side change. (This is also why `master.cf` keeps
   `chroot = n`: a chrooted `smtp(8)` cannot read `/etc/hosts`.)
2. **Hashed CA in CApath.** `gen_ca.py` generates a throwaway CA and a leaf for `smtp.mailkube.com`,
   and emits `ca_hash.txt`. The harness bind-mounts the CA PEM at
   `/etc/ssl/certs/$(cat ca_hash.txt).0`. That is plain OpenSSL trust-store behaviour: `main.cf`
   already sets `smtp_tls_CApath = /etc/ssl/certs` for production reasons. Nothing is disabled.

Confirmation that the seam is exercising the real path: the mail log must say **`Verified`** TLS
connection, not `Trusted` or `Untrusted`. `Verified` is what proves
`smtp_tls_security_level = secure` is enforcing name-matched verification.

Adding a third seam requires the same standard: it must be a production mechanism used as intended, not
a bypass.

## T-14b MUST use a spaced cadence

> **The connection-reuse test sends its messages SPACED, not as a burst. A burst test passes even when
> the design is completely broken.**

Postfix caches a connection on demand for back-to-back deliveries within roughly a 1-second window,
independently of `smtp_tls_connection_reuse`. So a rapid burst of N messages goes over one connection
whether or not the configuration is correct, and the assertion is vacuous.

The verified cadence is **20 messages spaced 3 seconds apart**: outside qmgr's 1s back-to-back window,
and inside the 45s `smtp_connection_cache_time_limit`. Results from spike S1:

```
reuse ENABLED  (shipped config):   20 messages -> 1 AUTH   (conn_use=2..20)
reuse DISABLED (counter-factual):   8 messages -> 8 AUTHs
```

Do not shorten the spacing to make the suite faster. If the suite runtime is a problem, reduce the
message count, never the interval.

## Counter-factual tests are required

The disabled-reuse run above is **kept as a test**, not just as a spike note. It proves the primary
assertion is not vacuous.

Any test that asserts "a tuning value has an effect" needs a counter-factual: a run with the value
inverted that fails the same assertion. Without it, a test that passes because the value does nothing
is indistinguishable from a test that passes because the value works. Trap 1 and trap 4 in
`.rules/POSTFIX_TUNING.md` are both failure modes that are *silent*, which is exactly the class a
counter-factual catches.

T-12b is the same idea applied to the boot gate: a positive control that `preflight()`'s output
predicate still rejects something, so the gate cannot silently become a no-op (remember
`postfix check` exits 0 even when it warns).

## Every new environment variable needs a matrix row

Non-negotiable, and enforced by `scripts/check-env-matrix.sh` in the `test` CI job. A variable added to
`env.sh`, or a `die_hint` branch added to `validate.sh`, with no matching named row fails the build.

A row must cover:

- the **accepting** case: the value takes effect, asserted on observable behaviour (what the sink saw,
  what `postconf -c /run/postfix -h <param>` reports, what the log line says), never on the fact that
  the container started;
- the **rejecting** case, for every `die_hint` branch, asserted on a non-zero exit and on the message;
- the **default** when omitted.

This is the behavior-coverage pillar; see `.rules/SOLID_DRY_KISS.md` for why it replaces line coverage
in this repo.

## Always set `RELAY_START_JITTER=0` in tests

The default is 15 seconds of random startup delay, which exists to decorrelate synchronized
multi-replica cold starts. In a test it turns every assertion into a flaky race. Set it to 0 in every
harness invocation, including in any new fixture.

## Environment-specific traps the suite must keep covering

- **Volume permissions.** Docker named volumes seed from the image directory with mode and owner
  preserved, so they hide the 0777 problem that Kubernetes emptyDir and most CSI provisioners create.
  T-23 mounts tmpfs at `mode=1777` specifically so CI reproduces the Kubernetes case. Do not replace it
  with a named volume.
- **Both upstream ports.** 587 (STARTTLS, `SINK_IMPLICIT_TLS=no`) and 465 (implicit TLS,
  `SINK_IMPLICIT_TLS=yes`, relay `smtp_tls_wrappermode = yes`) are separate paths and both must be
  exercised.
- **Header fidelity.** The five `X-Mailkube-*` headers must arrive byte-identical, including a long
  `X-Mailkube-Template-Variables` value near the 998-byte line limit.

## Resume protocol for a local run

1. `docker build -t ghcr.io/mailkube/smtp-relay:dev .`
2. Build the `test/sink` image; run `gen_ca.py` into `/tmp/mkrelay/certs`.
3. Create the docker network (`mknet`); start the sink with `--network-alias smtp.mailkube.com`.
4. Start the relay with `-v <ca>.pem:/etc/ssl/certs/$(cat ca_hash.txt).0:ro` and
   `RELAY_START_JITTER=0`.
