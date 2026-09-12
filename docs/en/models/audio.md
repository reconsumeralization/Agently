# Audio requests (4.1.4.8 development)

TTS and STT use an independent `AudioModelRequest`, not the text `ModelRequest`
Prompt chain. Audio models and credentials are explicit; Agent text settings,
session history, output schemas and `auto_continue()` do not enter audio calls.

```python
import os
from agently import Agently, AudioInput, SpeechOptions

audio = Agently.create_audio_request(
    driver="OMLX",  # OpenAICompatible supports the complete-file HTTP endpoints.
    base_url=os.environ["AUDIO_BASE_URL"],
    api_key=os.getenv("AUDIO_API_KEY", ""),
    tts_model=os.environ["AUDIO_TTS_MODEL"],
    stt_model=os.environ["AUDIO_STT_MODEL"],
)
agent = Agently.create_agent()
agent.use_audio(audio)

# Inside an async function:
speech = await agent.async_tts("Welcome to the meeting.", voice=os.getenv("AUDIO_VOICE"))
transcript = await agent.async_stt(AudioInput(speech.data))
print(transcript.text)
```

`tts()` / `stt()` are synchronous counterparts. Direct `audio.async_tts()` and
`audio.async_stt()` work without an Agent. Each call is a fresh request, not a
cached result reader. STT also accepts a local file path. `AudioInput` carries
bytes, filename and media type; declare the actual type for non-WAV input.
Files are read, never rewritten. No implicit recording, playback, URL download
or audio conversion is performed.

`SpeechOptions` provides response_format, speed, language, instructions and
provider-specific extra options; `TranscriptionOptions` provides language,
prompt and extra. Provider/model support still applies. Reserved request fields
cannot be replaced through extra. Default timeout is 120 seconds of HTTP
operation inactivity, not a whole workflow deadline. There is no automatic retry
or fallback model.

## Continuous consumption and output auto break

The basic tts/stt calls are unchanged. All four methods are available on the
standalone audio object and on an explicitly bound Agent. **Auto break selects
the output presentation, not whether input is already segmented.**

| Method | Input | Yielded output |
|---|---|---|
| stream_tts | str, nonblocking Iterable[str], or AsyncIterable[str] | Continuous headerless PCM bytes |
| stream_tts_with_auto_break | Same text source and internal segmentation | Independent complete SpeechResult audio segments |
| stream_stt | AsyncIterable[bytes] with explicit PCMFormat | Finalized TranscriptBlock per audio window |
| stream_stt_with_auto_break | Same PCM source | TranscriptSegment split after recognition at text sentence punctuation |

```python
from agently import PCMFormat, TextSegmentOptions, TranscriptionStreamOptions

segments = TextSegmentOptions(expect_chars=300, tolerance_ratio=0.1, grace_chars=100)
async with agent.stream_tts(text_chunks, segments=segments) as stream:
    fmt = stream.audio_format  # Ready on entry for nonempty input; None for empty input
    async for pcm in stream:
        await pcm_sink.write(pcm)  # Application sink configured with fmt; not a WAV file

async with agent.stream_tts_with_auto_break(fresh_text_chunks, segments=segments) as stream:
    async for speech in stream:
        await segment_sink.write(speech.data, speech.media_type)

async with agent.stream_stt_with_auto_break(
    pcm_chunks, audio_format=PCMFormat(sample_rate=16000),
    stream_options=TranscriptionStreamOptions(window_seconds=5, max_pending_chars=1000),
) as stream:
    async for segment in stream:
        print(segment.text, segment.reason, segment.first_block, segment.last_block)
```

Text segmentation prefers newlines/paragraphs, then sentence endings, then commas
inside the expected-length tolerance interval; the rightmost boundary wins
within a priority. With no candidate, read the grace interval, then use an earlier
boundary if available; hard-cut only when none exists. EOF flushes a short tail;
temporary lack of tokens is not EOF. Lengths are Unicode code points, not tokens.
The 300-character default is tunable, not a model-optimal claim. Input packet
boundaries do not change processing segments. Supply a fresh typed
`TextSegmenter` to replace boundary selection; `max_input_chars=65536` bounds a
single source item. Adapt blocking capture to an async source explicitly.

Continuous TTS currently accepts uncompressed s16le PCM WAV, or raw PCM with an
explicit `audio_format`. It parses actual WAV chunks, not a fixed 44-byte header.
The first segment locks the format; mismatches fail without implicit resampling.
An explicit `audio_format=PCMFormat(...)` requires that exact output format.
`chunk_bytes=8192` is rounded down to complete frames. Auto break supports
independent encoded results such as WAV/MP3 according to the base driver; it
rejects raw PCM without self-describing metadata. PCM is not a WAV file, and
concatenated WAV files are not one continuous audio file. Each base TTS finishes
a segment before delivery: initial latency, cross-segment prosody and continuous
playback throughput are not guaranteed.

STT accumulates sample frames into configurable windows (default 5 seconds).
EOF submits remaining complete frames and rejects incomplete frames without padding.
`max_input_bytes=1048576` bounds source items and windows; `max_transcript_chars=65536`
bounds each transcription. Blocks include text/index/model/language and
sample-derived start/end seconds. Base `TranscriptResult.duration` remains a
provider-origin field: oMLX currently reports processing time, not recording duration.

STT auto break consumes finalized block text, not audio pauses, packet boundaries
or provisional SSE deltas. Reasons are `sentence_end`, `limit`, and `input_end`.
When no punctuation arrives, the pending-length limit/EOF delivers a labelled
remainder without inventing punctuation or making another model request.
Source block ranges are not word-aligned sentence timestamps. The default
punctuation rules are not a universal semantic segmenter. An ASCII alphanumeric
block join adds a display space; it does not reconstruct split words, and
original blocks remain unchanged. Windowed recognition may lose/repeat words
or insert punctuation: the framework does not semantically deduplicate transcripts.

Use `async with`. Streams are pull-driven, one model request at a time, with no
unbounded prefetch queue. Errors/cancellation/early close do not synthesize pending
tails or retry already delivered speech. Already yielded prefixes are not full
success. The stream owns its resources, not a shared microphone. A realtime
capture adapter must report overflow or use an explicit application policy:
backpressure cannot pause a person speaking. Bounded framing does not bound all
allocations inside a third-party driver's complete response implementation.
No implicit recording, playback, full duplex, durable resume or replay is promised.

Do not automatically speak Agent thinking, tool events or text that validation
or retries can replace. Await final text for final-result guarantees; callers
must explicitly accept irreversible effects when choosing low-latency playback.

## Provider-native streams

`audio.supported_operations` describes composed operations;
`audio.driver.supported_operations` describes provider-native support. Neither
proves health. OpenAICompatible base tts/stt is sufficient for composed streams;
native duplex support is not required. OMLX additionally supports WAV output
streaming and uploaded-file transcript.text.delta/done SSE. Advanced native
access is explicit through `audio.driver.stream_tts(SpeechRequest(...))` and
`audio.driver.stream_stt(TranscriptionRequest(...))`.

Native bytes are transport chunks, not independent audio files. Native STT
done replaces accumulated deltas; EOF without done fails.
`driver.stream_stt_input(...)` remains a custom-driver native-input seam;
built-ins do not implement it. Windowed continuous consumption does not imply
a native realtime-ASR session has been implemented.

## Replacement and Execution dependencies

Register an `AudioModelRequester` class through
`Agently.plugin_manager.register("AudioModelRequester", Driver, activate=False)`;
its constructor receives `AudioConnection`. Or instantiate
`AudioModelRequest(driver_instance, tts_model=..., stt_model=...)`. A driver owns
its complete transport mechanism and scoped cleanup, not just HTTP parameters.
`agent.use_audio()` also accepts another complete `AudioCapability` implementation.

Registration does not mount audio. Missing `agent.audio`, `.tts()` or `.stt()`
raises a capability error. `use_audio(None)` removes future access without
closing an externally owned shared object.

Execution plugins can declare `required_agent_capabilities = ("audio",)`.
The factory checks presence before construction; the shared Execution implementation
captures these objects at construction. Producers obtain them through
`execution.require_agent_capability("audio")`, which also binds dynamic dependencies
before use. Replacing the Agent binding does not change an already captured object.
Child executions declare their own requirements; creating a child does not grant
permissions. A wholly custom Execution must implement the same binding contract.
Presence is not authorization, model support, health or proof of an actual call.
Snapshots with extra capability bindings currently fail explicitly: live clients
are not serialized, and no automatic rebinding/replay guarantee is claimed.

Audio calls do not currently emit text-model token events or enter Execution text
model-request budgets. Use application-owned deadlines/admission for audio work;
do not infer cost accounting, cancellation rollback or durable audio resume.

See [four continuous output modes](../../../examples/audio/continuous_audio.py) and
the [base/native round-trip example](../../../examples/audio/tts_stt_roundtrip.py).
