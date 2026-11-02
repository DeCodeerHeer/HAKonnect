"""Local identity (key + self-signed cert) and peer-certificate helpers.

Mirrors kdeconnect-kde sslhelper.cpp: EC P-256 key, CN=<deviceId>, O=KDE,
OU=KDE Connect, valid from -1y to +10y, no extensions (a SAN would override CN
in the peer's setPeerVerifyName check).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import secrets
import ssl
import uuid
from dataclasses import dataclass

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID


@dataclass(slots=True, frozen=True)
class LocalIdentity:
    device_id: str
    key_pem: str
    cert_pem: str

    def __repr__(self) -> str:  # never leak key material via logs/tracebacks
        return f"LocalIdentity(device_id={self.device_id!r})"

    def as_dict(self) -> dict[str, str]:
        return {"device_id": self.device_id, "key_pem": self.key_pem, "cert_pem": self.cert_pem}

    @classmethod
    def from_dict(cls, data: dict[str, str]) -> LocalIdentity:
        return cls(data["device_id"], data["key_pem"], data["cert_pem"])

    @property
    def cert_der(self) -> bytes:
        return ssl.PEM_cert_to_DER_cert(self.cert_pem)


def generate_identity() -> LocalIdentity:
    device_id = uuid.uuid4().hex  # 32 chars, matches DEVICE_ID_PATTERN
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name(
        [
            x509.NameAttribute(NameOID.COMMON_NAME, device_id),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "KDE"),
            x509.NameAttribute(NameOID.ORGANIZATIONAL_UNIT_NAME, "KDE Connect"),
        ]
    )
    now = dt.datetime.now(dt.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(int.from_bytes(secrets.token_bytes(19), "big") or 1)
        .not_valid_before(now - dt.timedelta(days=365))
        .not_valid_after(now + dt.timedelta(days=3650))
        .sign(key, hashes.SHA256())
    )
    return LocalIdentity(
        device_id=device_id,
        key_pem=key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ).decode(),
        cert_pem=cert.public_bytes(serialization.Encoding.PEM).decode(),
    )


def der_to_pem(der: bytes) -> str:
    return ssl.DER_cert_to_PEM_cert(der)


def fingerprint(der: bytes) -> str:
    return hashlib.sha256(der).hexdigest()


def cert_common_name(der: bytes) -> str | None:
    cert = x509.load_der_x509_certificate(der)
    attrs = cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)
    return str(attrs[0].value) if attrs else None


def _spki_der(cert_der: bytes) -> bytes:
    return (
        x509.load_der_x509_certificate(cert_der)
        .public_key()
        .public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    )


def verification_key(local_cert_der: bytes, peer_cert_der: bytes, timestamp: int) -> str:
    """PairingHandler::verificationKey: the 8-char code shown on the desktop."""
    a, b = _spki_der(local_cert_der), _spki_der(peer_cert_der)
    if a < b:
        a, b = b, a
    digest = hashlib.sha256(a + b + str(timestamp).encode()).hexdigest()
    return digest[:8].upper()
