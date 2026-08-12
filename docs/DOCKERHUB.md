# mailkube/smtp-relay

A companion SMTP relay for [Mailkube](https://mailkube.com). It gives the applications in your
cluster a plain, local, **unauthenticated** SMTP endpoint on port 25, and forwards everything to
`smtp.mailkube.com` over authenticated TLS.

Your application sends to `relay:25` with no credentials and no TLS. The relay queues the message
durably, returns `250` in milliseconds, and delivers in the background with retries.

Source, full documentation and deployment manifests:
[github.com/mailkube/mailkube-docker](https://github.com/mailkube/mailkube-docker)

## Quick start

```bash
docker run -d --name mailkube-relay \
  -e SMTP_USERNAME='myapp01@example.com' \
  -e SMTP_PASSWORD='<your-smtp-credential-secret>' \
  -v mailkube-spool:/var/spool/postfix \
  mailkube/smtp-relay:1
```

```
  your app  ──plain SMTP :25──▶  relay  ──TLS + AUTH──▶  smtp.mailkube.com  ──▶  recipient
                                   │
                                   └── durable queue: 250 immediately, retries on failure
```

## Why

* **Sending stops blocking your request path.** The local queue absorbs throttling and outages, so
  a transient upstream failure is a retry instead of an application error.
* **One credential, one place.** A single secret in the relay, not in every service.
* **Legacy applications work unmodified.** Anything that talks SMTP to port 25 becomes a Mailkube
  sender with no code change.
* **Respects the platform's limits by default.** One authenticated connection carries up to 90
  messages, so a queue drain does not trip Mailkube's authentication rate limit.

## Before you start

1. A verified domain in Mailkube.
2. An SMTP credential, created at
   [app.mailkube.com/domain/credentials#smtp](https://app.mailkube.com/domain/credentials#smtp).

Two details cause most first-run failures:

* **`SMTP_USERNAME` must be `<username>@<verified-domain>`**, not the bare username. Mailkube splits
  on the `@` to find your domain.
* **`SMTP_PASSWORD` is the SMTP credential's secret, not your API key.** API keys are for the REST
  API only.

Your message `From` address must also use the verified domain.

## Configuration

The upstream host is hardcoded and cannot be changed by configuration. Only the port is selectable.

| Variable | Default | Notes |
| --- | --- | --- |
| `SMTP_USERNAME` | *required* | `<username>@<verified-domain>` |
| `SMTP_PASSWORD` | *required* | SMTP credential secret, not an API key |
| `SMTP_PASSWORD_FILE` | | Read the secret from a mounted file instead |
| `SMTP_PORT` | `587` | `587` (STARTTLS) or `465` (implicit TLS) |
| `LISTEN_PORT` | `25` | Use a high port to drop `NET_BIND_SERVICE` |
| `RELAY_NETWORKS` | | Extra CIDRs allowed to send, comma separated |
| `RELAY_NETWORKS_MODE` | `append` | `append` or `replace`; loopback is always kept |
| `MESSAGE_SIZE_LIMIT` | `20971520` | 20 MiB |
| `RECIPIENT_LIMIT` | `50` | Recipients per delivery batch |
| `MAX_QUEUE_LIFETIME` | `1d` | Use `1h` for OTP traffic |
| `RELAY_CONCURRENCY` | `2` | Keep `instances × concurrency` at 18 or below |
| `RELAY_MSG_RATE` | | Your plan's messages per second |
| `BOUNCE_RECIPIENT` | | Deliver delivery-status notifications here |
| `ENFORCE_FROM_DOMAIN` | `no` | Rewrite non-compliant `From` domains |
| `SHUTDOWN_DRAIN_TIMEOUT` | `20` | Seconds to drain the queue on shutdown |
| `RELAY_START_JITTER` | `15` | Random start delay across replicas |
| `RELAY_DEBUG` | `no` | Verbose logging; never echoes the credential |
| `RELAY_AUTH_USERS` | | Comma-separated `user:password` pairs for inbound SMTP AUTH |
| `RELAY_AUTH_USERS_FILE` | | One `user:password` per line, from a mounted file |
| `RELAY_AUTH_REQUIRED` | `no` | `yes` requires every client to authenticate |
| `RELAY_TLS_CERT_FILE` | | Listener certificate; enables mandatory STARTTLS |
| `RELAY_TLS_KEY_FILE` | | Its private key |

Full reference, including the removed `boky/postfix` variables and the tuning rationale:
[github.com/mailkube/mailkube-docker#configuration](https://github.com/mailkube/mailkube-docker#configuration)

## Volumes

| Path | Purpose |
| --- | --- |
| `/var/spool/postfix` | The mail queue. Persist this, or queued mail is lost on restart. |
| `/var/lib/postfix` | Per-instance runtime state. Never share it between instances. |

## Security

The listener is **unauthenticated by default**. Anything that can reach the port can send mail as
your verified domain, so network isolation is the real control. Never publish this port to the
internet. The relay refuses to start with an all-addresses ingress rule, and connects upstream with
mandatory, certificate-verified TLS. A NetworkPolicy example is included in the repository.

If you would rather not rely on the network alone, set `RELAY_AUTH_USERS_FILE` and
`RELAY_AUTH_REQUIRED=yes` to require SMTP AUTH from every client, and mount a certificate with
`RELAY_TLS_CERT_FILE` so credentials are only ever sent over TLS.

## Kubernetes, Compose and OpenShift

Ready-made manifests, including a hardened read-only deployment and a NetworkPolicy, are in the
repository:
[github.com/mailkube/mailkube-docker/tree/main/examples](https://github.com/mailkube/mailkube-docker/tree/main/examples)

## Tags

`1.2.3` is immutable. `1.2` takes patches, `1` takes minors and patches, `latest` moves on every
release, and `edge` is built from every merge to `main`. Pin an exact version in production.

Images are multi-arch (`linux/amd64`, `linux/arm64`), ship SBOM and provenance attestations, and are
signed with cosign.

## License

[Apache-2.0](https://github.com/mailkube/mailkube-docker/blob/main/LICENSE) © 2026 Mailtactic, Corp.
