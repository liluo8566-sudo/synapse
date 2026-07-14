"""NotebookSync: pull dirty qidu books' markdown export, write to a local vault.

Rides QiduSignalPoller's existing ~5s poll loop (loop.py calls .tick() there)
instead of running its own thread/process. Every _SYNC_EVERY_TICKS ticks
(~60s at 5s/tick) it runs one sync pass:
  1. GET {api_base}/export/dirty (?all=1 on the first successful pass)
  2. per book: GET /books/{id}/export.md
  3. atomic write to {notebook_dir}/{safe_title}.md

Both tg and wx bridges instantiate this — flock (same pattern as
QiduParser, qidu_parser.py:102-127) guards against overlapping writes;
lock unavailable this tick → skip silently.

Source of truth is the book-server DB; vault files are a one-way
projection. Books deleted server-side are never deleted here (orphans
stay — see phase5-spec.md Batch C).
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import urllib.request
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_LOCK_PATH = "/tmp/qidu-notebook.lock"
_SYNC_EVERY_TICKS = 12  # ~60s at the poller's 5s cadence
_HTTP_TIMEOUT = 15
_UNSAFE_CHARS = str.maketrans({c: "·" for c in '/\\:*?"<>|'})


def safe_title(title: str) -> str:
    """Replace filesystem-unsafe characters with '·'."""
    return (title or "untitled").translate(_UNSAFE_CHARS)


def _read_frontmatter_book_id(path: Path) -> str | None:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    for line in lines[1:]:
        if line.strip() == "---":
            break
        if line.startswith("book_id:"):
            return line.split(":", 1)[1].strip()
    return None


class NotebookSync:
    """See module docstring. Disabled (no-op tick) unless api_base/token/
    notebook_dir are all configured."""

    def __init__(
        self,
        api_base: str,
        token: str,
        notebook_dir: str | Path | None,
        *,
        sync_every: int = _SYNC_EVERY_TICKS,
        lock_path: str = _LOCK_PATH,
        alerts: Any = None,
    ) -> None:
        self._api_base = (api_base or "").rstrip("/")
        self._token = token
        self._notebook_dir = Path(notebook_dir).expanduser() if notebook_dir else None
        self.enabled = bool(self._api_base and self._token and self._notebook_dir)
        self._sync_every = sync_every
        self._lock_path = lock_path
        self._alerts = alerts
        self._tick_count = 0
        self._first_pass_done = False
        self._lock_fd: int | None = None

    def tick(self) -> None:
        """Call once per poller tick (~5s). No-op unless enabled; only every
        sync_every-th call actually runs a pass. flock-guarded — held
        elsewhere → skip this tick silently."""
        if not self.enabled:
            return
        self._tick_count += 1
        if self._tick_count < self._sync_every:
            return
        self._tick_count = 0
        if not self._try_acquire_lock():
            return
        try:
            self._sync_once()
        finally:
            self._release_lock()

    # ── sync pass ────────────────────────────────────────────────────────

    def _sync_once(self) -> None:
        try:
            books = self._fetch_dirty(all_pass=not self._first_pass_done)
        except Exception as e:
            logger.warning("qidu notebook: dirty fetch failed: %s", e)
            return
        self._first_pass_done = True
        for book in books:
            bid = book.get("book_id") or book.get("id")
            title = book.get("title") or bid
            if not bid:
                continue
            try:
                content = self._fetch_export(bid)
            except Exception as e:
                logger.warning("qidu notebook: export fetch failed for %s: %s", bid, e)
                continue
            self._write_book(bid, title, content)

    def _write_book(self, book_id: str, title: str, content: str) -> None:
        try:
            self._notebook_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            logger.warning("qidu notebook: mkdir %s failed: %s", self._notebook_dir, e)
            return
        target = self._resolve_target(book_id, title)
        tmp = target.with_name(target.name + ".tmp")
        try:
            tmp.write_text(content, encoding="utf-8")
            os.replace(tmp, target)
        except OSError as e:
            logger.warning("qidu notebook: write %s failed: %s", target, e)
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass

    def _resolve_target(self, book_id: str, title: str) -> Path:
        safe = safe_title(title)
        target = self._notebook_dir / f"{safe}.md"
        if target.exists():
            existing_id = _read_frontmatter_book_id(target)
            if existing_id and existing_id != book_id:
                target = self._notebook_dir / f"{safe}-{book_id[:8]}.md"
        return target

    # ── HTTP ─────────────────────────────────────────────────────────────

    def _fetch_dirty(self, *, all_pass: bool) -> list[dict]:
        path = "/export/dirty?all=1" if all_pass else "/export/dirty"
        result = self._http_json(path)
        if isinstance(result, list):
            return result
        return result.get("books", []) if isinstance(result, dict) else []

    def _fetch_export(self, book_id: str) -> str:
        return self._http_text(f"/books/{book_id}/export.md")

    def _http_json(self, path: str) -> Any:
        raw = self._http_raw(path)
        return json.loads(raw) if raw else {}

    def _http_text(self, path: str) -> str:
        return self._http_raw(path).decode("utf-8")

    def _http_raw(self, path: str) -> bytes:
        req = urllib.request.Request(
            f"{self._api_base}{path}",
            headers={"Authorization": f"Bearer {self._token}"},
        )
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as resp:
            return resp.read()

    # ── flock (same pattern as QiduParser, qidu_parser.py:102-127) ─────────

    def _try_acquire_lock(self) -> bool:
        fd = None
        try:
            fd = os.open(self._lock_path, os.O_CREAT | os.O_WRONLY, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._lock_fd = fd
            return True
        except BlockingIOError:
            if fd is not None:
                os.close(fd)
            return False
        except OSError as e:
            logger.warning("qidu notebook: flock error: %s", e)
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
            return False

    def _release_lock(self) -> None:
        if self._lock_fd is not None:
            try:
                fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
                os.close(self._lock_fd)
            except OSError:
                pass
            self._lock_fd = None
