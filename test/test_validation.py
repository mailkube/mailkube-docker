"""T-01..T-10: fail-fast validation. No network, no sink.

Each case must fail for ITS OWN reason. An early revision of validate.sh had a
bug where `assert_clean` matched every value, so three of these passed for the
wrong reason while the rules they were meant to cover were untested. Asserting
on the message text, not just the exit code, is what caught it.
"""

from __future__ import annotations

import pytest


def _fails(factory, expect: str, **env) -> str:
    p = factory.run_to_completion(**env)
    combined = p.stdout + p.stderr
    assert p.returncode != 0, f"expected a non-zero exit, got 0.\n{combined}"
    assert expect.lower() in combined.lower(), f"expected {expect!r} in output:\n{combined}"
    return combined


def test_t01_username_without_at_is_rejected(factory):
    """A bare username does not authenticate upstream; the error must say so."""
    out = _fails(factory, "must be <username>@<verified-domain>", SMTP_USERNAME="myapp01", SMTP_PASSWORD="x")
    assert "@" in out and "example.com" in out, "the hint must show the expected form"


def test_t02_upstream_port_25_is_rejected_with_the_ban_warning(factory):
    """Port 25 upstream is bounce intake; relaying there gets the egress IP banned."""
    out = _fails(factory, "not a submission port", SMTP_USERNAME="a@b.com", SMTP_PASSWORD="x", SMTP_PORT="25")
    assert "ban" in out.lower(), "the operator must be told this causes a ban, not just that it is wrong"


def test_t03_arbitrary_upstream_port_is_rejected(factory):
    _fails(factory, "must be 587 or 465", SMTP_USERNAME="a@b.com", SMTP_PASSWORD="x", SMTP_PORT="2525")


def test_t04_open_relay_cidr_is_refused(factory):
    _fails(
        factory, "matches every address",
        SMTP_USERNAME="a@b.com", SMTP_PASSWORD="x", RELAY_NETWORKS="0.0.0.0/0",
    )


def test_t04b_newline_injection_into_config_is_refused(factory):
    """A newline in an env value would append an arbitrary Postfix parameter."""
    _fails(
        factory, "newline, carriage return",
        SMTP_USERNAME="a@b.com", SMTP_PASSWORD="x",
        RELAY_NETWORKS="10.0.0.0/8\nrelayhost = [evil.example]:25",
    )


def test_t05_missing_secret_file_fails_rather_than_skipping(factory):
    """The lineage script printed 'skipping' here, turning a broken mount into a later 535."""
    out = _fails(factory, "does not exist", SMTP_USERNAME="a@b.com", SMTP_PASSWORD_FILE="/nope")
    assert "skip" not in out.lower()


def test_t07_bounce_recipient_must_be_at_the_authenticated_domain(factory):
    """Any other domain produces a guaranteed 550 loop upstream."""
    _fails(
        factory, "must be at the authenticated domain",
        SMTP_USERNAME="a@b.com", SMTP_PASSWORD="x", BOUNCE_RECIPIENT="ops@other.com",
    )


def test_t08_concurrency_above_the_ceiling_is_refused(factory):
    _fails(factory, "between 1 and 9", SMTP_USERNAME="a@b.com", SMTP_PASSWORD="x", RELAY_CONCURRENCY="25")


def test_t08b_concurrency_of_ten_breaches_the_ceiling_alone(factory):
    """10 holds 20 connections, the whole per-source-IP ceiling, with no fleet involved.

    The boundary is the point of the rule, so it is pinned: 9 boots, 10 does not.
    """
    _fails(factory, "between 1 and 9", SMTP_USERNAME="a@b.com", SMTP_PASSWORD="x", RELAY_CONCURRENCY="10")


@pytest.mark.parametrize(
    ("var", "value"),
    [
        ("SMTP_NETWORKS", "10.0.0.0/8"),
        ("SERVER_HOSTNAME", "mail.example.com"),
        ("OVERWRITE_FROM", "noreply@example.com"),
        ("LOG_SUBJECT", "yes"),
        ("SMTP_HEADER_TAG", "relay"),
    ],
)
def test_t09_removed_variables_fail_loudly_and_name_the_replacement(factory, var, value):
    """Silently ignoring a renamed variable loses the operator's ingress allowlist at cutover."""
    out = _fails(factory, "no longer supported", SMTP_USERNAME="a@b.com", SMTP_PASSWORD="x", **{var: value})
    assert var in out


def test_t09b_legacy_check_has_an_escape_hatch(factory, tmp_path):
    """RELAY_IGNORE_LEGACY_ENV must let a migrating operator start anyway."""
    p = factory.run_to_completion(
        SMTP_USERNAME="a@b.com", SMTP_PASSWORD="x",
        SMTP_NETWORKS="10.0.0.0/8", RELAY_IGNORE_LEGACY_ENV="yes",
    )
    combined = p.stdout + p.stderr
    assert "no longer supported" not in combined


def test_t09c_generic_names_only_warn(factory):
    """SMTP_SERVER and DEBUG can legitimately belong to a co-located app sharing an env_file."""
    p = factory.run_to_completion(SMTP_USERNAME="a@b.com", SMTP_PASSWORD="x", SMTP_SERVER="smtp.example.org")
    combined = p.stdout + p.stderr
    assert "will be ignored" in combined
    assert "no longer supported" not in combined


def test_t10_missing_username_is_rejected(factory):
    _fails(factory, "SMTP_USERNAME is not set", SMTP_PASSWORD="x")


def test_api_key_shaped_password_warns(factory):
    """The marketing site tells people to use an API key here. It does not authenticate."""
    p = factory.run_to_completion(SMTP_USERNAME="a@b.com", SMTP_PASSWORD="mk_live_abc123")
    combined = p.stdout + p.stderr
    assert "looks like an api key" in combined.lower()
