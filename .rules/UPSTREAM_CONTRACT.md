# The Upstream Contract

Load this before touching anything that reaches `smtp.mailkube.com`.

Everything in this file is a **verified fact about the running platform**, not a design preference.
These values were established by reading the upstream implementation and confirmed in the spikes
recorded below.

> **Never re-derive these from first principles and never "improve" them.** Every item below has a
> failure mode that is silent locally and expensive upstream: a rejected message, a risk signal, or a
> TCP ban on an IP address shared by the customer's entire cluster. If a value here looks wrong, the
> correct action is to check the platform, then update this file in the same PR as the code.

## 1. The relay host is a literal

`relayhost = [smtp.mailkube.com]:587` in `rootfs/etc/postfix/main.cf`. There is no environment
variable, no build `ARG`, and no override path. `SMTP_SERVER`, which the `boky/postfix` lineage
supported, is accepted-and-warned in `validate_legacy_env()` precisely so a migrating user is told the
host is fixed rather than silently relaying somewhere else.

The `[brackets]` are load-bearing twice:

1. They suppress the MX lookup, making this a plain A/AAAA resolution. That is why musl (Alpine) is a
   non-issue for this workload, as recorded in the `Dockerfile` header.
2. `smtp_tls_security_level = secure` matches the server certificate against the nexthop, which the
   brackets pin to exactly this name.

`sender_dependent_relayhost_maps` is pinned empty in `main.cf` because trivial-rewrite evaluates it
**before** `relayhost`, so a `static:` value there needs no file on disk and would silently win.

## 2. Only ports 587 and 465. Port 25 upstream is forbidden

| Port | Meaning | Relay behaviour |
|---|---|---|
| 587 | STARTTLS submission | default, `smtp_tls_wrappermode = no` |
| 465 | implicit TLS submission | `smtp_tls_wrappermode = yes` |
| 25 | **bounce / DSN intake only** | rejected at boot by `validate_env()` |

Relaying to upstream port 25 fires **risk signal 100** and TCP-bans the source IP. `validate.sh` gives
`SMTP_PORT=25` its own `die_hint` branch, separate from the generic "not a valid port" branch, so the
operator is told *why* rather than just being refused.

Note the asymmetry: **25 is the default ingress port of this container** (`LISTEN_PORT`), and is
forbidden as an **upstream** port. Do not conflate them when editing `validate.sh`.

## 3. AUTH PLAIN only, inside verified TLS

Upstream registers only `smtp_server_auth_plain`, and passwords are hashed at rest, so CRAM-MD5 and
friends are structurally impossible there. `main.cf` sets:

- `smtp_sasl_mechanism_filter = plain`, which intersects the offered list down to PLAIN. It is
  intersection only and can never re-enable a mechanism that security options forbid.
- `smtp_sasl_security_options = noanonymous`. This line is mandatory; see `.rules/POSTFIX_TUNING.md`
  for why omitting it fails every delivery.
- `smtp_tls_security_level = secure`, so TLS is mandatory and the certificate name is verified. The
  deprecated `smtp_use_tls = yes` this replaces means *opportunistic* TLS, under which a
  STARTTLS-stripping MITM harvests the credential in cleartext.

## 4. `SMTP_USERNAME` must be `user@domain`

The upstream splits the AUTH identity on `@` into local part and domain and rejects anything else with
a 535 **plus a risk signal** (`AUTH_INVALID_USERNAME_FORMAT`). A bare username never authenticates.
`validate.sh` enforces `^[^@[:space:]]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$` at boot, because failing in the
container with a named reason is strictly better than 20 malformed AUTH attempts against the platform.

The domain part is **the authenticated domain by definition**, and `derive_identity()` in `env.sh`
takes `AUTH_DOMAIN` from it. Do not derive the domain from the hostname: the lineage script's
`awk 'BEGIN{FS=OFS="."}{print $(NF-1),$NF}'` yields `co.uk` for `mail.example.co.uk` and emits a
leading-comma garbage value for a single-label hostname.

## 5. The password is an SMTP credential secret, NOT an API key

They are different objects with different lifecycles. An API key (`mk_…`) presented as an SMTP password
produces a 535 that is indistinguishable from a typo. `validate.sh` warns (does not fail) on an `mk_`
prefix and points at `https://app.mailkube.com/domain/credentials#smtp`. It warns rather than fails
because the key prefix is a convention, not a guarantee, and a hard failure on a false positive would
lock out a legitimate password that happens to start with those characters.

## 6. The 550 From check reads the message HEADER, not the envelope

Upstream validates the **`From:` message header** against the authenticated domain and answers 550 when
it does not match. This has two consequences that are easy to get wrong:

- `myorigin` qualifies the **envelope** sender only. Setting it does not satisfy the upstream check.
- Postfix gates header rewriting for remote SMTP clients behind `local_header_rewrite_clients`, so
  `sender_canonical_maps` works in sidecar mode (shared network namespace) and silently does nothing in
  the cluster-Service topology, which is exactly where it is needed.

Therefore `ENFORCE_FROM_DOMAIN=yes` is implemented with `smtp_header_checks` (PCRE, generated by
`_write_header_checks()` in `render.sh`), which smtp(8) applies at delivery time and is
topology-independent. Do not replace it with a canonical map.

The same rule is why `BOUNCE_RECIPIENT` must be at `AUTH_DOMAIN`: a DSN sent elsewhere carries a
`MAILER-DAEMON@$myhostname` From, gets 550'd upstream, and generates a further bounce. That loop dumps
traffic on the platform, which is why `validate.sh` refuses it at boot and why the default posture
(tier 2) discards null-sender mail into `discard:` rather than relaying it.

## 7. The five `X-Mailkube-*` headers pass through untouched

| Header | Purpose |
|---|---|
| `X-Mailkube-Topic` | mailing-list topic slug, drives subscription and opt-out handling |
| `X-Mailkube-Tags` | JSON array of `{name, value}` send-time message tags |
| `X-Mailkube-Template-Id` | server-side template selection |
| `X-Mailkube-Template-Version` | template version pin |
| `X-Mailkube-Template-Variables` | JSON template variable payload |

The MTA reads these off the DATA headers and **strips them before delivery**, so they never reach the
recipient. This relay must pass them through byte-for-byte. Two rules follow:

- `always_add_missing_headers = yes` is safe because it only ADDS absent headers and never modifies
  present ones. Anything that rewrites headers generically is not safe.
- `smtp_line_length_limit = 998` breaks a long header line by inserting CRLF+SPACE at a fixed byte
  offset with **no whitespace search**, which corrupts a JSON header value. Every physical
  `X-Mailkube-*` header line must therefore stay at or under 998 bytes including the field name. See
  `.rules/POSTFIX_TUNING.md` for why the parameter is not set to 0.

`SMTP_HEADER_TAG` from the lineage image is removed; `validate_legacy_env()` points users at
`X-Mailkube-Tags`.

## 8. The 450/454 to RiskSignal to IP-ban chain

This is the reason most of `main.cf` looks the way it does. **Every 4xx the upstream returns emits a
RiskSignal**, and RiskSignals accumulate into a TCP ban.

| Signal rule | Window | Threshold | Ban escalation (1st / 2nd / 3rd+) |
|---|---|---|---|
| `IP_MSG_RATE_LIMITED` (message throttle, 450) | 30 min | 100 signals | 10 / 20 / 30 min |
| `IP_AUTH_RATE_LIMITED` (auth throttle, 454) | 15 min | 20 signals | 60 / 360 / 1440 min |

Note how much cheaper it is to trip the AUTH rule: 20 signals in 15 minutes for a **one hour** first
ban, escalating to a full day. The per-domain budget upstream is 2 AUTH/sec, so an image that
authenticates once per message reaches 20 throttled AUTHs during any ordinary backlog drain. That is
the whole justification for `smtp_tls_connection_reuse = yes`, `initial_destination_concurrency = 1`,
`minimal_backoff_time = 120s` and `RELAY_START_JITTER`.

**What a ban actually is:** a TCP-level reject on ports 587, 465 and 25, with **no SMTP banner**. The
client sees a connection failure, not a 5xx, so there is no diagnostic reply to read and nothing in the
mail log except connect failures.

**Who gets banned:** the source IP, which in a Kubernetes or Compose cluster is the **shared egress
NAT address**. It is shared by every replica of this relay, by every other workload behind the same
NAT, and potentially by other tenants of the same cluster. A single misconfigured replica bans them
all. This is why `main.cf` reasons in terms of a fleet budget rather than a per-container budget, and
why the ingress-side connection ceiling (20 concurrent per source IP at HAProxy) is treated as a
cluster-wide resource in `.rules/POSTFIX_TUNING.md`.

## Do not

- Do not add an environment variable that changes the relay host, the TLS security level, or the SASL
  mechanism list.
- Do not "helpfully" allow upstream port 25 behind a flag.
- Do not shorten `minimal_backoff_time` or raise `initial_destination_concurrency` for throughput. Both
  trade a bounded latency for an unbounded ban risk on an address you do not own.
- Do not relax the `SMTP_USERNAME` pattern to accept a bare username. The 535 it produces is a risk
  signal, not a harmless error.
