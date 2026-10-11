"""Pure-logic tests for synapse_tg.reactions: emoji normalisation, <react> tag
parsing/stripping, target resolution, old/new diff, event-line formatting,
summary labels. No bot, no network."""

from __future__ import annotations

import asyncio
import types

from telegram import ReactionTypeEmoji
from telegram.error import RetryAfter

from synapse_tg import reactions
from synapse_tg.config import TgConfig
from synapse_tg.loop import TgLoop

# ── emoji normalisation ────────────────────────────────────────────────────


def test_normalize_strips_fe0f_from_input():
    assert reactions.normalize_emoji("❤️") == "❤"  # ❤️ -> ❤


def test_normalize_bare_heart_matches_canonical():
    assert reactions.normalize_emoji("❤") == "❤"  # ❤ already canonical


def test_normalize_heart_fire_with_fe0f():
    # ❤️‍🔥 as Telegram actually sends it (FE0F present)
    raw = "❤️‍\U0001f525"
    assert reactions.normalize_emoji(raw) == "❤️‍\U0001f525"


def test_normalize_heart_fire_without_fe0f():
    # Same glyph, FE0F omitted by some client — must still match.
    raw = "❤‍\U0001f525"
    assert reactions.normalize_emoji(raw) == "❤️‍\U0001f525"


def test_normalize_shrug_man_with_trailing_fe0f():
    raw = "\U0001f937‍♂️"
    assert reactions.normalize_emoji(raw) == raw


def test_normalize_shrug_man_without_trailing_fe0f():
    raw = "\U0001f937‍♂"
    assert reactions.normalize_emoji(raw) == "\U0001f937‍♂️"


def test_normalize_not_in_whitelist_returns_none():
    assert reactions.normalize_emoji("\U0001f9ca") is None  # 🧊 not a reaction emoji


def test_normalize_empty_string_returns_none():
    assert reactions.normalize_emoji("") is None


def test_allowed_reaction_emoji_nonempty():
    assert len(reactions.ALLOWED_REACTION_EMOJI) == 73


# ── <react> tag parsing / stripping ─────────────────────────────────────────


def test_extract_default_target_tag():
    text, tags = reactions.extract_react_tags("hello <react>❤</react> world")
    assert text == "hello  world"
    assert tags == [reactions.ReactTag(to=None, emoji="❤")]


def test_extract_to_fragment_tag():
    text, tags = reactions.extract_react_tags('<react to="pizza">\U0001f525</react>ok')
    assert text == "ok"
    assert tags == [reactions.ReactTag(to="pizza", emoji="\U0001f525")]


def test_extract_multiple_tags():
    text, tags = reactions.extract_react_tags(
        '<react to="a">\U0001f44d</react>hi<react to="b">\U0001f525</react>'
    )
    assert text == "hi"
    assert tags == [
        reactions.ReactTag(to="a", emoji="\U0001f44d"),
        reactions.ReactTag(to="b", emoji="\U0001f525"),
    ]


def test_extract_no_tags_unchanged():
    text, tags = reactions.extract_react_tags("just a normal reply")
    assert text == "just a normal reply"
    assert tags == []


def test_extract_only_tag_leaves_empty_text():
    text, tags = reactions.extract_react_tags("<react>❤</react>")
    assert text == ""
    assert len(tags) == 1


def test_extract_strips_whitespace_in_emoji_payload():
    text, tags = reactions.extract_react_tags("<react> ❤ </react>body")
    assert tags[0].emoji == "❤"
    assert text == "body"


# ── fragment target resolution ──────────────────────────────────────────────


def test_find_target_by_fragment_case_insensitive():
    private_msgs = {1: "hello there", 2: "PIZZA time"}
    assert reactions.find_target_by_fragment(private_msgs, "pizza") == 2


def test_find_target_by_fragment_picks_newest_match():
    private_msgs = {1: "pizza first", 2: "unrelated", 3: "pizza again"}
    assert reactions.find_target_by_fragment(private_msgs, "pizza") == 3


def test_find_target_by_fragment_not_found_returns_none():
    private_msgs = {1: "hello there"}
    assert reactions.find_target_by_fragment(private_msgs, "pizza") is None


def test_find_target_by_fragment_matches_media_tag():
    private_msgs = {1: "[sticker] \U0001f60a", 2: "plain text"}
    assert reactions.find_target_by_fragment(private_msgs, "[sticker]") == 1


# ── resolve_reaction_targets: default target, to=, dedup, miss ─────────────


def test_resolve_default_target_uses_default_msg_id():
    tags = [reactions.ReactTag(to=None, emoji="❤")]
    out = reactions.resolve_reaction_targets(tags, default_msg_id=42, private_msgs={})
    assert out == [reactions.PendingReaction(target=42, emoji="❤", to=None)]


def test_resolve_to_fragment_target():
    tags = [reactions.ReactTag(to="pizza", emoji="\U0001f525")]
    out = reactions.resolve_reaction_targets(
        tags, default_msg_id=1, private_msgs={5: "pizza night"}
    )
    assert out == [reactions.PendingReaction(target=5, emoji="\U0001f525", to="pizza")]


def test_resolve_multiple_tags_different_messages():
    tags = [
        reactions.ReactTag(to="a", emoji="\U0001f44d"),
        reactions.ReactTag(to="b", emoji="\U0001f525"),
    ]
    out = reactions.resolve_reaction_targets(
        tags, default_msg_id=None, private_msgs={1: "msg a", 2: "msg b"}
    )
    assert {(p.target, p.emoji) for p in out} == {(1, "\U0001f44d"), (2, "\U0001f525")}


def test_resolve_same_target_twice_last_wins():
    tags = [
        reactions.ReactTag(to=None, emoji="\U0001f44d"),
        reactions.ReactTag(to=None, emoji="❤"),
    ]
    out = reactions.resolve_reaction_targets(tags, default_msg_id=9, private_msgs={})
    assert out == [reactions.PendingReaction(target=9, emoji="❤", to=None)]


def test_resolve_fragment_not_found_yields_unresolved_target():
    tags = [reactions.ReactTag(to="nope", emoji="❤")]
    out = reactions.resolve_reaction_targets(tags, default_msg_id=1, private_msgs={})
    assert out == [reactions.PendingReaction(target=None, emoji="❤", to="nope")]


def test_resolve_default_target_none_when_no_batch_known():
    tags = [reactions.ReactTag(to=None, emoji="❤")]
    out = reactions.resolve_reaction_targets(tags, default_msg_id=None, private_msgs={})
    assert out == [reactions.PendingReaction(target=None, emoji="❤", to=None)]


# ── old/new reaction diff ───────────────────────────────────────────────────


def _emoji_reaction(e: str) -> types.SimpleNamespace:
    return types.SimpleNamespace(type="emoji", emoji=e)


def _custom_reaction() -> types.SimpleNamespace:
    return types.SimpleNamespace(type="custom_emoji", custom_emoji_id="123")


def _paid_reaction() -> types.SimpleNamespace:
    return types.SimpleNamespace(type="paid")


def test_diff_added_only():
    added, removed = reactions.diff_reactions([], [_emoji_reaction("\U0001f525")])
    assert added == ["\U0001f525"]
    assert removed == []


def test_diff_removed_only():
    added, removed = reactions.diff_reactions([_emoji_reaction("❤")], [])
    assert added == []
    assert removed == ["❤"]


def test_diff_swap_counts_as_add_and_remove():
    added, removed = reactions.diff_reactions(
        [_emoji_reaction("❤")], [_emoji_reaction("\U0001f525")]
    )
    assert added == ["\U0001f525"]
    assert removed == ["❤"]


def test_diff_unchanged_yields_nothing():
    old = [_emoji_reaction("❤")]
    new = [_emoji_reaction("❤")]
    added, removed = reactions.diff_reactions(old, new)
    assert added == []
    assert removed == []


def test_diff_custom_emoji_displayed_as_custom():
    added, _removed = reactions.diff_reactions([], [_custom_reaction()])
    assert added == ["[custom]"]


def test_diff_paid_displayed_as_paid():
    added, _removed = reactions.diff_reactions([], [_paid_reaction()])
    assert added == ["[paid]"]


def test_diff_none_inputs_treated_as_empty():
    added, removed = reactions.diff_reactions(None, None)
    assert added == []
    assert removed == []


# ── event-line formatting ───────────────────────────────────────────────────


def test_format_event_added_and_removed_on_your_message():
    line = reactions.format_reaction_event(
        "", ["\U0001f525"], ["❤"], on="mine", summary="hi there"
    )
    assert line == '[reaction +\U0001f525 -❤] on your message: "hi there"'


def test_format_event_on_their_own_message():
    line = reactions.format_reaction_event(
        "", ["\U0001f44d"], [], on="theirs", summary="pizza?"
    )
    assert line == '[reaction +\U0001f44d] on their own message: "pizza?"'


def test_format_event_unknown_target():
    line = reactions.format_reaction_event("", ["❤"], [], on="unknown")
    assert line == "[reaction +❤] on an earlier message"


def test_format_event_with_private_prefix():
    line = reactions.format_reaction_event(
        "[pm] ", [], ["❤"], on="mine", summary="hey"
    )
    assert line == '[pm] [reaction -❤] on your message: "hey"'


def test_format_event_multiple_adds_and_removes():
    line = reactions.format_reaction_event(
        "", ["\U0001f525", "\U0001f44d"], ["❤"], on="mine", summary="x"
    )
    assert line == '[reaction +\U0001f525 +\U0001f44d -❤] on your message: "x"'


def test_format_event_truncates_long_summary_to_60_chars():
    long_text = "x" * 100
    line = reactions.format_reaction_event("", ["❤"], [], on="theirs", summary=long_text)
    assert line == '[reaction +❤] on their own message: "' + "x" * 60 + '"'


# ── summary labels ───────────────────────────────────────────────────────


def test_inbound_summary_photo_no_caption():
    assert reactions.inbound_media_summary("photo") == "[photo]"


def test_inbound_summary_photo_with_caption():
    assert reactions.inbound_media_summary("photo", caption="nice") == "[photo] nice"


def test_inbound_summary_sticker_with_emoji():
    assert reactions.inbound_media_summary("sticker", emoji="\U0001f60a") == "[sticker] \U0001f60a"


def test_inbound_summary_sticker_no_emoji():
    assert reactions.inbound_media_summary("sticker") == "[sticker]"


def test_inbound_summary_file_with_name():
    assert reactions.inbound_media_summary("file", filename="doc.pdf") == "[file] doc.pdf"


def test_inbound_summary_gif_video():
    assert reactions.inbound_media_summary("gif") == "[gif]"
    assert reactions.inbound_media_summary("video") == "[video]"


def test_outbound_summary_text_truncates_60():
    text = "a" * 100
    out = reactions.outbound_sent_summary("text", text)
    assert out == "a" * 60


def test_outbound_summary_voice_truncates_40_with_tag():
    text = "b" * 100
    out = reactions.outbound_sent_summary("voice", text)
    assert out == "[voice] " + "b" * 40


def test_outbound_summary_thinking_truncates_40_with_tag():
    text = "c" * 100
    out = reactions.outbound_sent_summary("thinking", text)
    assert out == "[thinking] " + "c" * 40


def test_outbound_summary_media_kinds_no_text():
    assert reactions.outbound_sent_summary("image") == "[image]"
    assert reactions.outbound_sent_summary("gif") == "[gif]"
    assert reactions.outbound_sent_summary("video") == "[video]"
    assert reactions.outbound_sent_summary("file") == "[file]"


# =============================================================================
# Wiring tests (TgLoop, mocked bot) — below this point every test touches
# synapse_tg.loop / synapse_tg.__main__, not just reactions.py.
# =============================================================================


def _owner_loop(tmp_path, **cfg_kwargs) -> TgLoop:
    cfg = TgConfig(data_dir=tmp_path / "tg-data", allowed_user_ids=[1], **cfg_kwargs)
    return TgLoop(cfg)


def _priv_msg(
    *,
    message_id=1,
    chat_id=777,
    chat_type="private",
    text=None,
    caption=None,
    photo=None,
    sticker=None,
    animation=None,
    video=None,
    document=None,
):
    return types.SimpleNamespace(
        message_id=message_id,
        chat_id=chat_id,
        chat=types.SimpleNamespace(type=chat_type, title="g", id=chat_id),
        from_user=types.SimpleNamespace(first_name="Her", id=1),
        reply_to_message=None,
        text=text,
        caption=caption,
        photo=photo,
        sticker=sticker,
        animation=animation,
        video=video,
        document=document,
    )


class _FakeCtx:
    def __init__(self, bot):
        self.bot = bot


class _FakeBot:
    def __init__(self):
        self.sent: list[dict] = []

    async def send_message(self, **kwargs):
        self.sent.append(kwargs)
        return types.SimpleNamespace(message_id=len(self.sent))

    async def send_chat_action(self, **_):
        pass

    async def edit_message_text(self, **kwargs):
        return types.SimpleNamespace(message_id=kwargs.get("message_id"))

    async def delete_message(self, **_kwargs):
        pass


# ── private message tracking (_track via the six inbound handlers) ─────────


def test_on_message_records_text_in_private_msgs(tmp_path):
    loop = _owner_loop(tmp_path)
    bot = _FakeBot()
    msg = _priv_msg(message_id=10, text="hello there")
    update = types.SimpleNamespace(message=msg)
    asyncio.run(loop.on_message(update, _FakeCtx(bot)))
    assert loop._private_msgs[10] == "hello there"
    assert loop._last_private_msg_id == 10


def test_on_photo_records_tag_and_caption(tmp_path, monkeypatch):
    loop = _owner_loop(tmp_path)
    bot = _FakeBot()
    monkeypatch.setattr("synapse_tg.loop.materialize_photo", _async_stub([]))
    msg = _priv_msg(message_id=11, photo=[object()], caption="nice")
    update = types.SimpleNamespace(message=msg)
    asyncio.run(loop.on_photo(update, _FakeCtx(bot)))
    assert loop._private_msgs[11] == "[photo] nice"


def test_on_sticker_records_tag_with_emoji(tmp_path, monkeypatch):
    loop = _owner_loop(tmp_path)
    bot = _FakeBot()
    monkeypatch.setattr("synapse_tg.loop.materialize_sticker", _async_stub(None))
    msg = _priv_msg(
        message_id=12, sticker=types.SimpleNamespace(emoji="\U0001f60a", set_name="s")
    )
    update = types.SimpleNamespace(message=msg)
    asyncio.run(loop.on_sticker(update, _FakeCtx(bot)))
    assert loop._private_msgs[12] == "[sticker] \U0001f60a"


def test_on_animation_records_as_gif(tmp_path, monkeypatch):
    loop = _owner_loop(tmp_path)
    bot = _FakeBot()
    monkeypatch.setattr("synapse_tg.loop.materialize_animation", _async_stub(None))
    msg = _priv_msg(message_id=13, animation=object())
    update = types.SimpleNamespace(message=msg)
    asyncio.run(loop.on_animation(update, _FakeCtx(bot)))
    assert loop._private_msgs[13] == "[gif]"


def test_on_video_records_tag(tmp_path, monkeypatch):
    loop = _owner_loop(tmp_path)
    bot = _FakeBot()
    monkeypatch.setattr("synapse_tg.loop.materialize_video", _async_stub(None))
    msg = _priv_msg(message_id=14, video=object())
    update = types.SimpleNamespace(message=msg)
    asyncio.run(loop.on_video(update, _FakeCtx(bot)))
    assert loop._private_msgs[14] == "[video]"


def test_on_document_records_filename(tmp_path, monkeypatch):
    loop = _owner_loop(tmp_path)
    bot = _FakeBot()
    monkeypatch.setattr("synapse_tg.loop.materialize_document", _async_stub(None))
    msg = _priv_msg(
        message_id=15, document=types.SimpleNamespace(file_name="doc.pdf")
    )
    update = types.SimpleNamespace(message=msg)
    asyncio.run(loop.on_document(update, _FakeCtx(bot)))
    assert loop._private_msgs[15] == "[file] doc.pdf"


def test_group_message_does_not_enter_private_msgs(tmp_path):
    loop = _owner_loop(tmp_path, group_ids=[-100123], group_mention_keywords=["!ask"])
    bot = _FakeBot()
    msg = _priv_msg(
        message_id=16, chat_id=-100123, chat_type="group", text="!ask hi"
    )
    update = types.SimpleNamespace(message=msg)
    asyncio.run(loop.on_message(update, _FakeCtx(bot)))
    assert 16 not in loop._private_msgs
    assert loop._last_private_msg_id is None


def test_private_msgs_cap_200(tmp_path):
    loop = _owner_loop(tmp_path)
    bot = _FakeBot()
    for i in range(201):
        loop._track(bot, 777, msg=_priv_msg(message_id=i, text=f"m{i}"))
    assert len(loop._private_msgs) == 200
    assert 0 not in loop._private_msgs  # oldest evicted
    assert 200 in loop._private_msgs


def test_track_count_activity_false_records_but_skips_default_target(tmp_path):
    """A slash command the registry handles never reaches the LLM (loop.py
    calls _track with count_activity=False for it) — it must still be
    recorded for <react to="..."> fragment matching, but must not become
    the default <react> target."""
    loop = _owner_loop(tmp_path)
    loop._last_private_msg_id = 1
    bot = _FakeBot()
    loop._track(bot, 777, count_activity=False, msg=_priv_msg(message_id=21, text="/model"))
    assert loop._private_msgs[21] == "/model"
    assert loop._last_private_msg_id == 1


def _async_stub(return_value):
    async def _stub(*_args, **_kwargs):
        return return_value

    return _stub


# ── on_reaction: owner/private gate, config switch, mine/theirs/unknown ────


def _reaction_update(
    *,
    chat_type="private",
    chat_id=777,
    message_id=50,
    user_id=1,
    is_bot=False,
    old=(),
    new=(),
    user=True,
):
    mr = types.SimpleNamespace(
        chat=types.SimpleNamespace(type=chat_type, id=chat_id),
        message_id=message_id,
        user=types.SimpleNamespace(id=user_id, is_bot=is_bot) if user else None,
        old_reaction=list(old),
        new_reaction=list(new),
    )
    return types.SimpleNamespace(message_reaction=mr)


def _new_heart():
    return types.SimpleNamespace(type="emoji", emoji="❤")


def test_on_reaction_group_dropped(tmp_path):
    loop = _owner_loop(tmp_path)
    update = _reaction_update(chat_type="group", new=[_new_heart()])
    asyncio.run(loop.on_reaction(update, _FakeCtx(_FakeBot())))
    assert len(loop._private_buffer) == 0


def test_on_reaction_non_owner_dropped(tmp_path):
    loop = _owner_loop(tmp_path)
    update = _reaction_update(user_id=999, new=[_new_heart()])
    asyncio.run(loop.on_reaction(update, _FakeCtx(_FakeBot())))
    assert len(loop._private_buffer) == 0


def test_on_reaction_bot_dropped(tmp_path):
    loop = _owner_loop(tmp_path)
    update = _reaction_update(is_bot=True, new=[_new_heart()])
    asyncio.run(loop.on_reaction(update, _FakeCtx(_FakeBot())))
    assert len(loop._private_buffer) == 0


def test_on_reaction_no_user_dropped(tmp_path):
    loop = _owner_loop(tmp_path)
    update = _reaction_update(user=False, new=[_new_heart()])
    asyncio.run(loop.on_reaction(update, _FakeCtx(_FakeBot())))
    assert len(loop._private_buffer) == 0


def test_on_reaction_disabled_by_config(tmp_path):
    loop = _owner_loop(tmp_path, reaction_events=False)
    update = _reaction_update(new=[_new_heart()])
    asyncio.run(loop.on_reaction(update, _FakeCtx(_FakeBot())))
    assert len(loop._private_buffer) == 0


def test_on_reaction_owner_private_buffers_event(tmp_path):
    loop = _owner_loop(tmp_path)
    update = _reaction_update(message_id=50, new=[_new_heart()])
    asyncio.run(loop.on_reaction(update, _FakeCtx(_FakeBot())))
    assert len(loop._private_buffer) == 1
    assert "[reaction +❤]" in loop._private_buffer.flush()


def test_on_reaction_tracks_routing_and_activity_not_default_target(tmp_path):
    """A reaction reaches the LLM like a message does: it must bind routing
    (_pending_chat_id) and restart the shell's silence cycle, but must NOT
    move the default <react> target (_last_private_msg_id)."""
    fired = []

    class _FakeShell:
        def on_user_message(self):
            fired.append(True)

    loop = _owner_loop(tmp_path)
    loop._shell = _FakeShell()
    loop._last_private_msg_id = 999  # some earlier real message
    update = _reaction_update(chat_id=12345, message_id=50, new=[_new_heart()])
    asyncio.run(loop.on_reaction(update, _FakeCtx(_FakeBot())))
    assert loop._pending_chat_id == 12345
    assert fired == [True]
    assert loop._last_private_msg_id == 999


def test_on_reaction_on_sent_message_labeled_mine(tmp_path):
    loop = _owner_loop(tmp_path)
    loop._sent_msgs[50] = "hi there"
    update = _reaction_update(message_id=50, new=[_new_heart()])
    asyncio.run(loop.on_reaction(update, _FakeCtx(_FakeBot())))
    body = loop._private_buffer.flush()
    assert 'on your message: "hi there"' in body


def test_on_reaction_on_their_message_labeled_theirs(tmp_path):
    loop = _owner_loop(tmp_path)
    loop._private_msgs[50] = "pizza night"
    update = _reaction_update(message_id=50, new=[_new_heart()])
    asyncio.run(loop.on_reaction(update, _FakeCtx(_FakeBot())))
    body = loop._private_buffer.flush()
    assert 'on their own message: "pizza night"' in body


def test_on_reaction_unknown_message_labeled_earlier(tmp_path):
    loop = _owner_loop(tmp_path)
    update = _reaction_update(message_id=999, new=[_new_heart()])
    asyncio.run(loop.on_reaction(update, _FakeCtx(_FakeBot())))
    body = loop._private_buffer.flush()
    assert "on an earlier message" in body


def test_on_reaction_no_change_buffers_nothing(tmp_path):
    """old == new (shouldn't happen per the API, but guard anyway)."""
    loop = _owner_loop(tmp_path)
    same = _new_heart()
    update = _reaction_update(old=[same], new=[same])
    asyncio.run(loop.on_reaction(update, _FakeCtx(_FakeBot())))
    assert len(loop._private_buffer) == 0


# ── _deliver_reply: <react> application (mocked bot) ───────────────────────


class _ReactBot(_FakeBot):
    def __init__(self):
        super().__init__()
        self.reactions: list[tuple] = []
        self.fail_reaction = False
        self.retry_after_once: float | None = None  # raises once, then succeeds

    async def set_message_reaction(self, chat_id, message_id, reaction):
        if self.fail_reaction:
            raise RuntimeError("api down")
        if self.retry_after_once is not None:
            wait, self.retry_after_once = self.retry_after_once, None
            raise RetryAfter(wait)
        self.reactions.append((chat_id, message_id, reaction))


def test_deliver_reply_default_target_reacts_and_strips_tag(tmp_path):
    loop = _owner_loop(tmp_path)
    bot = _ReactBot()
    asyncio.run(
        loop._deliver_reply(bot, 777, "<react>❤</react>hello", "", trigger_msg_id=55)
    )
    assert bot.reactions == [(777, 55, [ReactionTypeEmoji("❤")])]
    assert len(bot.sent) == 1
    assert bot.sent[0]["chat_id"] == 777
    assert bot.sent[0]["text"] == "hello"


def test_deliver_reply_only_tag_sends_zero_bubbles(tmp_path):
    loop = _owner_loop(tmp_path)
    bot = _ReactBot()
    asyncio.run(
        loop._deliver_reply(bot, 777, "<react>❤</react>", "", trigger_msg_id=55)
    )
    assert bot.sent == []
    assert bot.reactions == [(777, 55, [ReactionTypeEmoji("❤")])]


def test_deliver_reply_api_error_does_not_affect_text(tmp_path):
    loop = _owner_loop(tmp_path)
    bot = _ReactBot()
    bot.fail_reaction = True
    asyncio.run(
        loop._deliver_reply(bot, 777, "<react>❤</react>hi", "", trigger_msg_id=55)
    )
    assert any(c["text"] == "hi" for c in bot.sent)


def _patch_no_real_sleep(monkeypatch) -> list[float]:
    slept: list[float] = []

    async def fake_sleep(sec):
        slept.append(sec)

    monkeypatch.setattr("synapse_tg.loop.asyncio.sleep", fake_sleep)
    return slept


def test_apply_reactions_retry_after_then_success(tmp_path, monkeypatch):
    """Tag-only reply (zero bubbles) so the only sleep recorded is the
    RetryAfter wait — not the unrelated per-bubble _SEND_GAP_SEC sleep."""
    slept = _patch_no_real_sleep(monkeypatch)
    loop = _owner_loop(tmp_path)
    bot = _ReactBot()
    bot.retry_after_once = 3
    asyncio.run(loop._deliver_reply(bot, 777, "<react>❤</react>", "", trigger_msg_id=55))
    assert bot.reactions == [(777, 55, [ReactionTypeEmoji("❤")])]
    assert slept == [3 + 0.5]


def test_apply_reactions_retry_after_over_cap_skips_no_sleep(tmp_path, monkeypatch):
    slept = _patch_no_real_sleep(monkeypatch)
    loop = _owner_loop(tmp_path)
    bot = _ReactBot()
    bot.retry_after_once = 120  # > default retry_after_cap_sec (60.0)
    asyncio.run(loop._deliver_reply(bot, 777, "<react>❤</react>", "", trigger_msg_id=55))
    assert bot.reactions == []
    assert slept == []


def test_deliver_reply_group_target_strips_tag_no_react(tmp_path):
    """Group chat_ids are always negative in Telegram — the guard is a
    plain sign check, independent of _group_buffers state."""
    loop = _owner_loop(tmp_path)
    bot = _ReactBot()
    asyncio.run(
        loop._deliver_reply(bot, -100123, "<react>❤</react>hi", "", trigger_msg_id=55)
    )
    assert bot.reactions == []
    assert any(c["text"] == "hi" for c in bot.sent)


def test_deliver_reply_fragment_target(tmp_path):
    loop = _owner_loop(tmp_path)
    loop._private_msgs[5] = "pizza night"
    bot = _ReactBot()
    asyncio.run(
        loop._deliver_reply(
            bot, 777, '<react to="pizza">\U0001f525</react>ok', "", trigger_msg_id=55
        )
    )
    assert bot.reactions == [(777, 5, [ReactionTypeEmoji("\U0001f525")])]


def test_deliver_reply_records_sent_message_id(tmp_path):
    loop = _owner_loop(tmp_path)
    bot = _ReactBot()
    asyncio.run(loop._deliver_reply(bot, 777, "hello there", ""))
    assert len(bot.sent) == 1  # _FakeBot.send_message -> message_id == len(sent) == 1
    assert loop._sent_msgs[1] == "hello there"


def test_sent_msgs_cap_300(tmp_path):
    loop = _owner_loop(tmp_path)
    for i in range(301):
        loop._record_sent(i, "text", f"m{i}")
    assert len(loop._sent_msgs) == 300
    assert 0 not in loop._sent_msgs  # oldest evicted
    assert 300 in loop._sent_msgs


def test_deliver_reply_default_target_uses_snapshot_not_live_value(tmp_path):
    """She sends a NEW message mid-turn (updating _last_private_msg_id) —
    the default <react> target must still be the snapshot taken at flush
    time (trigger_msg_id), not the live pointer."""
    loop = _owner_loop(tmp_path)
    loop._last_private_msg_id = 999  # updated "mid-turn"
    bot = _ReactBot()
    asyncio.run(
        loop._deliver_reply(bot, 777, "<react>❤</react>hi", "", trigger_msg_id=42)
    )
    assert bot.reactions == [(777, 42, [ReactionTypeEmoji("❤")])]


def test_deliver_reply_no_trigger_falls_back_to_live_last_private_msg_id(tmp_path):
    """The other 4 callers of _deliver_reply pass no trigger_msg_id — the
    default target must fall back to the live _last_private_msg_id."""
    loop = _owner_loop(tmp_path)
    loop._last_private_msg_id = 777
    bot = _ReactBot()
    asyncio.run(loop._deliver_reply(bot, 777, "<react>❤</react>hi", ""))
    assert bot.reactions == [(777, 777, [ReactionTypeEmoji("❤")])]


def test_flush_one_snapshots_trigger_before_turn_runs(tmp_path, monkeypatch):
    """Integration: _flush_one must snapshot _last_private_msg_id right after
    buf.flush() and carry it through to _deliver_reply, even if the turn
    updates _last_private_msg_id while running."""
    from synapse_core.debounce import InboundBuffer

    loop = _owner_loop(tmp_path)
    loop._last_private_msg_id = 42  # the batch's last message
    buf = InboundBuffer(clock=lambda: 1000.0)
    buf.add("hi")
    bot = _ReactBot()
    loop._bot = bot

    monkeypatch.setattr(loop, "ensure_provider", lambda: None)
    loop._provider = type("P", (), {"session_id": None, "send": lambda self, b: None})()

    async def fake_stream(bot_, chat_id, typing):
        loop._last_private_msg_id = 999  # she sent another message mid-turn
        return "<react>❤</react>reply", ""

    monkeypatch.setattr(loop, "_stream_response", fake_stream)

    async def _immediate(fn, *args):
        return fn(*args)

    monkeypatch.setattr("synapse_tg.loop.asyncio.to_thread", _immediate)

    asyncio.run(loop._flush_one(bot, 777, buf))
    assert bot.reactions == [(777, 42, [ReactionTypeEmoji("❤")])]


# ── _translate_thinking_in_place: _sent_msgs bookkeeping ────────────────────


def test_translate_thinking_in_place_edit_updates_sent_msgs(tmp_path, monkeypatch):
    """An edited thinking bubble's cached summary must reflect the NEW
    (translated) text, not the original."""
    loop = _owner_loop(tmp_path)
    loop._sent_msgs[10] = "[thinking] original"
    bot = _FakeBot()

    async def fake_translate(*_a, **_kw):
        return "translated"

    monkeypatch.setattr("synapse_tg.loop.translate", fake_translate)
    monkeypatch.setattr("synapse_tg.loop.pack_for_tg", lambda text: ["translated"])
    asyncio.run(loop._translate_thinking_in_place(bot, 777, [10], "original"))
    assert loop._sent_msgs[10] == "[thinking] translated"


def test_translate_thinking_in_place_extra_bubble_recorded(tmp_path, monkeypatch):
    """Translation producing MORE bubbles than were originally sent: the
    extra bubble (sent fresh via bot.send_message) must also land in
    _sent_msgs, not just the edited one."""
    loop = _owner_loop(tmp_path)
    loop._sent_msgs[10] = "[thinking] original"
    bot = _FakeBot()

    async def fake_translate(*_a, **_kw):
        return "translated longer thinking"

    monkeypatch.setattr("synapse_tg.loop.translate", fake_translate)
    monkeypatch.setattr(
        "synapse_tg.loop.pack_for_tg", lambda text: ["bubble one", "bubble two"]
    )
    asyncio.run(loop._translate_thinking_in_place(bot, 777, [10], "original"))
    assert loop._sent_msgs[10] == "[thinking] bubble one"
    assert len(bot.sent) == 1
    assert loop._sent_msgs[1] == "[thinking] bubble two"


# ── __main__: allowed_updates + MessageReactionHandler wiring ──────────────


def test_run_polling_requests_allowed_updates_covering_message_reaction():
    """Source-level check (main() boots a real long-poll Application with
    many side effects, impractical to execute end-to-end in a unit test):
    run_polling must request allowed_updates covering message_reaction,
    which Telegram omits by default."""
    import inspect

    from telegram import Update
    from telegram.constants import UpdateType

    from synapse_tg import __main__ as tg_main

    assert UpdateType.MESSAGE_REACTION in Update.ALL_TYPES
    src = inspect.getsource(tg_main.main)
    assert "run_polling(allowed_updates=Update.ALL_TYPES)" in src


def test_message_reaction_handler_registered_excluding_count_updates():
    import inspect

    from synapse_tg import __main__ as tg_main

    src = inspect.getsource(tg_main.main)
    assert "MessageReactionHandler(" in src
    assert "loop.on_reaction" in src
    assert "MessageReactionHandler.MESSAGE_REACTION_UPDATED" in src
