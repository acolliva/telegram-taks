"""Italian speech-to-text for voice notes.

Two engines, chosen with TRANSCRIBER in .env:

- ``local``: faster-whisper running on the Raspberry itself (free, private, a bit slow).
  Install with ``uv sync --extra voice``. The model is downloaded on first use.
- ``openai``: OpenAI's transcription API (fast and accurate, costs a fraction of a cent per note).
- ``off``: voice notes are stored as tasks with the audio attached but no text.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Protocol

import httpx

log = logging.getLogger(__name__)


class Transcriber(Protocol):
    async def transcribe(self, path: Path) -> str: ...


class NoTranscriber:
    async def transcribe(self, path: Path) -> str:
        return ""


class LocalWhisper:
    def __init__(self, model: str = "small", language: str = "it", threads: int = 4):
        self.model_name = model
        self.language = language
        self.threads = threads
        self._model = None
        self._lock = asyncio.Lock()  # one transcription at a time: the Pi has 4 cores

    def _load(self):
        if self._model is None:
            from faster_whisper import WhisperModel  # optional dependency

            log.info("Loading whisper model %s (first time downloads it)…", self.model_name)
            self._model = WhisperModel(self.model_name, device="cpu", compute_type="int8",
                                       cpu_threads=self.threads)
        return self._model

    def _run(self, path: Path) -> str:
        model = self._load()
        segments, _ = model.transcribe(str(path), language=self.language, beam_size=1, vad_filter=True,
                                       initial_prompt="Promemoria di cose da fare in casa.")
        return " ".join(s.text.strip() for s in segments).strip()

    async def transcribe(self, path: Path) -> str:
        async with self._lock:
            return await asyncio.to_thread(self._run, path)

    async def warm_up(self) -> None:
        """Load the model at startup, so the first voice note isn't extra slow."""
        try:
            await asyncio.to_thread(self._load)
        except Exception:
            log.exception("Could not load the whisper model; voice notes will be saved without text")


class OpenAITranscriber:
    URL = "https://api.openai.com/v1/audio/transcriptions"

    def __init__(self, api_key: str, model: str = "gpt-4o-mini-transcribe", language: str = "it"):
        self.api_key = api_key
        self.model = model
        self.language = language

    async def transcribe(self, path: Path) -> str:
        async with httpx.AsyncClient(timeout=60) as client:
            r = await client.post(
                self.URL,
                headers={"Authorization": f"Bearer {self.api_key}"},
                data={"model": self.model, "language": self.language, "response_format": "json"},
                files={"file": (path.name, path.read_bytes(), "audio/ogg")},
            )
            r.raise_for_status()
            return r.json().get("text", "").strip()


def make_transcriber(kind: str, *, model: str, openai_key: str | None, openai_model: str) -> Transcriber:
    kind = (kind or "off").lower()
    if kind == "local":
        return LocalWhisper(model)
    if kind == "openai":
        if not openai_key:
            raise SystemExit("TRANSCRIBER=openai needs OPENAI_API_KEY")
        return OpenAITranscriber(openai_key, openai_model)
    return NoTranscriber()
