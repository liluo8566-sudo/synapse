"""ElevenLabs TTS synthesis for voice bubbles."""

from __future__ import annotations

import logging
import tempfile

import httpx

logger = logging.getLogger(__name__)

_TEXT_CAP = 1000
_TIMEOUT_S = 30.0
_API_BASE = "https://api.elevenlabs.io/v1/text-to-speech"


async def synthesize(text: str, cfg) -> str | None:
    """POST text to ElevenLabs and return the path to the mp3 temp file.

    Returns None on any failure (non-200, timeout, network error).
    cfg must expose: tts_api_key, tts_voice_id, tts_model_id, tts_output_format.
    """
    if len(text) > _TEXT_CAP:
        logger.warning("tts: text truncated from %d to %d chars", len(text), _TEXT_CAP)
        text = text[:_TEXT_CAP]

    url = f"{_API_BASE}/{cfg.tts_voice_id}?output_format={cfg.tts_output_format}"
    headers = {"xi-api-key": cfg.tts_api_key}
    payload = {"text": text, "model_id": cfg.tts_model_id}

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_S) as client:
            resp = await client.post(url, headers=headers, json=payload)
        if resp.status_code != 200:
            logger.warning(
                "tts: non-200 response %d for voice synthesis", resp.status_code
            )
            return None
        fd, path = tempfile.mkstemp(suffix=".mp3", dir=tempfile.gettempdir())
        import os
        os.close(fd)
        with open(path, "wb") as fh:
            fh.write(resp.content)
        return path
    except Exception as e:
        logger.warning("tts: synthesis failed: %s", e)
        return None
