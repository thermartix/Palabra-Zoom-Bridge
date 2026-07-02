from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time


def _base64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def create_meeting_sdk_jwt(
    client_id: str,
    client_secret: str,
    meeting_number: str,
    *,
    role: int = 0,
    ttl_seconds: int = 7200,
) -> str:
    if not client_id:
        raise ValueError("Zoom SDK client ID is required.")
    if not client_secret:
        raise ValueError("Zoom SDK client secret is required.")

    issued_at = int(time.time()) - 30
    expires_at = issued_at + max(1800, int(ttl_seconds))
    header = {"alg": "HS256", "typ": "JWT"}
    payload = {
        "appKey": client_id,
        "mn": str(meeting_number),
        "role": int(role),
        "iat": issued_at,
        "exp": expires_at,
        "tokenExp": expires_at,
    }
    encoded_header = _base64url(json.dumps(header, separators=(",", ":")).encode("utf-8"))
    encoded_payload = _base64url(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    signing_input = f"{encoded_header}.{encoded_payload}".encode("ascii")
    signature = hmac.new(client_secret.encode("utf-8"), signing_input, hashlib.sha256).digest()
    return f"{encoded_header}.{encoded_payload}.{_base64url(signature)}"
