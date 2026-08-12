"""Recording SMTP sink standing in for smtp.mailkube.com.

Reachable at the hardcoded hostname because the container is given the Docker
network alias ``smtp.mailkube.com``. The relay resolves it through
``smtp_host_lookup = native``, so no override environment variable and no
test-only code path is needed on the relay side.

What it records, and why each one matters:

* ``connections`` / ``auths`` -- the proof for T-14b. A correctly configured
  relay carries many messages over one authenticated connection. If
  ``smtp_tls_connection_reuse`` is unset, or the connection-cache destination is
  written in bracket form, these numbers equal the message count instead of 1.
* ``peak_concurrent`` -- guards the per-source-IP connection ceiling.
* ``auth_times`` -- lets a test assert AUTHs *per second*, not just the total,
  which is what the upstream limit actually constrains.

Scripted failures are driven by environment variables so a test can reconfigure
behaviour by restarting the container:

    SINK_REPLY_DATA   final reply to DATA, e.g. "550 5.7.1 From address ..."
    SINK_FAIL_FIRST   fail this many DATA commands, then start accepting
    SINK_AUTH_FAIL_S  reject AUTH for this many seconds after start, then accept
"""

from __future__ import annotations

import asyncio
import json
import os
import ssl
import time
from pathlib import Path

from aiosmtpd.controller import Controller
from aiosmtpd.smtp import SMTP, AuthResult, Envelope, LoginPassword, Session

OUT = Path(os.environ.get("SINK_OUT", "/out"))
CERT = Path(os.environ.get("SINK_CERT", "/certs/leaf.pem"))
KEY = Path(os.environ.get("SINK_KEY", "/certs/leaf.key"))
PORT = int(os.environ.get("SINK_PORT", "587"))
IMPLICIT_TLS = os.environ.get("SINK_IMPLICIT_TLS", "no") == "yes"

REPLY_DATA = os.environ.get("SINK_REPLY_DATA", "")
FAIL_FIRST = int(os.environ.get("SINK_FAIL_FIRST", "0"))
AUTH_FAIL_S = float(os.environ.get("SINK_AUTH_FAIL_S", "0"))

STARTED = time.time()


class Stats:
    """Counters, flushed to disk on every change so tests can poll a file."""

    def __init__(self) -> None:
        self.connections = 0
        self.auths = 0
        self.auth_times: list[float] = []
        self.messages = 0
        self.data_commands = 0
        self.concurrent = 0
        self.peak_concurrent = 0
        self.auth_failures = 0

    def flush(self) -> None:
        payload = {
            "connections": self.connections,
            "auths": self.auths,
            "auth_times": [round(t - STARTED, 3) for t in self.auth_times],
            "messages": self.messages,
            "data_commands": self.data_commands,
            "peak_concurrent": self.peak_concurrent,
            "auth_failures": self.auth_failures,
        }
        tmp = OUT / "stats.json.tmp"
        tmp.write_text(json.dumps(payload, indent=2))
        tmp.replace(OUT / "stats.json")


STATS = Stats()


class CountingSMTP(SMTP):
    """SMTP protocol that counts connections and tracks concurrency."""

    def connection_made(self, transport):  # noqa: ANN001, D102
        STATS.connections += 1
        STATS.concurrent += 1
        STATS.peak_concurrent = max(STATS.peak_concurrent, STATS.concurrent)
        STATS.flush()
        super().connection_made(transport)

    def connection_lost(self, error):  # noqa: ANN001, D102
        STATS.concurrent = max(0, STATS.concurrent - 1)
        STATS.flush()
        super().connection_lost(error)


def authenticate(server, session, envelope, mechanism, auth_data):  # noqa: ANN001, ARG001
    """Accept AUTH PLAIN whose identity is an email address.

    Mirrors the upstream contract: the identity must contain '@', because the
    real server splits it into local part and domain and 535s otherwise.
    """
    if AUTH_FAIL_S and (time.time() - STARTED) < AUTH_FAIL_S:
        STATS.auth_failures += 1
        STATS.flush()
        return AuthResult(success=False, handled=False)

    if mechanism != "PLAIN" or not isinstance(auth_data, LoginPassword):
        STATS.auth_failures += 1
        STATS.flush()
        return AuthResult(success=False, handled=False)

    username = auth_data.login.decode()
    if "@" not in username:
        STATS.auth_failures += 1
        STATS.flush()
        return AuthResult(success=False, handled=False)

    STATS.auths += 1
    STATS.auth_times.append(time.time())
    STATS.flush()
    return AuthResult(success=True)


class Recorder:
    """Persists every accepted message verbatim for fidelity assertions."""

    async def handle_DATA(self, server, session: Session, envelope: Envelope) -> str:  # noqa: ANN001, N802
        STATS.data_commands += 1

        if FAIL_FIRST and STATS.data_commands <= FAIL_FIRST:
            STATS.flush()
            return "450 4.7.1 Rate limit exceeded, please retry later"

        if REPLY_DATA:
            STATS.flush()
            return REPLY_DATA

        raw = envelope.original_content or envelope.content or b""
        record = {
            "seq": STATS.messages,
            "at": round(time.time() - STARTED, 3),
            "mail_from": envelope.mail_from,
            "rcpt_tos": list(envelope.rcpt_tos),
            # latin-1 round-trips every byte, so header fidelity assertions see
            # exactly what arrived on the wire.
            "raw": raw.decode("latin-1"),
        }
        with (OUT / "messages.jsonl").open("a") as fh:
            fh.write(json.dumps(record) + "\n")

        STATS.messages += 1
        STATS.flush()
        return "250 2.0.0 Ok: queued as SINK%05d" % STATS.messages


class CountingController(Controller):
    """Controller that builds the counting protocol.

    aiosmtpd's Controller has no ``server_class`` parameter; overriding
    ``factory`` is the supported way to substitute the protocol class.
    """

    def factory(self):  # noqa: ANN201, D102
        return CountingSMTP(self.handler, **self.SMTP_kwargs)


def tls_context() -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(str(CERT), str(KEY))
    return ctx


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "messages.jsonl").write_text("")
    STATS.flush()

    ctx = tls_context()
    kwargs = {
        "hostname": "0.0.0.0",
        "port": PORT,
        "authenticator": authenticate,
        # The relay always authenticates inside TLS, and requiring it here means
        # a regression that disables TLS on the relay fails loudly.
        "auth_require_tls": True,
    }
    if IMPLICIT_TLS:
        kwargs["ssl_context"] = ctx
    else:
        kwargs["tls_context"] = ctx
        kwargs["require_starttls"] = True

    controller = CountingController(Recorder(), **kwargs)
    controller.start()
    print(f"sink listening on :{PORT} implicit_tls={IMPLICIT_TLS}", flush=True)
    try:
        asyncio.get_event_loop().run_forever()
    except KeyboardInterrupt:
        controller.stop()


if __name__ == "__main__":
    main()
