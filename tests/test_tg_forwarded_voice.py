"""Forwarded-own-voice recognition: voice_ledger (record/lookup) and
TgLoop.on_voice (ownership check + buffered line), plus the send-path hook
that records the ledger after a successful voice send."""

from __future__ import annotations

import asyncio
import json
import types
from datetime import datetime, timezone

from telegram import MessageOriginUser, User

from synapse_tg import voice_ledger
from synapse_tg.config import TgConfig
from synapse_tg.loop import TgLoop

BOT_ID = 999


# =============================================================================
# voice_ledger: record/lookup
# =============================================================================


def _sent(*, chat_id=1, message_id=5, file_unique_id="uid", date=None):
    date = date or datetime(2026, 10, 15, 1, 2, 3, tzinfo=timezone.utc)
    return types.SimpleNamespace(
        chat_id=chat_id,
        message_id=message_id,
        date=date,
        voice=types.SimpleNamespace(file_unique_id=file_unique_id),
    )


def test_record_then_lookup_by_file_unique_id(tmp_path):
    path = tmp_path / "sent_voices.jsonl"
    voice_ledger.record(path, _sent(file_unique_id="uid-1"), "hello there")
    entry = voice_ledger.lookup(path, file_unique_id="uid-1")
    assert entry is not None
    assert entry["text"] == "hello there"
    assert entry["chat_id"] == 1
    assert entry["message_id"] == 5


def test_lookup_falls_back_to_date_when_uid_misses(tmp_path):
    path = tmp_path / "sent_voices.jsonl"
    when = datetime(2026, 10, 15, 1, 2, 3, tzinfo=timezone.utc)
    voice_ledger.record(path, _sent(file_unique_id="uid-2", date=when), "said this")
    entry = voice_ledger.lookup(path, file_unique_id="not-there", date=int(when.timestamp()))
    assert entry is not None
    assert entry["text"] == "said this"


def test_lookup_returns_none_on_total_miss(tmp_path):
    path = tmp_path / "sent_voices.jsonl"
    # File does not even exist yet.
    assert voice_ledger.lookup(path, file_unique_id="nope") is None
    voice_ledger.record(path, _sent(file_unique_id="uid-3"), "text")
    assert voice_ledger.lookup(path, file_unique_id="nope", date=1) is None


def test_lookup_skips_corrupt_line(tmp_path):
    path = tmp_path / "sent_voices.jsonl"
    good = json.dumps(
        {"file_unique_id": "good", "date": 1, "text": "ok", "chat_id": 1, "message_id": 1}
    )
    path.write_text(f"not json at all\n{good}\n")
    entry = voice_ledger.lookup(path, file_unique_id="good")
    assert entry is not None
    assert entry["text"] == "ok"


def test_record_trims_to_newest_cap(tmp_path):
    path = tmp_path / "sent_voices.jsonl"
    # Pre-seed one line past the trim threshold so a single further record()
    # call is what tips it over.
    preseeded = voice_ledger._TRIM_AT
    lines = [
        json.dumps({"file_unique_id": f"old-{i}", "date": i, "text": "x",
                     "chat_id": 1, "message_id": i})
        for i in range(preseeded)
    ]
    path.write_text("\n".join(lines) + "\n")
    voice_ledger.record(path, _sent(file_unique_id="newest"), "latest")
    remaining = path.read_text().splitlines()
    assert len(remaining) == voice_ledger._CAP
    dropped = preseeded + 1 - voice_ledger._CAP  # entries evicted from the front
    assert voice_ledger.lookup(path, file_unique_id="old-0") is None
    assert voice_ledger.lookup(path, file_unique_id=f"old-{dropped - 1}") is None
    assert voice_ledger.lookup(path, file_unique_id=f"old-{dropped}") is not None
    assert voice_ledger.lookup(path, file_unique_id="newest") is not None


def test_record_write_failure_is_swallowed(tmp_path, caplog):
    blocker = tmp_path / "blocker"
    blocker.write_text("i am a file, not a directory")
    path = blocker / "sub" / "sent_voices.jsonl"  # mkdir(parents=True) must fail
    with caplog.at_level("WARNING", logger="synapse_tg.voice_ledger"):
        voice_ledger.record(path, _sent(), "text")  # must not raise
    assert not path.exists()
    assert any("voice_ledger.record failed" in r.message for r in caplog.records)


# =============================================================================
# TgLoop.on_voice
# =============================================================================


def _bot():
    return types.SimpleNamespace(id=BOT_ID, username="mybot")


def _origin(uid, when):
    return MessageOriginUser(date=when, sender_user=User(id=uid, first_name="u", is_bot=(uid == BOT_ID)))


def _voice_msg(
    *,
    message_id=1,
    chat_id=777,
    chat_type="private",
    chat_title="g",
    from_uid=1,
    caption=None,
    file_unique_id="fu-1",
    forward_origin=None,
):
    return types.SimpleNamespace(
        message_id=message_id,
        chat_id=chat_id,
        chat=types.SimpleNamespace(type=chat_type, title=chat_title, id=chat_id),
        from_user=types.SimpleNamespace(first_name="Her", id=from_uid),
        reply_to_message=None,
        text=None,
        caption=caption,
        photo=None,
        sticker=None,
        animation=None,
        video=None,
        document=None,
        voice=types.SimpleNamespace(file_unique_id=file_unique_id, file_id="fid-" + file_unique_id),
        forward_origin=forward_origin,
    )


class _FakeCtx:
    def __init__(self, bot):
        self.bot = bot


def _priv_loop(tmp_path, **cfg_kwargs) -> TgLoop:
    cfg = TgConfig(data_dir=tmp_path / "tg-data", allowed_user_ids=[1], **cfg_kwargs)
    return TgLoop(cfg)


def _group_loop(tmp_path, **cfg_kwargs) -> TgLoop:
    cfg = TgConfig(
        data_dir=tmp_path / "tg-data",
        group_ids=[-100123],
        group_forward_allow_ids=[111],
        group_mention_keywords=[],
        **cfg_kwargs,
    )
    return TgLoop(cfg)


def test_on_voice_private_forwarded_own_ledger_hit(tmp_path):
    loop = _priv_loop(tmp_path)
    when = datetime(2026, 10, 15, 9, 30, tzinfo=timezone.utc)
    voice_ledger.record(
        loop._voice_ledger_path,
        _sent(file_unique_id="uid-hit", date=when),
        "hi from the bot",
    )
    msg = _voice_msg(file_unique_id="uid-hit", forward_origin=_origin(BOT_ID, when))
    update = types.SimpleNamespace(message=msg)
    asyncio.run(loop.on_voice(update, _FakeCtx(_bot())))

    buffered = loop._private_buffer.flush()
    assert buffered.count("forwarded your own voice message") == 1
    assert "hi from the bot" in buffered
    assert "10-15 " in buffered  # MM-DD local-time stamp present


def test_on_voice_group_forwarded_own_ledger_hit_from_allowed_forwarder(tmp_path):
    loop = _group_loop(tmp_path)
    when = datetime(2026, 10, 15, 9, 30, tzinfo=timezone.utc)
    voice_ledger.record(
        loop._voice_ledger_path,
        _sent(file_unique_id="uid-grp", date=when),
        "group-bound voice",
    )
    msg = _voice_msg(
        chat_type="group", chat_id=-100123, from_uid=111,  # 111 is in group_forward_allow_ids
        file_unique_id="uid-grp", forward_origin=_origin(BOT_ID, when),
    )
    update = types.SimpleNamespace(message=msg)
    asyncio.run(loop.on_voice(update, _FakeCtx(_bot())))

    buf = loop._group_buffers.get(-100123)
    assert buf is not None
    buffered = buf.flush()
    assert buffered.count("forwarded your own voice message") == 1
    assert "group-bound voice" in buffered


def test_on_voice_bot_origin_ledger_miss_time_only_line(tmp_path):
    """Voice sent before the ledger existed: forward_origin names the bot but
    there is no ledger entry — time-only line, no text."""
    loop = _priv_loop(tmp_path)
    when = datetime(2026, 9, 1, 8, 0, tzinfo=timezone.utc)
    msg = _voice_msg(file_unique_id="uid-unknown", forward_origin=_origin(BOT_ID, when))
    update = types.SimpleNamespace(message=msg)
    asyncio.run(loop.on_voice(update, _FakeCtx(_bot())))

    buffered = loop._private_buffer.flush()
    assert "forwarded your own voice message" in buffered
    assert "not in the voice ledger" in buffered
    assert "09-01 " in buffered


def test_on_voice_users_own_non_forwarded_voice_ignored(tmp_path):
    loop = _priv_loop(tmp_path)
    msg = _voice_msg(file_unique_id="uid-fresh", forward_origin=None)
    update = types.SimpleNamespace(message=msg)
    asyncio.run(loop.on_voice(update, _FakeCtx(_bot())))
    assert loop._private_buffer.flush() == ""
    assert 1 not in loop._private_msgs


def test_on_voice_forwarded_from_another_user_ignored(tmp_path):
    loop = _priv_loop(tmp_path)
    when = datetime(2026, 10, 15, 9, 30, tzinfo=timezone.utc)
    other_uid = 555
    msg = _voice_msg(file_unique_id="uid-other", forward_origin=_origin(other_uid, when))
    update = types.SimpleNamespace(message=msg)
    asyncio.run(loop.on_voice(update, _FakeCtx(_bot())))
    assert loop._private_buffer.flush() == ""


def test_on_voice_group_forward_from_non_allowed_member_ignored(tmp_path):
    """Gate blocks before the ownership check even fires — true even when the
    voice genuinely is the bot's own (forward_origin names the bot)."""
    loop = _group_loop(tmp_path)
    when = datetime(2026, 10, 15, 9, 30, tzinfo=timezone.utc)
    msg = _voice_msg(
        chat_type="group", chat_id=-100123, from_uid=222,  # not in group_forward_allow_ids
        file_unique_id="uid-blocked", forward_origin=_origin(BOT_ID, when),
    )
    update = types.SimpleNamespace(message=msg)
    asyncio.run(loop.on_voice(update, _FakeCtx(_bot())))
    assert -100123 not in loop._group_buffers


# =============================================================================
# Send path: ledger recorded on real voice send, not on the text fallback
# =============================================================================


class _SendBot:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send_message(self, **kwargs):
        self.sent.append(kwargs)
        return types.SimpleNamespace(message_id=len(self.sent))

    async def send_chat_action(self, **_kwargs):
        return None


def test_deliver_reply_voice_send_records_ledger(tmp_path, monkeypatch):
    loop = _priv_loop(
        tmp_path, tts_enabled=True, tts_api_key="key", tts_voice_id="voice",
    )
    audio_file = tmp_path / "out.mp3"
    audio_file.write_bytes(b"fake-mp3")

    async def fake_synthesize(text, cfg):
        return str(audio_file)

    monkeypatch.setattr("synapse_tg.loop._tts.synthesize", fake_synthesize)

    sent_message = types.SimpleNamespace(
        chat_id=777,
        message_id=42,
        date=datetime(2026, 10, 15, 9, 30, tzinfo=timezone.utc),
        voice=types.SimpleNamespace(file_unique_id="sent-uid"),
    )

    async def fake_send_media(bot, chat_id, kind, path, **kwargs):
        assert kind == "voice"
        return sent_message

    monkeypatch.setattr("synapse_tg.loop.send_media", fake_send_media)

    asyncio.run(loop._deliver_reply(_SendBot(), 777, "<voice>hear this</voice>", ""))

    entry = voice_ledger.lookup(loop._voice_ledger_path, file_unique_id="sent-uid")
    assert entry is not None
    assert entry["text"] == "hear this"


def test_deliver_reply_voice_fallback_does_not_record_ledger(tmp_path, monkeypatch):
    """TTS unavailable (tts_enabled=False, the default) -> plain text send,
    never ledgered."""
    loop = _priv_loop(tmp_path)
    assert not loop._cfg.tts_effective_enabled()

    calls: list[tuple] = []

    async def fake_send_media(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("send_media must not be called on the fallback path")

    monkeypatch.setattr("synapse_tg.loop.send_media", fake_send_media)

    bot = _SendBot()
    asyncio.run(loop._deliver_reply(bot, 777, "<voice>hear this</voice>", ""))

    assert calls == []
    assert len(bot.sent) == 1
    assert not loop._voice_ledger_path.exists()
