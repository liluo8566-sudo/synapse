"""Pure logic for Telegram full-screen message effects (private chat only) —
no bot, no network, no mutable state. loop.py wires these into _deliver_reply."""

from __future__ import annotations

import re

# Bot API 7.4 free message effects: emoji -> effect_id. Cross-checked against
# several open-source bots; the heart entry is the bare glyph (no U+FE0F), matching how
# Telegram's own reaction whitelist represents it.
EFFECT_IDS: dict[str, str] = {
    "\U0001f525": "5104841245755180586",  # 🔥
    "\U0001f44d": "5107584321108051014",  # 👍
    "\U0001f44e": "5104858069142078462",  # 👎
    "❤": "5159385139981059251",  # ❤
    "\U0001f389": "5046509860389126442",  # 🎉
    "\U0001f4a9": "5046589136895476101",  # 💩
}

_FE0F = "️"


def _strip_fe0f(s: str) -> str:
    return s.replace(_FE0F, "")


_CANONICAL_BY_STRIPPED: dict[str, str] = {_strip_fe0f(e): e for e in EFFECT_IDS}


def normalize_effect_emoji(raw: str) -> str | None:
    """Canonical EFFECT_IDS key for `raw` (FE0F-stripped compare), or None."""
    if not raw:
        return None
    return _CANONICAL_BY_STRIPPED.get(_strip_fe0f(raw.strip()))


def effect_id_for(raw: str) -> str | None:
    """Bot API message_effect_id for `raw` emoji, or None if not in EFFECT_IDS."""
    canonical = normalize_effect_emoji(raw)
    return EFFECT_IDS.get(canonical) if canonical else None


# ── <effect> tag parsing / stripping ────────────────────────────────────────

_EFFECT_TAG_RE = re.compile(r"<effect>(.*?)</effect>", re.DOTALL)


def extract_effect_tags(text: str) -> tuple[str, list[str]]:
    """Strip every <effect> tag, returning (stripped_text, raw_emoji_list) in
    order. The caller uses only the first; extras are its concern to log."""
    tags: list[str] = []

    def _sub(m: re.Match[str]) -> str:
        tags.append(m.group(1).strip())
        return ""

    stripped = _EFFECT_TAG_RE.sub(_sub, text)
    return stripped.strip(), tags
