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

## Streaming is two separate questions

| Driver | Complete TTS / STT | TTS output stream | Uploaded-file STT output stream | Continuous STT input |
|---|---|---|---|---|
| OpenAICompatible | Yes, when the provider/model implements these endpoints | Not declared | Not declared | Not declared |
| OMLX | Yes | WAV bytes | transcript.text.delta/done SSE | Not yet adapted |
| Custom | Declared by the driver | Declared by the driver | Declared by the driver | May implement stream_stt_input |

```python
async with agent.audio.stream_tts("Welcome.", options=SpeechOptions(response_format="wav")) as chunks:
    async for chunk in chunks:
        await audio_sink.write(chunk)  # Application-owned sink; a chunk is not a WAV file.

async with agent.audio.stream_stt("recording.wav") as events:
    async for event in events:
        if event.kind == "delta":
            print(event.text, end="", flush=True)
        else:
            final_text = event.text  # Complete transcript, not another append.
```

Use `async with` even when breaking early. It closes the transport on exit,
failure or cancellation. Partial data is not a successful final result. STT
requires a done event; an abrupt EOF raises `AudioProtocolError`. For raw speech
bytes a clean HTTP end only proves transport completion, not linguistic quality.
Streaming output is async-only; do not collect an entire stream and call that
real-time input. `stream_stt_input(chunks, audio_format=PCMFormat(...))` is the
separate custom-driver seam; built-ins currently fail explicitly before consuming
input. oMLX service/model real-time support alone does not mean this adapter exists.

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

See the real [round-trip example](../../../examples/audio/tts_stt_roundtrip.py).
