"""T-11/T-12: the effective Postfix configuration.

These pin the values that a well-meaning contributor is most likely to "tidy up"
and thereby break silently. Several of them are defaults that are actively wrong
for this workload, and two fail with no error at all when misconfigured.
"""

from __future__ import annotations

import pytest


def test_t11_relayhost_is_the_hardcoded_upstream_on_587(pair):
    _, relay = pair
    assert relay.postconf_one("relayhost") == "[smtp.mailkube.com]:587"


def test_t11b_port_465_switches_to_implicit_tls_and_keeps_one_relayhost_line(factory):
    """The 465 path must set wrappermode AND must not leave a duplicate relayhost.

    A duplicate would emit "overriding earlier entry" on stderr, which the boot
    preflight treats as fatal. This is the regression that ruled out an
    append-only overlay in favour of a batched `postconf -e`.
    """
    factory.sink(SINK_PORT="465", SINK_IMPLICIT_TLS="yes")
    relay = factory.relay(SMTP_PORT="465")
    assert relay.postconf_one("relayhost") == "[smtp.mailkube.com]:465"
    assert relay.postconf_one("smtp_tls_wrappermode") == "yes"
    count = relay.exec("sh", "-c", "grep -c '^relayhost' /run/postfix/main.cf").stdout.strip()
    assert count == "1", "main.cf must contain exactly one relayhost definition"


def test_t12_connection_reuse_is_actually_enabled(pair):
    """Without this, every message pays a fresh TCP+TLS+AUTH. Postfix defaults it to `no`."""
    _, relay = pair
    assert relay.postconf_one("smtp_tls_connection_reuse") == "yes"


def test_t12_cache_destination_is_a_bare_host(pair):
    """Bracket form is silently ignored: no error, no warning, clean `postfix check`.

    smtp_connect.c parses the nexthop to a bare domain before matching, and
    match_list.c exempts '['-prefixed patterns from parsing.
    """
    _, relay = pair
    value = relay.postconf_one("smtp_connection_cache_destinations")
    assert value == "smtp.mailkube.com"
    assert "[" not in value and ":" not in value


def test_t12_sasl_allows_plaintext_inside_tls(pair):
    """The CLIENT default is `noplaintext, noanonymous`, which forbids AUTH PLAIN entirely."""
    _, relay = pair
    assert relay.postconf_one("smtp_sasl_security_options") == "noanonymous"


def test_t12_verified_variant_resolves_to_noanonymous(pair):
    """The parameter actually in force under security_level=secure.

    `-hx` is required. Plain `-h` returns the literal
    "$smtp_sasl_tls_security_options" and the assertion would pass vacuously.
    """
    _, relay = pair
    assert relay.postconf_one("smtp_sasl_tls_verified_security_options", expand=True) == "noanonymous"


def test_t12_sasl_auth_cache_is_not_set(pair):
    """Deliberately empty.

    It must be `proxy:`-prefixed or smtp(8) msg_fatal()s, and it caches on the
    535 reply code, which upstream returns for transient API errors too. Caching
    those would stop the pod attempting AUTH while it still reports healthy.
    """
    _, relay = pair
    assert relay.postconf_one("smtp_sasl_auth_cache_name") == ""


def test_t12_connection_cache_ttl_is_not_left_at_the_2s_default(pair):
    """scache's own TTL silently caps smtp_connection_cache_time_limit."""
    _, relay = pair
    ttl = relay.postconf_one("connection_cache_ttl_limit")
    cache = relay.postconf_one("smtp_connection_cache_time_limit")
    assert ttl == "45s" and cache == "45s", f"ttl={ttl} cache={cache}"


def test_t12_cache_time_differs_from_queue_run_delay(pair):
    """If they matched, every deferred retry would find the cache just expired and re-AUTH."""
    _, relay = pair
    assert relay.postconf_one("smtp_connection_cache_time_limit") != relay.postconf_one("queue_run_delay")


@pytest.mark.parametrize(
    ("param", "expected"),
    [
        ("smtp_tls_security_level", "secure"),
        ("smtp_sasl_mechanism_filter", "plain"),
        ("in_flow_delay", "0s"),
        ("initial_destination_concurrency", "1"),
        ("smtp_destination_concurrency_limit", "2"),
        ("smtp_destination_concurrency_failed_cohort_limit", "10"),
        ("message_size_limit", "20971520"),
        ("smtp_line_length_limit", "998"),
        ("minimal_backoff_time", "120s"),
        ("queue_run_delay", "120s"),
        ("sender_dependent_relayhost_maps", ""),
        ("mydomain", "example.com"),
    ],
)
def test_t12_pinned_values(pair, param, expected):
    _, relay = pair
    assert relay.postconf_one(param) == expected


def test_t12_tlsproxy_and_scache_services_exist(pair):
    """tlsproxy is commented out in stock master.cf and is required by TLS reuse."""
    _, relay = pair
    for svc in ("tlsproxy/unix", "scache/unix", "discard/unix"):
        assert relay.exec("postconf", "-c", "/run/postfix", "-M", svc).returncode == 0


@pytest.mark.parametrize(
    ("env", "param", "expected"),
    [
        ({"INET_PROTOCOLS": "ipv4"}, "inet_protocols", "ipv4"),
        ({"RELAY_DEBUG": "yes"}, "smtp_tls_loglevel", "2"),
        ({"RELAY_HOSTNAME": "relay.example.com"}, "myhostname", "relay.example.com"),
        ({"SMTPUTF8_ENABLE": "yes"}, "smtputf8_enable", "yes"),
        ({"MAX_QUEUE_LIFETIME": "4h"}, "maximal_queue_lifetime", "4h"),
        ({"RECIPIENT_LIMIT": "10"}, "smtp_destination_recipient_limit", "10"),
    ],
)
def test_env_knobs_reach_postfix(factory, env, param, expected):
    """Behavior coverage for the remaining configuration surface.

    scripts/check-env-matrix.sh fails the build if any environment variable
    lacks a row here, which is how the redefined Coverage pillar is enforced.
    """
    factory.sink()
    relay = factory.relay(**env)
    assert relay.postconf_one(param) == expected


def test_relay_msg_rate_enables_pacing_only_at_one_per_second(factory):
    """Pacing is opt-in, and any non-zero delay collapses concurrency to 1 in Postfix.

    Postfix time values are integral, so 1s is the smallest usable delay. That is
    why a blanket default would throttle higher plan tiers by 6x.
    """
    factory.sink()
    slow = factory.relay(RELAY_MSG_RATE="1")
    assert slow.postconf_one("smtp_destination_rate_delay") == "1s"

    fast = factory.relay(RELAY_MSG_RATE="6")
    assert fast.postconf_one("smtp_destination_rate_delay") == "0s"


def test_shutdown_drain_timeout_is_accepted_and_bounded(factory):
    """Must stay below the advertised grace period, or the drain is cut short by SIGKILL."""
    factory.sink()
    relay = factory.relay(SHUTDOWN_DRAIN_TIMEOUT="10")
    assert "preflight passed" in relay.logs()
    assert relay.wait_ready()


def test_t12b_preflight_rejects_a_misspelled_parameter(factory):
    """Positive control.

    `postfix check` exits 0 even when it warns, so the gate is the output
    predicate. Without this test the gate could silently become a no-op.
    """
    relay = factory.relay(wait=False, POSTFIX_SMOKE="1", extra_args=[
        "--entrypoint", "sh",
    ])
    # Craft the failure directly: copy config, inject a bad parameter, run the
    # same predicate the entrypoint uses.
    out = relay.exec(
        "sh", "-c",
        "mkdir -p /run/postfix && cp -a /etc/postfix/. /run/postfix/ && "
        "postconf -c /run/postfix -e 'config_directory = /run/postfix' && "
        "echo 'smtp_totally_bogus_parameter = 1' >> /run/postfix/main.cf && "
        "postfix -c /run/postfix check 2>&1 | grep -ciE 'unknown parameter|unused parameter' || true",
        check=False,
    ).stdout.strip()
    assert out != "0", "postfix check must surface an unknown parameter for the gate to have value"


def test_t12c_all_overrides_together_produce_a_clean_preflight(factory):
    """The combination that an append-only overlay would have made unbootable."""
    factory.sink(SINK_PORT="465", SINK_IMPLICIT_TLS="yes")
    relay = factory.relay(
        SMTP_PORT="465",
        RELAY_NETWORKS="10.42.0.0/16,192.168.5.0/24",
        RELAY_CONCURRENCY="3",
        MESSAGE_SIZE_LIMIT="15728640",
        RECIPIENT_LIMIT="25",
        MAX_QUEUE_LIFETIME="2h",
    )
    logs = relay.logs()
    assert "overriding earlier entry" not in logs
    assert "preflight passed" in logs
    assert relay.postconf_one("smtp_destination_concurrency_limit") == "3"
    assert relay.postconf_one("message_size_limit") == "15728640"


def test_t12e_timezone_defaults_to_utc(pair):
    """Log timestamps are UTC unless the operator says otherwise.

    Postfix stamps maillog lines from the process's local time, so an unset TZ
    would silently inherit whatever the base image happens to default to.
    """
    _, relay = pair
    assert relay.exec("sh", "-c", "date +%Z").stdout.strip() == "UTC"


def test_t12e_timezone_is_overridable(factory):
    """`TZ` must actually take effect, which also proves tzdata is installed.

    Alpine ships no zoneinfo database by default: without the tzdata package an
    override is accepted, exported, and then silently ignored, leaving UTC.
    """
    factory.sink()
    relay = factory.relay(TZ="Europe/Paris")
    assert relay.exec("sh", "-c", "date +%Z").stdout.strip() in {"CET", "CEST"}
