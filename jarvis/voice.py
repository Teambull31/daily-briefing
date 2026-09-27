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
    import ctranslate2
    from faster_whisper import WhisperModel

    # GPU NVIDIA si présent ; sinon (cartes AMD comprises, non gérées par CTranslate2)
    # CPU en int8, rapide et léger en mémoire.
    if ctranslate2.get_cuda_device_count() > 0:
        return WhisperModel(name, device="cuda", compute_type="float16")
    return WhisperModel(name, device="cpu", compute_type="int8")


def _transcribe(path: Path, model_name: str) -> str:
    segments, _ = _model(model_name).transcribe(str(path), language="fr", vad_filter=True)
    return " ".join(s.text.strip() for s in segments).strip()


async def transcribe(path: Path, model_name: str) -> str:
    return await asyncio.to_thread(_transcribe, path, model_name)
