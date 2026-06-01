import pytest

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

    assert events == [("message", "attempt-1")]
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
    assert events == [("message", "ok")]
    assert [observation.kind for observation in observations] == ["retry", "output_started"]


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

    assert len(events) == 1
    assert events[0][0] == "error"
    assert isinstance(events[0][1], RuntimeError)


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
