"""Synthetic protocol/segmentation fixtures, not speech quality evidence."""

import asyncio
import struct

import pytest

from agently import (
    Agently, AudioConnection, AudioModelRequest, AudioProtocolError, PCMFormat, SpeechOptions,
    SpeechResult, TextSegmentOptions, TranscriptResult, TranscriptionStreamOptions,
)
from agently.builtins.plugins.AudioModelRequester import OpenAICompatible
from agently.core.model.audio_stream.pcm import encode_wav, decode_pcm
from agently.core.model.audio_stream.segmentation import GreedyTextSegmenter, text_parts


async def pieces(parts):
    for part in parts:
        yield part


class Driver(OpenAICompatible):
    def __init__(self, connection: AudioConnection = AudioConnection("http://unused.test")):
        super().__init__(connection)
        self.texts = []
        self.audio = []
        self.answers = iter(["第一句", "继续。第二句！", "最后没有标点"])
        self.active = False
        self.wait = False
        self.cancelled = False

    async def tts(self, request):
        self.texts.append(request.text)
        self.active = True
        try:
            if self.wait:
                await asyncio.Event().wait()
            data = encode_wav(b"\x01\x00" * len(request.text), PCMFormat(24000)).data
            return SpeechResult(data, "audio/wav", request.model)
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        finally:
            self.active = False

    async def stt(self, request):
        self.audio.append(request.audio)
        return TranscriptResult(next(self.answers), request.model, duration=999)


def setup():
    driver = Driver()
    return AudioModelRequest(driver, tts_model="tts", stt_model="stt"), driver


@pytest.mark.asyncio
async def test_tts_output_modes_share_segments_and_continuous_samples():
    audio, driver = setup()
    text = "甲乙。丙丁。戊己。庚辛。"
    options = TextSegmentOptions(expect_chars=5, tolerance_ratio=.2, grace_chars=2)
    async with audio.stream_tts_with_auto_break(pieces(list(text)), segments=options) as stream:
        files = [part async for part in stream]
    segments = list(driver.texts)
    driver.texts.clear()
    async with audio.stream_tts(text, segments=options, chunk_bytes=4) as stream:
        assert stream.audio_format == PCMFormat(24000)
        chunks = [chunk async for chunk in stream]
    assert driver.texts == segments
    assert "".join(segments) == text
    assert b"".join(chunks) == b"".join(decode_pcm(f, None, raw=False)[0] for f in files)
    assert all(len(chunk) <= 4 for chunk in chunks)


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["", " ", "无标点" * 60, "a\r\n\r\nb。c!”d,e。" * 8, "3.14 is decimal. Next sentence!尾部"])
async def test_segmentation_partition_invariance(text):
    options = TextSegmentOptions(expect_chars=10, tolerance_ratio=.1, grace_chars=3)
    results = []
    for chunks in ([text], list(text), [text[i:i+7] for i in range(0, len(text), 7)]):
        results.append([part async for part in text_parts(pieces(chunks), options, GreedyTextSegmenter(options))])
    assert results[0] == results[1] == results[2]
    if text.strip():
        assert "".join(results[0]) == text


@pytest.mark.asyncio
async def test_stt_continuous_windows_offsets_and_auto_break():
    audio, driver = setup()
    options = TranscriptionStreamOptions(window_seconds=.001)
    raw = b"\x00\x00" * 48  # 3 windows at 16kHz
    async with audio.stream_stt(pieces([raw[:3], raw[3:]]), audio_format=PCMFormat(), stream_options=options) as stream:
        blocks = [block async for block in stream]
    assert [b.index for b in blocks] == [0, 1, 2]
    assert blocks[-1].end_seconds == .003
    assert blocks[0].start_seconds == 0
    driver.answers = iter(["第一句", "继续。第二句！", "最后没有标点"])
    async with audio.stream_stt_with_auto_break(pieces([raw]), audio_format=PCMFormat(), stream_options=options) as stream:
        sentences = [part async for part in stream]
    assert [s.text for s in sentences] == ["第一句继续。", "第二句！", "最后没有标点"]
    assert [s.reason for s in sentences] == ["sentence_end", "sentence_end", "input_end"]
    assert (sentences[0].first_block, sentences[0].last_block) == (0, 1)


@pytest.mark.asyncio
async def test_stt_no_punctuation_limit_and_partial_frame():
    audio, driver = setup()
    driver.answers = iter(["abcde", "fghij"])
    options = TranscriptionStreamOptions(window_seconds=.001, max_pending_chars=4)
    async with audio.stream_stt_with_auto_break(pieces([b"\0" * 64]), audio_format=PCMFormat(), stream_options=options) as stream:
        items = [item async for item in stream]
    assert all(len(item.text) <= 4 for item in items)
    assert items[0].reason == "limit"
    with pytest.raises(AudioProtocolError, match="sample frames"):
        async with audio.stream_stt(pieces([b"\0"]), audio_format=PCMFormat()) as stream:
            await anext(stream)


@pytest.mark.asyncio
async def test_empty_and_early_close_no_extra_requests():
    audio, driver = setup()
    async with audio.stream_tts("") as stream:
        assert stream.audio_format is None
        assert [item async for item in stream] == []
    async with audio.stream_tts_with_auto_break("one. two. three.", segments=TextSegmentOptions(4, 0, 1)) as stream:
        await anext(stream)
    assert len(driver.texts) == 1
    assert not driver.active


@pytest.mark.asyncio
async def test_cancellation_reaches_inflight_driver():
    audio, driver = setup()
    driver.wait = True

    async def consume():
        async with audio.stream_tts("hello") as stream:
            await anext(stream)

    task = asyncio.create_task(consume())
    for _ in range(100):
        if driver.active:
            break
        await asyncio.sleep(.001)
    assert driver.active
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert driver.cancelled
    assert not driver.active


@pytest.mark.asyncio
async def test_agent_mount_and_bound_stream():
    audio, driver = setup()
    agent = Agently.create_agent().use_audio(audio)
    context = agent.stream_tts_with_auto_break("hello")
    agent.use_audio(None)
    async with context as stream:
        assert len([part async for part in stream]) == 1
    assert driver.texts == ["hello"]


def test_wav_chunks_and_format_guard():
    wav = encode_wav(b"\x01\x00" * 10, PCMFormat(24000)).data
    extra = b"JUNK" + struct.pack("<I", 3) + b"abc\0"
    enriched = wav[:4] + struct.pack("<I", len(wav) - 8 + len(extra)) + wav[8:12] + extra + wav[12:]
    samples, fmt = decode_pcm(SpeechResult(enriched, "audio/wav", "m"), None, raw=False)
    assert samples == b"\x01\x00" * 10
    assert fmt.sample_rate == 24000
    with pytest.raises(AudioProtocolError, match="format changed"):
        decode_pcm(SpeechResult(wav, "audio/wav", "m"), PCMFormat(), raw=False)
    with pytest.raises(AudioProtocolError):
        decode_pcm(SpeechResult(wav[:-1], "audio/wav", "m"), None, raw=False)


@pytest.mark.parametrize("options", [TextSegmentOptions(0), TextSegmentOptions(tolerance_ratio=float("nan")), TextSegmentOptions(grace_chars=-1)])
def test_bad_options_before_source_consumption(options):
    audio, driver = setup()
    with pytest.raises(ValueError):
        audio.stream_tts(pieces(["hello"]), segments=options)
    assert not driver.texts


@pytest.mark.parametrize("text,cut", [
    ("a" * 8 + "\n" + "b。c,tail", 9),  # paragraph outranks later sentence/comma
    ("a" * 9 + "。b,tail", 10),
    ("a" * 9 + ",tail", 10),
    ("a" * 12 + ",tail", 13),  # grace period
    ("a" * 15, 14),  # no punctuation: upper11 + grace3
    ("a。" + "b" * 14, 2),  # no later candidate: use earlier boundary before hard cut
])
def test_greedy_priority_and_grace(text, cut):
    assert GreedyTextSegmenter(TextSegmentOptions(10, .1, 3)).cut(text, final=False) == cut


@pytest.mark.asyncio
async def test_randomized_chunking_equivalence():
    import random
    randomizer = random.Random(918)
    options = TextSegmentOptions(20, .1, 4)
    for _ in range(50):
        text = "".join(randomizer.choice("abc文字，。\r\n.!?” ") for _ in range(120))
        expected = [x async for x in text_parts(text, options, GreedyTextSegmenter(options))]
        actual = [x async for x in text_parts(pieces(list(text)), options, GreedyTextSegmenter(options))]
        assert actual == expected


@pytest.mark.asyncio
async def test_source_error_after_output_preserves_prefix_and_no_retry():
    audio, driver = setup()

    async def source():
        yield "first. next"
        raise LookupError("source failed")

    with pytest.raises(LookupError, match="source failed"):
        async with audio.stream_tts_with_auto_break(source(), segments=TextSegmentOptions(6, 0, 2)) as stream:
            assert (await anext(stream)).data
            await anext(stream)
    assert driver.texts == ["first."]


@pytest.mark.asyncio
async def test_backpressure_and_concurrent_consumer():
    audio, driver = setup()
    driver.wait = True
    reads = []

    async def source():
        reads.append(1)
        yield "hello"

    async with audio.stream_tts_with_auto_break(source()) as stream:
        assert not driver.texts and not reads
        task = asyncio.ensure_future(anext(stream))
        for _ in range(100):
            if driver.active:
                break
            await asyncio.sleep(.001)
        with pytest.raises(RuntimeError, match="one active consumer"):
            await anext(stream)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert reads == [1]
    assert driver.texts == ["hello"]


@pytest.mark.asyncio
async def test_stream_option_snapshot_and_two_streams():
    audio, driver = setup()
    extra: dict[str, object] = {"seed": 1}
    context = audio.stream_tts_with_auto_break("original", options=SpeechOptions(extra=extra))
    extra["input"] = "should not be seen"
    async with context as stream:
        assert len([part async for part in stream]) == 1

    async def run(text):
        async with audio.stream_tts(text) as stream:
            return b"".join([part async for part in stream])

    first, second = await asyncio.gather(run("hi"), run("hello"))
    assert (len(first), len(second)) == (4, 10)


@pytest.mark.asyncio
async def test_preflight_and_oversized_input_fail_explicitly():
    audio, driver = setup()
    with pytest.raises(ValueError, match="reserved"):
        audio.stream_tts(pieces(["unused"]), options=SpeechOptions(extra={"input": "override"}))
    with pytest.raises(ValueError, match="max_input_chars"):
        async with audio.stream_tts_with_auto_break("abcd", segments=TextSegmentOptions(max_input_chars=3)) as stream:
            await anext(stream)
    with pytest.raises(ValueError, match="max_input_bytes"):
        async with audio.stream_stt(pieces([b"\0" * 65]), audio_format=PCMFormat(), stream_options=TranscriptionStreamOptions(window_seconds=.001, max_input_bytes=64)) as stream:
            await anext(stream)
    assert not driver.texts and not driver.audio


@pytest.mark.asyncio
async def test_custom_segmenter_and_invalid_output():
    audio, driver = setup()

    class EveryTwo:
        def cut(self, text: str, *, final: bool) -> int | None:
            return 2 if len(text) >= 2 else None

    async with audio.stream_tts_with_auto_break("abcdef", segmenter=EveryTwo()) as stream:
        assert len([part async for part in stream]) == 3
    assert driver.texts == ["ab", "cd", "ef"]

    class Invalid:
        def cut(self, text: str, *, final: bool) -> int | None:
            return 0

    with pytest.raises(ValueError, match="prefix length"):
        async with audio.stream_tts_with_auto_break("abcdef", segmenter=Invalid()) as stream:
            await anext(stream)


@pytest.mark.asyncio
async def test_context_close_cancels_active_read_and_stt_delivers_before_eof():
    audio, driver = setup()
    driver.wait = True
    async with audio.stream_tts_with_auto_break("hello") as stream:
        task = asyncio.ensure_future(anext(stream))
        for _ in range(100):
            if driver.active:
                break
            await asyncio.sleep(.001)
        assert driver.active
    with pytest.raises(asyncio.CancelledError):
        await task
    assert driver.cancelled and not driver.active

    async def open_microphone():
        yield b"\0" * 32
        await asyncio.Event().wait()  # never EOF; first window must still be recognized

    async with audio.stream_stt(open_microphone(), audio_format=PCMFormat(), stream_options=TranscriptionStreamOptions(window_seconds=.001)) as stream:
        assert (await asyncio.wait_for(anext(stream), timeout=1)).index == 0


@pytest.mark.asyncio
async def test_completed_segment_executions_are_released():
    from agently.core.model.audio_stream.flow import _PullFlow

    async def request(value: str) -> str:
        return value

    stream = _PullFlow(pieces(["x"] * 100), request, lambda value: iter((value,)))
    try:
        assert len([value async for value in stream]) == 100
        assert not stream.flow._executions
    finally:
        await stream.aclose("test_finished")
