"""Pure-logic + wiring tests for synapse_tg.effects (Telegram message
effects) and the spoiler attribute on outbound media tags. No bot, no
network for the pure logic; fake bots below for the wiring tests."""

from __future__ import annotations

import asyncio
import types

import pytest
from telegram.error import BadRequest

from synapse_tg import effects
from synapse_tg.config import TgConfig
from synapse_tg.loop import TgLoop
from synapse_tg.media.outbound import send_media
from synapse_tg.split import split_for_tg_typed

# ── effects.py: emoji normalisation ─────────────────────────────────────────


def test_normalize_strips_fe0f_from_input():
    assert effects.normalize_effect_emoji("❤️") == "❤"  # ❤️ -> ❤


def test_normalize_bare_heart_is_canonical():
    assert effects.normalize_effect_emoji("❤") == "❤"


def test_normalize_not_in_table_returns_none():
    assert effects.normalize_effect_emoji("\U0001f9ca") is None  # 🧊


def test_normalize_empty_returns_none():
    assert effects.normalize_effect_emoji("") is None


def test_effect_id_for_each_whitelisted_emoji():
    assert effects.effect_id_for("\U0001f525") == "5104841245755180586"  # 🔥
    assert effects.effect_id_for("\U0001f44d") == "5107584321108051014"  # 👍
    assert effects.effect_id_for("\U0001f44e") == "5104858069142078462"  # 👎
    assert effects.effect_id_for("❤️") == "5159385139981059251"  # ❤️
    assert effects.effect_id_for("\U0001f389") == "5046509860389126442"  # 🎉
    assert effects.effect_id_for("\U0001f4a9") == "5046589136895476101"  # 💩


def test_effect_id_for_not_in_table_returns_none():
    assert effects.effect_id_for("\U0001f9ca") is None


# ── effects.py: <effect> tag parsing / stripping ────────────────────────────


def test_extract_default_tag():
    text, tags = effects.extract_effect_tags("hi <effect>\U0001f389</effect> there")
    assert text == "hi  there"
    assert tags == ["\U0001f389"]


def test_extract_no_tag_unchanged():
    text, tags = effects.extract_effect_tags("just a normal reply")
    assert text == "just a normal reply"
    assert tags == []


def test_extract_multiple_tags_keeps_order_for_caller_to_pick_first():
    text, tags = effects.extract_effect_tags(
        "<effect>\U0001f525</effect>hi<effect>\U0001f389</effect>"
    )
    assert text == "hi"
    assert tags == ["\U0001f525", "\U0001f389"]


def test_extract_only_tag_leaves_empty_text():
    text, tags = effects.extract_effect_tags("<effect>\U0001f389</effect>")
    assert text == ""
    assert tags == ["\U0001f389"]


def test_extract_strips_whitespace_in_payload():
    text, tags = effects.extract_effect_tags("<effect> \U0001f389 </effect>ok")
    assert tags == ["\U0001f389"]
    assert text == "ok"


# ── split.py: spoiler attribute on media tags ───────────────────────────────


def test_image_tag_no_spoiler_has_no_flag():
    bubbles = split_for_tg_typed('<image path="/a.png">')
    assert bubbles == [{"kind": "image", "path": "/a.png"}]


def test_image_tag_bare_spoiler_self_closing():
    bubbles = split_for_tg_typed('<image path="/a.png" spoiler/>')
    assert bubbles[0] == {"kind": "image", "path": "/a.png", "spoiler": True}


def test_image_tag_bare_spoiler_no_slash():
    bubbles = split_for_tg_typed('<image path="/a.png" spoiler>')
    assert bubbles[0]["spoiler"] is True


def test_image_tag_spoiler_with_value():
    bubbles = split_for_tg_typed('<image path="/a.png" spoiler="true"/>')
    assert bubbles[0]["spoiler"] is True


def test_gif_and_video_tags_accept_spoiler():
    bubbles = split_for_tg_typed(
        '<gif path="/b.gif" spoiler/>\n\n<video path="/c.mp4" spoiler/>'
    )
    by_kind = {b["kind"]: b for b in bubbles}
    assert by_kind["gif"]["spoiler"] is True
    assert by_kind["video"]["spoiler"] is True


def test_file_tag_with_spoiler_still_parses():
    # Parsing doesn't discriminate by kind; send_media (outbound) is what
    # ignores the flag for file.
    bubbles = split_for_tg_typed('<file path="/d.pdf" spoiler/>')
    assert bubbles[0] == {"kind": "file", "path": "/d.pdf", "spoiler": True}


# ── send_media: spoiler + effect_id + BadRequest retry ──────────────────────


class _MediaBot:
    def __init__(self):
        self.calls: list[tuple] = []
        self.fail_once_with: Exception | None = None

    async def _record(self, kind, kwargs):
        if self.fail_once_with is not None:
            exc, self.fail_once_with = self.fail_once_with, None
            raise exc
        self.calls.append((kind, kwargs))
        return types.SimpleNamespace(message_id=1)

    async def send_photo(self, **kwargs):
        return await self._record("photo", kwargs)

    async def send_animation(self, **kwargs):
        return await self._record("animation", kwargs)

    async def send_video(self, **kwargs):
        return await self._record("video", kwargs)

    async def send_document(self, **kwargs):
        return await self._record("document", kwargs)


@pytest.fixture(autouse=True)
def no_real_sleep(monkeypatch):
    async def fake_sleep(_sec):
        return None

    monkeypatch.setattr("synapse_tg.media.outbound.asyncio.sleep", fake_sleep)
    monkeypatch.setattr("synapse_tg.loop.asyncio.sleep", fake_sleep)


def test_send_media_spoiler_passed_to_photo(tmp_path):
    f = tmp_path / "a.png"
    f.write_bytes(b"x")
    bot = _MediaBot()
    asyncio.run(send_media(bot, 1, "image", str(f), spoiler=True))
    kind, kwargs = bot.calls[0]
    assert kind == "photo"
    assert kwargs["chat_id"] == 1
    assert kwargs["has_spoiler"] is True


def test_send_media_spoiler_passed_to_video(tmp_path):
    f = tmp_path / "a.mp4"
    f.write_bytes(b"x")
    bot = _MediaBot()
    asyncio.run(send_media(bot, 1, "video", str(f), spoiler=True))
    assert bot.calls[0][1]["has_spoiler"] is True


def test_send_media_small_gif_as_photo_keeps_spoiler(tmp_path, monkeypatch):
    f = tmp_path / "a.gif"
    f.write_bytes(b"x")
    monkeypatch.setattr("synapse_tg.media.outbound._gif_dimensions", lambda _p: (100, 100))
    bot = _MediaBot()
    asyncio.run(send_media(bot, 1, "gif", str(f), spoiler=True))
    assert bot.calls[0][0] == "photo"
    assert bot.calls[0][1]["has_spoiler"] is True


def test_send_media_file_ignores_spoiler(tmp_path):
    f = tmp_path / "d.pdf"
    f.write_bytes(b"x")
    bot = _MediaBot()
    asyncio.run(send_media(bot, 1, "file", str(f), spoiler=True))
    assert "has_spoiler" not in bot.calls[0][1]


def test_send_media_effect_id_passed(tmp_path):
    f = tmp_path / "a.png"
    f.write_bytes(b"x")
    bot = _MediaBot()
    asyncio.run(send_media(bot, 1, "image", str(f), effect_id="123"))
    assert bot.calls[0][1]["message_effect_id"] == "123"


def test_send_media_bad_request_with_effect_retries_without_it(tmp_path):
    f = tmp_path / "a.png"
    f.write_bytes(b"x")
    bot = _MediaBot()
    bot.fail_once_with = BadRequest("MESSAGE_EFFECT_ID_INVALID")
    sent = asyncio.run(send_media(bot, 1, "image", str(f), effect_id="123"))
    assert sent is not None
    kind, kwargs = bot.calls[0]
    assert kind == "photo"
    assert kwargs["chat_id"] == 1
    assert "message_effect_id" not in kwargs


def test_send_media_bad_request_without_effect_gives_up(tmp_path):
    f = tmp_path / "a.png"
    f.write_bytes(b"x")
    bot = _MediaBot()
    bot.fail_once_with = BadRequest("boom")
    sent = asyncio.run(send_media(bot, 1, "image", str(f)))
    assert sent is None
    assert bot.calls == []


def test_send_media_bad_request_with_effect_retry_also_fails(tmp_path):
    f = tmp_path / "a.png"
    f.write_bytes(b"x")

    class _AlwaysFail(_MediaBot):
        async def send_photo(self, **kwargs):
            raise BadRequest("still bad")

    bot = _AlwaysFail()
    sent = asyncio.run(send_media(bot, 1, "image", str(f), effect_id="123"))
    assert sent is None


# ── _deliver_reply: effect wiring (private-chat only, first bubble) ────────


def _owner_loop(tmp_path, **cfg_kwargs) -> TgLoop:
    cfg = TgConfig(data_dir=tmp_path / "tg-data", allowed_user_ids=[1], **cfg_kwargs)
    return TgLoop(cfg)


class _FakeBot:
    def __init__(self):
        self.sent: list[dict] = []
        self.media: list[tuple] = []
        self.send_message_fail_once_with: Exception | None = None

    async def send_message(self, **kwargs):
        if self.send_message_fail_once_with is not None:
            exc, self.send_message_fail_once_with = self.send_message_fail_once_with, None
            raise exc
        self.sent.append(kwargs)
        return types.SimpleNamespace(message_id=len(self.sent))

    async def send_photo(self, **kwargs):
        self.media.append(("photo", kwargs))
        return types.SimpleNamespace(message_id=1)


def test_deliver_reply_effect_attaches_to_first_text_bubble(tmp_path):
    loop = _owner_loop(tmp_path)
    bot = _FakeBot()
    asyncio.run(loop._deliver_reply(bot, 777, "<effect>\U0001f389</effect>hello", ""))
    assert bot.sent[0]["message_effect_id"] == "5046509860389126442"


def test_deliver_reply_effect_only_on_first_of_multiple_bubbles(tmp_path, monkeypatch):
    loop = _owner_loop(tmp_path)
    bot = _FakeBot()
    monkeypatch.setattr(
        "synapse_tg.loop.split_for_tg_typed",
        lambda text: [
            {"kind": "text", "text": "bubble one"},
            {"kind": "text", "text": "bubble two"},
        ],
    )
    asyncio.run(loop._deliver_reply(bot, 777, "<effect>\U0001f525</effect>ignored", ""))
    assert bot.sent[0]["message_effect_id"] == "5104841245755180586"
    assert "message_effect_id" not in bot.sent[1]


def test_deliver_reply_effect_dropped_in_group_chat(tmp_path):
    loop = _owner_loop(tmp_path)
    bot = _FakeBot()
    asyncio.run(loop._deliver_reply(bot, -100123, "<effect>\U0001f389</effect>hi", ""))
    assert "message_effect_id" not in bot.sent[0]
    assert bot.sent[0]["text"] == "hi"


def test_deliver_reply_effect_not_in_whitelist_dropped(tmp_path):
    loop = _owner_loop(tmp_path)
    bot = _FakeBot()
    asyncio.run(loop._deliver_reply(bot, 777, "<effect>\U0001f9ca</effect>hi", ""))
    assert "message_effect_id" not in bot.sent[0]
    assert bot.sent[0]["text"] == "hi"


def test_deliver_reply_effect_only_tag_sends_nothing(tmp_path):
    loop = _owner_loop(tmp_path)
    bot = _FakeBot()
    asyncio.run(loop._deliver_reply(bot, 777, "<effect>\U0001f389</effect>", ""))
    assert bot.sent == []
    assert bot.media == []


def test_deliver_reply_effect_attaches_to_first_media_bubble(tmp_path, monkeypatch):
    loop = _owner_loop(tmp_path)
    bot = _FakeBot()
    f = tmp_path / "a.png"
    f.write_bytes(b"x")
    monkeypatch.setattr(
        "synapse_tg.loop.split_for_tg_typed",
        lambda text: [{"kind": "image", "path": str(f)}],
    )
    asyncio.run(loop._deliver_reply(bot, 777, "<effect>\U0001f389</effect>placeholder", ""))
    assert bot.media[0][1]["message_effect_id"] == "5046509860389126442"


def test_deliver_reply_bad_request_with_effect_retries_without_it(tmp_path):
    loop = _owner_loop(tmp_path)
    bot = _FakeBot()
    bot.send_message_fail_once_with = BadRequest("MESSAGE_EFFECT_ID_INVALID")
    asyncio.run(loop._deliver_reply(bot, 777, "<effect>\U0001f389</effect>hello", ""))
    assert len(bot.sent) == 1
    assert bot.sent[0]["text"] == "hello"
    assert "message_effect_id" not in bot.sent[0]
