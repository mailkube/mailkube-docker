"""T-20..T-24: the security posture the shipped manifests claim.

The rule here is that the Kubernetes manifests may only assert what these tests
prove. Anything not exercised below must not appear in a securityContext we ship.
"""

from __future__ import annotations

import smtplib
import time
import uuid

import pytest

from conftest import AUTH_DOMAIN, docker, message

CAPS = ["CHOWN", "DAC_OVERRIDE", "FOWNER", "SETGID", "SETUID", "NET_BIND_SERVICE"]

HARDENED_ARGS = (
    ["--read-only", "--tmpfs", "/run", "--tmpfs", "/tmp", "--cap-drop", "ALL"]
    + [arg for cap in CAPS for arg in ("--cap-add", cap)]
    + ["--security-opt", "no-new-privileges"]
)


@pytest.mark.slow
def test_t20_full_delivery_under_readonly_root_and_dropped_capabilities(factory):
    """The exact posture the StatefulSet ships.

    An earlier revision of this test omitted the volume mounts, which cannot
    work: with a read-only root and no writable spool, `post-install
    create-missing` fails and the boot aborts.
    """
    sink = factory.sink()
    relay = factory.relay(volumes=True, extra_args=HARDENED_ARGS)
    relay.send(message(subject="hardened"))
    assert len(sink.wait_for_messages(1)) == 1
    assert "preflight passed" in relay.logs()


def test_t20b_postqueue_works_under_no_new_privileges(factory):
    """postdrop and postqueue are setgid `postdrop`, and no_new_privs ignores setgid.

    It works anyway because the process runs as root with DAC_OVERRIDE. If this
    ever fails, the graceful-drain design (which calls postqueue) is broken under
    the securityContext we publish.
    """
    factory.sink()
    relay = factory.relay(volumes=True, extra_args=HARDENED_ARGS)
    out = relay.exec("postqueue", "-c", "/run/postfix", "-p")
    assert out.returncode == 0
    assert "empty" in out.stdout.lower() or "Request" in out.stdout


@pytest.mark.slow
def test_t21_high_listen_port_needs_no_net_bind_service(factory):
    """The capability-free deployment. A Service maps 25 -> this port."""
    caps = [c for c in CAPS if c != "NET_BIND_SERVICE"]
    args = (
        ["--read-only", "--tmpfs", "/run", "--tmpfs", "/tmp", "--cap-drop", "ALL"]
        + [a for cap in caps for a in ("--cap-add", cap)]
        + ["--security-opt", "no-new-privileges"]
    )
    sink = factory.sink()
    relay = factory.relay(volumes=True, extra_args=args, LISTEN_PORT="1025")
    relay.send(message(subject="high port"))
    assert len(sink.wait_for_messages(1)) == 1


def test_t21b_port_25_is_not_left_listening_when_listen_port_changes(factory):
    """The port-25 entry is REPLACED, not supplemented.

    A leftover listener would fail to bind without NET_BIND_SERVICE and defeat
    the point of choosing a high port.
    """
    factory.sink()
    relay = factory.relay(LISTEN_PORT="1025")
    master = relay.exec("cat", "/run/postfix/master.cf").stdout
    active = [ln for ln in master.splitlines() if ln.strip() and not ln.strip().startswith("#")]
    assert any(ln.startswith("1025") for ln in active), "the new port must be defined"
    assert not any(ln.split()[0] == "smtp" and "inet" in ln for ln in active), (
        "the port-25 inet listener must be removed, not left alongside"
    )


def test_t22_client_outside_mynetworks_is_rejected(factory, network):
    """Not an open relay.

    The test client must sit OUTSIDE private space. Docker bridge networks
    allocate from private ranges, which are inside the default mynetworks, so a
    naive version of this test exercises an unreachable branch and always passes.
    """
    netname = f"mkpub-{uuid.uuid4().hex[:8]}"
    docker("network", "create", "--subnet", "203.0.113.0/24", netname, check=False)
    try:
        factory.sink()
        relay = factory.relay(RELAY_NETWORKS_MODE="replace", RELAY_NETWORKS="10.99.0.0/16")
        docker("network", "connect", netname, relay.name, check=False)
        time.sleep(1)

        # Submit from a container on the 203.0.113.0/24 network.
        probe = docker(
            "run", "--rm", "--network", netname, "alpine:3.22",
            "sh", "-c",
            f"apk add --no-cache -q netcat-openbsd >/dev/null 2>&1; "
            f"(sleep 0.5; printf 'EHLO t\\r\\n'; sleep 0.5; "
            f"printf 'MAIL FROM:<a@{AUTH_DOMAIN}>\\r\\n'; sleep 0.5; "
            f"printf 'RCPT TO:<b@example.net>\\r\\n'; sleep 0.5; printf 'QUIT\\r\\n') "
            f"| nc -w 6 {relay.name} 25 || true",
            check=False,
        ).stdout
        assert "554" in probe or "Access denied" in probe or not probe.strip(), (
            f"a client outside mynetworks must be refused, got: {probe!r}"
        )
    finally:
        docker("network", "rm", netname, check=False)


@pytest.mark.slow
def test_t23_boots_with_kubernetes_style_0777_volume_modes(factory):
    """Kubernetes hardcodes emptyDir at 0777 and most CSI PV roots are 0777.

    Postfix's check-warn then reports "group or other writable" and the fatal
    preflight rejects it. Docker NAMED volumes hide this entirely, because they
    seed from the image with mode and owner preserved, so without this test CI
    passes green while every shipped manifest CrashLoopBackOffs.
    """
    sink = factory.sink()
    relay = factory.relay(
        extra_args=[
            "--tmpfs", "/var/spool/postfix:mode=1777",
            "--tmpfs", "/var/lib/postfix:mode=1777",
        ]
    )
    logs = relay.logs()
    assert "group or other writable" not in logs, "normalize_mounts must fix 0777 volume roots"
    assert "preflight passed" in logs
    relay.send(message(subject="k8s-style mounts"))
    assert len(sink.wait_for_messages(1)) == 1


def test_t24_enforce_from_domain_rewrites_a_remote_clients_header(factory):
    """The migration aid, and the reason it uses smtp_header_checks.

    The submitting client here is NOT loopback and does not share the relay's
    network namespace, which is the cluster-Service topology. A
    sender_canonical_maps implementation would silently no-op in exactly this
    case, because Postfix gates header rewriting for remote clients behind
    local_header_rewrite_clients. smtp_header_checks runs at delivery time and is
    topology-independent.
    """
    sink = factory.sink()
    relay = factory.relay(ENFORCE_FROM_DOMAIN="yes")

    raw = (
        "From: WordPress <wordpress@localhost>\r\n"
        "To: customer@example.net\r\n"
        "Subject: legacy app\r\n\r\n"
        "body\r\n"
    )
    with smtplib.SMTP("127.0.0.1", relay.port, timeout=20) as s:
        s.sendmail(f"noreply@{AUTH_DOMAIN}", ["customer@example.net"], raw.encode())

    got = sink.wait_for_messages(1)[0]["raw"]
    from_line = next(ln for ln in got.split("\n") if ln.lower().startswith("from:"))
    assert f"@{AUTH_DOMAIN}" in from_line, f"From should be rewritten to the auth domain, got: {from_line!r}"
    assert "wordpress@" in from_line.lower(), "the local part must be preserved"


def test_t24b_compliant_from_is_left_alone(factory):
    """The rewrite must touch only non-compliant domains."""
    sink = factory.sink()
    relay = factory.relay(ENFORCE_FROM_DOMAIN="yes")
    relay.send(message(subject="already compliant", frm=f"billing@{AUTH_DOMAIN}"))
    got = sink.wait_for_messages(1)[0]["raw"]
    from_line = next(ln for ln in got.split("\n") if ln.lower().startswith("from:"))
    assert f"billing@{AUTH_DOMAIN}" in from_line
