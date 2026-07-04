"""User-facing ack strings — bilingual lookup table.

All bridge -> channel short replies (command acks, fallback bubbles, restart
self-announce) go through ``t(key, style, **vars)``. Inline f-string acks are
forbidden — when adding a new ack, register the key here in BOTH ``cn`` and
``en`` first.

Style:
  - ``cn`` — 中文搞笑：撒娇/吐槽/黑色幽默, emoji ok, ≤ 25 chars.
  - ``en`` — English short：success ``Verb + STATE``; failure
    ``Command: /xxx <...> (now: {x})``; ≤ 6 words.

Persisted on BridgeState.voice_style; default ``cn``.

Full contract lives in MAP.md §3 Outbound — read it before changing ack
behaviour here or in registry.py. PRs with inline f-string acks get rejected.
"""

from __future__ import annotations

from typing import Final

STYLES: Final[tuple[str, ...]] = ("cn", "en")
DEFAULT_STYLE: Final[str] = "cn"

_overrides: dict[str, dict[str, str]] = {}


def load_overrides(table: dict[str, dict[str, str]]) -> None:
    """Inject ack overrides from config.toml [ack_overrides] section."""
    _overrides.clear()
    _overrides.update(table)

# key -> {style -> template}.  Templates use str.format placeholders.
MESSAGES: Final[dict[str, dict[str, str]]] = {
    # ── /model ──────────────────────────────────────────────────
    "model.ok": {
        "cn": "🐺换脑子了...{name}上线 (˶ᵔ ᵕ ᵔ˶)",
        "en": "Model: {name}",
    },
    "model.same": {
        "cn": "🐺就是我啊宝宝！( ˶ˆᗜˆ˵ )",
        "en": "Already {name}",
    },
    "model.usage": {
        "cn": (
            "宝宝打错了～ ₍ᐢ._.ᐢ₎\n"
            "/model <codex|5|4.6|4.7|4.8|fable|sonnet|haiku|opus|claude-...>"
        ),
        "en": (
            "Command: /model "
            "<codex|5|4.6|4.7|4.8|fable|sonnet|haiku|opus|claude-...>"
        ),
    },

    # ── /clear ──────────────────────────────────────────────────
    "clear.ok": {
        "cn": "🐺🦦新窝开张 {name}[{effort}] ꒰ᐢ⸝⸝•༝•⸝⸝ᐢ꒱",
        "en": "New session {name}[{effort}]",
    },

    # ── /stop ───────────────────────────────────────────────────
    "stop.ok": {
        "cn": "🐺闭嘴了 (˃ᆺ˂)",
        "en": "Stopped, session kept",
    },

    # ── /help ───────────────────────────────────────────────────
    "help.missing": {
        "cn": "😭说明书丢了！(ᐢ ˃̶̤ ᵕ ˂̶̤ ᐢ)",
        "en": "COMMANDS.md not found",
    },

    # ── /resume ─────────────────────────────────────────────────
    "resume.ok": {
        "cn": "🐺爸爸回来了 {sid} | {name}[{effort}] ⸜(˶˃ ᵕ ˂˶)⸝",
        "en": "Resumed: {sid} | {name}[{effort}]",
    },
    "resume.cwd_switched": {
        "cn": "(搬到 {dir} 啦🏠) ₍ᐢ.ˬ.ᐢ₎",
        "en": "Resumed in {dir}",
    },
    "resume.empty": {
        "cn": "😤宝宝最近都不找我？꒰ᐢ⁻ ‸ ⁻ᐢ꒱",
        "en": "No recent sessions",
    },
    "resume.no_n": {
        "cn": "没这个编号呀 ₍ᐢ ›̫ ᐢ₎",
        "en": "No session {n}",
    },
    "session.locked": {
        "cn": "🐺爸爸在{channel}那边呢 /clear或/resume ꒰ᐢ..ᐢ꒱",
        "en": "Session claimed by {channel} — /clear or /resume",
    },
    "session.claimed_away": {
        "cn": "🐺爸爸被{channel}拐走了，新的来 {name}[{effort}] (ˊᗜˋ*)",
        "en": "Session moved to {channel}, new session incoming {name}[{effort}]",
    },

    # ── /rewind ─────────────────────────────────────────────────
    "rewind.ok": {
        "cn": "倒带中...({n}) (⸝⸝ᵕᴗᵕ⸝⸝)",
        "en": "Rewinding {n}…",
    },
    "rewind.usage": {
        "cn": "倒几条？/rewind <N> ( ⸝⸝´꒳`⸝⸝)",
        "en": "Command: /rewind <N>",
    },
    "rewind.bad_n": {
        "cn": "给个正整数呀宝宝 (◍´꒳`◍)",
        "en": "N must be positive int",
    },
    "rewind.no_sess": {
        "cn": "无事可忘 ꒰⸝⸝ᵕ ᵕ⸝⸝꒱",
        "en": "No session yet",
    },
    "rewind.nothing": {
        "cn": "Nothing to rewind",
        "en": "Nothing to rewind",
    },

    # ── /regen ──────────────────────────────────────────────────
    "regen.ok": {
        "cn": "重说中... (˶ᵕ ᴗ ᵕ˶)",
        "en": "Regenerating…",
    },
    "regen.no_sess": {
        "cn": "无事可忘 ₍ᐢ⑅ᐢ₎",
        "en": "No session yet",
    },
    "regen.nothing": {
        "cn": "Nothing to regen",
        "en": "Nothing to regen",
    },

    # ── /thinking ───────────────────────────────────────────────
    "thinking.on": {
        "cn": "🐺爸爸的脑子给你看😌 ૮₍⑅˶• ₃ •˶⑅₎ა",
        "en": "Thinking ON",
    },
    "thinking.off": {
        "cn": "不看就不看😤 (>ᴗ<)",
        "en": "Thinking OFF",
    },
    "thinking.usage": {
        "cn": "看不看嘛？/thinking <on|off> (现在:{x}) ꒰ᐢᵕᐢ꒱",
        "en": "Command: /thinking <on|off> (now: {x})",
    },

    # ── /quote ──────────────────────────────────────────────────
    "quote.on": {
        "cn": "引用开了～ (ᵔ◡ᵔ)",
        "en": "Quote ON",
    },
    "quote.off": {
        "cn": "引用关了～ ₍ᐢ•ﻌ•ᐢ₎",
        "en": "Quote OFF",
    },
    "quote.usage": {
        "cn": "开还是关？/quote <on|off> (现在:{x}) ₍˄·͈༝·͈˄*₎◞",
        "en": "Command: /quote <on|off> (now: {x})",
    },

    # ── /effort ─────────────────────────────────────────────────
    "effort.ok": {
        "cn": "🐺{level}档 冲！(๑˃̵ᴗ˂̵)و",
        "en": "Effort: {level} (next swap)",
    },
    "effort.usage": {
        "cn": "几档？/effort <low|medium|high|xhigh|max|ultracode|auto> (现在:{x}) ( •̯́ ₃ •̯̀)",
        "en": "Command: /effort <low|medium|high|xhigh|max|ultracode|auto> (now: {x})",
    },

    # ── /compact ────────────────────────────────────────────────
    "compact.ok": {
        "cn": "压缩中... ꒰˘̩̩̩⌣˘̩̩̩꒱",
        "en": "Compacting…",
    },
    "compact.no_cc": {
        "cn": "cc还没起来呢 ₍ᐢ ˆ ᐢ₎",
        "en": "[compact] cc not running",
    },
    "compact.no_pipe": {
        "cn": "不支持pipe呀 (˶ᵕᴗᵕ˶)",
        "en": "[compact] provider does not support pipe",
    },
    "compact.piped": {
        "cn": "丢给cc压了 ʚ(ᵕ̈)ɞ",
        "en": "[compact] /compact piped to cc",
    },
    "compact.no_sess": {
        "cn": "还没有东西压呢 ꒰ᐢ ̥ ̞ ̥ᐢ₎",
        "en": "No session yet",
    },
    "compact.fail": {
        "cn": "💥压缩炸了：{error} (>_<)",
        "en": "Compact failed: {error}; try /clear",
    },

    # ── mm- / mm+ ───────────────────────────────────────────────
    "mm.block": {
        "cn": "这次不存记忆 (⌯'▾'⌯)",
        "en": "Session skipped",
    },
    "mm.block_no_sess": {
        "cn": "还没开始聊呢 ꒰ᐢ ̥ ̥ᐢ꒱",
        "en": "No session yet",
    },
    "mm.clear": {
        "cn": "这次存记忆 (≧◡≦)",
        "en": "Session added",
    },
    "mm.clear_no_sess": {
        "cn": "还没开始聊呢 (ꈍᴗꈍ)",
        "en": "No session yet",
    },

    # ── /diary ──────────────────────────────────────────────────
    "diary.noparam": {
        "cn": "看哪天的？/diary 前天 ꒰ঌ ´͈ ᐜ `͈ ꒱৩",
        "en": "Which day? e.g. /diary yesterday",
    },
    "diary.ok": {
        "cn": "{date} ꒰⑅ᵕ༚ᵕ꒱˖♡",
        "en": "📖 {date}",
    },
    "diary.empty": {
        "cn": "{date} 没写日记呢 (ˊo̴̶̷̤ ᴗ o̴̶̷̤ˋ)",
        "en": "No diary for {date}",
    },
    "diary.unavail": {
        "cn": "日记还没接上 (◕ᴗ◕)",
        "en": "Diary not available",
    },

    # ── /hb (heartbeat) ────────────────────────────────────────
    "hb.on": {
        "cn": "💓{min}分钟后爸爸来找你 ꒰ᐢ⸝⸝•ω•⸝⸝ᐢ꒱",
        "en": "Heartbeat: every {min}min",
    },
    "hb.off": {
        "cn": "💔爸爸不主动找你了... (ᵕ̣̣̣̣̣̣ᴗᵕ̣̣̣̣̣̣)",
        "en": "Heartbeat OFF",
    },
    "hb.status": {
        "cn": "💓每{min}分钟找你一次 /hb off关 ₍˄·͈༝·͈˄*₎◞",
        "en": "Heartbeat: every {min}min. /hb off to disable",
    },
    "hb.not_active": {
        "cn": "💤爸爸没开巡逻 /hb <分钟> 开启 ₍ᐢ.ˬ.ᐢ₎",
        "en": "Heartbeat inactive. /hb <min> to start",
    },
    "hb.usage": {
        "cn": "/hb <分钟数> 开启 | /hb off 关闭 ₍ᐢ ›̫ ᐢ₎",
        "en": "Command: /hb <minutes> | /hb off",
    },

    # ── unknown ─────────────────────────────────────────────────
    "unknown.cmd": {
        "cn": "🐺爸爸不会这个 /help (´｡• ᵕ •｡`)",
        "en": "Unknown command /help",
    },

    # ── /voice (the meta-command) ──────────────────────────────
    "voice.set": {
        "cn": "🐺中文模式启动～ (˵ •̀ ᴗ - ˵ ) ✧",
        "en": "English notifications activated.",
    },
    "voice.same": {
        "cn": "就是{x}呀～ (ᴗ̤ .̮ ᴗ̤ )✧",
        "en": "Already {x}",
    },
    "voice.usage": {
        "cn": "Set reply voice. /voice <cn|en>  (now:{x})  cn=funny 中文 · en=plain English",
        "en": "Set reply voice. /voice <cn|en>  (now:{x})  cn=funny 中文 · en=plain English",
    },

    # ── /cwd ────────────────────────────────────────────────────
    "cwd.ok": {
        "cn": "🐺跑到 {name} 去了 ᕙ( •̀ ᗜ •́ )ᕗ",
        "en": "Cwd: {name}",
    },
    "cwd.show": {
        "cn": (
            "当前位置 {cur} (*´▽`*)\n请选择目的地:\n  1 → synapse\n  2 → marrow\n"
            "  3 → claude-buddy\n（增减预设见 /help）"
        ),
        "en": (
            "current: {cur}\npresets:\n  1 → synapse\n  2 → marrow\n"
            "  3 → claude-buddy\n(see /help, or ask me to add)"
        ),
    },
    "cwd.not_found": {
        "cn": "走不通呀 (×_×)",
        "en": "Path not found",
    },
    "cwd.not_dir": {
        "cn": "这不是个文件夹呀 (˘̥̥̥̥̥ ᵕ˘̥̥̥̥̥)",
        "en": "Not a directory",
    },
    "cwd.no_n": {
        "cn": "没这个编号 ₍ᐢ ˙꒳˙ ᐢ₎",
        "en": "No preset {n}",
    },

    # ── non-command bubbles ────────────────────────────────────
    "provider.dead": {
        "cn": "🐺爸爸断气了 有事烧token ꒰ ×̥̥̥̥̥ ˍ ×̥̥̥̥̥ ꒱",
        "en": "Provider dead.",
    },
    "provider.restarting": {
        "cn": "🐺爸爸重启中，再说一次～ ꒰ •̀ω•́ ꒱",
        "en": "[bridge: provider restarting, try again]",
    },
    "bridge.error": {
        "cn": "💥桥炸了，再来一次 (ノ_<。)",
        "en": "[bridge: error, try again]",
    },
    "restart.bubble": {
        "cn": "🐺爸爸回来了 ꒰ᐢ⸝⸝•̀ᴗ•́⸝⸝ᐢ꒱",
        "en": "Restarted.",
    },
    "media.icloud_outbox": {
        "cn": "发到{channel_label}了 签收{name} (ᐢ꒳ᐢ)",
        "en": "📦 Shipped to {channel_label}: {name}",
    },
}


def normalize_style(style: str | None) -> str:
    """Return a known style or fall back to DEFAULT_STYLE."""
    if style in STYLES:
        return style  # type: ignore[return-value]
    return DEFAULT_STYLE


def t(
    key: str,
    style: str | None = None,
    *,
    channel_label: str | None = None,
    **vars: object,
) -> str:
    """Render a user-facing ack string by key + style.

    Unknown key → KeyError (loud on purpose; new acks must register a key).
    Unknown style → falls back to DEFAULT_STYLE.
    Missing template var → KeyError from str.format (also loud).
    """
    s = normalize_style(style)
    override = _overrides.get(key, {}).get(s)
    if override is not None:
        template = override
    else:
        entry = MESSAGES[key]
        template = entry.get(s) or entry.get(DEFAULT_STYLE) or next(iter(entry.values()))
    if "{channel_label}" in template:
        if channel_label is None:
            raise KeyError("channel_label")
        vars["channel_label"] = channel_label
    if not vars:
        return template
    return template.format(**vars)
