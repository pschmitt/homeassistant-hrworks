"""Validate provisioning URIs and generate RFC 6238 codes locally.

The seed stays in Home Assistant. Only a short-lived code reaches the worker.
"""

import base64
import binascii
import hashlib
import hmac
import struct
import time
from urllib.parse import parse_qs, urlsplit


class InvalidTotp(ValueError):
    """A fixed message which never contains the supplied secret."""

    def __init__(self):
        super().__init__("invalid_totp")


def parameters(uri: str) -> tuple[bytes, str, int, int]:
    try:
        if not isinstance(uri, str) or len(uri) > 4096:
            raise ValueError
        parsed = urlsplit(uri.strip())
        if parsed.scheme != "otpauth" or parsed.netloc != "totp" or not parsed.path.strip("/"):
            raise ValueError
        if parsed.fragment:
            raise ValueError
        query = parse_qs(parsed.query, keep_blank_values=True, strict_parsing=True)
        if any(len(values) != 1 for values in query.values()):
            raise ValueError
        seed = query["secret"][0].upper().rstrip("=")
        secret = base64.b32decode(seed + "=" * (-len(seed) % 8))
        algorithm = query.get("algorithm", ["SHA1"])[0].upper()
        digits = int(query.get("digits", ["6"])[0])
        period = int(query.get("period", ["30"])[0])
        if not secret or algorithm not in {"SHA1", "SHA256", "SHA512"}:
            raise ValueError
        if digits not in {6, 8} or not 1 <= period <= 300:
            raise ValueError
        return secret, algorithm, digits, period
    except (KeyError, TypeError, ValueError, binascii.Error):
        raise InvalidTotp() from None


def totp_code(uri: str, *, timestamp: float | None = None) -> str:
    secret, algorithm, digits, period = parameters(uri)
    counter = int((time.time() if timestamp is None else timestamp) // period)
    digest = hmac.new(
        secret, struct.pack(">Q", counter), getattr(hashlib, algorithm.lower())
    ).digest()
    offset = digest[-1] & 15
    number = (struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF) % (10**digits)
    return str(number).zfill(digits)
