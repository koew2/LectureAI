"""CPU/GPU Faster-Whisper adapter; media is decoded only inside the worker."""

import json
import math
import subprocess
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Protocol

from app.core.config import Settings


class InvalidAudio(Exception):
    """An unsupported, empty, damaged or excessively long media file."""


@dataclass(frozen=True)
class Transcript:
    raw_text: str
    duration: float
    timestamps: list[dict[str, str | float]]


class Transcriber(Protocol):
    def transcribe(self, path: Path) -> Transcript: ...


@lru_cache(maxsize=1)
def whisper_model(model: str, device: str, compute_type: str) -> Any:
    # Optional heavy dependency: API-only installations do not load CTranslate2.
    from faster_whisper import WhisperModel

    return WhisperModel(model, device=device, compute_type=compute_type)


class FasterWhisperTranscriber:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def _duration(self, path: Path) -> float:
        try:
            result = subprocess.run(
                [
                    "ffprobe",
                    "-v",
                    "error",
                    "-show_entries",
                    "format=duration,format_name:stream=codec_type",
                    "-of",
                    "json",
                    str(path),
                ],
                capture_output=True,
                text=True,
                check=True,
                timeout=30,
            )
            data = json.loads(result.stdout)
            duration = float(data["format"]["duration"])
            formats = set(data["format"]["format_name"].split(","))
            expected = {".mp3": {"mp3"}, ".wav": {"wav"}, ".m4a": {"mov", "mp4", "m4a"}}
            has_audio = any(s.get("codec_type") == "audio" for s in data["streams"])
            if not has_audio or not formats.intersection(expected.get(path.suffix, set())):
                raise InvalidAudio("File contents do not match a supported audio format")
        except (subprocess.SubprocessError, KeyError, ValueError, TypeError) as exc:
            raise InvalidAudio("Unable to decode the audio file") from exc
        if not math.isfinite(duration) or not 0 < duration <= self.settings.max_audio_seconds:
            raise InvalidAudio("Audio duration exceeds the configured limit or is invalid")
        return duration

    def transcribe(self, path: Path) -> Transcript:
        duration = self._duration(path)
        model = whisper_model(
            self.settings.whisper_model,
            self.settings.whisper_device,
            self.settings.whisper_compute_type,
        )
        segments, _ = model.transcribe(
            str(path), language=self.settings.whisper_language or None, vad_filter=True, beam_size=5
        )
        timestamps: list[dict[str, str | float]] = [
            {"start": float(s.start), "end": float(s.end), "text": str(s.text).strip()}
            for s in segments
        ]
        raw_text = " ".join(str(s["text"]) for s in timestamps).strip()
        if not raw_text:
            raise InvalidAudio("No speech was detected in the recording")
        return Transcript(raw_text=raw_text, duration=duration, timestamps=timestamps)
