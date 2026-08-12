"""T-06, T-13, T-14: delivery, header fidelity, and connection reuse."""

from __future__ import annotations

import re
import smtplib
import time

import pytest

from conftest import AUTH_DOMAIN, PASSWORD, USERNAME, message

MAILKUBE_HEADERS = {
    "X-Mailkube-Template-Id": "6f1a2b3c-4d5e-6f70-8192-a3b4c5d6e7f8",
    "X-Mailkube-Template-Version": "latest",
    "X-Mailkube-Topic": "newsletter",
    "X-Mailkube-Tags": '[{"name":"campaign","value":"spring"},{"name":"tier","value":"pro"}]',
}


def _unfold(raw: str) -> str:
    """RFC 5322 unfolding: remove the CRLF of a folded line, keep the WSP."""
    return re.sub(r"\r?\n(?=[ \t])", "", raw)


def _header(raw: str, name: str) -> str:
    for line in _unfold(raw).split("\n"):
        if line.lower().startswith(name.lower() + ":"):
            return line.split(":", 1)[1].strip()
    return ""


def test_happy_path_delivers(pair):
    sink, relay = pair
    relay.send(message(subject="basic"))
    msgs = sink.wait_for_messages(1)
    assert len(msgs) == 1
    assert msgs[0]["mail_from"] == f"noreply@{AUTH_DOMAIN}"
    assert "status=sent" in relay.logs()


def test_tls_is_verified_not_merely_encrypted(pair):
    """`Verified` proves security_level=secure matched the cert to the nexthop.

    `Trusted` or `Untrusted` would mean verification was not enforced, which is
    the STARTTLS-stripping exposure the old `smtp_use_tls = yes` allowed.
    """
    sink, relay = pair
    relay.send(message())
    sink.wait_for_messages(1)
    assert "Verified TLS connection established to smtp.mailkube.com" in relay.logs()


def test_t06_password_file_with_trailing_newline_authenticates(factory, tmp_path):
    """`kubectl create secret --from-file` with `echo` embeds a newline.

    This is the most common silent 535 in the ecosystem, so it must be a
    delivery test, not a parsing test.
    """
    pw = tmp_path / "pw.txt"
    pw.write_text(PASSWORD + "\n")
    sink = factory.sink()
    relay = factory.relay(
        SMTP_PASSWORD=None,
        SMTP_PASSWORD_FILE="/run/secrets/pw",
        extra_args=["-v", f"{pw}:/run/secrets/pw:ro"],
    )
    relay.send(message(subject="trailing newline"))
    assert len(sink.wait_for_messages(1)) == 1
    assert sink.stats()["auths"] >= 1


def test_t13_mailkube_headers_pass_through_byte_exact(pair):
    """The relay must not rewrite, reorder or strip the X-Mailkube-* family."""
    sink, relay = pair
    relay.send(message(subject="headers", **{k.replace("-", "_"): v for k, v in MAILKUBE_HEADERS.items()}))
    msgs = sink.wait_for_messages(1)
    raw = msgs[0]["raw"]
    for name, value in MAILKUBE_HEADERS.items():
        assert _header(raw, name) == value, f"{name} was altered in transit"


def test_t13b_long_unbroken_token_is_broken_at_998_bytes(pair):
    """Pins the documented limitation so it can never silently regress.

    The breaking parameter is smtp_line_length_limit (998), NOT
    line_length_limit (2048, which is internal queue chopping and is
    reconstructed on delivery). smtp_text_out() breaks at a fixed byte offset
    with no whitespace search, so a long token is split wherever 998 lands.

    A 900-char token survives; a 1200-char one does not. The usual consequence
    is SILENT corruption, because a space injected inside a JSON string value is
    still valid JSON.
    """
    sink, relay = pair
    short_tok = "a" * 900
    long_tok = "b" * 1200
    raw = (
        f"From: noreply@{AUTH_DOMAIN}\r\n"
        "To: customer@example.net\r\n"
        "Subject: line length\r\n"
        f"X-Mailkube-Short: {short_tok}\r\n"
        f"X-Mailkube-Long: {long_tok}\r\n"
        "\r\n"
        "body\r\n"
    )
    with smtplib.SMTP("127.0.0.1", relay.port, timeout=20) as s:
        s.sendmail(f"noreply@{AUTH_DOMAIN}", ["customer@example.net"], raw.encode())

    got = sink.wait_for_messages(1)[0]["raw"]
    assert _header(got, "X-Mailkube-Short") == short_tok, "a 900-char token must survive intact"

    received_long = _header(got, "X-Mailkube-Long")
    assert received_long != long_tok, (
        "a 1200-char unbroken token is expected to be corrupted by smtp_line_length_limit=998. "
        "If this now passes, the documented limit changed and README/POSTFIX_TUNING.md must be updated."
    )
    assert " " in received_long, "corruption should appear as an injected space after unfolding"
    assert received_long.replace(" ", "") == long_tok, "only whitespace should have been injected"


@pytest.mark.slow
def test_t14_burst_reuses_the_connection(pair):
    sink, relay = pair
    for i in range(20):
        relay.send(message(subject=f"burst {i}", to=f"c{i}@example.net"))
    sink.wait_for_messages(20)
    stats = sink.stats()
    assert stats["messages"] == 20
    assert stats["auths"] <= 2, f"20 messages should cost at most 2 AUTHs, got {stats['auths']}"


@pytest.mark.slow
def test_t14b_spaced_sends_cost_exactly_one_auth(pair):
    """THE load-bearing test.

    The spacing is the entire point. A rapid burst is satisfied by Postfix's
    on-demand connection caching regardless of configuration, so a burst test
    passes even when smtp_tls_connection_reuse is unset or the cache destination
    is written in bracket form. 3s is outside qmgr's 1s back-to-back window and
    inside the 45s cache time limit.

    Measured: reuse enabled -> 1 AUTH for 20 messages. Reuse disabled ->
    1 AUTH per message, which bans a Free-tier customer's egress IP during any
    backlog drain, because every 454 emits a RiskSignal.
    """
    sink, relay = pair
    count = 12
    for i in range(count):
        relay.send(message(subject=f"spaced {i}", to=f"s{i}@example.net"))
        time.sleep(3)
    sink.wait_for_messages(count)

    stats = sink.stats()
    assert stats["messages"] == count
    assert stats["auths"] == 1, (
        f"{count} messages spaced 3s apart must ride ONE authenticated connection, "
        f"got {stats['auths']} AUTHs. Check smtp_tls_connection_reuse, the tlsproxy "
        f"service, and that smtp_connection_cache_destinations is a bare hostname."
    )
    assert re.search(r"conn_use=(\d+)", relay.logs()), "expected conn_use markers proving reuse"


@pytest.mark.slow
def test_t14c_counter_factual_reuse_disabled_costs_one_auth_per_message(pair):
    """Proves T-14b is not vacuous.

    If this test ever fails, T-14b is no longer measuring what it claims and the
    reuse assertion above has become meaningless.
    """
    sink, relay = pair
    relay.exec("postconf", "-c", "/run/postfix", "-e", "smtp_tls_connection_reuse = no")
    relay.exec("postfix", "-c", "/run/postfix", "reload", check=False)
    time.sleep(2)

    count = 5
    for i in range(count):
        relay.send(message(subject=f"cf {i}", to=f"x{i}@example.net"))
        time.sleep(3)
    sink.wait_for_messages(count)

    stats = sink.stats()
    assert stats["auths"] >= count, (
        f"with reuse disabled each message should re-authenticate; got {stats['auths']} "
        f"AUTHs for {count} messages. If this drops, T-14b no longer detects the defect."
    )


def test_t15_peak_concurrency_respects_the_limit(pair):
    """The upstream rejects at 20 concurrent connections per SOURCE IP."""
    sink, relay = pair
    for i in range(15):
        relay.send(message(subject=f"conc {i}", to=f"k{i}@example.net"))
    sink.wait_for_messages(15)
    assert sink.stats()["peak_concurrent"] <= 2


def test_auth_identity_is_user_at_domain(pair):
    """Upstream splits the AUTH identity on '@' and 535s if it cannot."""
    sink, relay = pair
    relay.send(message())
    sink.wait_for_messages(1)
    assert sink.stats()["auth_failures"] == 0
    assert "@" in USERNAME
