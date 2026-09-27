"""Transcription locale des messages vocaux (optionnelle, via faster-whisper)."""

from __future__ import annotations

import asyncio
from functools import lru_cache
from pathlib import Path


def available() -> bool:
    try:
        import faster_whisper  # noqa: F401
    except ImportError:
        return False
    return True


@lru_cache(maxsize=1)
def _model(name: str):
    from faster_whisper import WhisperModel

    # "auto" utilise le GPU s'il est disponible, sinon le CPU.
    return WhisperModel(name, device="auto", compute_type="default")


def _transcribe(path: Path, model_name: str) -> str:
    segments, _ = _model(model_name).transcribe(str(path), language="fr", vad_filter=True)
    return " ".join(s.text.strip() for s in segments).strip()


async def transcribe(path: Path, model_name: str) -> str:
    return await asyncio.to_thread(_transcribe, path, model_name)
