"""T-06, T-13, T-14: delivery, header fidelity, and connection reuse."""

from __future__ import annotations

import re
import smtplib
import time
from concurrent.futures import ThreadPoolExecutor

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
def test_t14_burst_reuses_the_connection(factory):
    """One lane, so this measures reuse rather than lane fan-out.

    Reuse is a per-lane property: each lane holds its own authenticated connection, so
    with the default 4 lanes a burst legitimately costs up to 4 AUTHs and this assertion
    would be measuring the lane count instead. test_t14d covers the fan-out budget.
    """
    sink = factory.sink()
    relay = factory.relay(RELAY_LANES="1")
    for i in range(20):
        relay.send(message(subject=f"burst {i}", to=f"c{i}@example.net"))
    sink.wait_for_messages(20)
    stats = sink.stats()
    assert stats["messages"] == 20
    assert stats["auths"] <= 2, f"20 messages should cost at most 2 AUTHs, got {stats['auths']}"


@pytest.mark.slow
def test_t14d_lanes_cost_one_auth_each_and_no_more(factory):
    """The AUTH budget is 6/sec per domain, so a lane must not authenticate per message.

    Each lane carries its own authenticated connection, so N lanes cost N AUTHs for a
    burst. The failure this guards is reuse breaking inside a lane, where the cost would
    become one AUTH per message and 20 messages would blow the budget on their own.
    """
    lanes = 4
    sink = factory.sink()
    relay = factory.relay(RELAY_LANES=str(lanes))
    for i in range(20):
        relay.send(message(subject=f"lane burst {i}", to=f"c{i}@example.net"))
    sink.wait_for_messages(20)

    auths = sink.stats()["auths"]
    assert auths <= lanes + 1, (
        f"20 messages over {lanes} lanes cost {auths} AUTHs. One per lane plus a margin is the budget; "
        f"more means connection reuse is broken inside the lanes and every message is paying a fresh "
        f"AUTH against the 6/sec per-domain limit."
    )


@pytest.mark.slow
def test_t14b_spaced_sends_cost_exactly_one_auth(factory):
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
    sink = factory.sink()
    #  One lane: reuse is per-lane, so the default 4 would spread these 12 messages over
    #  4 authenticated connections and this assertion would count lanes, not reuse.
    relay = factory.relay(RELAY_LANES="1")
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
def test_t14c_counter_factual_reuse_disabled_costs_one_auth_per_message(factory):
    """Proves T-14b is not vacuous.

    If this test ever fails, T-14b is no longer measuring what it claims and the
    reuse assertion above has become meaningless. One lane, to match T-14b.
    """
    sink = factory.sink()
    relay = factory.relay(RELAY_LANES="1")
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


#  Sockets are not deliveries. `smtp_destination_concurrency_limit` bounds how many
#  messages Postfix delivers at once, but with `smtp_tls_connection_reuse = yes` a
#  finished delivery hands its socket to scache(8), which holds it OPEN for
#  `smtp_connection_cache_time_limit` (45s). An idle cached socket still occupies a
#  HAProxy slot, so what the upstream ceiling actually sees is twice the configured
#  concurrency. Measured at exactly 2x for RELAY_CONCURRENCY 1, 2 and 4.
#
#  This factor is the reason the fleet rule divides by two; see the fleet connection
#  budget in .rules/POSTFIX_TUNING.md. If this test starts failing high, the published
#  rule is wrong and customers will be banned for following it.
SOCKETS_PER_CONCURRENCY_SLOT = 2


@pytest.mark.slow
@pytest.mark.parametrize("concurrency", [1, 2, 4])
def test_t15_peak_sockets_stay_within_the_fleet_budget(factory, concurrency):
    """The upstream admits 20 concurrent connections per SOURCE IP.

    Sends in parallel on purpose. Sequential submission drains the queue as fast as it
    fills, so the concurrency limit is never actually reached and the observed peak
    depends on scheduling luck, which is what made the original form of this test flaky.
    """
    sink = factory.sink()
    #  RELAY_LANES=0, because RELAY_CONCURRENCY governs the socket count only when pacing
    #  is off; with lanes on the budget is 2 x lanes and test_t15b measures that instead.
    relay = factory.relay(RELAY_LANES="0", RELAY_CONCURRENCY=str(concurrency))
    count = 30

    with ThreadPoolExecutor(max_workers=count) as pool:
        list(pool.map(lambda i: relay.send(message(subject=f"conc {i}", to=f"k{i}@example.net")), range(count)))
    sink.wait_for_messages(count)

    peak = sink.stats()["peak_concurrent"]
    ceiling = SOCKETS_PER_CONCURRENCY_SLOT * concurrency
    assert peak <= ceiling, (
        f"RELAY_CONCURRENCY={concurrency} peaked at {peak} upstream sockets, above the {ceiling} the "
        f"fleet rule budgets for. Every socket over budget is a HAProxy slot the fleet rule does not "
        f"account for, and the published rule now understates the ban risk."
    )
    #  Counter-factual: if the queue never built, the ceiling above was never approached
    #  and the assertion proved nothing.
    assert peak > concurrency, (
        f"expected reuse to leave idle cached sockets open alongside the {concurrency} active "
        f"delivery slots, but peaked at only {peak}. The parallel blast is no longer creating queue "
        f"pressure, so this test has stopped measuring the ceiling it claims to measure."
    )


#  Lane measurement. Postfix has one pacing knob, smtp_destination_rate_delay, whose value
#  is an integral time, so a single paced stream cannot exceed one message per second. The
#  lane design asks whether N paced TRANSPORTS to the same relayhost are N independent
#  streams, which would put the per-second rate back under the operator's control.
#
#  Postfix exposing both <transport>_destination_rate_delay and
#  <transport>_transport_rate_delay is the documentation-side evidence that the former is
#  scoped per transport-and-destination rather than transport-wide. These two tests are the
#  measurement, because the whole design collapses to 1/s if that scoping is wrong.
#
#  NOTE: sender_dependent_default_transport_maps is NOT the parameter main.cf pins empty.
#  That one is sender_dependent_relayhost_maps, which can redirect mail off the relay host
#  and stays pinned. This one only picks a local transport.
#  Measured 2026-10-08 against the sink: 12 messages over 4 paced lanes delivered in 3.2s,
#  which is 3.75 messages a second, against the 11s a single paced stream needs. The
#  single-sender case measured the same 3.2s, so the sender-keyed randmap lookup is not
#  cached per sender. A lane therefore carries up to, and slightly under, one message a
#  second: the delay is inserted BETWEEN deliveries, so a lane cycles in 1s plus the
#  delivery's own time. Document lanes as "up to 1/s each", never as exactly N/s.
LANES = 4
LANE_BATCH = 12
#  A single paced stream needs LANE_BATCH-1 seconds of gaps, so 11s here. LANES streams need
#  about (LANE_BATCH / LANES) - 1, so 2s. Anything under this bound rules out one stream.
LANES_CEILING_S = 6


def _configure_lanes(relay, lanes: int, *, pace: bool = True) -> None:
    """Add `lanes` paced smtp transports and spread messages across them at random.

    Prototypes the design at runtime rather than in the image, so the measurement runs
    against the shipped relay. randmap returns a random value per lookup, which is the
    only distribution primitive Postfix has: transport selection is otherwise a
    deterministic lookup on the sender or the recipient.
    """
    names = [f"mklane{i}" for i in range(1, lanes + 1)]
    for name in names:
        relay.exec("postconf", "-c", "/run/postfix", "-M", f"{name}/unix={name} unix - - n - - smtp")
        if pace:
            relay.exec("postconf", "-c", "/run/postfix", "-e", f"{name}_destination_rate_delay=1s")
    chosen = ",".join(f"{name}:" for name in names)
    relay.exec("postconf", "-c", "/run/postfix", "-e",
               f"sender_dependent_default_transport_maps=randmap:{{{chosen}}}")
    relay.exec("postfix", "-c", "/run/postfix", "reload")


def _delivery_spread(sink, count: int) -> float:
    """Seconds between the first and last delivery the sink saw."""
    msgs = sink.wait_for_messages(count)
    stamps = [m["at"] for m in msgs]
    return max(stamps) - min(stamps)


@pytest.mark.slow
def test_lane_rate_delay_is_scoped_per_transport(factory):
    """N paced transports deliver N messages a second, not one between them.

    The claim the lane design rests on. Senders vary here so that the distribution
    question is isolated into the next test: this one asks only whether the paced
    transports run independently.
    """
    sink = factory.sink()
    relay = factory.relay(RELAY_START_JITTER="0")
    _configure_lanes(relay, LANES)

    for i in range(LANE_BATCH):
        relay.send(message(subject=f"lane {i}", frm=f"s{i}@{AUTH_DOMAIN}", to=f"r{i}@example.net"))

    spread = _delivery_spread(sink, LANE_BATCH)
    assert spread < LANES_CEILING_S, (
        f"{LANE_BATCH} messages over {LANES} paced transports took {spread:.1f}s. A single paced "
        f"stream would take about {LANE_BATCH - 1}s, so the rate delay is being applied across the "
        f"transports rather than per transport, and the lane design cannot raise the rate above 1/s."
    )
    #  Counter-factual: one connection means only one transport ever delivered, so the
    #  spread above could have been luck rather than parallel lanes.
    peak = sink.stats()["peak_concurrent"]
    assert peak > 1, (
        f"peaked at {peak} upstream connection(s), so only one transport ever delivered. The lanes "
        f"are not running in parallel whatever the timing says."
    )


@pytest.mark.slow
def test_lane_assignment_spreads_messages_from_one_sender(factory):
    """Every message from one envelope sender must still spread across the lanes.

    The relay's normal workload is a single From address, so this is the case that
    decides whether lanes are useful. sender_dependent_default_transport_maps is keyed
    on the sender, and trivial-rewrite caches address resolution; if that cache pins the
    sender to one lane, the aggregate collapses back to one message per second.
    """
    sink = factory.sink()
    relay = factory.relay(RELAY_START_JITTER="0")
    _configure_lanes(relay, LANES)

    for i in range(LANE_BATCH):
        relay.send(message(subject=f"one sender {i}", to=f"r{i}@example.net"))

    spread = _delivery_spread(sink, LANE_BATCH)
    assert spread < LANES_CEILING_S, (
        f"{LANE_BATCH} messages from one sender took {spread:.1f}s over {LANES} lanes. The sender-keyed "
        f"lookup is resolving to a single lane, so lanes do nothing for the normal single-sender "
        f"workload. Key the map on the recipient instead, and measure what that does to a "
        f"multi-recipient message."
    )


@pytest.mark.slow
@pytest.mark.parametrize("lanes", [1, 2, 4])
def test_t15b_lane_sockets_stay_within_the_fleet_budget(factory, lanes):
    """With pacing on the budget is 2 x RELAY_LANES, and the fleet rule divides by it.

    The same factor of two as T-15, for the same reason: a lane's finished delivery hands
    its socket to scache(8), which holds it open for 45s waiting to be reused, and an idle
    socket still occupies an upstream slot. If this starts failing high, the published
    fleet rule understates the ban risk for every customer following it.
    """
    sink = factory.sink()
    relay = factory.relay(RELAY_LANES=str(lanes))
    count = 30

    with ThreadPoolExecutor(max_workers=count) as pool:
        list(pool.map(lambda i: relay.send(message(subject=f"lanes {i}", to=f"l{i}@example.net")), range(count)))
    sink.wait_for_messages(count)

    peak = sink.stats()["peak_concurrent"]
    ceiling = SOCKETS_PER_CONCURRENCY_SLOT * lanes
    assert peak <= ceiling, (
        f"RELAY_LANES={lanes} peaked at {peak} upstream sockets, above the {ceiling} the fleet rule "
        f"budgets for. Every socket over budget is an edge slot the rule does not account for."
    )


def test_auth_identity_is_user_at_domain(pair):
    """Upstream splits the AUTH identity on '@' and 535s if it cannot."""
    sink, relay = pair
    relay.send(message())
    sink.wait_for_messages(1)
    assert sink.stats()["auth_failures"] == 0
    assert "@" in USERNAME
