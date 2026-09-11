"""Independent, reusable audio capability. Each call owns a fresh provider request."""

from __future__ import annotations

import math
import mimetypes
from collections.abc import AsyncIterable, AsyncIterator
from contextlib import AbstractAsyncContextManager
from copy import deepcopy
from dataclasses import replace
from os import PathLike
from pathlib import Path

from agently_stage import default_stage_call_bridge

from agently.types.data.audio import (
    AudioCapabilityError, AudioInput, AudioOperation, PCMFormat, SpeechOptions,
    SpeechRequest, SpeechResult, TranscriptEvent, TranscriptResult,
    TranscriptionOptions, TranscriptionRequest,
)
from agently.types.plugins.AudioModelRequester import AudioModelRequester


class AudioModelRequest:
    def __init__(
        self, driver: AudioModelRequester, *, tts_model: str | None = None, stt_model: str | None = None,
    ):
        self.__driver = driver
        self.__tts_model = tts_model
        self.__stt_model = stt_model

    @property
    def supported_operations(self) -> frozenset[AudioOperation]:
        """Driver support, not proof of model support, authorization or service health."""
        return self.__driver.supported_operations

    def __require(self, operation: AudioOperation) -> None:
        if operation not in self.supported_operations:
            raise AudioCapabilityError(f"Audio driver {self.__driver.name!r} does not support {operation}.")

    @staticmethod
    def __model(selected: str | None, default: str | None) -> str:
        value = default if selected is None else selected
        if not isinstance(value, str) or not value.strip():
            raise ValueError("An explicit non-empty audio model is required.")
        return value

    def __speech(
        self, text: str, model: str | None, voice: str | None, options: SpeechOptions | None,
    ) -> SpeechRequest:
        if not isinstance(text, str) or not text.strip():
            raise ValueError("TTS text must be non-empty.")
        if voice is not None and (not isinstance(voice, str) or not voice.strip()):
            raise ValueError("voice must be non-empty when supplied.")
        selected = options or SpeechOptions()
        if isinstance(selected.speed, bool) or not math.isfinite(selected.speed) or selected.speed <= 0:
            raise ValueError("Speech speed must be finite and positive.")
        if selected.response_format not in {"wav", "mp3", "opus", "aac", "flac", "pcm"}:
            raise ValueError("Unsupported audio format.")
        selected = replace(selected, extra=deepcopy(dict(selected.extra)))
        return SpeechRequest(text, self.__model(model, self.__tts_model), voice, selected)

    def __transcription(
        self, audio: AudioInput | str | PathLike[str], model: str | None, options: TranscriptionOptions | None,
    ) -> TranscriptionRequest:
        selected_model = self.__model(model, self.__stt_model)
        if not isinstance(audio, AudioInput):
            path = Path(audio)
            audio = AudioInput(path.read_bytes(), path.name, mimetypes.guess_type(path.name)[0] or "application/octet-stream")
        if not isinstance(audio.data, bytes) or not audio.data:
            raise ValueError("STT requires non-empty audio bytes.")
        if not audio.filename or not audio.content_type:
            raise ValueError("Audio filename and content_type must be non-empty.")
        selected = options or TranscriptionOptions()
        return TranscriptionRequest(audio, selected_model, replace(selected, extra=deepcopy(dict(selected.extra))))

    async def async_tts(
        self, text: str, *, model: str | None = None, voice: str | None = None,
        options: SpeechOptions | None = None,
    ) -> SpeechResult:
        self.__require("tts")
        return await self.__driver.tts(self.__speech(text, model, voice, options))

    def tts(
        self, text: str, *, model: str | None = None, voice: str | None = None,
        options: SpeechOptions | None = None,
    ) -> SpeechResult:
        return default_stage_call_bridge.as_sync(self.async_tts)(text, model=model, voice=voice, options=options)

    async def async_stt(
        self, audio: AudioInput | str | PathLike[str], *, model: str | None = None,
        options: TranscriptionOptions | None = None,
    ) -> TranscriptResult:
        self.__require("stt")
        return await self.__driver.stt(self.__transcription(audio, model, options))

    def stt(
        self, audio: AudioInput | str | PathLike[str], *, model: str | None = None,
        options: TranscriptionOptions | None = None,
    ) -> TranscriptResult:
        return default_stage_call_bridge.as_sync(self.async_stt)(audio, model=model, options=options)

    def stream_tts(
        self, text: str, *, model: str | None = None, voice: str | None = None,
        options: SpeechOptions | None = None,
    ) -> AbstractAsyncContextManager[AsyncIterator[bytes]]:
        self.__require("stream_tts")
        return self.__driver.stream_tts(self.__speech(text, model, voice, options))

    def stream_stt(
        self, audio: AudioInput | str | PathLike[str], *, model: str | None = None,
        options: TranscriptionOptions | None = None,
    ) -> AbstractAsyncContextManager[AsyncIterator[TranscriptEvent]]:
        self.__require("stream_stt")
        return self.__driver.stream_stt(self.__transcription(audio, model, options))

    def stream_stt_input(
        self, chunks: AsyncIterable[bytes], *, model: str | None = None,
        audio_format: PCMFormat | None = None, options: TranscriptionOptions | None = None,
    ) -> AbstractAsyncContextManager[AsyncIterator[TranscriptEvent]]:
        self.__require("stream_stt_input")
        selected = options or TranscriptionOptions()
        return self.__driver.stream_stt_input(
            chunks, model=self.__model(model, self.__stt_model), audio_format=audio_format or PCMFormat(),
            options=replace(selected, extra=deepcopy(dict(selected.extra))),
        )
