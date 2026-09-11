"""Thin Agent access to an explicitly bound, independently replaceable audio capability."""

from __future__ import annotations

from os import PathLike
from typing import TypeVar, cast

from agently_stage import default_stage_call_bridge

from agently.core.Agent import BaseAgent
from agently.types.data.audio import AudioInput, SpeechOptions, SpeechResult, TranscriptionOptions, TranscriptResult
from agently.types.plugins.AudioModelRequester import AudioCapability

AudioAgent = TypeVar("AudioAgent", bound="AudioExtension")


class AudioExtension(BaseAgent):
    def use_audio(self: AudioAgent, audio: AudioCapability | None) -> AudioAgent:
        """Mount audio explicitly; None unbinds it for future calls/executions."""
        if audio is not None:
            for method in ("async_tts", "async_stt", "stream_tts", "stream_stt", "stream_stt_input"):
                if not callable(getattr(audio, method, None)):
                    raise TypeError(f"Audio capability must implement {method}.")
            if not isinstance(audio.supported_operations, frozenset):
                raise TypeError("Audio capability supported_operations must be a frozenset.")
        self.use_capability("audio", audio)
        return self

    @property
    def audio(self) -> AudioCapability:
        """Bound audio capability, including its explicitly scoped stream methods."""
        return cast(AudioCapability, self.require_capability("audio"))

    async def async_tts(
        self, text: str, *, model: str | None = None, voice: str | None = None,
        options: SpeechOptions | None = None,
    ) -> SpeechResult:
        return await self.audio.async_tts(text, model=model, voice=voice, options=options)

    def tts(
        self, text: str, *, model: str | None = None, voice: str | None = None,
        options: SpeechOptions | None = None,
    ) -> SpeechResult:
        return default_stage_call_bridge.as_sync(self.async_tts)(text, model=model, voice=voice, options=options)

    async def async_stt(
        self, audio: AudioInput | str | PathLike[str], *, model: str | None = None,
        options: TranscriptionOptions | None = None,
    ) -> TranscriptResult:
        return await self.audio.async_stt(audio, model=model, options=options)

    def stt(
        self, audio: AudioInput | str | PathLike[str], *, model: str | None = None,
        options: TranscriptionOptions | None = None,
    ) -> TranscriptResult:
        return default_stage_call_bridge.as_sync(self.async_stt)(audio, model=model, options=options)
