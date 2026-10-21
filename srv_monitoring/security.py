from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

from .config import Settings


COOKIE_NAME = "srvmon_session"
SESSION_TTL_SECONDS = 8 * 60 * 60


class SecretBox:
    def __init__(self, key: bytes) -> None:
        self._fernet = Fernet(key)

    def encrypt_json(self, value: dict[str, Any]) -> str:
        raw = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        return self._fernet.encrypt(raw).decode("ascii")

    def decrypt_json(self, value: str) -> dict[str, Any]:
        try:
            raw = self._fernet.decrypt(value.encode("ascii"))
            result = json.loads(raw.decode("utf-8"))
        except (InvalidToken, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            raise RuntimeError(
                "The stored source configuration cannot be decrypted. "
                "Check MONITORING_ENCRYPTION_KEY and restore its original value."
            ) from exc
        if not isinstance(result, dict):
            raise RuntimeError("Stored source configuration is not an object")
        return result


def hash_agent_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def new_agent_token() -> str:
    return secrets.token_urlsafe(36)


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _unb64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def create_session(settings: Settings, username: str, now: int | None = None) -> str:
    issued = int(time.time() if now is None else now)
    payload = json.dumps(
        {"sub": username, "iat": issued, "exp": issued + SESSION_TTL_SECONDS, "nonce": secrets.token_hex(8)},
        separators=(",", ":"),
    ).encode("utf-8")
    signature = hmac.new(settings.session_secret, payload, hashlib.sha256).digest()
    return f"{_b64(payload)}.{_b64(signature)}"


def verify_session(settings: Settings, token: str | None, now: int | None = None) -> str | None:
    if not token or "." not in token:
        return None
    try:
        payload_part, signature_part = token.split(".", 1)
        payload = _unb64(payload_part)
        signature = _unb64(signature_part)
        expected = hmac.new(settings.session_secret, payload, hashlib.sha256).digest()
        if not hmac.compare_digest(signature, expected):
            return None
        data = json.loads(payload.decode("utf-8"))
        current = int(time.time() if now is None else now)
        if not isinstance(data, dict) or data.get("exp", 0) < current:
            return None
        username = data.get("sub")
        if username != settings.admin_user:
            return None
        return str(username)
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError, TypeError):
        return None


def verify_admin_password(settings: Settings, username: str, password: str) -> bool:
    user_ok = hmac.compare_digest(username.encode("utf-8"), settings.admin_user.encode("utf-8"))
    password_ok = hmac.compare_digest(
        password.encode("utf-8"), settings.admin_password.encode("utf-8")
    )
    return user_ok and password_ok
