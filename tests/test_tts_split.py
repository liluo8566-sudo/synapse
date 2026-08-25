"""Tests for <voice> tag parsing in split_for_tg_typed."""

from __future__ import annotations

from synapse_tg.split import split_for_tg_typed


def test_lone_voice_tag() -> None:
    out = split_for_tg_typed("<voice>hello world</voice>")
    assert out == [{"kind": "voice", "text": "hello world"}]


def test_voice_tag_with_text_before_and_after() -> None:
    out = split_for_tg_typed("before\n\n<voice>speak this</voice>\n\nafter")
    assert out == [
        {"kind": "text", "text": "before"},
        {"kind": "voice", "text": "speak this"},
        {"kind": "text", "text": "after"},
    ]


def test_voice_tag_interleaved_with_image_tag() -> None:
    text = '<voice>hello</voice><image path="/tmp/a.png">'
    out = split_for_tg_typed(text)
    assert out[0] == {"kind": "voice", "text": "hello"}
    assert out[1] == {"kind": "image", "path": "/tmp/a.png"}


def test_image_before_voice() -> None:
    text = '<image path="/tmp/a.png"><voice>after image</voice>'
    out = split_for_tg_typed(text)
    assert out[0] == {"kind": "image", "path": "/tmp/a.png"}
    assert out[1] == {"kind": "voice", "text": "after image"}


def test_voice_tag_multiline_inner_text() -> None:
    out = split_for_tg_typed("<voice>line one\nline two</voice>")
    assert len(out) == 1
    assert out[0]["kind"] == "voice"
    assert "line one" in out[0]["text"]
    assert "line two" in out[0]["text"]


def test_empty_voice_tag_skipped() -> None:
    out = split_for_tg_typed("hello <voice>   </voice> world")
    # Empty inner text: no voice bubble emitted; surrounding text still delivered.
    assert all(b["kind"] != "voice" for b in out)
    combined = " ".join(b["text"] for b in out if b["kind"] == "text")
    assert "hello" in combined
    assert "world" in combined


def test_empty_voice_tag_whitespace_only_skipped() -> None:
    out = split_for_tg_typed("<voice></voice>")
    assert out == []


def test_voice_tag_case_insensitive() -> None:
    out = split_for_tg_typed("<VOICE>spoken</VOICE>")
    assert out == [{"kind": "voice", "text": "spoken"}]


def test_no_tag_regression() -> None:
    out = split_for_tg_typed("plain text no tags")
    assert out == [{"kind": "text", "text": "plain text no tags"}]


def test_existing_image_tag_unaffected() -> None:
    out = split_for_tg_typed('<image path="/tmp/x.png">')
    assert out == [{"kind": "image", "path": "/tmp/x.png"}]
