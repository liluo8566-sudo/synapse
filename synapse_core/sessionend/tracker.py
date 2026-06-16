"""Session tracker: user_id -> current cc session_id, persisted atomically."""

from __future__ import annotations

import json
import logging
import os
import stat
import threading
from pathlib import Path

logger = logging.getLogger(__name__)

class SessionTracker:
    """Maps user_id -> current cc session_id. Thread-safe, disk-persisted."""

    def __init__(self, state_path: Path) -> None:
        self._state_path = Path(state_path)
        self._lock = threading.RLock()
        self._sessions: dict[str, str] = {}
        self._load()

    # ── persistence ────────────────────────────────────────────────

    def _load(self) -> None:
        try:
            raw = self._state_path.read_text()
        except FileNotFoundError:
            return
        except OSError as e:
            logger.warning("SessionTracker load failed: %s", e)
            return
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            logger.warning("SessionTracker decode failed: %s", e)
            return
        if isinstance(data, dict):
            with self._lock:
                self._sessions.update(
                    {str(k): str(v) for k, v in data.items() if v}
                )

    def _save_locked(self) -> None:
        """Atomic write under lock. Caller must hold self._lock."""
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._state_path.with_suffix(self._state_path.suffix + ".tmp")
        tmp.write_text(json.dumps(self._sessions, indent=2, sort_keys=True))
        try:
            os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass
        os.replace(tmp, self._state_path)

    # ── public API ─────────────────────────────────────────────────

    def get(self, user_id: str) -> str | None:
        with self._lock:
            return self._sessions.get(user_id)

    def set(self, user_id: str, sid: str) -> None:
        if not user_id or not sid:
            raise ValueError("user_id and sid must be non-empty")
        with self._lock:
            self._sessions[user_id] = sid
            self._save_locked()

    def forget(self, user_id: str) -> str | None:
        with self._lock:
            old = self._sessions.pop(user_id, None)
            if old is not None:
                self._save_locked()
            return old

    def snapshot(self) -> dict[str, str]:
        with self._lock:
            return dict(self._sessions)
