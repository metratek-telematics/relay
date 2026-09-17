"""Web Push with VAPID (RFC 8292), without a payload.

A push with no body needs no message encryption: the browser wakes Relay's service worker (web/sw.js), which
fetches the newest notification for the signed-in person from Relay and shows it. Signing needs the
`cryptography` package; without it Web Push reports itself unavailable and browsers fall back to
notifications while Relay is open.
"""
from __future__ import annotations

import base64
import json
import time
from urllib.parse import urlparse

try:  # optional dependency
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
    AVAILABLE = True
except Exception:  # pragma: no cover
    AVAILABLE = False


def b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def generate_keys() -> dict:
    if not AVAILABLE:
        raise RuntimeError("Web Push needs the cryptography package (pip install cryptography).")
    key = ec.generate_private_key(ec.SECP256R1())
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
    pub = key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    return {"private_key": pem, "public_key": b64u(pub)}


def vapid_headers(endpoint: str, private_pem: str, public_key: str, subject: str, ttl: int = 86400) -> dict:
    if not AVAILABLE:
        raise RuntimeError("Web Push needs the cryptography package.")
    u = urlparse(endpoint)
    aud = f"{u.scheme}://{u.netloc}"
    header = b64u(json.dumps({"typ": "JWT", "alg": "ES256"}, separators=(",", ":")).encode())
    claims = b64u(json.dumps({"aud": aud, "exp": int(time.time()) + 12 * 3600, "sub": subject or "mailto:relay@localhost"},
                             separators=(",", ":")).encode())
    signing_input = f"{header}.{claims}".encode()
    key = serialization.load_pem_private_key(private_pem.encode(), password=None)
    der = key.sign(signing_input, ec.ECDSA(hashes.SHA256()))
    r, s = decode_dss_signature(der)
    sig = r.to_bytes(32, "big") + s.to_bytes(32, "big")
    jwt = f"{header}.{claims}.{b64u(sig)}"
    return {"Authorization": f"vapid t={jwt}, k={public_key}", "TTL": str(ttl), "Urgency": "high"}


def verify_jwt(auth_header: str) -> dict:
    """For tests and the mock push service: check a VAPID Authorization header, return its claims."""
    part = dict(x.strip().split("=", 1) for x in auth_header[len("vapid "):].split(","))
    jwt, k = part["t"], part["k"]
    h, c, s = jwt.split(".")
    pad = lambda x: x + "=" * (-len(x) % 4)
    pub = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), base64.urlsafe_b64decode(pad(k)))
    raw = base64.urlsafe_b64decode(pad(s))
    from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
    der = encode_dss_signature(int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big"))
    pub.verify(der, f"{h}.{c}".encode(), ec.ECDSA(hashes.SHA256()))
    return json.loads(base64.urlsafe_b64decode(pad(c)))
