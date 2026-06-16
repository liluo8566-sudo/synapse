"""Alert sink: write alerts to disk; optionally pipe to marrow.repo via subprocess.

File-write is the source of truth; the marrow leg is best-effort and silently
swallowed on failure. One alert per file (easy log rotate), JSON one-liner body,
chmod 600.
"""

from __future__ import annotations

import json
import logging
import os
import shlex
import stat
import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_VALID_SEVERITY = ("warn", "critical")


class AlertSink:
    """Write alerts to disk; optionally pipe each one to marrow.repo."""

    def __init__(
        self,
        *,
        alerts_dir: Path,
        marrow_repo_cmd: str | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._alerts_dir = Path(alerts_dir)
        self._marrow_repo_cmd = (marrow_repo_cmd or "").strip()
        self._clock = clock
        self._alerts_dir.mkdir(parents=True, exist_ok=True)

    def write(
        self,
        severity: str,
        kind: str,
        message: str,
        source: str = "",
        *,
        fingerprint: str | None = None,
    ) -> Path:
        """Write one alert. Returns the path of the written file.

        fingerprint — stable dedup key passed to marrow.repo.add_alert.
                      Defaults to kind when omitted (legacy behaviour).
        """
        if severity not in _VALID_SEVERITY:
            logger.warning("unknown alert severity %r; coercing to 'warn'", severity)
            severity = "warn"
        ts = int(self._clock())
        safe_kind = _safe_token(kind) or "alert"
        path = self._alerts_dir / f"{ts}_{severity}_{safe_kind}.txt"
        payload: dict[str, Any] = {
            "ts": ts,
            "severity": severity,
            "kind": kind,
            "fingerprint": fingerprint or kind,
            "message": message,
            "source": source,
        }
        try:
            path.write_text(json.dumps(payload, ensure_ascii=False) + "\n")
            os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
        except OSError as e:
            logger.warning("alert file write failed: %s", e)
        self._maybe_pipe_to_marrow(severity, kind, message, source, fingerprint=fingerprint)
        return path

    def list_recent(self, since_ts: float = 0.0) -> list[dict]:
        """Return alert records with mtime >= since_ts (sorted by mtime asc)."""
        if not self._alerts_dir.is_dir():
            return []
        rows: list[tuple[float, dict]] = []
        for entry in self._alerts_dir.iterdir():
            if not entry.is_file() or not entry.name.endswith(".txt"):
                continue
            try:
                mtime = entry.stat().st_mtime
            except OSError:
                continue
            if mtime < since_ts:
                continue
            try:
                body = entry.read_text().strip()
                data = json.loads(body) if body else {}
            except (OSError, json.JSONDecodeError) as e:
                logger.warning("alert read failed for %s: %s", entry.name, e)
                continue
            data["_path"] = str(entry)
            data["_mtime"] = mtime
            rows.append((mtime, data))
        rows.sort(key=lambda kv: kv[0])
        return [d for _, d in rows]

    def _maybe_pipe_to_marrow(
        self,
        severity: str,
        kind: str,
        message: str,
        source: str,
        *,
        fingerprint: str | None = None,
    ) -> None:
        if not self._marrow_repo_cmd:
            return
        try:
            argv = shlex.split(self._marrow_repo_cmd)
        except ValueError as e:
            logger.warning("marrow_repo_cmd parse failed: %s", e)
            return
        if not argv:
            return
        argv.extend([severity, kind, fingerprint or kind])
        if source:
            argv.extend(["--source", source])
        if message:
            argv.extend(["--message", message])
        try:
            subprocess.Popen(  # noqa: S603 - marrow_repo_cmd is operator-supplied config
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
                start_new_session=True,
            )
        except OSError as e:
            logger.warning("marrow_repo spawn failed: %s", e)


def _safe_token(s: str) -> str:
    """Strip unsafe chars from a token used in a file name."""
    out = []
    for ch in s:
        if ch.isalnum() or ch in ("-", "_"):
            out.append(ch)
        elif ch == " ":
            out.append("_")
    return "".join(out)[:60]
