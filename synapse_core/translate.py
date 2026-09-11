"""Optional thinking-bubble translation via an external CLI subprocess."""

from __future__ import annotations

import asyncio
import logging
import re

logger = logging.getLogger(__name__)

_CJK_RE = re.compile(r"[一-鿿぀-ヿ가-힯]")
_LETTER_RE = re.compile(r"[^\W\d_]", re.UNICODE)


def needs_translation(text: str, target: str) -> bool:
    """Whether `text` is worth sending to translate() for `target`."""
    if not text:
        return False
    letters = _LETTER_RE.findall(text)
    if target.startswith("zh"):
        if not letters:
            return False
        cjk = len(_CJK_RE.findall(text))
        return (cjk / len(letters)) < 0.3
    return True


async def translate(
    text: str, target: str, cmd: list[str], timeout: float = 45.0
) -> str | None:
    """Run `cmd`, feed it a translation prompt on stdin, return stripped
    stdout. None on non-zero exit, timeout, or any exception — never raises."""
    prompt = (
        f"Translate the following text into {target}. Preserve paragraph "
        f"breaks and markdown. Output only the translation, nothing else.\n\n{text}"
    )
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except Exception as e:
        logger.warning("translate: failed to spawn %s: %s", cmd, e)
        return None

    try:
        out, err = await asyncio.wait_for(
            proc.communicate(prompt.encode("utf-8")), timeout=timeout
        )
    except asyncio.TimeoutError:
        logger.warning("translate: timed out after %.1fs (%s)", timeout, cmd)
        try:
            proc.kill()
            await proc.communicate()
        except Exception:
            pass
        return None
    except Exception as e:
        logger.warning("translate: subprocess error: %s", e)
        return None

    if proc.returncode != 0:
        logger.warning(
            "translate: exit %s: %s", proc.returncode, err.decode("utf-8", "replace")[:500]
        )
        return None

    return out.decode("utf-8", "replace").strip()


def default_translate_cmd(claude_bin: str, model: str) -> list[str]:
    return [
        claude_bin, "-p", "--model", model,
        "--setting-sources", "", "--strict-mcp-config",
    ]
