"""OAuth /api/oauth/usage client.

Surfaces the same 5h / 7d utilization percentages cc itself shows in its
status line. Designed so /info can render `42%(5h) 17%(7d)` instead of the
`?(5h) ?(7d)` placeholder, without ever crashing or stalling the handler:

  * stdlib-only HTTP (urllib.request) — zero deps.
  * Lazy keychain token load with `~/.claude/.credentials.json` fallback.
  * TTL cache on a monotonic clock (default 5 min).
  * 429 / 5xx / network / bad-json → return last cached if any, else None,
    log a warning. `.fetch()` MUST NEVER raise into the caller.

Endpoint shape captured in docs/notes/oauth-usage-endpoint.md.
"""

from __future__ import annotations

import json
import logging
import subprocess
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
BETA_HEADER = "oauth-2025-04-20"
USER_AGENT = "synapse-wx (claude-code-oauth)"
KEYCHAIN_SERVICE = "Claude Code-credentials"
FALLBACK_CREDS = Path.home() / ".claude" / ".credentials.json"
HTTP_TIMEOUT_SEC = 8.0

HttpGet = Callable[[str, "dict[str, str]"], "tuple[int, bytes]"]
TokenLoader = Callable[[], "str | None"]


@dataclass(frozen=True)
class Usage:
    """Snapshot of the OAuth /usage response, normalised for /info."""

    five_hour_pct: float | None = None
    seven_day_pct: float | None = None
    five_hour_resets_at_unix: int | None = None
    seven_day_resets_at_unix: int | None = None


class UsageClient:
    """Cached fetcher for the OAuth /usage endpoint. Never raises."""

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        ttl_sec: float = 300.0,
        http_get: HttpGet | None = None,
        token_loader: TokenLoader | None = None,
    ) -> None:
        self._clock = clock
        self._ttl_sec = ttl_sec
        self._http_get = http_get or _urllib_get
        self._token_loader = token_loader or _load_token
        self._cached: Usage | None = None
        self._cached_at: float | None = None

    def fetch(self) -> Usage | None:
        """Return current Usage, refreshing if TTL expired. Stale on failure."""
        now = self._clock()
        if (
            self._cached is not None
            and self._cached_at is not None
            and (now - self._cached_at) < self._ttl_sec
        ):
            return self._cached

        token = self._safe_token()
        if not token:
            logger.warning("usage: no oauth token available")
            return self._cached  # stale OK, else None

        try:
            status, body = self._http_get(USAGE_URL, _headers(token))
        except Exception as e:
            logger.warning("usage: http error %s", e)
            return self._cached

        if status != 200:
            logger.warning("usage: http %s", status)
            return self._cached

        try:
            parsed = _parse_usage(body)
        except Exception as e:
            logger.warning("usage: bad json %s", e)
            return self._cached

        self._cached = parsed
        self._cached_at = now
        return parsed

    def _safe_token(self) -> str | None:
        try:
            return self._token_loader()
        except Exception as e:
            logger.warning("usage: token load failed %s", e)
            return None


# ── helpers ──────────────────────────────────────────────────────────


def _headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "anthropic-beta": BETA_HEADER,
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
    }


def _urllib_get(url: str, headers: dict[str, str]) -> tuple[int, bytes]:
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_SEC) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        # 4xx/5xx — surface status to caller, drain body so the socket closes.
        try:
            body = e.read() or b""
        except Exception:
            body = b""
        return e.code, body


def _parse_usage(body: bytes) -> Usage:
    data = json.loads(body)
    return Usage(
        five_hour_pct=_window_pct(data.get("five_hour")),
        seven_day_pct=_window_pct(data.get("seven_day")),
        five_hour_resets_at_unix=_window_resets(data.get("five_hour")),
        seven_day_resets_at_unix=_window_resets(data.get("seven_day")),
    )


def _window_pct(window: object) -> float | None:
    if not isinstance(window, dict):
        return None
    v = window.get("utilization")
    if isinstance(v, bool):  # bool is int — exclude defensively
        return None
    return float(v) if isinstance(v, (int, float)) else None


def _window_resets(window: object) -> int | None:
    if not isinstance(window, dict):
        return None
    s = window.get("resets_at")
    if not isinstance(s, str):
        return None
    try:
        return int(datetime.fromisoformat(s).timestamp())
    except ValueError:
        return None


def _load_token() -> str | None:
    """macOS keychain first, then `~/.claude/.credentials.json` fallback."""
    try:
        out = subprocess.run(
            [
                "security",
                "find-generic-password",
                "-s",
                KEYCHAIN_SERVICE,
                "-w",
            ],
            capture_output=True,
            text=True,
            timeout=3.0,
            check=False,
        )
        if out.returncode == 0 and out.stdout.strip():
            tok = _extract_token(out.stdout.strip())
            if tok:
                return tok
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    try:
        text = FALLBACK_CREDS.read_text()
    except OSError:
        return None
    try:
        return _extract_token(text)
    except json.JSONDecodeError:
        return None


def _extract_token(blob: str) -> str | None:
    data = json.loads(blob)
    oauth = data.get("claudeAiOauth") if isinstance(data, dict) else None
    if not isinstance(oauth, dict):
        return None
    tok = oauth.get("accessToken")
    return tok if isinstance(tok, str) and tok else None
