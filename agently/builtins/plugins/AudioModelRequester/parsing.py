"""Provider response validation; no model-generated quality judgment."""

from __future__ import annotations

import json
import math
from collections.abc import AsyncIterator, Mapping

import httpx
from httpx_sse import EventSource

from agently.types.data.audio import AudioProtocolError, TranscriptEvent, TranscriptResult


def transcript(value: object, model: str) -> TranscriptResult:
    if not isinstance(value, dict) or not isinstance(value.get("text"), str):
        raise AudioProtocolError("Audio transcription response must contain a string text field.")
    language = value.get("language")
    duration = value.get("duration")
    if language is not None and not isinstance(language, str):
        raise AudioProtocolError("Transcription language must be a string or null.")
    if duration is not None and (
        isinstance(duration, bool) or not isinstance(duration, (int, float))
        or not math.isfinite(duration) or duration < 0
    ):
        raise AudioProtocolError("Transcription duration must be a non-negative finite number or null.")
    return TranscriptResult(value["text"], model, language, duration)


def merge_extra(payload: dict[str, object], extra: Mapping[str, object], *, reserved: set[str]) -> dict[str, object]:
    collisions = reserved.intersection(extra)
    if collisions:
        raise ValueError(f"Audio extra options cannot override reserved fields: {sorted(collisions)}")
    payload.update(extra)
    # Fail locally rather than dispatching invalid JSON (including NaN).
    json.dumps(payload, allow_nan=False)
    return payload


async def transcript_events(response: httpx.Response) -> AsyncIterator[TranscriptEvent]:
    async for event in EventSource(response).aiter_sse():
        if not event.data:
            continue
        try:
            value = json.loads(event.data)
        except ValueError as error:
            raise AudioProtocolError("Invalid transcription SSE JSON.") from error
        if not isinstance(value, dict):
            raise AudioProtocolError("Transcription SSE payload must be an object.")
        kind = value.get("type", event.event)
        if kind in {"error", "transcript.error"} or "error" in value:
            raise AudioProtocolError("Transcription provider emitted an error event.")
        if kind == "transcript.text.delta":
            if not isinstance(value.get("delta"), str):
                raise AudioProtocolError("Transcription delta must contain string delta.")
            yield TranscriptEvent("delta", value["delta"])
        elif kind == "transcript.text.done":
            if not isinstance(value.get("text"), str):
                raise AudioProtocolError("Transcription done must contain string text.")
            yield TranscriptEvent("done", value["text"])
            return
        # Unknown metadata events are not completion evidence.
    raise AudioProtocolError("Transcription stream ended without transcript.text.done; partial text is not final.")
