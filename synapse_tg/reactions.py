"""Pure logic for Telegram emoji reactions (private chat only) — no bot,
no network, no mutable state. loop.py wires these into the handlers."""

from __future__ import annotations

import re
from dataclasses import dataclass

from telegram.constants import ReactionEmoji

# Telegram strips/adds U+FE0F (variation selector) inconsistently across
# clients — some whitelist entries carry it (mid-string, e.g. heart-on-fire;
# trailing, e.g. the shrug variants), others never do (plain heart). Compare
# with FE0F stripped from both sides, but always SEND the canonical whitelist
# string back (the one the Bot API actually expects).
_FE0F = "\ufe0f"


def _strip_fe0f(s: str) -> str:
    return s.replace(_FE0F, "")


ALLOWED_REACTION_EMOJI: tuple[str, ...] = tuple(str(e) for e in ReactionEmoji)

_CANONICAL_BY_STRIPPED: dict[str, str] = {
    _strip_fe0f(e): e for e in ALLOWED_REACTION_EMOJI
}


def normalize_emoji(raw: str) -> str | None:
    """Canonical whitelist form of `raw` (FE0F-stripped compare), or None."""
    if not raw:
        return None
    return _CANONICAL_BY_STRIPPED.get(_strip_fe0f(raw))


# ── <react> tag parsing / stripping ─────────────────────────────────────────


@dataclass(frozen=True)
class ReactTag:
    """One <react> or <react to="FRAGMENT"> tag, before resolution/normalisation."""

    to: str | None
    emoji: str


_REACT_TAG_RE = re.compile(r'<react(?:\s+to="([^"]*)")?\s*>(.*?)</react>', re.DOTALL)


def extract_react_tags(text: str) -> tuple[str, list[ReactTag]]:
    """Strip every <react> tag, returning (stripped_text, tags) in order.
    Leaves surrounding whitespace as-is, like <!-- --> stripping does."""
    tags: list[ReactTag] = []

    def _sub(m: re.Match[str]) -> str:
        tags.append(ReactTag(to=m.group(1), emoji=m.group(2).strip()))
        return ""

    stripped = _REACT_TAG_RE.sub(_sub, text)
    return stripped.strip(), tags


# ── fragment target resolution ──────────────────────────────────────────────


def find_target_by_fragment(private_msgs, fragment: str) -> int | None:
    """Newest message (by insertion order) whose summary contains fragment,
    case-insensitive — same matching rule <quote> uses."""
    needle = fragment.lower()
    for msg_id, summary in reversed(list(private_msgs.items())):
        if needle in summary.lower():
            return msg_id
    return None


@dataclass(frozen=True)
class PendingReaction:
    """One resolved reaction target (target=None: no default, or no fragment match)."""

    target: int | None
    emoji: str
    to: str | None


def resolve_reaction_targets(
    tags: list[ReactTag], *, default_msg_id: int | None, private_msgs
) -> list[PendingReaction]:
    """Resolve each tag's target (default or fragment match); same-target
    tags collapse to the last one. Unresolved tags are kept (target=None)
    for the caller to log."""
    by_target: dict[int, PendingReaction] = {}
    unresolved: list[PendingReaction] = []
    for tag in tags:
        target = default_msg_id if tag.to is None else find_target_by_fragment(private_msgs, tag.to)
        pending = PendingReaction(target=target, emoji=tag.emoji, to=tag.to)
        if target is None:
            unresolved.append(pending)
        else:
            by_target[target] = pending
    return [*by_target.values(), *unresolved]


# ── old/new reaction diff (inbound) ─────────────────────────────────────────


def reaction_display(reaction) -> str:
    """Render one ReactionType (or duck-typed equivalent) for the diff/event line."""
    kind = getattr(reaction, "type", None)
    if kind == "custom_emoji":
        return "[custom]"
    if kind == "paid":
        return "[paid]"
    return getattr(reaction, "emoji", "?")


def diff_reactions(old, new) -> tuple[list[str], list[str]]:
    """(added, removed) emoji-display strings between two reaction sequences."""
    old_disp = [reaction_display(r) for r in (old or [])]
    new_disp = [reaction_display(r) for r in (new or [])]
    added = [e for e in new_disp if e not in old_disp]
    removed = [e for e in old_disp if e not in new_disp]
    return added, removed


def format_reaction_diff(added: list[str], removed: list[str]) -> str:
    parts = [f"+{e}" for e in added] + [f"-{e}" for e in removed]
    return " ".join(parts)


def format_reaction_event(
    private_prefix: str,
    added: list[str],
    removed: list[str],
    *,
    on: str,
    summary: str | None = None,
) -> str:
    """Build the inbound event line. `on`: "mine"/"theirs"/"unknown" picks
    the tail wording; `summary` is truncated to 60 chars for display (the
    caches hold it in full, for fragment matching elsewhere)."""
    diff = format_reaction_diff(added, removed)
    display = summary[:60] if summary is not None else None
    if on == "mine":
        tail = f'on your message: "{display}"'
    elif on == "theirs":
        tail = f'on their own message: "{display}"'
    else:
        tail = "on an earlier message"
    return f"{private_prefix}[reaction {diff}] {tail}"


# ── summary labels ───────────────────────────────────────────────────────


def inbound_media_summary(kind: str, *, emoji: str = "", filename: str = "", caption: str = "") -> str:
    """Summary label for an inbound message: kind is photo/sticker/gif/video/voice/file."""
    if kind == "sticker":
        tag = f"[sticker] {emoji}" if emoji else "[sticker]"
    elif kind == "file":
        tag = f"[file] {filename}" if filename else "[file]"
    else:
        tag = f"[{kind}]"
    return f"{tag} {caption}" if caption else tag


def outbound_sent_summary(kind: str, text: str = "", seq: int | None = None) -> str:
    """Summary label for a sent message: text keeps 60 chars, voice/thinking
    tag+40 chars. Other kinds (media) are numbered per reply when `seq` is
    given — `[{kind} {seq}]` — with the file name (if any) appended, up to
    60 chars; without `seq` it's tag only, `[{kind}]`."""
    if kind == "text":
        return text[:60]
    if kind in ("voice", "thinking"):
        return f"[{kind}] {text[:40]}"
    tag = f"[{kind} {seq}]" if seq is not None else f"[{kind}]"
    return f"{tag} {text[:60]}" if text else tag
