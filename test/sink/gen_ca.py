"""Generate a throwaway CA and a leaf certificate for smtp.mailkube.com.

The relay runs with ``smtp_tls_security_level = secure``, which demands a
verifiable chain whose name matches the nexthop. Rather than weakening the relay
for tests (which would mean a test-only code path in a security-critical
setting), the harness installs this CA into the container's OpenSSL trust store
as a single hashed file at ``/etc/ssl/certs/<hash>.0``. That is ordinary
trust-store behaviour, not an override.

The hashed filename must be the subject hash OpenSSL computes, which is why this
script emits ``ca_hash.txt`` alongside the PEM.
"""

from __future__ import annotations

import datetime as dt
import pathlib
import subprocess
import sys

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

HOSTNAME = "smtp.mailkube.com"
# Fixed instants: workflow scripts and reproducible builds should not depend on
# wall-clock drift, and a fixed window keeps regenerated fixtures byte-stable.
NOT_BEFORE = dt.datetime(2020, 1, 1, tzinfo=dt.timezone.utc)
NOT_AFTER = dt.datetime(2040, 1, 1, tzinfo=dt.timezone.utc)


def _key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _write(path: pathlib.Path, data: bytes) -> None:
    path.write_bytes(data)


def main(outdir: str) -> None:
    out = pathlib.Path(outdir)
    out.mkdir(parents=True, exist_ok=True)

    ca_key = _key()
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "mailkube-relay test CA")])
    ca_cert = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(NOT_BEFORE)
        .not_valid_after(NOT_AFTER)
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(ca_key, hashes.SHA256())
    )

    leaf_key = _key()
    leaf_cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, HOSTNAME)]))
        .issuer_name(ca_name)
        .public_key(leaf_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(NOT_BEFORE)
        .not_valid_after(NOT_AFTER)
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(HOSTNAME)]), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .sign(ca_key, hashes.SHA256())
    )

    _write(out / "ca.pem", ca_cert.public_bytes(serialization.Encoding.PEM))
    _write(out / "leaf.pem", leaf_cert.public_bytes(serialization.Encoding.PEM))
    _write(
        out / "leaf.key",
        leaf_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        ),
    )

    # OpenSSL looks up trusted CAs in a CApath by subject hash. Getting this
    # name wrong makes verification fail in a way that looks like a relay bug.
    digest = subprocess.run(
        ["openssl", "x509", "-hash", "-noout", "-in", str(out / "ca.pem")],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    (out / "ca_hash.txt").write_text(digest)
    print(f"wrote CA (hash {digest}) and leaf for {HOSTNAME} to {out}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "certs")
