"""Synthèse vocale locale et gratuite (Piper) : rappels et réponses en messages vocaux,
et sons optionnels sur les haut-parleurs du PC."""

from __future__ import annotations

import asyncio
import io
import logging
import re
import shutil
import subprocess
import tempfile
import threading
import wave
from pathlib import Path

log = logging.getLogger("jarvis.tts")

# Émojis, pictogrammes et symboles : illisibles à voix haute.
_EMOJI = re.compile("[\U0001F000-\U0001FAFF\u2300-\u23FF\u2600-\u27BF\u2B00-\u2BFF\uFE0F\u200D]")
_URL = re.compile(r"https?://\S+")
_COMMAND = re.compile(r"(?<!\w)/[a-z_]+")


def clean_for_speech(text: str, max_chars: int = 600) -> str:
    text = _URL.sub("", text)
    text = _EMOJI.sub("", text)
    text = _COMMAND.sub("", text)
    text = re.sub(r"[*_`#>\[\]|]", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > max_chars:  # un vocal trop long ne sera pas écouté
        cut = text.rfind(".", 0, max_chars)
        text = text[: cut + 1 if cut > max_chars // 2 else max_chars]
    return text


class Speaker:
    def __init__(self, voice_name: str, voices_dir: Path):
        self.voice_name = voice_name
        self.voices_dir = voices_dir
        self._voice = None
        self._lock = threading.Lock()

    @staticmethod
    def available() -> bool:
        try:
            import piper  # noqa: F401
        except ImportError:
            return False
        return True

    def _load(self):
        with self._lock:
            if self._voice is None:
                from piper import PiperVoice
                from piper.download_voices import download_voice

                model = self.voices_dir / f"{self.voice_name}.onnx"
                if not model.exists():  # premier usage : téléchargement de la voix (~60 Mo)
                    self.voices_dir.mkdir(parents=True, exist_ok=True)
                    download_voice(self.voice_name, self.voices_dir)
                self._voice = PiperVoice.load(model)
            return self._voice

    def wav_bytes(self, text: str) -> bytes:
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wav:
            self._load().synthesize_wav(text, wav)
        return buf.getvalue()

    def voice_note(self, text: str) -> tuple[bytes, str]:
        """Retourne (audio, format) : "ogg" (vrai message vocal Telegram) si ffmpeg est là, sinon "wav"."""
        wav = self.wav_bytes(text)
        if shutil.which("ffmpeg"):
            proc = subprocess.run(
                ["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "wav", "-i", "pipe:0",
                 "-c:a", "libopus", "-b:a", "32k", "-f", "ogg", "pipe:1"],
                input=wav, capture_output=True, timeout=60,
            )
            if proc.returncode == 0 and proc.stdout:
                return proc.stdout, "ogg"
            log.warning("Conversion Opus impossible : %s", proc.stderr.decode(errors="replace")[:200])
        return wav, "wav"

    async def speak(self, text: str) -> tuple[bytes, str] | None:
        text = clean_for_speech(text)
        if not text:
            return None
        return await asyncio.to_thread(self.voice_note, text)

    async def play_on_pc(self, text: str) -> None:
        """Lit le message sur les haut-parleurs du PC (PipeWire / PulseAudio / ALSA)."""
        player = next((p for p in ("pw-play", "paplay", "aplay") if shutil.which(p)), None)
        text = clean_for_speech(text)
        if not player or not text:
            return
        wav = await asyncio.to_thread(self.wav_bytes, text)
        with tempfile.NamedTemporaryFile(suffix=".wav") as tmp:
            tmp.write(wav)
            tmp.flush()
            proc = await asyncio.create_subprocess_exec(
                player, tmp.name, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL
            )
            await proc.wait()
