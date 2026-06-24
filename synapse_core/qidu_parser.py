"""QiduParser: poll book server for pending books, dispatch temporary sonnet sessions."""

from __future__ import annotations

import fcntl
import json
import logging
import os
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request
from typing import Any

logger = logging.getLogger(__name__)

_LOCK_PATH = "/tmp/qidu-parser.lock"
_TMP_BASE = "/tmp/qidu-parse"
_PARSE_TIMEOUT = 600  # seconds wall-clock per book
_WAIT_AFTER_RESULT = 30  # seconds to wait for proc exit after result event
_MAX_FAILURES = 3
_QUOTA_PROBE_INTERVAL = 1800  # 30 minutes
_FLOCK_RETRY_INTERVAL = 60  # seconds between flock retries
_RAPID_FAIL_THRESHOLD = 3  # N rapid non-timeout failures → suspect quota
_RAPID_FAIL_WINDOW = 120  # seconds


class QiduParser:
    """Poll qidu book-server for pending books, dispatch temporary sonnet sessions.

    start() launches a daemon thread. stop() signals it to exit and joins.
    WX and TG bridges both call start(); flock ensures only one polls at a time.
    """

    def __init__(
        self,
        api_base: str,
        token: str,
        binary: str = "claude",
        max_concurrent: int = 2,
        poll_interval: float = 10.0,
        extract_script: str = "~/workshop/qidu/local/extract_book.py",
        alerts: Any = None,
    ) -> None:
        self._api_base = api_base.rstrip("/")
        self._token = token
        self._binary = binary
        self._max_concurrent = max_concurrent
        self._poll_interval = poll_interval
        self._extract_script = extract_script
        self._alerts = alerts

        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

        # book_id → Popen (or sentinel thread tracking it)
        self._active: dict[str, subprocess.Popen] = {}
        self._fail_count: dict[str, int] = {}

        self._quota_exhausted = False
        self._last_quota_probe: float = 0.0

        # rapid failure tracking (non-timeout exits)
        self._rapid_fail_times: list[float] = []

        self._lock_fd: int | None = None

    # ── lifecycle ──────────────────────────────────────────────────────────────

    def start(self) -> None:
        self._thread = threading.Thread(target=self._poll_loop, daemon=True, name="qidu-poll")
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=15)

    # ── polling ────────────────────────────────────────────────────────────────

    def _poll_loop(self) -> None:
        while not self._stop_event.is_set():
            if not self._try_acquire_lock():
                self._stop_event.wait(timeout=_FLOCK_RETRY_INTERVAL)
                continue

            # we hold the lock — poll normally
            try:
                while not self._stop_event.is_set():
                    self._reap_finished()
                    if self._quota_exhausted:
                        self._maybe_probe_quota()
                    else:
                        self._poll_once()
                    self._stop_event.wait(timeout=self._poll_interval)
            finally:
                self._release_lock()

    def _try_acquire_lock(self) -> bool:
        try:
            fd = os.open(_LOCK_PATH, os.O_CREAT | os.O_WRONLY, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._lock_fd = fd
            return True
        except BlockingIOError:
            return False
        except OSError as e:
            logger.warning("flock error: %s", e)
            return False

    def _release_lock(self) -> None:
        if self._lock_fd is not None:
            try:
                fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
                os.close(self._lock_fd)
            except OSError:
                pass
            self._lock_fd = None

    def _poll_once(self) -> None:
        try:
            pending = self._fetch_pending()
        except Exception as e:
            logger.warning("fetch_pending failed: %s", e)
            return

        with self._lock:
            active_ids = set(self._active.keys())
            slots = self._max_concurrent - len(active_ids)

        for book in pending:
            bid = book.get("book_id") or book.get("id", "")
            if not bid:
                continue
            with self._lock:
                if bid in self._active:
                    continue
                if len(self._active) >= self._max_concurrent:
                    break
            self._spawn_parser(book)

    def _reap_finished(self) -> None:
        with self._lock:
            done = [bid for bid, proc in self._active.items() if proc.poll() is not None]
            for bid in done:
                del self._active[bid]

    # ── HTTP ───────────────────────────────────────────────────────────────────

    def _http(self, method: str, path: str, body: dict | None = None) -> dict:
        url = f"{self._api_base}{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            url,
            data=data,
            method=method,
            headers={
                "Authorization": f"Bearer {self._token}",
                "Content-Type": "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else {}

    def _fetch_pending(self) -> list:
        result = self._http("GET", "/books/pending")
        if isinstance(result, list):
            return result
        return result.get("books", [])

    def _report_failed(self, bid: str) -> None:
        try:
            self._http("POST", f"/books/{bid}/parse-failed")
        except Exception as e:
            logger.warning("report_failed(%s) error: %s", bid, e)

    def _report_quota(self, bid: str) -> None:
        try:
            self._http("POST", f"/books/{bid}/parse-quota")
        except Exception as e:
            logger.warning("report_quota(%s) error: %s", bid, e)

    def _retry_quota(self) -> None:
        try:
            self._http("POST", "/books/retry-quota")
        except Exception as e:
            logger.warning("retry_quota error: %s", e)

    def _check_parse_status(self, bid: str) -> int:
        try:
            result = self._http("GET", f"/books/{bid}/parse-status")
            return result.get("parsed", 0)
        except Exception as e:
            logger.warning("check_parse_status(%s) error: %s", bid, e)
            return 0

    # ── sonnet process ─────────────────────────────────────────────────────────

    def _spawn_parser(self, book: dict) -> None:
        bid = book.get("book_id") or book.get("id", "")

        class _Sentinel:
            """Placeholder in _active until the real Popen is registered."""
            def poll(self):
                return None  # treat as still running

        with self._lock:
            self._active[bid] = _Sentinel()  # type: ignore[assignment]

        t = threading.Thread(target=self._parse_one, args=(book,), daemon=True, name=f"qidu-{bid[:8]}")
        t.start()

    def _parse_one(self, book: dict) -> None:
        bid = book.get("book_id") or book.get("id", "")
        filename = book.get("upload_filename") or book.get("filename", "")
        proc: subprocess.Popen | None = None

        cmd = [
            self._binary,
            "--model", "sonnet",
            "--output-format", "stream-json",
            "--input-format", "stream-json",
            "--permission-mode", "bypassPermissions",
            "--setting-sources", "",
            "--max-turns", "25",
        ]
        prompt = self._build_prompt(book)
        user_msg = json.dumps({
            "type": "user",
            "message": {"role": "user", "content": prompt},
        }) + "\n"

        deadline = time.time() + _PARSE_TIMEOUT
        got_result = False
        timed_out = False

        try:
            os.makedirs(_TMP_BASE, exist_ok=True)
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,
            )

            with self._lock:
                self._active[bid] = proc

            proc.stdin.write(user_msg)
            proc.stdin.flush()
            # do NOT close stdin — claude needs it open for multi-turn tool calls

            for line in proc.stdout:
                if time.time() > deadline:
                    timed_out = True
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue

                if event.get("type") == "result":
                    got_result = True
                    break

            if timed_out:
                logger.warning("book %s: parse timed out", bid)
                if proc:
                    proc.kill()
                self._record_failure(bid, rapid=False)
                return

            if got_result:
                try:
                    proc.wait(timeout=_WAIT_AFTER_RESULT)
                except subprocess.TimeoutExpired:
                    proc.kill()

                parsed = self._check_parse_status(bid)
                if parsed == 1:
                    logger.info("book %s: parse success", bid)
                    with self._lock:
                        self._fail_count.pop(bid, None)
                else:
                    logger.warning("book %s: result event but parse-status=%s", bid, parsed)
                    self._record_failure(bid, rapid=False)
            else:
                # no result event, proc exited unexpectedly
                exit_code = proc.poll()
                logger.warning("book %s: no result event, exit_code=%s", bid, exit_code)
                self._record_failure(bid, rapid=True)

        except Exception as e:
            logger.exception("book %s: _parse_one error: %s", bid, e)
            self._record_failure(bid, rapid=False)
        finally:
            if proc is not None:
                try:
                    proc.kill()
                except Exception:
                    pass
            with self._lock:
                self._active.pop(bid, None)
            self._cleanup_tmp(book)

    def _record_failure(self, bid: str, *, rapid: bool) -> None:
        if rapid:
            now = time.time()
            self._rapid_fail_times = [t for t in self._rapid_fail_times if now - t < _RAPID_FAIL_WINDOW]
            self._rapid_fail_times.append(now)
            if len(self._rapid_fail_times) >= _RAPID_FAIL_THRESHOLD:
                logger.warning("rapid failures detected, entering quota mode for %s", bid)
                self._enter_quota_mode(bid)
                return

        with self._lock:
            count = self._fail_count.get(bid, 0) + 1
            self._fail_count[bid] = count

        if count >= _MAX_FAILURES:
            logger.error("book %s: %d consecutive failures, marking parse-failed", bid, count)
            self._report_failed(bid)
            if self._alerts:
                self._alerts.write("warn", "qidu_parse_failed", f"book {bid} failed {count} times", source="qidu_parser")
            with self._lock:
                self._fail_count.pop(bid, None)

    def _build_prompt(self, book: dict) -> str:
        bid = book.get("book_id") or book.get("id", "")
        filename = book.get("upload_filename") or book.get("filename", "unknown")
        fmt = book.get("format", "")
        token = self._token
        api_base = self._api_base
        extract_script = self._extract_script

        return (
            f"你是栖读的书籍解析器。你有一个预写好的提取工具可以用。请完成以下工作:\n\n"
            f"## 第一步: 下载文件\n"
            f"curl -H \"Authorization: Bearer {token}\" {api_base}/books/{bid}/file -o /tmp/qidu-parse/{filename}\n\n"
            f"## 第二步: 调用提取工具\n"
            f"python3 {extract_script} /tmp/qidu-parse/{filename} {fmt} /tmp/qidu-parse/{bid}/\n"
            f"这会生成:\n"
            f"- /tmp/qidu-parse/{bid}/text.txt (纯文本)\n"
            f"- /tmp/qidu-parse/{bid}/cover.jpg (封面, 可能没有)\n"
            f"如果工具报错, 查看 stderr 错误信息, 尝试诊断和修复.\n\n"
            f"## 第三步: 分章分段 (你的核心工作)\n"
            f"读取 text.txt, 分析内容:\n"
            f"- 识别章节标题 (如 \"第X章\", \"Chapter X\", 数字编号, 或明显的分隔标记)\n"
            f"- 如果没有明显章节结构, 按内容主题合理分章, 每章不超过 200 段\n"
            f"- 每个自然段落作为一个段落, 去除空段落\n"
            f"- 保持原文不做任何修改或缩写\n"
            f"- 从内容或文件名推断书名和作者\n\n"
            f"## 第四步: 检查结果\n"
            f"确认每章都有标题和段落, 段落不为空, 总段落数合理.\n\n"
            f"## 第五步: 推送结果\n"
            f'把结果写入 /tmp/qidu-parse/{bid}/result.json:\n'
            f'{{"title": "...", "author": "...", "chapters": [{{"title": "...", "paragraphs": ["...", ...]}}]}}\n\n'
            f"上传结果 (纯 JSON):\n"
            f"curl -X POST {api_base}/books/{bid}/parse-result \\\n"
            f"  -H \"Authorization: Bearer {token}\" \\\n"
            f"  -H \"Content-Type: application/json\" \\\n"
            f"  -d @/tmp/qidu-parse/{bid}/result.json\n\n"
            f"如果有封面, 单独上传:\n"
            f"curl -X POST {api_base}/books/{bid}/cover \\\n"
            f"  -H \"Authorization: Bearer {token}\" \\\n"
            f"  -F 'cover=@/tmp/qidu-parse/{bid}/cover.jpg'\n\n"
            f"## 第六步: 清理\n"
            f"rm -rf /tmp/qidu-parse/{bid}/ /tmp/qidu-parse/{filename}\n"
        )

    def _cleanup_tmp(self, book: dict) -> None:
        bid = book.get("book_id") or book.get("id", "")
        filename = book.get("upload_filename") or book.get("filename", "")
        try:
            book_dir = os.path.join(_TMP_BASE, bid)
            if os.path.exists(book_dir):
                shutil.rmtree(book_dir, ignore_errors=True)
        except Exception as e:
            logger.warning("cleanup_tmp dir error for %s: %s", bid, e)
        if filename:
            try:
                file_path = os.path.join(_TMP_BASE, filename)
                if os.path.exists(file_path):
                    os.remove(file_path)
            except Exception as e:
                logger.warning("cleanup_tmp file error for %s: %s", filename, e)

    # ── quota ──────────────────────────────────────────────────────────────────

    def _enter_quota_mode(self, bid: str) -> None:
        self._quota_exhausted = True
        self._last_quota_probe = time.time()
        self._report_quota(bid)
        logger.warning("entered quota mode due to book %s", bid)
        if self._alerts:
            self._alerts.write("warn", "qidu_quota_exhausted", f"quota exhausted, pausing parse (book={bid})", source="qidu_parser")

    def _maybe_probe_quota(self) -> None:
        now = time.time()
        if now - self._last_quota_probe < _QUOTA_PROBE_INTERVAL:
            return
        self._last_quota_probe = now
        if self._probe_quota():
            logger.info("quota probe succeeded, resuming")
            self._quota_exhausted = False
            self._rapid_fail_times.clear()
            self._retry_quota()

    def _probe_quota(self) -> bool:
        cmd = [
            self._binary,
            "--model", "sonnet",
            "--output-format", "stream-json",
            "--input-format", "stream-json",
            "--permission-mode", "bypassPermissions",
            "--setting-sources", "",
            "--max-turns", "1",
        ]
        probe_msg = json.dumps({
            "type": "user",
            "message": {"role": "user", "content": "ping"},
        }) + "\n"
        try:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,
            )
            proc.stdin.write(probe_msg)
            proc.stdin.flush()
            deadline = time.time() + 60
            for line in proc.stdout:
                if time.time() > deadline:
                    proc.kill()
                    return False
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if event.get("type") == "result":
                    try:
                        proc.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                    return True
            proc.kill()
            return False
        except Exception as e:
            logger.warning("probe_quota error: %s", e)
            return False
