"""Tests for synapse_wx.commands.aliases."""

from __future__ import annotations

from synapse_core.commands.aliases import display_name, resolve_model


def test_resolve_alias_46_pins_1m() -> None:
    assert resolve_model("4.6") == "claude-opus-4-6[1m]"


def test_resolve_alias_47_pins_1m() -> None:
    assert resolve_model("4.7") == "claude-opus-4-7[1m]"


def test_resolve_alias_48_pins_1m() -> None:
    assert resolve_model("4.8") == "claude-opus-4-8[1m]"


def test_resolve_alias_sonnet() -> None:
    assert resolve_model("sonnet") == "claude-sonnet-4-6"


def test_resolve_alias_opus_is_4_8_1m() -> None:
    assert resolve_model("opus") == "claude-opus-4-8[1m]"


def test_resolve_alias_haiku_dated() -> None:
    assert resolve_model("haiku") == "claude-haiku-4-5-20251001"


def test_resolve_alias_fable_5() -> None:
    assert resolve_model("5") == "claude-fable-5[1m]"
    assert resolve_model("fable") == "claude-fable-5[1m]"


def test_resolve_alias_codex() -> None:
    assert resolve_model("codex") == "codex"


def test_resolve_alias_case_insensitive() -> None:
    assert resolve_model("Sonnet") == "claude-sonnet-4-6"
    assert resolve_model("OPUS") == "claude-opus-4-8[1m]"


def test_resolve_canonical_pass_through() -> None:
    # Canonical ids return unchanged.
    assert resolve_model("claude-opus-4-7") == "claude-opus-4-7"


def test_resolve_canonical_with_suffix_pass_through() -> None:
    # cc accepts context-window-pinned ids like "[1m]"; bridge must not strip them.
    assert resolve_model("claude-opus-4-7[1m]") == "claude-opus-4-7[1m]"
    assert resolve_model("claude-opus-4-6[1m]") == "claude-opus-4-6[1m]"


def test_resolve_unknown_passes_through() -> None:
    # Non-alias non-empty tokens flow to cc, which validates.
    assert resolve_model("claude-future-9") == "claude-future-9"


def test_resolve_empty_returns_none() -> None:
    assert resolve_model("") is None
    assert resolve_model("   ") is None


def test_display_name_known() -> None:
    assert display_name("claude-opus-4-6") == "Opus 4.6"
    assert display_name("claude-opus-4-7") == "Opus 4.7"
    assert display_name("claude-opus-4-8") == "Opus 4.8"
    assert display_name("claude-sonnet-4-6") == "Sonnet 4.6"
    assert display_name("claude-haiku-4-5-20251001") == "Haiku 4.5"


def test_display_name_fable() -> None:
    assert display_name("claude-fable-5") == "Fable 5"


def test_display_name_codex() -> None:
    assert display_name("codex") == "Codex"


def test_display_name_known_with_context_suffix() -> None:
    # The 1M-context variant must surface as "[1M]" in /info.
    assert display_name("claude-opus-4-7[1m]") == "Opus 4.7 [1M]"
    assert display_name("claude-opus-4-8[1m]") == "Opus 4.8 [1M]"
    assert display_name("claude-opus-4-7[200k]") == "Opus 4.7 [200K]"


def test_display_name_unknown_with_suffix_passes_through() -> None:
    assert display_name("claude-future-9[1m]") == "claude-future-9 [1M]"


def test_display_name_none() -> None:
    # Bridge starts with no model known; show "?" not the misleading "default".
    assert display_name(None) == "?"
    assert display_name("") == "?"


def test_display_name_unknown_falls_back_to_id() -> None:
    assert display_name("claude-future-9") == "claude-future-9"
