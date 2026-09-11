"""oMLX HTTP audio profile: file-to-SSE STT and WAV speech streaming."""

from __future__ import annotations

from agently.types.data.audio import AudioOperation, SpeechRequest
from .OpenAICompatible import OpenAICompatible


class OMLX(OpenAICompatible):
    name = "OMLX"

    @property
    def supported_operations(self) -> frozenset[AudioOperation]:
        return frozenset({"tts", "stt", "stream_tts", "stream_stt"})

    @staticmethod
    def _speech(request: SpeechRequest, *, stream: bool = False) -> dict[str, object]:
        if stream and request.options.response_format != "wav":
            raise ValueError("oMLX streaming TTS requires response_format='wav'.")
        return OpenAICompatible._speech(request, stream=stream)
