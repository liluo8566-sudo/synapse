"""Media download + AES-128-ECB decrypt (inbound) and encrypt + upload (outbound).

cryptography is imported lazily so the rest of the client runs without it.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import os
import time
from pathlib import Path
from urllib.parse import urlencode

import httpx

logger = logging.getLogger(__name__)

CDN_BASE = "https://novac2c.cdn.weixin.qq.com/c2c/download"
CDN_UPLOAD = "https://novac2c.cdn.weixin.qq.com/c2c/upload"

# WeChat CDN gates on User-Agent; python-httpx UA causes throttling / HTTP 500.
# MicroMessenger UA reliably passes for payloads up to ~512KB.
_CDN_UA = "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) MicroMessenger/8.0.0"

# CDN server deadline is ~30s at ~20KB/s → hard ceiling ~550KB ciphertext.
# Chunked/resumable upload is a FUTURE item.
_CDN_MAX_CIPHERTEXT = 550_000

# CDN intermittently returns HTTP 500 -5104001 (~1/3) even at small sizes;
# re-ticket + retry the two-step upload. 3 attempts → ~96% per-send success.
_CDN_UPLOAD_ATTEMPTS = 3
_CDN_RETRY_BACKOFF_S = 0.8

# media_type codes for getuploadurl (different from item_list type codes).
_MEDIA_TYPE_IMAGE = 1
_MEDIA_TYPE_VIDEO = 2
_MEDIA_TYPE_FILE = 3


def _parse_aes_key(aes_key: str) -> bytes | None:
    """Return 16-byte AES key, trying hex / base64(hex) / raw base64 in order.

    # aes_key arrives in 3 shapes: base64(hex), raw hex, raw base64.
    """
    if not aes_key:
        return None
    try:
        decoded = base64.b64decode(aes_key)
        return bytes.fromhex(decoded.decode("ascii"))
    except Exception:
        pass
    try:
        return bytes.fromhex(aes_key)
    except ValueError:
        pass
    try:
        return base64.b64decode(aes_key)
    except Exception:
        logger.warning("Cannot parse AES key")
        return None


def download_and_decrypt(
    http: httpx.Client,
    cdn_url: str,
    aes_key: str,
    save_path: Path,
    encrypt_query_param: str = "",
) -> bool:
    """Download from WeChat CDN, decrypt AES-128-ECB, write to save_path."""
    if not cdn_url and not encrypt_query_param:
        return False
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    except ImportError:
        logger.error("cryptography not installed — cannot decrypt media")
        return False

    try:
        if encrypt_query_param:
            url = f"{CDN_BASE}?encrypted_query_param={encrypt_query_param}"
        elif cdn_url.startswith("http"):
            url = cdn_url
        else:
            url = f"{CDN_BASE}?fileid={cdn_url}"

        resp = http.get(url, timeout=30)
        resp.raise_for_status()
        encrypted = resp.content
        if not encrypted:
            return False

        key_bytes = _parse_aes_key(aes_key)
        if not key_bytes or len(key_bytes) != 16 or len(encrypted) % 16 != 0:
            save_path.write_bytes(encrypted)
            return True

        cipher = Cipher(algorithms.AES(key_bytes), modes.ECB())
        decryptor = cipher.decryptor()
        decrypted = decryptor.update(encrypted) + decryptor.finalize()
        if decrypted:
            pad_len = decrypted[-1]
            if 1 <= pad_len <= 16 and all(b == pad_len for b in decrypted[-pad_len:]):
                decrypted = decrypted[:-pad_len]
        save_path.write_bytes(decrypted)
        return True
    except Exception as e:
        logger.error("Failed to download media: %s", e)
        return False


# ── outbound: encrypt + upload ──────────────────────────────────────────────


def pkcs7_pad(data: bytes, block: int = 16) -> bytes:
    """PKCS7-pad data to a multiple of `block` (always pads, even on boundary)."""
    pad_len = block - (len(data) % block)
    return data + bytes([pad_len]) * pad_len


def _random_key() -> bytes:
    """16 random bytes for AES-128. Indirection so tests can pin the key."""
    return os.urandom(16)


def _random_filekey() -> str:
    """32-char hex filekey (16 random bytes). Indirection so tests can pin it."""
    return os.urandom(16).hex()


def encrypt_aes_ecb(data: bytes, key: bytes) -> bytes:
    """AES-128-ECB encrypt with PKCS7 padding. Raises if key length != 16."""
    if len(key) != 16:
        raise ValueError(f"AES-128 key must be 16 bytes, got {len(key)}")
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    padded = pkcs7_pad(data, 16)
    encryptor = Cipher(algorithms.AES(key), modes.ECB()).encryptor()
    return encryptor.update(padded) + encryptor.finalize()


def decrypt_aes_ecb(ciphertext: bytes, key: bytes) -> bytes | None:
    """AES-128-ECB decrypt with PKCS7 padding strip. Symmetric to encrypt_aes_ecb.

    Returns None if ciphertext length is not a 16-byte multiple (unsafe).
    Raises ValueError if key length != 16.
    """
    if len(key) != 16:
        raise ValueError(f"AES-128 key must be 16 bytes, got {len(key)}")
    if not ciphertext or len(ciphertext) % 16 != 0:
        return None
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    decryptor = Cipher(algorithms.AES(key), modes.ECB()).decryptor()
    raw = decryptor.update(ciphertext) + decryptor.finalize()
    if not raw:
        return raw
    pad_len = raw[-1]
    if 1 <= pad_len <= 16 and all(b == pad_len for b in raw[-pad_len:]):
        return raw[:-pad_len]
    return raw


def _item_type_to_media_type(item_type: str) -> int:
    """Map item kind string to getuploadurl media_type int (1=image,2=video,3=file)."""
    if item_type == "image":
        return _MEDIA_TYPE_IMAGE
    if item_type == "video":
        return _MEDIA_TYPE_VIDEO
    return _MEDIA_TYPE_FILE


def upload_and_encrypt(
    http: httpx.Client,
    *,
    base_url: str,
    headers: dict[str, str],
    path: Path,
    item_type: str,
    to_user_id: str,
) -> dict:
    """Read → AES-128-ECB encrypt → two-step CDN upload → return media metadata.

    Step A: POST {base_url}/ilink/bot/getuploadurl — returns upload_param ticket.
    Step B: POST CDN_UPLOAD?encrypted_query_param=...&filekey=... — octet-stream.

    Returns {encrypt_query_param, aes_key_hex, aes_key_b64, padded_size, rawsize, md5}
    on success, or {} on any failure.
    """
    p = Path(path)
    if not p.exists() or not p.is_file():
        logger.warning("upload_and_encrypt: missing file %s", p)
        return {}

    import time as _time
    for _attempt in range(3):
        try:
            data = p.read_bytes()
            break
        except OSError as e:
            if _attempt < 2:
                _time.sleep(0.4)
                continue
            logger.warning("upload_and_encrypt: read failed: %s", e)
            return {}

    try:
        key = _random_key()
        ciphertext = encrypt_aes_ecb(data, key)
    except (ImportError, ValueError) as e:
        logger.error("upload_and_encrypt: encrypt failed: %s", e)
        return {}

    if len(ciphertext) > _CDN_MAX_CIPHERTEXT:
        logger.warning(
            "media too large for CDN (>550KB ciphertext), skipping: %s", p
        )
        return {}

    md5_hex = hashlib.md5(data).hexdigest()
    key_hex = key.hex()
    # double-encoded: base64 of the UTF-8 bytes of the hex string
    aes_key_b64 = base64.b64encode(key_hex.encode("ascii")).decode("ascii")
    rawsize = len(data)
    padded_size = len(ciphertext)
    media_type = _item_type_to_media_type(item_type)

    # Two-step upload with re-ticket + retry (CDN is ~1/3 flaky).
    encrypt_query_param = ""
    for attempt in range(_CDN_UPLOAD_ATTEMPTS):
        if attempt:
            time.sleep(_CDN_RETRY_BACKOFF_S * attempt)
        filekey = _random_filekey()

        # Step A — get upload ticket (fresh per attempt)
        ticket_body = {
            "filekey": filekey,
            "media_type": media_type,
            "to_user_id": to_user_id,
            "rawsize": rawsize,
            "rawfilemd5": md5_hex,
            "filesize": padded_size,
            "no_need_thumb": True,
            "aeskey": key_hex,
            "base_info": {"channel_version": _get_channel_version()},
        }
        try:
            ticket_resp = http.post(
                f"{base_url}/ilink/bot/getuploadurl",
                headers=headers,
                json=ticket_body,
                timeout=30,
            )
        except Exception as e:
            logger.warning("upload_and_encrypt: getuploadurl POST failed: %s", e)
            continue
        if ticket_resp.status_code != 200:
            logger.warning(
                "upload_and_encrypt: getuploadurl HTTP %s — %s",
                ticket_resp.status_code,
                ticket_resp.text[:200],
            )
            continue
        try:
            ticket_data = ticket_resp.json()
        except Exception:
            logger.warning("upload_and_encrypt: getuploadurl non-JSON response")
            continue
        if ticket_data.get("ret") not in (0, None):
            logger.warning("upload_and_encrypt: getuploadurl ret=%s", ticket_data.get("ret"))
            continue
        upload_param = ticket_data.get("upload_param", "")
        if not upload_param:
            logger.warning("upload_and_encrypt: getuploadurl missing upload_param")
            continue

        # Step B — CDN direct upload
        cdn_url = CDN_UPLOAD + "?" + urlencode(
            {"encrypted_query_param": upload_param, "filekey": filekey}
        )
        try:
            cdn_resp = http.post(
                cdn_url,
                headers={
                    "Content-Type": "application/octet-stream",
                    "User-Agent": _CDN_UA,
                },
                content=ciphertext,
                timeout=60,
            )
        except Exception as e:
            logger.warning("upload_and_encrypt: CDN upload POST failed: %s", e)
            continue
        if cdn_resp.status_code != 200:
            err = cdn_resp.headers.get("x-error-message", "") or cdn_resp.headers.get("x-error-code", "")
            logger.warning(
                "upload_and_encrypt: CDN upload HTTP %s — %s (attempt %d/%d)",
                cdn_resp.status_code,
                err or cdn_resp.text[:200],
                attempt + 1,
                _CDN_UPLOAD_ATTEMPTS,
            )
            continue
        encrypt_query_param = cdn_resp.headers.get("x-encrypted-param", "")
        if not encrypt_query_param:
            logger.warning("upload_and_encrypt: CDN response missing x-encrypted-param header")
            continue
        break

    if not encrypt_query_param:
        logger.warning(
            "upload_and_encrypt: all %d upload attempts failed", _CDN_UPLOAD_ATTEMPTS
        )
        return {}

    return {
        "encrypt_query_param": encrypt_query_param,
        "aes_key_hex": key_hex,
        "aes_key_b64": aes_key_b64,
        "padded_size": padded_size,
        "rawsize": rawsize,
        "md5": md5_hex,
    }


def _get_channel_version() -> str:
    """Import CHANNEL_VERSION lazily to avoid circular import."""
    try:
        from ._auth import CHANNEL_VERSION
        return CHANNEL_VERSION
    except ImportError:
        return ""
