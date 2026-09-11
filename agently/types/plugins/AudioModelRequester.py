"""Replaceable audio interaction contracts, including streaming input."""

from __future__ import annotations

from collections.abc import AsyncIterable, AsyncIterator
from contextlib import AbstractAsyncContextManager
from os import PathLike
from typing import Protocol

from agently.types.data.audio import (
    AudioConnection, AudioInput, AudioOperation, PCMFormat, SpeechOptions,
    SpeechRequest, SpeechResult, TranscriptEvent, TranscriptResult,
    TranscriptionOptions, TranscriptionRequest,
)
from .base import AgentlyPlugin


class AudioModelRequester(AgentlyPlugin, Protocol):
    """Driver owns each transport through completion, cancellation or context exit."""

    name: str

    def __init__(self, connection: AudioConnection): ...

    @property
    def supported_operations(self) -> frozenset[AudioOperation]: ...

    async def tts(self, request: SpeechRequest) -> SpeechResult: ...

    async def stt(self, request: TranscriptionRequest) -> TranscriptResult: ...

    def stream_tts(self, request: SpeechRequest) -> AbstractAsyncContextManager[AsyncIterator[bytes]]: ...

    def stream_stt(self, request: TranscriptionRequest) -> AbstractAsyncContextManager[AsyncIterator[TranscriptEvent]]: ...

    def stream_stt_input(
        self, chunks: AsyncIterable[bytes], *, model: str, audio_format: PCMFormat,
        options: TranscriptionOptions,
    ) -> AbstractAsyncContextManager[AsyncIterator[TranscriptEvent]]: ...


class AudioCapability(Protocol):
    """Agent may bind any complete implementation of this capability, not only the default facade."""

    @property
    def supported_operations(self) -> frozenset[AudioOperation]: ...

    async def async_tts(
        self, text: str, *, model: str | None = None, voice: str | None = None,
        options: SpeechOptions | None = None,
    ) -> SpeechResult: ...

    async def async_stt(
        self, audio: AudioInput | str | PathLike[str], *, model: str | None = None,
        options: TranscriptionOptions | None = None,
    ) -> TranscriptResult: ...

    def stream_tts(
        self, text: str, *, model: str | None = None, voice: str | None = None,
        options: SpeechOptions | None = None,
    ) -> AbstractAsyncContextManager[AsyncIterator[bytes]]: ...

    def stream_stt(
        self, audio: AudioInput | str | PathLike[str], *, model: str | None = None,
        options: TranscriptionOptions | None = None,
    ) -> AbstractAsyncContextManager[AsyncIterator[TranscriptEvent]]: ...

    def stream_stt_input(
        self, chunks: AsyncIterable[bytes], *, model: str | None = None,
        audio_format: PCMFormat | None = None, options: TranscriptionOptions | None = None,
    ) -> AbstractAsyncContextManager[AsyncIterator[TranscriptEvent]]: ...
