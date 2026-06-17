"""Tests for C1 media-tag extraction in split.

`split_for_wechat_typed(text)` returns a typed bubble list:
    {"kind": "text", "text": "..."}
    {"kind": "image"|"file"|"gif"|"video", "path": "..."}

`split_for_wechat(text)` (text-only) MUST remain regression-clean — media
tags are stripped from its output, never leak as raw text.
"""

from __future__ import annotations

from synapse_wx.split import split_for_wechat, split_for_wechat_typed

# ── typed split: media bubbles ─────────────────────────────────────────────


def test_typed_plain_text_only() -> None:
    out = split_for_wechat_typed("hi there")
    assert out == [{"kind": "text", "text": "hi there"}]


def test_typed_image_tag_extracted() -> None:
    out = split_for_wechat_typed('<image path="/tmp/a.png">')
    assert out == [{"kind": "image", "path": "/tmp/a.png"}]


def test_typed_file_tag_extracted() -> None:
    out = split_for_wechat_typed('<file path="/tmp/report.pdf">')
    assert out == [{"kind": "file", "path": "/tmp/report.pdf"}]


def test_typed_gif_tag_extracted() -> None:
    out = split_for_wechat_typed('<gif path="/tmp/cat.gif">')
    assert out == [{"kind": "gif", "path": "/tmp/cat.gif"}]


def test_typed_video_tag_extracted() -> None:
    out = split_for_wechat_typed('<video path="/tmp/clip.mp4">')
    assert out == [{"kind": "video", "path": "/tmp/clip.mp4"}]


def test_typed_text_and_image_mixed() -> None:
    text = 'look at this\n<image path="/tmp/a.png">\nnice right'
    out = split_for_wechat_typed(text)
    assert out == [
        {"kind": "text", "text": "look at this"},
        {"kind": "image", "path": "/tmp/a.png"},
        {"kind": "text", "text": "nice right"},
    ]


def test_typed_image_inline_in_text() -> None:
    # tag mid-line: split text around tag, emit media bubble between.
    text = 'before <image path="/tmp/a.png"> after'
    out = split_for_wechat_typed(text)
    assert out == [
        {"kind": "text", "text": "before"},
        {"kind": "image", "path": "/tmp/a.png"},
        {"kind": "text", "text": "after"},
    ]


def test_typed_multiple_media_in_a_row() -> None:
    text = '<image path="/a.png"><file path="/b.pdf">'
    out = split_for_wechat_typed(text)
    assert out == [
        {"kind": "image", "path": "/a.png"},
        {"kind": "file", "path": "/b.pdf"},
    ]


def test_typed_image_self_closing_tag() -> None:
    # `<image path="..."/>` (self-closing) — accepted too.
    out = split_for_wechat_typed('<image path="/tmp/a.png"/>')
    assert out == [{"kind": "image", "path": "/tmp/a.png"}]


def test_typed_image_single_quotes() -> None:
    out = split_for_wechat_typed("<image path='/tmp/a.png'>")
    assert out == [{"kind": "image", "path": "/tmp/a.png"}]


def test_typed_empty_input() -> None:
    assert split_for_wechat_typed("") == []


def test_typed_long_text_still_splits_by_bubbles() -> None:
    # Text content still cuts via the bubble rules — long line → multi text bubbles.
    text = "a" * 200
    out = split_for_wechat_typed(text)
    assert all(b["kind"] == "text" for b in out)
    assert "".join(b["text"] for b in out) == text


# ── text-only split: regression / additive guarantee ──────────────────────


def test_split_for_wechat_strips_media_tags() -> None:
    # Plain text path: media tags removed, text bubbles untouched.
    text = 'hello\n<image path="/tmp/a.png">\nworld'
    out = split_for_wechat(text)
    assert out == ["hello", "world"]


def test_split_for_wechat_unchanged_for_pure_text() -> None:
    # Single \n keeps lines in one bubble; \n\n splits.
    out = split_for_wechat("line one\nline two")
    assert out == ["line one\nline two"]
    out2 = split_for_wechat("line one\n\nline two")
    assert out2 == ["line one", "line two"]


def test_split_for_wechat_media_only_returns_empty_text() -> None:
    # When only media tags exist, text-only split returns [].
    out = split_for_wechat('<image path="/tmp/a.png">')
    assert out == []
