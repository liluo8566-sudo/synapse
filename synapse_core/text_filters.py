"""Shared outbound text filters.

HTML-comment silence protocol: the model can wrap a whole reply (or any
fragment) in `<!-- ... -->` to keep it off the wire. Both bridges strip
complete comments before delivery; a reply that strips to empty sends
nothing (the thinking bubble, where enabled, still ships).
"""

from __future__ import annotations

import re

_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)


def strip_html_comments(text: str) -> str:
    """Remove all complete HTML comments from text and strip whitespace."""
    return _HTML_COMMENT_RE.sub("", text).strip()
