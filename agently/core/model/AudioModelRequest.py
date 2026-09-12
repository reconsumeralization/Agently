"""Independent, reusable audio capability. Each call owns a fresh provider request."""

from __future__ import annotations

import math
import mimetypes
from collections.abc import AsyncIterable, AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from copy import deepcopy
from dataclasses import replace
from os import PathLike
from pathlib import Path

from agently_stage import default_stage_call_bridge

from agently.types.data.audio import (
    AudioCapabilityError, AudioInput, AudioOperation, PCMFormat, SpeechOptions,
    SpeechRequest, SpeechResult, TranscriptResult,
    TranscriptionOptions, TranscriptionRequest, PCMStream, TextSource, TextSegmentOptions,
    TranscriptBlock, TranscriptSegment, TranscriptionStreamOptions,
)
from agently.types.plugins.AudioModelRequester import AudioModelRequester, TextSegmenter
from .audio_stream.flow import pull_flow
from .audio_stream.streams import (
    _PCMOutput, SentenceOutput, TranscriptionOutput, audio_windows, pcm_stream, sentence_stream,
    speech_chunks, speech_source, validate_transcription_stream,
)


class AudioModelRequest:
    def __init__(
        self, driver: AudioModelRequester, *, tts_model: str | None = None, stt_model: str | None = None,
    ):
        self.__driver = driver
        self.__tts_model = tts_model
        self.__stt_model = stt_model

    @property
    def supported_operations(self) -> frozenset[AudioOperation]:
        """Composed capability support, not proof of model support or service health."""
        operations: set[AudioOperation] = set()
        if "tts" in self.__driver.supported_operations:
            operations.update(("tts", "stream_tts", "stream_tts_with_auto_break"))
        if "stt" in self.__driver.supported_operations:
            operations.update(("stt", "stream_stt", "stream_stt_with_auto_break"))
        return frozenset(operations)

    @property
    def driver(self) -> AudioModelRequester:
        """Explicit provider-native access; its streaming contracts are not composed streams."""
        return self.__driver

    def __require(self, operation: AudioOperation) -> None:
        if operation not in self.supported_operations:
            raise AudioCapabilityError(f"Audio driver {self.__driver.name!r} does not support {operation}.")

    @staticmethod
    def __model(selected: str | None, default: str | None) -> str:
        value = default if selected is None else selected
        if not isinstance(value, str) or not value.strip():
            raise ValueError("An explicit non-empty audio model is required.")
        return value

    @staticmethod
    def __stream_extra(extra: dict[str, object], *, speech: bool) -> None:
        reserved = {"model", "stream", "response_format", "language"}
        reserved.update({"input", "voice", "speed", "instructions"} if speech else {"file", "prompt"})
        conflicts = reserved.intersection(extra)
        if conflicts:
            raise ValueError(f"Audio extra cannot override reserved fields: {sorted(conflicts)}")

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
        self, text: TextSource, *, model: str | None = None, voice: str | None = None,
        options: SpeechOptions | None = None, segments: TextSegmentOptions | None = None,
        segmenter: TextSegmenter | None = None, audio_format: PCMFormat | None = None, chunk_bytes: int = 8192,
    ) -> AbstractAsyncContextManager[PCMStream]:
        """Consume text chunks; yield one fixed-format, headerless PCM stream.

        Format is ready on context entry (which synthesizes the first nonempty
        segment). No implicit playback, resampling, retries or provisional Agent subscription.
        """
        self.__require("tts")
        template = self.__speech("_", model, voice, options)
        self.__stream_extra(dict(template.options.extra), speech=True)
        if template.options.response_format not in {"wav", "pcm"}:
            raise ValueError("Continuous TTS requires wav or pcm; use auto break for independent encoded audio.")
        output = _PCMOutput(audio_format, template.options.response_format == "pcm", chunk_bytes)
        source = speech_source(text, segments or TextSegmentOptions(), segmenter)

        async def request(part: str) -> SpeechResult:
            return await self.__driver.tts(replace(template, text=part))

        return pcm_stream(source, request, output)

    def stream_tts_with_auto_break(
        self, text: TextSource, *, model: str | None = None, voice: str | None = None,
        options: SpeechOptions | None = None, segments: TextSegmentOptions | None = None,
        segmenter: TextSegmenter | None = None,
    ) -> AbstractAsyncContextManager[AsyncIterator[SpeechResult]]:
        """Same text segmentation as stream_tts; yield independently usable audio segments."""
        self.__require("tts")
        template = self.__speech("_", model, voice, options)
        self.__stream_extra(dict(template.options.extra), speech=True)
        if template.options.response_format == "pcm":
            raise ValueError("Auto-break PCM lacks self-describing format; choose a container such as wav.")
        source = speech_source(text, segments or TextSegmentOptions(), segmenter)

        async def request(part: str) -> SpeechResult:
            return await self.__driver.tts(replace(template, text=part))

        return pull_flow(source, request, speech_chunks)

    def stream_stt(
        self, audio: AsyncIterable[bytes], *, audio_format: PCMFormat, model: str | None = None,
        options: TranscriptionOptions | None = None, stream_options: TranscriptionStreamOptions | None = None,
    ) -> AbstractAsyncContextManager[AsyncIterator[TranscriptBlock]]:
        """Continuously consume declared PCM; one finalized transcript per bounded audio window."""
        self.__require("stt")
        selected_model = self.__model(model, self.__stt_model)
        config = stream_options or TranscriptionStreamOptions()
        validate_transcription_stream(audio_format, config)
        if not hasattr(audio, "__aiter__"):
            raise TypeError("stream_stt requires an async PCM byte source; use stt for complete files.")
        selected = options or TranscriptionOptions()
        selected = replace(selected, extra=deepcopy(dict(selected.extra)))
        self.__stream_extra(dict(selected.extra), speech=False)
        output = TranscriptionOutput(audio_format, config)

        async def request(part: AudioInput) -> TranscriptBlock:
            import io
            import wave
            with wave.open(io.BytesIO(part.data), "rb") as reader:
                frames = reader.getnframes()
            result = await self.__driver.stt(TranscriptionRequest(part, selected_model, selected))
            return output.block(result, frames)

        return pull_flow(audio_windows(audio, audio_format, config), request, lambda block: iter((block,)))

    def stream_stt_with_auto_break(
        self, audio: AsyncIterable[bytes], *, audio_format: PCMFormat, model: str | None = None,
        options: TranscriptionOptions | None = None, stream_options: TranscriptionStreamOptions | None = None,
        segmenter: TextSegmenter | None = None,
    ) -> AbstractAsyncContextManager[AsyncIterator[TranscriptSegment]]:
        """Break finalized recognized text, not audio pauses; flush labelled limit/EOF tails."""
        config = stream_options or TranscriptionStreamOptions()
        blocks = self.stream_stt(audio, audio_format=audio_format, model=model, options=options, stream_options=config)

        @asynccontextmanager
        async def context() -> AsyncIterator[AsyncIterator[TranscriptSegment]]:
            async with blocks as source:
                async with sentence_stream(source, SentenceOutput(config.max_pending_chars, segmenter)) as sentences:
                    yield sentences

        return context()
