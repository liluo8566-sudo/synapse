"""TG outbound resilience: 429 RetryAfter handling, wrapped fallback so a
fallback failure can't kill the turn, media send returning bool, and config
defaults."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from telegram.error import RetryAfter

from synapse_core.debounce import InboundBuffer
from synapse_tg.config import TgConfig, load_config
from synapse_tg.loop import TgLoop
from synapse_tg.media.outbound import send_media


class RecordingAlerts:
    def __init__(self) -> None:
        self.written: list[dict] = []

    def write(self, severity, kind, message, source="", *, fingerprint=None):
        self.written.append(
            {"severity": severity, "kind": kind, "message": message,
             "source": source, "fingerprint": fingerprint}
        )
        return Path("/dev/null")


class FakeBot:
    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.media: list[tuple] = []

    async def send_message(self, **kwargs):
        self.sent.append(kwargs)
        return type("SentMessage", (), {"message_id": len(self.sent)})()

    async def send_chat_action(self, **_kwargs):
        return None

    async def edit_message_text(self, **kwargs):
        return type("SentMessage", (), {"message_id": kwargs.get("message_id")})()

    async def send_photo(self, **kwargs):
        self.media.append(("photo", kwargs))
        return type("SentMessage", (), {"message_id": 1})()


@pytest.fixture(autouse=True)
def no_real_sleep(monkeypatch):
    slept: list[float] = []

    async def fake_sleep(sec):
        slept.append(sec)

    monkeypatch.setattr("synapse_tg.loop.asyncio.sleep", fake_sleep)
    monkeypatch.setattr("synapse_tg.media.outbound.asyncio.sleep", fake_sleep)
    return slept


def _loop(tmp_path: Path, alerts=None) -> TgLoop:
    cfg = TgConfig(data_dir=tmp_path / "tg-data")
    return TgLoop(cfg, alerts=alerts)


# --- text bubble helper ---------------------------------------------------

def test_retry_after_sleeps_stated_seconds_then_succeeds(tmp_path, no_real_sleep):
    loop = _loop(tmp_path)
    bot = FakeBot()
    calls = {"n": 0}

    async def send_message(**kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RetryAfter(7)
        bot.sent.append(kwargs)
        return type("M", (), {"message_id": 1})()

    bot.send_message = send_message  # type: ignore[assignment]

    ok = asyncio.run(
        loop._send_text_bubble(bot, {"chat_id": 1, "text": "hi"}, {"chat_id": 1, "text": "hi"})
    )
    assert ok is True
    assert calls["n"] == 2
    # slept exactly retry_after + margin
    assert no_real_sleep == [7 + 0.5]


def test_retry_after_over_cap_fails_immediately(tmp_path, no_real_sleep):
    loop = _loop(tmp_path)
    bot = FakeBot()

    async def send_message(**kwargs):
        raise RetryAfter(120)  # > cap 60

    bot.send_message = send_message  # type: ignore[assignment]

    ok = asyncio.run(
        loop._send_text_bubble(bot, {"chat_id": 1, "text": "hi"}, {"chat_id": 1, "text": "hi"})
    )
    assert ok is False
    assert no_real_sleep == []  # never waited


def test_fallback_raise_is_caught_returns_false(tmp_path):
    loop = _loop(tmp_path)
    bot = FakeBot()

    async def send_message(**kwargs):
        raise RuntimeError("boom")  # both primary and fallback fail

    bot.send_message = send_message  # type: ignore[assignment]

    # Must NOT raise; returns False.
    ok = asyncio.run(
        loop._send_text_bubble(bot, {"chat_id": 1, "text": "hi"}, {"chat_id": 1, "text": "hi"})
    )
    assert ok is False


def test_fallback_recovers_when_primary_fails(tmp_path):
    loop = _loop(tmp_path)
    bot = FakeBot()
    calls = {"n": 0}

    async def send_message(**kwargs):
        calls["n"] += 1
        if "parse_mode" in kwargs:
            raise RuntimeError("bad html")
        bot.sent.append(kwargs)
        return type("M", (), {"message_id": 1})()

    bot.send_message = send_message  # type: ignore[assignment]

    ok = asyncio.run(
        loop._send_text_bubble(
            bot,
            {"chat_id": 1, "text": "<b>hi", "parse_mode": "HTML"},
            {"chat_id": 1, "text": "hi"},
        )
    )
    assert ok is True
    assert bot.sent == [{"chat_id": 1, "text": "hi"}]


# --- media --------------------------------------------------------------

def test_send_media_returns_true_on_success(tmp_path, no_real_sleep):
    f = tmp_path / "pic.jpg"
    f.write_bytes(b"x")
    bot = FakeBot()
    ok = asyncio.run(send_media(bot, 1, "image", str(f)))
    assert ok is True
    assert bot.media and bot.media[0][0] == "photo"


def test_send_media_returns_false_on_failure(tmp_path, no_real_sleep):
    f = tmp_path / "pic.jpg"
    f.write_bytes(b"x")
    bot = FakeBot()

    async def send_photo(**kwargs):
        raise RuntimeError("api down")

    bot.send_photo = send_photo  # type: ignore[assignment]
    ok = asyncio.run(send_media(bot, 1, "image", str(f)))
    assert ok is False


def test_send_media_missing_file_returns_false(tmp_path):
    bot = FakeBot()
    ok = asyncio.run(send_media(bot, 1, "image", str(tmp_path / "nope.jpg")))
    assert ok is False


def test_send_media_retry_after_over_cap_fails(tmp_path, no_real_sleep):
    f = tmp_path / "pic.jpg"
    f.write_bytes(b"x")
    bot = FakeBot()

    async def send_photo(**kwargs):
        raise RetryAfter(200)

    bot.send_photo = send_photo  # type: ignore[assignment]
    ok = asyncio.run(send_media(bot, 1, "image", str(f), retry_after_cap_sec=60.0))
    assert ok is False
    assert no_real_sleep == []


# --- config defaults ----------------------------------------------------

def test_config_send_defaults():
    cfg = TgConfig()
    assert cfg.send_retry_max == 2
    assert cfg.retry_after_cap_sec == 60.0


def test_config_send_overrides(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text(
        "[send]\nsend_retry_max = 4\nretry_after_cap_sec = 30\n"
    )
    cfg = load_config(p)
    assert cfg.send_retry_max == 4
    assert cfg.retry_after_cap_sec == 30.0


# --- check_flush integration: no exception escapes, alert + break ---------

class FailBot(FakeBot):
    async def send_message(self, **kwargs):
        raise RuntimeError("all sends fail")


def test_check_flush_send_failure_stops_bubbles_and_alerts(tmp_path, monkeypatch):
    clock = _FakeClock()
    alerts = RecordingAlerts()
    loop = _loop(tmp_path, alerts=alerts)
    loop._buffer = InboundBuffer(clock=clock)
    loop._buffer.add("hello")
    clock.now += 60.0  # past debounce quiet window
    loop._pending_chat_id = 123
    loop._bot = FailBot()

    # Stub provider + streaming so we reach the bubble loop with a real response.
    monkeypatch.setattr(loop, "ensure_provider", lambda: None)
    loop._provider = type("P", (), {"session_id": None, "send": lambda self, b: None})()

    async def fake_stream(bot, chat_id, typing):
        return "bubble one\n\nbubble two\n\nbubble three", "", None

    monkeypatch.setattr(loop, "_stream_response", fake_stream)
    monkeypatch.setattr("synapse_tg.loop.asyncio.to_thread", _immediate)
    monkeypatch.setattr(
        "synapse_tg.loop.split_for_tg_typed",
        lambda text: [
            {"kind": "text", "text": "bubble one"},
            {"kind": "text", "text": "bubble two"},
            {"kind": "text", "text": "bubble three"},
        ],
    )

    class Ctx:
        bot = loop._bot

    # Must not raise even though every send fails.
    asyncio.run(loop.check_flush(Ctx()))

    # Alert fired exactly once at the first failed bubble; the rest were stopped.
    assert len(alerts.written) == 1
    a = alerts.written[0]
    assert a["kind"] == "tg_send_rejected"
    assert a["severity"] == "warn"
    assert a["fingerprint"] == "tg.send_rejected"
    assert "1/3" in a["message"]


async def _immediate(fn, *args):
    return fn(*args)


class _FakeClock:
    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now


# --- stream final-edit handoff --------------------------------------------

def _stream_check_flush_loop(tmp_path, monkeypatch, bot, alerts=None):
    """Drive check_flush with a streaming stream_msg_id set, three text bubbles."""
    clock = _FakeClock()
    loop = _loop(tmp_path, alerts=alerts)
    loop._buffer = InboundBuffer(clock=clock)
    loop._buffer.add("hello")
    clock.now += 60.0
    loop._pending_chat_id = 123
    loop._bot = bot

    monkeypatch.setattr(loop, "ensure_provider", lambda: None)
    loop._provider = type("P", (), {"session_id": None, "send": lambda self, b: None})()

    async def fake_stream(bot_, chat_id, typing):
        return "bubble one\n\nbubble two\n\nbubble three", "", 999  # stream_msg_id=999

    monkeypatch.setattr(loop, "_stream_response", fake_stream)
    monkeypatch.setattr("synapse_tg.loop.asyncio.to_thread", _immediate)
    monkeypatch.setattr(
        "synapse_tg.loop.split_for_tg_typed",
        lambda text: [
            {"kind": "text", "text": "bubble one"},
            {"kind": "text", "text": "bubble two"},
            {"kind": "text", "text": "bubble three"},
        ],
    )
    return loop


class EditFailBot(FakeBot):
    """Both HTML and plain-text final edits fail; normal sends succeed."""

    async def edit_message_text(self, **kwargs):
        raise RuntimeError("message to edit not found")


def test_stream_final_edit_failure_resends_bubble_zero_as_new_message(tmp_path, monkeypatch, caplog):
    bot = EditFailBot()
    loop = _stream_check_flush_loop(tmp_path, monkeypatch, bot)

    class Ctx:
        pass

    Ctx.bot = bot

    with caplog.at_level("WARNING", logger="synapse_tg.loop"):
        asyncio.run(loop.check_flush(Ctx()))

    # bubble 0 was sent as a fresh message (not silently dropped), remaining
    # bubbles proceed in order.
    assert [m["text"] for m in bot.sent] == [
        "bubble one",  # gfm_to_tg_html passthrough for plain text
        "bubble two",
        "bubble three",
    ]
    assert any(
        "stream final edit failed" in r.message for r in caplog.records
    )


class EditOkBot(FakeBot):
    def __init__(self) -> None:
        super().__init__()
        self.edits: list[dict] = []

    async def edit_message_text(self, **kwargs):
        self.edits.append(kwargs)
        return type("M", (), {"message_id": kwargs.get("message_id")})()


def test_stream_final_edit_success_skips_bubble_zero_unchanged(tmp_path, monkeypatch):
    bot = EditOkBot()
    loop = _stream_check_flush_loop(tmp_path, monkeypatch, bot)

    class Ctx:
        pass

    Ctx.bot = bot

    asyncio.run(loop.check_flush(Ctx()))

    # Preview edited once (HTML path succeeds), bubble 0 NOT re-sent as a new message.
    assert len(bot.edits) == 1
    assert [m["text"] for m in bot.sent] == ["bubble two", "bubble three"]


class NotModifiedBothBot(FakeBot):
    """Both HTML and plain-text final edits fail with 'message is not modified'
    (preview already matches bubble 0 — content was already delivered)."""

    async def edit_message_text(self, **kwargs):
        raise RuntimeError("Message is not modified")


def test_stream_final_edit_not_modified_both_does_not_resend_bubble_zero(tmp_path, monkeypatch):
    bot = NotModifiedBothBot()
    loop = _stream_check_flush_loop(tmp_path, monkeypatch, bot)

    class Ctx:
        pass

    Ctx.bot = bot

    asyncio.run(loop.check_flush(Ctx()))

    # bubble 0 already delivered via the streamed preview — must NOT be re-sent.
    assert [m["text"] for m in bot.sent] == ["bubble two", "bubble three"]


class NotModifiedHtmlOnlyBot(FakeBot):
    """HTML final edit fails with 'message is not modified'; plain-text retry
    must be skipped entirely (no second edit call)."""

    def __init__(self) -> None:
        super().__init__()
        self.edit_calls = 0

    async def edit_message_text(self, **kwargs):
        self.edit_calls += 1
        raise RuntimeError("Message is not modified")


def test_stream_final_edit_not_modified_html_skips_plain_retry(tmp_path, monkeypatch):
    bot = NotModifiedHtmlOnlyBot()
    loop = _stream_check_flush_loop(tmp_path, monkeypatch, bot)

    class Ctx:
        pass

    Ctx.bot = bot

    asyncio.run(loop.check_flush(Ctx()))

    # Only the HTML edit attempt runs — no fallthrough to a plain-text retry.
    assert bot.edit_calls == 1
    assert [m["text"] for m in bot.sent] == ["bubble two", "bubble three"]
