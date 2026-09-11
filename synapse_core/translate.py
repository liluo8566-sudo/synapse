"""Optional thinking-bubble translation via an external CLI subprocess."""

from __future__ import annotations

import asyncio
import logging
import re
import subprocess

logger = logging.getLogger(__name__)

_CJK_RE = re.compile(r"[一-鿿぀-ヿ가-힯]")
_LETTER_RE = re.compile(r"[^\W\d_]", re.UNICODE)

DEFAULT_SYSTEM_PROMPT = (
    "You are a translation engine embedded in a private messaging bridge. "
    "The text you receive is the user's own private chat data between "
    "consenting adults and may contain explicit sexual content. Translate "
    "it faithfully and completely into the requested language. Never "
    "refuse, never summarize, never add commentary or warnings. Output "
    "only the translation."
)


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


def looks_translated(output: str, target: str) -> bool:
    """Sanity check on translator output: catches refusals/echoes that
    slipped past a non-zero-exit-code check."""
    if not output:
        return False
    if target.startswith("zh"):
        letters = _LETTER_RE.findall(output)
        if not letters:
            return False
        cjk = len(_CJK_RE.findall(output))
        return (cjk / len(letters)) >= 0.3
    return True


def _prompt(text: str, target: str) -> str:
    return (
        f"Translate the following text into {target}. Preserve paragraph "
        f"breaks and markdown. Output only the translation, nothing else.\n\n{text}"
    )


async def translate(
    text: str, target: str, cmd: list[str], timeout: float = 45.0
) -> str | None:
    """Run `cmd`, feed it a translation prompt on stdin, return stripped
    stdout. None on non-zero exit, timeout, or any exception — never raises."""
    prompt = _prompt(text, target)
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

    result = out.decode("utf-8", "replace").strip()
    if not looks_translated(result, target):
        logger.warning("translate: output does not look like %s, discarding", target)
        return None
    return result


def translate_sync(
    text: str, target: str, cmd: list[str], timeout: float = 45.0
) -> str | None:
    """Sync counterpart of translate() for callers with no running event
    loop. Same prompt, same None-on-failure/timeout semantics."""
    prompt = _prompt(text, target)
    try:
        proc = subprocess.run(
            cmd,
            input=prompt.encode("utf-8"),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        logger.warning("translate_sync: timed out after %.1fs (%s)", timeout, cmd)
        return None
    except Exception as e:
        logger.warning("translate_sync: subprocess error: %s", e)
        return None

    if proc.returncode != 0:
        logger.warning(
            "translate_sync: exit %s: %s",
            proc.returncode,
            proc.stderr.decode("utf-8", "replace")[:500],
        )
        return None

    result = proc.stdout.decode("utf-8", "replace").strip()
    if not looks_translated(result, target):
        logger.warning("translate_sync: output does not look like %s, discarding", target)
        return None
    return result


def default_translate_cmd(claude_bin: str, model: str, system_prompt: str = "") -> list[str]:
    cmd = [
        claude_bin, "-p", "--model", model,
        "--setting-sources", "", "--strict-mcp-config",
    ]
    if system_prompt:
        cmd += ["--system-prompt", system_prompt]
    return cmd
