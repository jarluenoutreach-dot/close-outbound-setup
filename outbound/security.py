"""Close webhook signature verification.

Close signs each delivery with HMAC-SHA256 using the subscription's hex signature key over
(close-sig-timestamp header + raw request body), and sends the hex digest in close-sig-hash.
"""
from __future__ import annotations

import hashlib
import hmac
import time

MAX_AGE_SECONDS = 15 * 60


def sign(signature_key_hex: str, timestamp: str, body: bytes) -> str:
    key = bytes.fromhex(signature_key_hex)
    return hmac.new(key, timestamp.encode() + body, hashlib.sha256).hexdigest()


def verify_signature(
    signature_key_hex: str,
    body: bytes,
    signature: str | None,
    timestamp: str | None,
    *,
    now: float | None = None,
    max_age: int = MAX_AGE_SECONDS,
) -> bool:
    if not signature_key_hex or not signature or not timestamp:
        return False
    try:
        expected = sign(signature_key_hex, timestamp, body)
    except ValueError:  # key is not valid hex
        return False
    if not hmac.compare_digest(expected, signature.strip().lower()):
        return False
    try:
        age = abs((now if now is not None else time.time()) - float(timestamp))
    except ValueError:
        return True  # signature is valid; timestamp just isn't numeric
    return age <= max_age
