"""Typed audio inputs, options and results; independent of text Prompt data."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Literal

AudioFormat = Literal["wav", "mp3", "opus", "aac", "flac", "pcm"]
AudioOperation = Literal["tts", "stt", "stream_tts", "stream_stt", "stream_stt_input"]


class AudioCapabilityError(RuntimeError):
    """A requested audio operation is not available on the selected driver."""


class AudioProtocolError(RuntimeError):
    """The provider did not deliver a valid result or required terminal event."""


@dataclass(frozen=True)
class AudioConnection:
    base_url: str
    api_key: str = field(default="", repr=False)
    timeout: float = 120.0


@dataclass(frozen=True)
class AudioInput:
    data: bytes = field(repr=False)
    filename: str = "audio.wav"
    content_type: str = "audio/wav"


@dataclass(frozen=True)
class SpeechOptions:
    response_format: AudioFormat = "wav"
    speed: float = 1.0
    language: str | None = None
    instructions: str | None = None
    extra: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class TranscriptionOptions:
    language: str | None = None
    prompt: str | None = None
    extra: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class SpeechRequest:
    text: str
    model: str
    voice: str | None = None
    options: SpeechOptions = field(default_factory=SpeechOptions)


@dataclass(frozen=True)
class TranscriptionRequest:
    audio: AudioInput
    model: str
    options: TranscriptionOptions = field(default_factory=TranscriptionOptions)


@dataclass(frozen=True)
class SpeechResult:
    data: bytes = field(repr=False)
    media_type: str
    model: str


@dataclass(frozen=True)
class TranscriptResult:
    text: str
    model: str
    language: str | None = None
    duration: float | None = None


@dataclass(frozen=True)
class TranscriptEvent:
    """Delta text is incremental; done text is the authoritative full transcript."""

    kind: Literal["delta", "done"]
    text: str


@dataclass(frozen=True)
class PCMFormat:
    """Raw input frames; no codec conversion is implied by this declaration."""

    sample_rate: int = 16000
    channels: int = 1
    encoding: Literal["s16le"] = "s16le"
