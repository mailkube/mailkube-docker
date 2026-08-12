"""Harness for the ghcr.io/mailkube/smtp-relay integration tests.

Two seams let the tests exercise a relay whose upstream host is hardcoded, with
no override environment variable and no test-only code path in the image:

1. **Name.** The sink container is given the Docker network alias
   ``smtp.mailkube.com``. The relay resolves it because main.cf keeps
   ``smtp_host_lookup = native, dns``.
2. **Trust.** ``smtp_tls_security_level = secure`` needs a verifiable chain, so
   the harness bind-mounts the throwaway CA into the container's OpenSSL trust
   store as ``/etc/ssl/certs/<subject-hash>.0``. That is ordinary CApath
   behaviour, not a weakening of the relay.

Every relay started here sets ``RELAY_START_JITTER=0``. The image defaults to a
random start delay to decorrelate multi-replica cold starts, which would
otherwise make timings here nondeterministic.
"""

from __future__ import annotations

import json
import shutil
import smtplib
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
IMAGE = "ghcr.io/mailkube/smtp-relay:test"
SINK_IMAGE = "mk-sink:test"
NETWORK = "mkrelay-test-net"
UPSTREAM_ALIAS = "smtp.mailkube.com"

USERNAME = "myapp01@example.com"
PASSWORD = "s3cret64charsupersecretpasswordvalue"
AUTH_DOMAIN = "example.com"


def run(*args: str, check: bool = True, timeout: int = 180) -> subprocess.CompletedProcess[str]:
    return subprocess.run(list(args), capture_output=True, text=True, check=check, timeout=timeout)


def docker(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return run("docker", *args, check=check)


# --------------------------------------------------------------------------- #
#  Session-scoped infrastructure
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="session", autouse=True)
def _require_docker() -> None:
    if shutil.which("docker") is None:
        pytest.skip("docker is not available", allow_module_level=True)


@pytest.fixture(scope="session")
def relay_image() -> str:
    docker("build", "-t", IMAGE, str(REPO))
    return IMAGE


@pytest.fixture(scope="session")
def sink_image() -> str:
    docker("build", "-t", SINK_IMAGE, str(REPO / "test" / "sink"))
    return SINK_IMAGE


@pytest.fixture(scope="session")
def certs(tmp_path_factory: pytest.TempPathFactory, sink_image: str) -> dict[str, str]:
    """Throwaway CA plus a leaf for smtp.mailkube.com."""
    out = tmp_path_factory.mktemp("certs")
    docker(
        "run", "--rm", "--entrypoint", "python",
        "-v", f"{out}:/certs", sink_image, "/opt/gen_ca.py", "/certs",
    )
    return {"dir": str(out), "hash": (out / "ca_hash.txt").read_text().strip()}


@pytest.fixture(scope="session")
def network() -> str:
    docker("network", "create", NETWORK, check=False)
    yield NETWORK
    docker("network", "rm", NETWORK, check=False)


# --------------------------------------------------------------------------- #
#  Containers
# --------------------------------------------------------------------------- #
@dataclass
class Sink:
    """The stand-in for smtp.mailkube.com, plus its recorded observations."""

    name: str
    outdir: Path

    def stats(self) -> dict:
        f = self.outdir / "stats.json"
        if not f.exists():
            return {}
        for _ in range(5):  # tolerate reading mid-rename
            try:
                return json.loads(f.read_text())
            except json.JSONDecodeError:
                time.sleep(0.1)
        return {}

    def messages(self) -> list[dict]:
        f = self.outdir / "messages.jsonl"
        if not f.exists():
            return []
        return [json.loads(line) for line in f.read_text().splitlines() if line.strip()]

    def wait_for_messages(self, count: int, timeout: float = 60) -> list[dict]:
        deadline = time.time() + timeout
        while time.time() < deadline:
            msgs = self.messages()
            if len(msgs) >= count:
                return msgs
            time.sleep(0.5)
        return self.messages()

    def logs(self) -> str:
        return docker("logs", self.name, check=False).stdout + docker("logs", self.name, check=False).stderr


@dataclass
class Relay:
    """A running relay container."""

    name: str
    port: int
    _stopped: bool = field(default=False)

    def logs(self) -> str:
        p = docker("logs", self.name, check=False)
        return p.stdout + p.stderr

    def exec(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        return docker("exec", self.name, *args, check=check)

    def postconf(self, *params: str, expand: bool = False) -> list[str]:
        flag = "-hx" if expand else "-h"
        out = self.exec("postconf", "-c", "/run/postfix", flag, *params).stdout
        return [line.strip() for line in out.splitlines()]

    def postconf_one(self, param: str, expand: bool = False) -> str:
        vals = self.postconf(param, expand=expand)
        return vals[0] if vals else ""

    def send(self, msg, host: str = "127.0.0.1", timeout: int = 20) -> None:
        with smtplib.SMTP(host, self.port, timeout=timeout) as s:
            s.send_message(msg)

    def queue_count(self) -> int:
        out = self.exec("postqueue", "-c", "/run/postfix", "-p", check=False).stdout.strip().splitlines()
        if not out or "empty" in out[-1]:
            return 0
        parts = out[-1].split()
        for i, tok in enumerate(parts):
            if tok.startswith("Request"):
                return int(parts[i - 1])
        return 0

    def wait_healthy(self, timeout: float = 60) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            st = docker(
                "inspect", "-f", "{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}", self.name,
                check=False,
            ).stdout.strip()
            if st == "healthy":
                return True
            if docker("inspect", "-f", "{{.State.Status}}", self.name, check=False).stdout.strip() == "exited":
                return False
            time.sleep(1)
        return False

    def wait_ready_internal(self, timeout: float = 45) -> bool:
        """Wait until smtpd answers from INSIDE the container.

        Needed after `docker restart`: some Docker runtimes (OrbStack, and
        Docker Desktop in some versions) do not re-establish the host-side port
        forward when a container restarts, so the published port stays refused
        even though smtpd is listening and healthy. That is a runtime quirk, not
        a relay fault, so a restart test must probe internally or it will fail
        for the wrong reason.
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.exec("/usr/local/bin/healthcheck.sh", check=False).returncode == 0:
                return True
            time.sleep(1)
        return False

    def wait_ready(self, timeout: float = 45) -> bool:
        """Wait until smtpd answers, which is faster than the healthcheck interval."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                with smtplib.SMTP("127.0.0.1", self.port, timeout=3):
                    return True
            except (OSError, smtplib.SMTPException):
                time.sleep(0.5)
        return False

    def stop(self, timeout: int = 30) -> int:
        """docker stop, returning the container's exit code."""
        docker("stop", "-t", str(timeout), self.name, check=False)
        self._stopped = True
        return int(docker("inspect", "-f", "{{.State.ExitCode}}", self.name, check=False).stdout.strip() or 1)


class Factory:
    """Starts sinks and relays and guarantees cleanup."""

    def __init__(self, relay_image: str, sink_image: str, certs: dict, network: str, tmp: Path):
        self.relay_image, self.sink_image = relay_image, sink_image
        self.certs, self.network, self.tmp = certs, network, tmp
        self._names: list[str] = []
        self._volumes: list[str] = []

    def sink(self, **env: str) -> Sink:
        name = f"mksink-{uuid.uuid4().hex[:8]}"
        outdir = self.tmp / name
        outdir.mkdir(parents=True, exist_ok=True)
        args = [
            "run", "-d", "--name", name,
            "--network", self.network, "--network-alias", UPSTREAM_ALIAS,
            "-v", f"{self.certs['dir']}:/certs:ro", "-v", f"{outdir}:/out",
        ]
        for k, v in env.items():
            args += ["-e", f"{k}={v}"]
        args.append(self.sink_image)
        docker(*args)
        self._names.append(name)
        s = Sink(name=name, outdir=outdir)
        deadline = time.time() + 30
        while time.time() < deadline and not (outdir / "stats.json").exists():
            time.sleep(0.3)
        return s

    def relay(
        self,
        *,
        extra_args: list[str] | None = None,
        wait: bool = True,
        volumes: bool = False,
        **env: str,
    ) -> Relay:
        name = f"mkrelay-{uuid.uuid4().hex[:8]}"
        port = _free_port()
        merged = {
            "SMTP_USERNAME": USERNAME,
            "SMTP_PASSWORD": PASSWORD,
            # Deterministic timings: the image defaults to a random start delay.
            "RELAY_START_JITTER": "0",
            **env,
        }
        args = [
            "run", "-d", "--name", name, "--network", self.network,
            "-p", f"{port}:{merged.get('LISTEN_PORT', '25')}",
            "-v", f"{self.certs['dir']}/ca.pem:/etc/ssl/certs/{self.certs['hash']}.0:ro",
        ]
        if volumes:
            for path in ("/var/spool/postfix", "/var/lib/postfix"):
                vol = f"{name}{path.replace('/', '-')}"
                self._volumes.append(vol)
                args += ["-v", f"{vol}:{path}"]
        for k, v in merged.items():
            if v is not None:
                args += ["-e", f"{k}={v}"]
        args += (extra_args or []) + [self.relay_image]
        docker(*args)
        self._names.append(name)
        r = Relay(name=name, port=port)
        if wait:
            r.wait_ready()
        return r

    def run_to_completion(self, timeout: float = 30, **env: str) -> subprocess.CompletedProcess[str]:
        """Start the entrypoint and report how it terminated.

        Runs DETACHED and polls. A blocking `docker run` cannot be used here:
        the validation suite deliberately includes cases that are expected to
        start SUCCESSFULLY (the legacy-variable escape hatch, and the
        warn-only generic names), and those run forever, hanging the suite.

        A container still running when the timeout expires is reported as
        returncode 0, because for these tests "still up" means "the entrypoint
        accepted the configuration".
        """
        name = f"mkval-{uuid.uuid4().hex[:8]}"
        args = ["run", "-d", "--name", name]
        for k, v in {"RELAY_START_JITTER": "0", **env}.items():
            if v is not None:
                args += ["-e", f"{k}={v}"]
        args.append(self.relay_image)
        docker(*args)
        self._names.append(name)

        rc: int | None = None
        deadline = time.time() + timeout
        while time.time() < deadline:
            status = docker("inspect", "-f", "{{.State.Status}}", name, check=False).stdout.strip()
            if status == "exited":
                rc = int(docker("inspect", "-f", "{{.State.ExitCode}}", name, check=False).stdout.strip() or 1)
                break
            time.sleep(0.3)

        p = docker("logs", name, check=False)
        logs = p.stdout + p.stderr
        if rc is None:
            docker("rm", "-f", name, check=False)
            rc = 0
        return subprocess.CompletedProcess(args=[name], returncode=rc, stdout=logs, stderr="")

    def cleanup(self) -> None:
        for n in self._names:
            docker("rm", "-f", n, check=False)
        for v in self._volumes:
            docker("volume", "rm", "-f", v, check=False)


def _free_port() -> int:
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture
def factory(relay_image, sink_image, certs, network, tmp_path) -> Factory:
    f = Factory(relay_image, sink_image, certs, network, tmp_path)
    yield f
    f.cleanup()


@pytest.fixture
def pair(factory: Factory):
    """A sink plus a relay pointed at it, the common case."""
    sink = factory.sink()
    relay = factory.relay()
    return sink, relay


def message(subject: str = "hello", to: str = "customer@example.net", frm: str = f"noreply@{AUTH_DOMAIN}", **headers):
    import email.message

    m = email.message.EmailMessage()
    m["From"], m["To"], m["Subject"] = frm, to, subject
    for k, v in headers.items():
        m[k.replace("_", "-")] = v
    m.set_content("test body")
    return m
