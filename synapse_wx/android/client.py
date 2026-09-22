"""HTTP relay client for a real Android WeChat account (AutoJs6 bridge).

Duck-types ILinkClient enough for MainLoop: poll_messages/send_text/
send_typing/extract_text/extract_media/is_logged_in. The phone is the HTTP
client; this class runs the server the phone talks to.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

logger = logging.getLogger(__name__)

_MULTI_PREFIX_RE = re.compile(r"^\[\d+条\]")

_PING_ONLINE_WINDOW_SEC = 60.0


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr: tuple[str, int], android: "AndroidClient") -> None:
        super().__init__(addr, _Handler)
        self.android = android


class _Handler(BaseHTTPRequestHandler):
    server: _Server  # type: ignore[assignment]

    def log_message(self, fmt: str, *args: Any) -> None:
        logger.debug("android http: " + fmt, *args)

    def _authorized(self) -> bool:
        return self.headers.get("X-Token", "") == self.server.android._token

    def _send_json(self, code: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json_body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length) if length else b""
        return json.loads(raw) if raw else {}

    def do_POST(self) -> None:  # noqa: N802
        if not self._authorized():
            self._send_json(401, {"ok": False})
            return
        try:
            body = self._read_json_body()
        except (json.JSONDecodeError, ValueError):
            self._send_json(400, {"ok": False})
            return
        if self.path == "/inbox":
            self.server.android._handle_inbox(body)
            self._send_json(200, {"ok": True})
        elif self.path == "/ack":
            self.server.android._handle_ack(body)
            self._send_json(200, {"ok": True})
        else:
            self._send_json(404, {"ok": False})

    def do_GET(self) -> None:  # noqa: N802
        if not self._authorized():
            self._send_json(401, {"ok": False})
            return
        if self.path == "/outbox":
            self._send_json(200, {"items": self.server.android._handle_outbox()})
        elif self.path == "/ping":
            self._send_json(200, self.server.android._handle_ping())
        else:
            self._send_json(404, {"ok": False})


class AndroidClient:
    """Server side of the phone HTTP relay. Runs a daemon ThreadingHTTPServer."""

    def __init__(
        self,
        listen: str,
        port: int,
        token: str,
        resend_after_sec: float = 120.0,
        allow_chats: list[str] | None = None,
    ) -> None:
        self._token = token
        self._resend_after_sec = max(0.0, resend_after_sec)
        # Notification title allowlist. Empty/None = accept every title
        # (incl. WeChat system senders like 微信团队 / 微信支付).
        self._allow_chats = set(allow_chats) if allow_chats else None
        self._lock = threading.Lock()
        self._inbox: deque[dict] = deque()
        self._outbox: dict[str, dict] = {}
        self._next_id = 1
        self._last_ping_at: float | None = None
        # Overridable in tests: swap for a fake clock to exercise resend timing
        # without sleeping real seconds.
        self._now = time.monotonic

        self._server = _Server((listen, port), self)
        self._thread = threading.Thread(
            target=self._server.serve_forever, name="android-http", daemon=True
        )
        self._thread.start()

    @property
    def server_port(self) -> int:
        return self._server.server_address[1]

    # -- iLink duck-type surface ---------------------------------------------

    @property
    def is_logged_in(self) -> bool:
        return True

    @property
    def phone_online(self) -> bool:
        if self._last_ping_at is None:
            return False
        return (self._now() - self._last_ping_at) < _PING_ONLINE_WINDOW_SEC

    def poll_messages(self) -> list[dict]:
        with self._lock:
            items = list(self._inbox)
            self._inbox.clear()
        return [self._to_ilink_message(item) for item in items]

    def send_text(self, to_user_id: str, context_token: str, text: str) -> bool:
        with self._lock:
            oid = f"o{self._next_id}"
            self._next_id += 1
            self._outbox[oid] = {
                "id": oid,
                "chat": to_user_id,
                "text": text,
                "first_sent_at": None,
                "resent": False,
            }
        return True

    def send_typing(self, to_user_id: str, context_token: str = "") -> None:
        pass  # no typing indicator on a real WeChat account

    @staticmethod
    def extract_text(message: dict) -> str:
        parts = []
        for item in message.get("item_list", []):
            if item.get("type") == 1:
                parts.append(item.get("text_item", {}).get("text", ""))
        return "\n".join(parts)

    @staticmethod
    def extract_media(message: dict) -> list[dict]:
        return []

    def send_image(self, path: str, **kwargs: Any) -> None:
        logger.warning("AndroidClient.send_image not supported yet (V2): %s", path)
        return None

    def send_file(self, path: str, **kwargs: Any) -> None:
        logger.warning("AndroidClient.send_file not supported yet (V2): %s", path)
        return None

    def send_gif(self, path: str, **kwargs: Any) -> None:
        logger.warning("AndroidClient.send_gif not supported yet (V2): %s", path)
        return None

    def send_video(self, path: str, **kwargs: Any) -> None:
        logger.warning("AndroidClient.send_video not supported yet (V2): %s", path)
        return None

    def reconnect(self) -> None:
        pass  # no persistent outbound connection to drop

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    # -- HTTP handlers (called from _Handler on the server thread) ----------

    def _handle_inbox(self, body: dict) -> None:
        title = body.get("title", "")
        if self._allow_chats is not None and title not in self._allow_chats:
            logger.info("android inbox: dropping notification from %r (not in allow_chats)", title)
            return
        with self._lock:
            self._inbox.append(body)

    def _handle_ack(self, body: dict) -> None:
        oid = body.get("id")
        ok = body.get("ok", True)
        err = body.get("err", "")
        with self._lock:
            entry = self._outbox.pop(oid, None)
        if entry is not None and not ok:
            logger.warning("android outbox %s reported send failure: %s", oid, err)

    def _handle_outbox(self) -> list[dict]:
        now = self._now()
        due: list[dict] = []
        drop_ids: list[str] = []
        with self._lock:
            for oid, entry in self._outbox.items():
                if entry["first_sent_at"] is None:
                    entry["first_sent_at"] = now
                    due.append(entry)
                elif (now - entry["first_sent_at"]) >= self._resend_after_sec:
                    if entry["resent"]:
                        drop_ids.append(oid)
                    else:
                        entry["resent"] = True
                        entry["first_sent_at"] = now
                        due.append(entry)
            for oid in drop_ids:
                del self._outbox[oid]
        for oid in drop_ids:
            logger.error("android outbox: dropping unacked item %s after resend timeout", oid)
        return [{"id": e["id"], "chat": e["chat"], "text": e["text"]} for e in due]

    def _handle_ping(self) -> dict:
        now = self._now()
        with self._lock:
            self._last_ping_at = now
        return {"ok": True, "ts": now}

    @staticmethod
    def _to_ilink_message(item: dict) -> dict:
        text = _MULTI_PREFIX_RE.sub("", item.get("text", ""), count=1)
        return {
            "message_type": 1,
            "from_user_id": item.get("title", ""),
            "context_token": "",
            "item_list": [{"type": 1, "text_item": {"text": text}}],
        }
