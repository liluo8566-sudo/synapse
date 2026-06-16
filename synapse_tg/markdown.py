from __future__ import annotations

import html
import re


_TOKEN_RE = re.compile(r"\x00MD(\d+)\x00")
_FENCE_RE = re.compile(r"```([^\n`]*)\n(.*?)```", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`([^`\n]*)`")


def gfm_to_tg_html(text: str) -> str:
    if not text:
        return ""

    tokens: list[str] = []

    def store(value: str) -> str:
        tokens.append(value)
        return f"\x00MD{len(tokens) - 1}\x00"

    def replace_fence(match: re.Match[str]) -> str:
        info = match.group(1).strip()
        lang = info.split(None, 1)[0] if info else ""
        code = html.escape(match.group(2), quote=False)
        class_attr = f' class="language-{html.escape(lang, quote=True)}"' if lang else ""
        return store(f"<pre><code{class_attr}>{code}</code></pre>")

    def replace_inline_code(match: re.Match[str]) -> str:
        return store(f"<code>{html.escape(match.group(1), quote=False)}</code>")

    prepared = _FENCE_RE.sub(replace_fence, text)
    prepared = _INLINE_CODE_RE.sub(replace_inline_code, prepared)
    return _render_blocks(prepared, tokens)


def _render_blocks(text: str, tokens: list[str]) -> str:
    lines = text.splitlines(keepends=True)
    rendered: list[str] = []
    quote_lines: list[str] = []
    quote_newlines: list[str] = []

    def flush_quote() -> None:
        if not quote_lines:
            return
        body = "".join(
            _render_inline(line, tokens) + newline
            for line, newline in zip(quote_lines, quote_newlines, strict=True)
        )
        rendered.append(f"<blockquote>{body}</blockquote>")
        quote_lines.clear()
        quote_newlines.clear()

    for raw_line in lines:
        line = raw_line[:-1] if raw_line.endswith("\n") else raw_line
        newline = "\n" if raw_line.endswith("\n") else ""
        match = re.match(r"^\s*>\s?(.*)$", line)
        if match:
            quote_lines.append(match.group(1))
            quote_newlines.append(newline)
            continue
        flush_quote()
        rendered.append(_render_inline(line, tokens) + newline)

    flush_quote()
    return "".join(rendered)


def _render_inline(text: str, tokens: list[str]) -> str:
    rendered: list[str] = []
    i = 0

    while i < len(text):
        token_match = _TOKEN_RE.match(text, i)
        if token_match:
            rendered.append(tokens[int(token_match.group(1))])
            i = token_match.end()
            continue

        replacement = _consume_delimited(text, i, "**", "b", tokens)
        if replacement:
            value, i = replacement
            rendered.append(value)
            continue

        replacement = _consume_delimited(text, i, "__", "b", tokens)
        if replacement:
            value, i = replacement
            rendered.append(value)
            continue

        replacement = _consume_single_italic(text, i, "*", tokens)
        if replacement:
            value, i = replacement
            rendered.append(value)
            continue

        replacement = _consume_single_italic(text, i, "_", tokens)
        if replacement:
            value, i = replacement
            rendered.append(value)
            continue

        replacement = _consume_delimited(text, i, "~~", "s", tokens)
        if replacement:
            value, i = replacement
            rendered.append(value)
            continue

        replacement = _consume_link(text, i, tokens)
        if replacement:
            value, i = replacement
            rendered.append(value)
            continue

        rendered.append(html.escape(text[i], quote=False))
        i += 1

    return "".join(rendered)


def _consume_delimited(
    text: str, start: int, marker: str, tag: str, tokens: list[str]
) -> tuple[str, int] | None:
    if not text.startswith(marker, start):
        return None

    close = _find_closing_marker(text, start + len(marker), marker)
    if close == -1:
        return None

    inner = _render_inline(text[start + len(marker) : close], tokens)
    return f"<{tag}>{inner}</{tag}>", close + len(marker)


def _find_closing_marker(text: str, start: int, marker: str) -> int:
    pos = start
    while True:
        close = text.find(marker, pos)
        if close == -1:
            return -1
        next_pos = close + len(marker)
        if next_pos >= len(text) or text[next_pos] != marker[0]:
            return close
        pos = close + 1


def _consume_single_italic(
    text: str, start: int, marker: str, tokens: list[str]
) -> tuple[str, int] | None:
    if not text.startswith(marker, start) or text.startswith(marker * 2, start):
        return None
    if marker == "_" and _is_word_char(text[start - 1 : start]):
        return None

    close = _find_single_italic_close(text, start + 1, marker)
    if close == -1:
        return None
    if marker == "_" and _is_word_char(text[close + 1 : close + 2]):
        return None

    inner = _render_inline(text[start + 1 : close], tokens)
    return f"<i>{inner}</i>", close + 1


def _find_single_italic_close(text: str, start: int, marker: str) -> int:
    pos = start
    while True:
        close = text.find(marker, pos)
        if close == -1:
            return -1
        if text[close - 1 : close] != marker and text[close + 1 : close + 2] != marker:
            return close
        pos = close + 1


def _consume_link(text: str, start: int, tokens: list[str]) -> tuple[str, int] | None:
    if not text.startswith("[", start) or text[start - 1 : start] == "!":
        return None

    label_end = text.find("]", start + 1)
    if label_end == -1 or text[label_end + 1 : label_end + 2] != "(":
        return None

    url_end = text.find(")", label_end + 2)
    if url_end == -1:
        return None

    label = _render_inline(text[start + 1 : label_end], tokens)
    url = html.escape(text[label_end + 2 : url_end], quote=True)
    return f'<a href="{url}">{label}</a>', url_end + 1


def _is_word_char(value: str) -> bool:
    return bool(value and re.match(r"\w", value))
