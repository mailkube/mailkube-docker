# Postfix Tuning

Load this before editing `rootfs/etc/postfix/main.cf`, `rootfs/etc/postfix/master.cf`, or the
`postconf -e` batch in `rootfs/usr/local/lib/mailkube-relay/render.sh`.

`main.cf` is the source of truth for every value that does not depend on the environment. Every value
in it carries its rationale in a comment **at the value**. That is a rule, not a habit: a number
without a reason gets "optimized" by the next contributor.

Read `.rules/UPSTREAM_CONTRACT.md` first. Most numbers here defend a limit documented there.

## Named traps

These six defaults are the ones that cost real debugging time. Each was confirmed on the real
`alpine:3.24` + `postfix 3.11.6-r0` image.

### 1. `smtp_tls_connection_reuse` defaults to `no`

Every delivery here is TLS, and Postfix does **not** hand a TLS session to `scache(8)` unless this is
explicitly enabled. Leave it unset and all four connection-cache parameters below are silently inert:
no error, no warning, and every message pays a fresh TCP + TLS handshake + AUTH. Enabling it also
requires the `tlsproxy` service in `master.cf`, which stock Alpine Postfix ships **commented out**
(line 16 of the stock file). Both halves are asserted at build time.

### 2. `connection_cache_ttl_limit` (default `2s`) silently caps `smtp_connection_cache_time_limit`

`scache(8)` has its own TTL. Setting `smtp_connection_cache_time_limit = 45s` without also setting
`connection_cache_ttl_limit = 45s` gives you a 2-second cache. This is the single most-missed parameter
in Postfix relay tuning. Both are set in `main.cf` and must be changed together.

### 3. The client-side `smtp_sasl_security_options` default already contains `noplaintext`

The Postfix **SMTP client** default is `noplaintext, noanonymous`, which forbids PLAIN both inside and
outside TLS. This is the opposite of the server-side default, which is what everyone remembers.
Omitting `smtp_sasl_security_options = noanonymous` fails **every** delivery with
`no worthy mechs found`.

The inheritance chain matters when debugging: because `smtp_tls_security_level = secure` verifies the
certificate, the setting actually in force is `smtp_sasl_tls_verified_security_options`, which inherits
from `smtp_sasl_tls_security_options`, which inherits from `smtp_sasl_security_options`. Setting one of
the intermediate names instead is a common half-fix.

### 4. `smtp_connection_cache_destinations` needs a BARE hostname and fails silently on bracket form

`smtp_connection_cache_destinations = smtp.mailkube.com`. No brackets, no port.

`smtp_connect.c` parses the nexthop into a bare domain before matching, and `match_list.c` exempts
`[`-prefixed patterns from parsing. A bracketed value therefore never matches: no error, no warning,
and a clean `postfix check`. The correct value carries no port, so the entrypoint must
**never** template a port into it. It is the one upstream-related value `render.sh` deliberately does
not touch.

### 5. A non-zero rate delay collapses concurrency to 1, and 1s is the floor

`smtp_destination_rate_delay` is not independent of `smtp_destination_concurrency_limit`: any non-zero
value implicitly forces per-destination concurrency to 1. Postfix time values are integral, so 1s is
the smallest usable delay, and therefore **one paced stream can never exceed one message per second.**

That floor is why pacing is built from **lanes** rather than from a single rate value. The delay is
scoped per transport-and-destination, so N paced transports to the same relayhost are N independent
streams. `RELAY_LANES=N` arms `mklane1` through `mklaneN`, each at `1s`, and spreads messages across
them with `randmap`, which is the only distribution primitive Postfix has: every other transport
selection is a deterministic lookup on the sender or the recipient.

Measured 2026-10-08 against the sink: 12 messages over 4 lanes delivered in **3.2s**, 3.75/sec,
against the 11s a single paced stream needs. The single-sender case measured the same 3.2s, so the
sender-keyed `randmap` lookup is **not** cached per sender, which is what makes lanes useful for this
relay's normal one-`From` workload. See the lane tests in `test/test_relay.py`.

A lane carries *up to* one message per second, slightly under: the delay is inserted **between**
deliveries, so a lane cycles in 1s plus the delivery's own time. Document lanes as "up to 1/s each",
never as exactly N/sec.

`apply_config()` still writes an explicit `smtp_destination_rate_delay = 0s`. The plain `smtp`
transport must stay unpaced, because it carries mail only at `RELAY_LANES=0`, where
`RELAY_CONCURRENCY` is the control.

### 5b. A lane is its own transport, so `smtp_`-prefixed destination limits do not reach it

This is the trap that makes lanes dangerous to add carelessly. Per-destination parameters fall back
to their `default_*` form per transport, so a value set on `smtp_*` governs the plain transport only:

| Parameter | Must be set on | Postfix default if it does not reach a lane |
|---|---|---|
| `destination_recipient_limit` | `default_` | 50, which **equals** the relay's own value, so the mistake is invisible until `RECIPIENT_LIMIT` is lowered |
| `destination_concurrency_failed_cohort_limit` | `default_` | **1**, the exact value `main.cf` overrides to 10, where one connect blip defers a whole lane's queue |

Both are set on `default_` in `main.cf` and `render.sh` for that reason, and both have a test.
Daemon-wide `smtp_*` parameters, the TLS policy, the SASL credential and connection reuse among them,
are read by every `smtp(8)` instance and reach the lanes unchanged.

### 5c. The lane map shares a parameter with DSN suppression

`sender_dependent_default_transport_maps` carries both the null-sender `discard:` rule and the lane
`randmap`. Postfix searches maps left to right and takes the first match, and a `randmap` matches
**every** lookup, so the `pcre:` map must come first. Reversing them routes Postfix's own DSNs
upstream, where a `MAILER-DAEMON` From is rejected 550 and starts the bounce loop tier 2 exists to
prevent. `_apply_transport_maps()` composes the value in one place, and a test pins the order.

This parameter is **not** `sender_dependent_relayhost_maps`, which `main.cf` pins empty because it can
redirect mail off the relay host entirely. Do not conflate them.

### 6. `smtp_line_length_limit` (998) is the header-breaking parameter, not `line_length_limit` (2048)

`line_length_limit` (2048) is about input parsing. `smtp_line_length_limit` (998) is what breaks a long
line on output by inserting CRLF+SPACE **at a fixed byte offset with no whitespace search**, which
corrupts a JSON `X-Mailkube-*` header value. It is pinned at its default in `main.cf` with a comment,
so nobody "fixes" the wrong parameter while chasing a mangled header.

Do not set it to 0. That disables the break, but then Postfix emits lines over 1000 octets to
third-party receivers on the final hop, violating RFC 5321 section 4.5.3.1.6. The correct fix for a
mangled header is to keep each physical header line at or under 998 bytes including the field name.

## Deliberately NOT set: `smtp_sasl_auth_cache_name`

It looks like exactly the right parameter for this workload (cache a failed AUTH so a broken credential
does not hammer the upstream). It is not, for two independent reasons, and it is documented as absent
in `main.cf` so nobody adds it as an "obvious win":

1. **It must be `proxy:`-prefixed or `smtp(8)` calls `msg_fatal()` on every start.** `postfix check`
   cannot see this, so the mistake passes preflight and fails at delivery time, in a process that
   restarts and fails again.
2. **Postfix caches on the 535 REPLY CODE**, and the upstream returns 535 for any API transport error,
   not only for a genuinely bad credential. A brief upstream API blip would therefore make this pod
   stop attempting AUTH at all, for the cache lifetime, **while it still reports healthy**. That is a
   silent outage caused by a transient error, which is the worst available failure mode.

If a future change makes this parameter attractive again, the second reason still stands and must be
answered first.

## The AUTH arithmetic

The upstream per-domain budget is **6 AUTH/sec**. Everything below is arranged so a normal workload
performs approximately one AUTH per *burst* **per lane**, not one per message. With the default 4
lanes that is 4 AUTHs per burst per instance, which still clears the 6/sec budget for one instance and
does not for two starting together; `RELAY_START_JITTER` is what spreads them.

```
without reuse:  N messages  ->  N connections  ->  N AUTHs
with reuse:     N messages  ->  1 connection   ->  1 AUTH   (up to the reuse count/time limits)
```

Measured in spike S1, 20 messages spaced 3s apart:

```
reuse ENABLED  (shipped config):   20 messages -> 1 AUTH   (conn_use=2..20)
reuse DISABLED (counter-factual):   8 messages -> 8 AUTHs
```

Against `IP_AUTH_RATE_LIMITED` (20 signals in 15 min for a 60-minute first ban), the disabled case bans
the customer's shared egress IP during any backlog drain.

The four reuse bounds and why each has the value it has:

| Parameter | Value | Reason |
|---|---|---|
| `smtp_connection_cache_time_limit` | `45s` | upstream `client_timeout` is 1m, so we always close first and never burn a delivery attempt on a server-closed socket. Deliberately **not** equal to `queue_run_delay`, or every deferred retry would arrive exactly as the cached connection expired |
| `connection_cache_ttl_limit` | `45s` | must match the line above, see trap 2 |
| `smtp_connection_reuse_time_limit` | `300s` | bounds a reused channel so DNS is re-resolved and TLS sessions rotate |
| `smtp_connection_reuse_count_limit` | `90` | upstream `max_messages_per_connection` is 5,000 and counts RCPT TO, not messages; 90 deliveries × `RECIPIENT_LIMIT` 50 = 4,500 recipients ends the session at a clean boundary instead of a `421 4.5.3` |

Cold start matters as much as steady state. `initial_destination_concurrency` defaults to **5**, which
on a cold start means 5 sockets and 5 AUTHs in the first second. One replica now fits the 6/sec budget,
but replicas that restart together share it per domain, and every 454 is a RiskSignal. It is pinned to `1`. `RELAY_START_JITTER` (default 15s, set to 0 in
tests) decorrelates synchronized multi-replica restarts for the same reason.

## The fleet connection budget

Upstream HAProxy admits **20 concurrent connections per source IP**, and the source IP is the cluster
egress NAT shared by every replica and by anything else sending from behind it. Its rule is
`src_conn_cur ge MAX_CONN_PER_IP` against a counter that already includes the connection being
checked, so the deployed 21 admits 20. The fleet rule below leaves two of those 20 spare, for the
healthcheck of another workload behind the same address and for a replica restarting into an
overlapping window.

> **FLEET RULE: `2 × RELAY_LANES × instances ≤ 18`** with pacing on, which is the default, and
> **`instances × RELAY_CONCURRENCY × 2 ≤ 18`** at `RELAY_LANES=0`. Either way the fleet holds at most
> 18 of the 20 admitted connections, with a floor of two per running instance.

Both forms are the same arithmetic, because a lane's rate delay pins its concurrency to 1: the unit
that costs two connections is a lane when pacing is on and a concurrency slot when it is off. Two
replicas at the default 4 lanes hold 16; a third breaches the budget. `RELAY_LANES` is capped at 10
rather than 9 because 10 is the highest plan rate, and `validate.sh` warns above 9 that a single
instance then holds all 20 and starves everything else behind the same address.

Lanes also multiply AUTH. Each lane authenticates once per burst against the 6/sec per-domain budget,
so the "one AUTH per burst" arithmetic below holds **per lane**, not per instance.

**The factor of two is not headroom, it is measured.** `RELAY_CONCURRENCY` sets
`smtp_destination_concurrency_limit`, which bounds concurrent *deliveries*. It does not bound
concurrent *sockets*. Because `smtp_tls_connection_reuse = yes`, a finished delivery hands its socket
to `scache(8)`, which holds it open for `smtp_connection_cache_time_limit` (45s). While that idle
socket waits to be reused it still occupies a HAProxy slot. Under sustained load a relay therefore
holds `RELAY_CONCURRENCY` active sockets plus up to `RELAY_CONCURRENCY` cached idle ones.

Measured against the test sink at steady state, peak sockets came to exactly `2 × RELAY_CONCURRENCY`
for concurrency 1, 2 and 4 (`peak = 2C`, `connections = 2C + 1`, `auths = C`). T-15 in
`test/test_relay.py` pins this and fails if the factor grows.

In sidecar mode "instances" is the customer's **application pod count**, which this container cannot
see and therefore cannot validate. `validate.sh` warns above `RELAY_CONCURRENCY=2` and states the rule;
the README must state it too. It also caps the accepted range at 9 rather than 10: at 10 a single
instance holds 20 connections and breaches the ceiling alone, so that is a rejection, not a warning.

`smtp_destination_concurrency_limit = 2` (the default for `RELAY_CONCURRENCY`) is chosen from both
ends: 2 rather than more because with reuse the throughput per connection is RTT-bound, not
connection-bound, so extra concurrency buys nothing and only spends HAProxy slots; 2 rather than 1 so a
large DATA transfer cannot head-of-line-block the queue behind it.

`smtp_destination_concurrency_failed_cohort_limit = 10` overrides a Postfix default of 1, under which a
single connect or handshake blip marks the destination dead and defers the **entire** queue. With
exactly one destination that is a total outage triggered by one bad packet.

## Other values worth not breaking

- `in_flow_delay = 0s`. Postfix's 1s default throttles **ingest** when the queue outpaces delivery,
  which makes the application's `send()` block precisely when upstream is throttled. That is the exact
  negation of this product's reason to exist.
- `message_size_limit = 26214400`. The Postfix default of 10240000 (9.77 MiB) is **below** even the
  Free tier's 10485760, so left unset this relay would locally 552 messages the platform accepts,
  quoting a number that appears in no plan. 26214400 matches the upstream submission listener and
  the top (Enterprise) plan entitlement.
- `minimal_backoff_time = 120s` and `queue_run_delay = 120s`. `queue_run_delay` must stay at or below
  `minimal_backoff_time` or the backoff never takes effect. 120s rather than Postfix's 300s because
  upstream 4xx windows are one second wide; 120s rather than the 30s of an earlier revision because a
  fast retry cadence on a synchronized multi-replica cold start can trip `IP_AUTH_RATE_LIMITED` on its
  own. `maximal_backoff_time = 1800s` gives the ramp 120 -> 240 -> 480 -> 960 -> 1800 and bounds
  post-outage latency to 30 minutes.
- `smtp_data_done_timeout = 600s` must stay **above** the upstream `data_processing_timeout` of 5m. Do
  not "optimize" it below 300s.
- `smtp_quit_timeout = 10s`, because a slow QUIT otherwise holds a HAProxy connection slot for the
  5-minute default.
- `smtp_sasl_password_maps = texthash:/run/postfix/sasl_passwd`. `texthash` needs no `postmap` and
  writes no `.lmdb` sidecar, so combined with `/run` being tmpfs the credential never touches a
  disk-backed layer.
- `smtp_sasl_auth_soft_bounce = yes`, so a 535 defers instead of bouncing. A typo'd secret then costs a
  stalled queue you can fix rather than a mailbox of bounces and permanently lost mail.
- `smtpd_client_restrictions = permit_mynetworks, reject`. This is **not** an open relay, and
  `validate_networks()` refuses any `/0` prefix outright. `mynetworks` is only the second line of
  defence; network isolation is the real control.

## master.cf

Two deliberate deviations from stock, both documented in the file header:

1. `tlsproxy` is enabled (stock ships it commented out). Required by `smtp_tls_connection_reuse`.
2. `chroot` stays `n` on every service. Not cosmetic: a chrooted `smtp(8)` cannot read
   `/etc/resolv.conf` (nothing copies it into the chroot), which breaks resolution of
   `smtp.mailkube.com` and therefore the integration-test seam; a chrooted `tlsproxy(8)` cannot read
   `/etc/ssl/certs`, which breaks `smtp_tls_security_level = secure`. It also removes any need for
   `CAP_SYS_CHROOT`.

The `smtp inet` line is **replaced**, never supplemented, when `LISTEN_PORT` is not 25 (see
`_apply_listen_port()`): leaving a port-25 listener in place would fail to bind without
`NET_BIND_SERVICE` and defeat the point of choosing a high port.

`local`, `virtual` and `lmtp` are kept at stock even though they can never be invoked
(`mydestination` is empty and `local_transport` is an `error:` transport). `master` spawns services on
demand, so leaving them defined costs nothing at runtime and keeps the diff from upstream reviewable.

## Changing a value

1. State which upstream limit the new value defends, with the arithmetic.
2. Update the comment at the value in `main.cf` in the same change.
3. Update this file if the reasoning changed.
4. Add or update the test row (`.rules/TESTING.md`). A tuning change with no counter-factual test is
   indistinguishable from a change that does nothing, which is exactly the failure trap 1 describes.
