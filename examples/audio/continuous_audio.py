"""Real TTS/STT continuous consumption, with output auto break.

Environment: AUDIO_BASE_URL, AUDIO_TTS_MODEL, AUDIO_STT_MODEL; optional AUDIO_API_KEY,
AUDIO_DRIVER (OpenAICompatible), AUDIO_VOICE. No microphone capture or playback.
Text chunks -> greedy TTS segments -> independent WAV files OR one PCM stream.
PCM input -> bounded STT requests -> recognition blocks OR punctuation-delimited text.
The recorded PCM is replayed as input chunks, not claimed as live microphone evidence.
The output directory must be new. Full audio/transcripts are retained for human review.

Expected key output (one real oMLX run; not an exact-match speech quality gate):
  2 independent WAV segments; continuous PCM 24kHz mono s16le, 579840 bytes / 12.08s.
  3 STT windows [0,5], [5,10], [10,12.08] seconds; 5 punctuation-delimited outputs.
  First sentence: 欢迎参加项目会议。 A window-boundary artifact was also recognized as 好。
  10 recorded audio requests across four deliberately separate modes; no judge/retry.
  Counts/audio vary with model generation; this does not prove live microphone performance.
"""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import AsyncIterator
from dataclasses import asdict
import json
import os
from pathlib import Path
import time
import wave

from agently import Agently, SpeechOptions, TextSegmentOptions, TranscriptionStreamOptions


async def text_source(text: str) -> AsyncIterator[str]:
    for offset in range(0, len(text), 3):
        yield text[offset:offset + 3]
        await asyncio.sleep(0)


async def audio_source(data: bytes) -> AsyncIterator[bytes]:
    for offset in range(0, len(data), 4093):  # intentionally not frame-aligned network packets
        yield data[offset:offset + 4093]
        await asyncio.sleep(0)


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    audio = Agently.create_audio_request(
        base_url=os.environ["AUDIO_BASE_URL"], api_key=os.getenv("AUDIO_API_KEY", ""),
        tts_model=os.environ["AUDIO_TTS_MODEL"], stt_model=os.environ["AUDIO_STT_MODEL"],
        driver=os.getenv("AUDIO_DRIVER", "OpenAICompatible"), timeout=120,
    )
    agent = Agently.create_agent().use_audio(audio)
    text = "欢迎参加项目会议。请在下午三点到会议室，带上测试报告。\n会议结束后，请将确认事项发送给项目负责人。谢谢。"
    segments = TextSegmentOptions(expect_chars=25, tolerance_ratio=.2, grace_chars=10)
    speech_options = SpeechOptions(language="Chinese")
    started = time.monotonic()
    record: dict[str, object] = {"text": text, "segments": asdict(segments), "tts_model": os.environ["AUDIO_TTS_MODEL"], "stt_model": os.environ["AUDIO_STT_MODEL"]}
    events: list[dict[str, object]] = []

    def log(event: str, **details: object) -> None:
        item = {"event": event, "elapsed": round(time.monotonic() - started, 3), **details}
        events.append(item)
        with (args.output / "events.jsonl").open("a", encoding="utf-8") as file:
            file.write(json.dumps(item, ensure_ascii=False) + "\n")

    files = []
    async with agent.stream_tts_with_auto_break(text_source(text), voice=os.getenv("AUDIO_VOICE"), options=speech_options, segments=segments) as stream:
        async for result in stream:
            path = args.output / f"segment-{len(files) + 1}.wav"
            path.write_bytes(result.data)
            files.append(str(path))
            log("tts_audio_segment", path=str(path), bytes=len(result.data))
    record["audio_segments"] = files
    raw_parts = []
    async with agent.stream_tts(text_source(text), voice=os.getenv("AUDIO_VOICE"), options=speech_options, segments=segments) as stream:
        fmt = stream.audio_format
        if fmt is None:
            raise RuntimeError("Nonempty example produced no audio format.")
        log("pcm_format_ready", format=asdict(fmt))
        async for part in stream:
            raw_parts.append(part)
    raw = b"".join(raw_parts)
    record["pcm"] = {"format": asdict(fmt), "bytes": len(raw), "chunks": len(raw_parts), "duration_seconds": len(raw) / (fmt.sample_rate * fmt.channels * 2)}
    with wave.open(str(args.output / "continuous.wav"), "wb") as writer:
        writer.setparams((fmt.channels, 2, fmt.sample_rate, 0, "NONE", "not compressed"))
        writer.writeframes(raw)
    log("pcm_complete", bytes=len(raw), chunks=len(raw_parts))
    config = TranscriptionStreamOptions(window_seconds=5)
    blocks = []
    async with agent.stream_stt(audio_source(raw), audio_format=fmt, stream_options=config) as stream:
        async for block in stream:
            blocks.append(asdict(block))
            log("stt_block", **asdict(block))
    record["transcript_blocks"] = blocks
    sentences = []
    async with agent.stream_stt_with_auto_break(audio_source(raw), audio_format=fmt, stream_options=config) as stream:
        async for sentence in stream:
            sentences.append(asdict(sentence))
            log("stt_sentence", **asdict(sentence))
    record["transcript_segments"] = sentences
    record["elapsed_seconds"] = round(time.monotonic() - started, 3)
    # Each mode is a separate requested run, not a re-reader of cached results.
    record["request_accounting"] = {"tts_auto_break_calls": len(files), "stt_plain_calls": len(blocks), "note": "Stream modes each dispatch their own requests; provider attempts/usage require provider traces."}
    (args.output / "result.json").write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(record, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
