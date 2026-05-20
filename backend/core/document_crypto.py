from __future__ import annotations

import base64
import hashlib
import os
from typing import Any

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from core.env import env_bool, env_str


ENCRYPTION_METADATA_KEY = "_encryption"
ALGORITHM = "AES-256-GCM"
VERSION = 1


def document_encryption_enabled() -> bool:
    return env_bool("DOCUMENT_ENCRYPTION_ENABLED", True)


def _b64_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _b64_decode(value: str) -> bytes:
    padded = value + "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(padded.encode("ascii"))


def _decode_configured_key(raw_value: str) -> bytes | None:
    value = raw_value.strip()
    if not value:
        return None

    try:
        if value.startswith("base64:"):
            decoded = _b64_decode(value.removeprefix("base64:"))
            return decoded if len(decoded) == 32 else None
        if value.startswith("hex:"):
            decoded = bytes.fromhex(value.removeprefix("hex:"))
            return decoded if len(decoded) == 32 else None

        decoded = _b64_decode(value)
        if len(decoded) == 32:
            return decoded
    except Exception:  # noqa: BLE001
        return None

    return hashlib.sha256(value.encode("utf-8")).digest()


def _master_key() -> bytes:
    configured = _decode_configured_key(env_str("DOCUMENT_ENCRYPTION_KEY", "", strip=True))
    if configured is not None:
        return configured

    fallback_secret = env_str(
        "APP_SECRET_KEY",
        env_str("ADMIN_UI_SECRET_KEY", "change-me-document-encryption-key"),
        strip=True,
    )
    return hashlib.sha256(fallback_secret.encode("utf-8")).digest()


def encryption_key_id() -> str:
    return hashlib.sha256(_master_key()).hexdigest()[:16]


def encrypt_content(content: bytes, *, associated_data: bytes) -> tuple[bytes, dict[str, Any]]:
    if not document_encryption_enabled():
        return content, {}

    nonce = os.urandom(12)
    encrypted = AESGCM(_master_key()).encrypt(nonce, content, associated_data)
    return encrypted, {
        "version": VERSION,
        "algorithm": ALGORITHM,
        "keyId": encryption_key_id(),
        "nonce": _b64_encode(nonce),
    }


def decrypt_content(content: bytes, metadata: dict[str, Any], *, associated_data: bytes) -> bytes:
    encryption = metadata.get(ENCRYPTION_METADATA_KEY)
    if not encryption:
        return content
    if encryption.get("algorithm") != ALGORITHM or int(encryption.get("version", 0) or 0) != VERSION:
        raise ValueError("unsupported stored file encryption metadata")

    nonce = _b64_decode(str(encryption.get("nonce") or ""))
    return AESGCM(_master_key()).decrypt(nonce, content, associated_data)
