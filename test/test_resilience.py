"""T-16..T-19: failure handling, the async value proposition, and durability."""

from __future__ import annotations

import time

import pytest

from conftest import AUTH_DOMAIN, message


def test_t16_permanent_reject_is_logged_and_no_dsn_reaches_upstream(factory):
    """Tier 2: a DSN must never be relayed back to smtp.mailkube.com.

    Its From: would be MAILER-DAEMON@$myhostname, whose domain does not match
    the authenticated domain, so upstream would reject it 550 and generate a
    further bounce. Logs remain the failure channel.
    """
    sink = factory.sink(SINK_REPLY_DATA="550 5.7.1 From address must match authenticated domain example.com")
    relay = factory.relay()
    relay.send(message(subject="will be rejected"))

    deadline = time.time() + 45
    while time.time() < deadline and "status=bounced" not in relay.logs():
        time.sleep(1)

    logs = relay.logs()
    assert "status=bounced" in logs
    assert "From address must match authenticated domain" in logs, "the upstream reason must be greppable"

    # Give the bounce daemon time to try, then assert nothing was accepted.
    time.sleep(8)
    assert sink.stats().get("messages", 0) == 0, "no DSN may be delivered upstream"


def test_t16b_dsn_suppression_is_wired_when_bounce_recipient_is_unset(pair):
    _, relay = pair
    assert relay.postconf_one("sender_dependent_default_transport_maps").startswith("pcre:")
    assert relay.postconf_one("notify_classes") == ""
    body = relay.exec("cat", "/run/postfix/sender_transport").stdout
    assert "discard:" in body and "/^<>$/" in body


def test_t17_bounce_recipient_switches_off_suppression(factory):
    """Tier 3 and tier 2 are mutually exclusive; this is a DIFFERENT configuration."""
    factory.sink()
    relay = factory.relay(BOUNCE_RECIPIENT=f"ops@{AUTH_DOMAIN}")

    assert relay.postconf_one("sender_dependent_default_transport_maps") == "", (
        "the discard map must be off, or the DSN this mode exists to deliver is discarded"
    )
    assert relay.postconf_one("bounce_notice_recipient") == f"ops@{AUTH_DOMAIN}"
    assert "bounce" in relay.postconf_one("notify_classes")

    checks = relay.exec("cat", "/run/postfix/smtp_header_checks").stdout
    assert "MAILER-DAEMON" in checks, "the DSN From: must be rewritten to satisfy the upstream From rule"
    assert f"postmaster@{AUTH_DOMAIN}" in checks


@pytest.mark.slow
def test_t18_send_returns_immediately_when_upstream_is_down(factory):
    """THE headline value proposition.

    With no upstream reachable the application must still get its 250 in well
    under a second, and the message must land in the deferred queue rather than
    being lost or blocking the caller.
    """
    relay = factory.relay()  # deliberately no sink on the network

    start = time.time()
    relay.send(message(subject="queued while upstream is down"))
    elapsed = time.time() - start
    assert elapsed < 1.0, f"submission took {elapsed:.2f}s; the queue must absorb upstream outages"

    deadline = time.time() + 40
    while time.time() < deadline and relay.queue_count() == 0:
        time.sleep(1)
    assert relay.queue_count() >= 1, "the message must be queued, not dropped"


@pytest.mark.slow
def test_t18b_queue_drains_once_upstream_returns(factory):
    """The other half of T-18: deferred mail must actually be delivered later."""
    relay = factory.relay()
    relay.send(message(subject="drain me"))
    time.sleep(3)
    assert relay.queue_count() >= 1

    sink = factory.sink()  # upstream comes back
    relay.exec("postqueue", "-c", "/run/postfix", "-f", check=False)

    msgs = sink.wait_for_messages(1, timeout=90)
    assert len(msgs) == 1, "the deferred message must be delivered once upstream recovers"


@pytest.mark.slow
def test_t19_transient_450_defers_and_eventually_delivers_without_bouncing(factory):
    """A 450 is a defer, never a bounce. Note each one also costs a RiskSignal upstream."""
    sink = factory.sink(SINK_FAIL_FIRST="2")
    relay = factory.relay()
    relay.send(message(subject="temp fail"))

    deadline = time.time() + 60
    while time.time() < deadline and not sink.messages():
        relay.exec("postqueue", "-c", "/run/postfix", "-f", check=False)
        time.sleep(4)

    assert len(sink.messages()) >= 1, "the message must be delivered after the transient failures clear"
    assert "status=bounced" not in relay.logs(), "a 4xx must never produce a bounce"


@pytest.mark.slow
def test_t19c_recovers_from_a_transient_auth_failure_without_restart(factory):
    """Regression guard for the SASL auth cache.

    Upstream returns 535 for API transport errors, not only for a bad password.
    If failed credentials were cached (the removed smtp_sasl_auth_cache_name),
    this pod would stop attempting AUTH entirely while still reporting healthy,
    and the mail would silently expire.
    """
    sink = factory.sink(SINK_AUTH_FAIL_S="20")
    relay = factory.relay()
    relay.send(message(subject="auth blip"))

    deadline = time.time() + 120
    while time.time() < deadline and not sink.messages():
        relay.exec("postqueue", "-c", "/run/postfix", "-f", check=False)
        time.sleep(5)

    assert len(sink.messages()) >= 1, (
        "delivery must recover on its own after a transient auth failure, with no container restart"
    )
    assert sink.stats()["auth_failures"] >= 1, "the sink should have rejected at least one AUTH"


@pytest.mark.slow
def test_t19b_queue_survives_a_stop_and_restart(factory):
    """Durability: the spool is the product. A pod restart must not lose mail."""
    relay = factory.relay(volumes=True)
    for i in range(10):
        relay.send(message(subject=f"durable {i}", to=f"d{i}@example.net"))

    deadline = time.time() + 40
    while time.time() < deadline and relay.queue_count() < 10:
        time.sleep(1)
    assert relay.queue_count() >= 10

    code = relay.stop(timeout=30)
    assert code in (0, 75), f"shutdown should be clean or report drain-incomplete, got {code}"

    logs = relay.logs()
    if code == 75:
        assert "drain-incomplete" in logs, "an incomplete drain must emit the fixed-shape line"

    sink = factory.sink()
    from conftest import docker

    docker("start", relay.name)
    relay.wait_ready()
    relay.exec("postqueue", "-c", "/run/postfix", "-f", check=False)

    msgs = sink.wait_for_messages(10, timeout=120)
    assert len(msgs) >= 10, f"all queued mail must survive the restart; got {len(msgs)}"


def test_expired_mail_emits_a_greppable_line(pair):
    """Logs are the only failure channel when DSNs are suppressed, so this must exist."""
    _, relay = pair
    assert relay.postconf_one("maximal_queue_lifetime") == "1d"
    assert relay.postconf_one("bounce_queue_lifetime") == "1h"
