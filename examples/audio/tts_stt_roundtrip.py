"""Real audio round trip: Chinese speech -> audio bytes -> transcription.

Set AUDIO_BASE_URL, AUDIO_TTS_MODEL, AUDIO_STT_MODEL, and optionally AUDIO_API_KEY,
AUDIO_DRIVER (default OMLX), AUDIO_VOICE. No chat model or canned transcript is used.
The output directory must be new: existing recordings are never overwritten.
Use --stream to additionally exercise WAV output streaming and uploaded-file STT SSE.
Streaming input is a separate driver capability, not file upload in chunks.

Expected key output (one real oMLX run, not an exact-match quality gate):
  transcript: 欢迎使用语音服务。请在下午三点参加项目会议，并带上测试报告。
  media_type: audio/wav; STT stream ends with kind=done and the same transcript.
  Observed 52 TTS transport chunks; chunk counts and generated audio vary by run.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import time

from agently import Agently, AudioInput, SpeechOptions, TranscriptionOptions, SpeechRequest, TranscriptionRequest


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--stream", action="store_true")
    args = parser.parse_args()
    audio = Agently.create_audio_request(
        driver=os.getenv("AUDIO_DRIVER", "OMLX"),
        base_url=os.environ["AUDIO_BASE_URL"], api_key=os.getenv("AUDIO_API_KEY", ""),
        tts_model=os.environ["AUDIO_TTS_MODEL"], stt_model=os.environ["AUDIO_STT_MODEL"],
        timeout=120,
    )
    args.output.mkdir(parents=True, exist_ok=False)
    agent = Agently.create_agent()
    agent.use_audio(audio)
    text = "欢迎使用语音服务。请在下午三点参加项目会议，并带上测试报告。"
    started = time.monotonic()
    speech = await agent.async_tts(
        text, voice=os.getenv("AUDIO_VOICE"), options=SpeechOptions(language="Chinese"),
    )
    (args.output / "speech.wav").write_bytes(speech.data)
    transcript = await agent.async_stt(AudioInput(speech.data), options=TranscriptionOptions(language="zh"))
    report: dict[str, object] = {
        "input_text": text, "tts_model": speech.model, "stt_model": transcript.model,
        "media_type": speech.media_type, "audio_bytes": len(speech.data),
        "transcript": transcript.text, "elapsed_seconds": round(time.monotonic() - started, 3),
    }
    if args.stream:
        parts: list[bytes] = []
        # Provider-native output streaming, deliberately distinct from the
        # framework's text/PCM continuous-consumption APIs.
        async with audio.driver.stream_tts(SpeechRequest(
            text, os.environ["AUDIO_TTS_MODEL"], voice=os.getenv("AUDIO_VOICE"), options=SpeechOptions(language="Chinese"),
        )) as chunks:
            async for chunk in chunks:
                parts.append(chunk)
        streamed_audio = b"".join(parts)
        (args.output / "streamed-speech.wav").write_bytes(streamed_audio)
        events = []
        async with audio.driver.stream_stt(TranscriptionRequest(AudioInput(streamed_audio), os.environ["AUDIO_STT_MODEL"])) as stream:
            async for event in stream:
                events.append({"kind": event.kind, "text": event.text})
        report.update({"tts_stream_chunks": len(parts), "tts_stream_bytes": len(streamed_audio), "stt_events": events})
    report["total_elapsed_seconds"] = round(time.monotonic() - started, 3)
    result = json.dumps(report, ensure_ascii=False, indent=2)
    (args.output / "result.json").write_text(result + "\n", encoding="utf-8")
    print(result)


if __name__ == "__main__":
    asyncio.run(main())
