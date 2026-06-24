import pytest
import asyncio

from agently.core import AttemptRunner
from agently.types.data import AttemptDecision, AttemptHandlers, AttemptObservation, AttemptState


@pytest.mark.asyncio
async def test_attempt_runner_streams_successful_attempt():
    async def execute(state: AttemptState):
        yield "message", f"attempt-{state.attempt_index}"

    async def handle_error(error: BaseException, state: AttemptState):
        del state
        return AttemptDecision.raise_error(error)

    runner = AttemptRunner(AttemptHandlers(execute=execute, handle_error=handle_error))
    events = [event async for event in runner.run_stream()]

    assert events == [
        ("message", "attempt-1"),
        ("status", {"status": "completed", "attempt_index": 1, "retry": False}),
    ]
    assert runner.state.output_started is True


@pytest.mark.asyncio
async def test_attempt_runner_retries_before_output_started():
    calls = 0
    observations: list[AttemptObservation] = []

    async def execute(state: AttemptState):
        nonlocal calls
        calls += 1
        if state.attempt_index == 1:
            raise RuntimeError("retry me")
        yield "message", "ok"

    async def handle_error(error: BaseException, state: AttemptState):
        assert str(error) == "retry me"
        assert state.output_started is False
        return AttemptDecision.retry(reason="transient")

    async def observe(observation: AttemptObservation, _state: AttemptState):
        observations.append(observation)

    runner = AttemptRunner(AttemptHandlers(execute=execute, handle_error=handle_error, on_observation=observe))
    events = [event async for event in runner.run_stream()]

    assert calls == 2
    assert events == [
        (
            "status",
            {
                "status": "failed",
                "attempt_index": 1,
                "retry": True,
                "next_attempt_index": 2,
                "reason": "retry me",
                "error_type": "RuntimeError",
            },
        ),
        ("message", "ok"),
        ("status", {"status": "completed", "attempt_index": 2, "retry": False}),
    ]
    assert [observation.kind for observation in observations] == ["status", "retry", "output_started", "status"]


@pytest.mark.asyncio
async def test_attempt_runner_does_not_retry_after_max_attempts():
    async def execute(_state: AttemptState):
        if False:
            yield "message", "never"
        raise RuntimeError("boom")

    async def handle_error(_error: BaseException, _state: AttemptState):
        return AttemptDecision.retry(reason="always")

    runner = AttemptRunner(
        AttemptHandlers(execute=execute, handle_error=handle_error),
        state=AttemptState(max_attempts=1),
    )

    with pytest.raises(RuntimeError, match="boom"):
        [event async for event in runner.run_stream()]


@pytest.mark.asyncio
async def test_attempt_runner_does_not_retry_after_output_started_by_default():
    observations: list[AttemptObservation] = []

    async def execute(state: AttemptState):
        yield "message", f"partial-{state.attempt_index}"
        raise RuntimeError("stream broke")

    async def handle_error(error: BaseException, state: AttemptState):
        assert str(error) == "stream broke"
        assert state.output_started is True
        return AttemptDecision.retry(reason="transient")

    async def observe(observation: AttemptObservation, _state: AttemptState):
        observations.append(observation)

    runner = AttemptRunner(AttemptHandlers(execute=execute, handle_error=handle_error, on_observation=observe))

    with pytest.raises(RuntimeError, match="stream broke"):
        [event async for event in runner.run_stream()]

    assert [observation.kind for observation in observations] == ["output_started", "status", "retry_blocked"]


@pytest.mark.asyncio
async def test_attempt_runner_can_retry_after_output_started_when_explicitly_allowed():
    calls = 0

    async def execute(state: AttemptState):
        nonlocal calls
        calls += 1
        if state.attempt_index == 1:
            yield "message", "partial"
            raise RuntimeError("allowed retry")
        yield "message", "replacement"

    async def handle_error(_error: BaseException, state: AttemptState):
        assert state.output_started is True
        return AttemptDecision.retry(reason="explicit", allow_after_output_started=True)

    runner = AttemptRunner(AttemptHandlers(execute=execute, handle_error=handle_error))
    events = [event async for event in runner.run_stream()]

    assert calls == 2
    assert events == [
        ("message", "partial"),
        (
            "status",
            {
                "status": "failed",
                "attempt_index": 1,
                "retry": True,
                "next_attempt_index": 2,
                "reason": "allowed retry",
                "error_type": "RuntimeError",
            },
        ),
        ("message", "replacement"),
        ("status", {"status": "completed", "attempt_index": 2, "retry": False}),
    ]


@pytest.mark.asyncio
async def test_attempt_runner_ingests_typed_decision_observations():
    observations: list[AttemptObservation] = []

    async def execute(_state: AttemptState):
        if False:
            yield "message", "never"
        raise RuntimeError("observable")

    async def handle_error(error: BaseException, _state: AttemptState):
        return AttemptDecision.raise_error(
            error,
            reason="observed",
            observations=[AttemptObservation("provider_error", {"code": "E_OBS"})],
        )

    async def observe(observation: AttemptObservation, _state: AttemptState):
        observations.append(observation)

    runner = AttemptRunner(AttemptHandlers(execute=execute, handle_error=handle_error, on_observation=observe))

    with pytest.raises(RuntimeError, match="observable"):
        [event async for event in runner.run_stream()]

    assert observations[0] == AttemptObservation("provider_error", {"code": "E_OBS"})
    assert observations[1].kind == "status"
    assert observations[1].data["status"] == "failed"


@pytest.mark.asyncio
async def test_attempt_runner_propagates_cancellation_without_error_handler():
    handled = False

    async def execute(_state: AttemptState):
        if False:
            yield "message", "never"
        raise asyncio.CancelledError()

    async def handle_error(_error: BaseException, _state: AttemptState):
        nonlocal handled
        handled = True
        return AttemptDecision.yield_error(_error)

    runner = AttemptRunner(AttemptHandlers(execute=execute, handle_error=handle_error))

    events = []
    with pytest.raises(asyncio.CancelledError):
        async for event in runner.run_stream():
            events.append(event)

    assert handled is False
    assert events == [
        (
            "status",
            {
                "status": "cancelled",
                "attempt_index": 1,
                "retry": False,
                "reason": "CancelledError",
                "error_type": "CancelledError",
            },
        )
    ]


@pytest.mark.asyncio
async def test_attempt_runner_propagates_generator_exit_without_error_handler():
    # Regression: when a downstream consumer (e.g. a response adapter that breaks
    # after the terminal event) closes the stream early, GeneratorExit must be a
    # clean control signal — not routed through error classification, which would
    # emit a spurious empty-message requester error.
    handled = False

    async def execute(_state: AttemptState):
        yield "message", "first"
        yield "message", "second"

    async def handle_error(error: BaseException, _state: AttemptState):
        nonlocal handled
        handled = True
        return AttemptDecision.yield_error(error)

    runner = AttemptRunner(AttemptHandlers(execute=execute, handle_error=handle_error))
    stream = runner.run_stream()

    first = await anext(stream)
    assert first == ("message", "first")

    # Closing the suspended generator raises GeneratorExit at the paused yield.
    await stream.aclose()

    assert handled is False


@pytest.mark.asyncio
async def test_attempt_runner_preserves_timeout_when_error_handler_raises():
    timeout = TimeoutError("deadline")

    async def execute(_state: AttemptState):
        if False:
            yield "message", "never"
        raise timeout

    async def handle_error(error: BaseException, _state: AttemptState):
        return AttemptDecision.raise_error(error)

    runner = AttemptRunner(AttemptHandlers(execute=execute, handle_error=handle_error))

    with pytest.raises(TimeoutError) as exc_info:
        [event async for event in runner.run_stream()]

    assert exc_info.value is timeout


@pytest.mark.asyncio
async def test_attempt_runner_can_yield_error_without_raising():
    async def execute(_state: AttemptState):
        if False:
            yield "message", "never"
        raise RuntimeError("provider failed")

    async def handle_error(error: BaseException, state: AttemptState):
        assert state.output_started is False
        return AttemptDecision.yield_error(error)

    runner = AttemptRunner(AttemptHandlers(execute=execute, handle_error=handle_error))
    events = [event async for event in runner.run_stream()]

    assert len(events) == 2
    assert events[0][0] == "status"
    assert events[0][1]["status"] == "failed"
    assert events[0][1]["reason"] == "provider failed"
    assert events[1][0] == "error"
    assert isinstance(events[1][1], RuntimeError)


@pytest.mark.asyncio
async def test_attempt_runner_status_keeps_provider_detail_without_request_body():
    async def execute(_state: AttemptState):
        if False:
            yield "message", "never"
        raise RuntimeError("Status Code: 502\nDetail: upstream reset\nRequest Data: {'input': 'private'}")

    async def handle_error(error: BaseException, _state: AttemptState):
        return AttemptDecision.yield_error(error)

    runner = AttemptRunner(AttemptHandlers(execute=execute, handle_error=handle_error))
    events = [event async for event in runner.run_stream()]

    assert events[0] == (
        "status",
        {
            "status": "failed",
            "attempt_index": 1,
            "retry": False,
            "reason": "Status Code: 502\nDetail: upstream reset",
            "error_type": "RuntimeError",
        },
    )


@pytest.mark.asyncio
async def test_attempt_runner_preserves_original_error_on_raise_decision():
    async def execute(_state: AttemptState):
        if False:
            yield "message", "never"
        raise ValueError("bad")

    async def handle_error(error: BaseException, _state: AttemptState):
        return AttemptDecision.raise_error(error)

    runner = AttemptRunner(AttemptHandlers(execute=execute, handle_error=handle_error))

    with pytest.raises(ValueError, match="bad"):
        [event async for event in runner.run_stream()]
