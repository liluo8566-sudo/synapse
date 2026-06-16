"""iLink auth helpers — token persistence, headers, base URL validation."""

from __future__ import annotations

import base64
import json
import logging
import os
import random
import stat
from pathlib import Path
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

ILINK_BASE_URL = "https://ilinkai.weixin.qq.com"
CHANNEL_VERSION = "1.0.2"
POLL_TIMEOUT_S = 40  # slightly above server's 35s hold

CONFIG_DIR = Path.home() / ".config" / "synapse-wx"
TOKEN_FILE = CONFIG_DIR / "token.json"


def random_uin() -> str:
    """Generate X-WECHAT-UIN header value (random uint32 -> base64)."""
    val = random.randint(0, 0xFFFFFFFF)
    return base64.b64encode(str(val).encode()).decode()


def auth_headers(bot_token: str) -> dict[str, str]:
    """Build iLink auth headers. AuthorizationType is iLink-specific."""
    return {
        "Content-Type": "application/json",
        "AuthorizationType": "ilink_bot_token",
        "X-WECHAT-UIN": random_uin(),
        "Authorization": f"Bearer {bot_token}",
    }


def validate_base_url(url: str) -> str:
    """Validate that base_url belongs to official WeChat domains."""
    parsed = urlparse(url)
    allowed_suffixes = (".weixin.qq.com", ".wechat.com")
    if parsed.scheme != "https":
        logger.warning("Rejecting non-HTTPS base_url: %s", url)
        return ILINK_BASE_URL
    if not any(
        parsed.hostname and parsed.hostname.endswith(s) for s in allowed_suffixes
    ):
        logger.warning("Rejecting untrusted base_url: %s", url)
        return ILINK_BASE_URL
    return url


def save_token(data: dict, token_file: Path | None = None) -> None:
    """Persist token with restricted file permissions (owner-only read/write)."""
    tf = token_file if token_file is not None else TOKEN_FILE
    config_dir = tf.parent
    config_dir.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(config_dir, stat.S_IRWXU)  # 0700
    except OSError:
        pass
    tf.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    try:
        os.chmod(tf, stat.S_IRUSR | stat.S_IWUSR)  # 0600
    except OSError:
        pass


def load_token(token_file: Path | None = None) -> dict | None:
    """Load saved token dict, or None if missing/corrupt.

    Defaults are resolved at call time so monkeypatching TOKEN_FILE works.
    """
    tf = token_file if token_file is not None else TOKEN_FILE
    if tf.exists():
        try:
            return json.loads(tf.read_text())
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("Failed to load token: %s", e)
    return None
