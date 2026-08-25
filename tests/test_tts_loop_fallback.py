"""Tests for voice bubble fallback when TTS is disabled."""

from __future__ import annotations

from synapse_tg.split import split_for_tg_typed


def test_voice_bubble_structure_for_fallback() -> None:
    """When TTS is disabled, the loop sends bubble["text"] as a text message.
    Verify split_for_tg_typed produces a voice bubble with non-empty text so
    the fallback path has something to send (words are never lost)."""
    out = split_for_tg_typed("<voice>please say this aloud</voice>")
    assert len(out) == 1
    assert out[0]["kind"] == "voice"
    assert out[0]["text"] == "please say this aloud"


def test_voice_fallback_text_preserved_mixed() -> None:
    """Mixed turn: text before + voice tag. Text bubble and voice bubble both
    carry non-empty content for the fallback path."""
    out = split_for_tg_typed("intro text\n\n<voice>spoken part</voice>")
    text_bubbles = [b for b in out if b["kind"] == "text"]
    voice_bubbles = [b for b in out if b["kind"] == "voice"]
    assert text_bubbles and text_bubbles[0]["text"]
    assert voice_bubbles and voice_bubbles[0]["text"] == "spoken part"
