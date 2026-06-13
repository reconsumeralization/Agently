import asyncio

import pytest

from agently.utils.RequestScheduler import RequestScheduler
from agently.utils import Settings


def test_backoff_delay_is_clamped_and_monotonic_without_jitter():
    delays = [RequestScheduler.backoff_delay(n, base=0.5, cap=8.0, jitter=False) for n in range(1, 7)]
    assert delays == [0.5, 1.0, 2.0, 4.0, 8.0, 8.0]  # exponential then clamped at cap


def test_backoff_delay_jitter_within_bounds():
    for _ in range(50):
        d = RequestScheduler.backoff_delay(4, base=0.5, cap=30.0, jitter=True)
        assert 0.0 <= d <= 4.0  # full jitter in [0, base*2**3]


@pytest.mark.asyncio
async def test_inactive_scheduler_slot_is_noop():
    scheduler = RequestScheduler()
    assert scheduler.is_active("openai") is False
    async with scheduler.slot("openai"):
        pass  # no error, no blocking


@pytest.mark.asyncio
async def test_concurrency_limit_caps_in_flight_requests():
    scheduler = RequestScheduler().configure("p", max_concurrency=2)
    in_flight = 0
    peak = 0

    async def worker():
        nonlocal in_flight, peak
        async with scheduler.slot("p"):
            in_flight += 1
            peak = max(peak, in_flight)
            await asyncio.sleep(0.02)
            in_flight -= 1

    await asyncio.gather(*[worker() for _ in range(6)])
    assert peak <= 2


@pytest.mark.asyncio
async def test_reconfigure_rebuilds_loop_primitives_for_provider():
    scheduler = RequestScheduler().configure("p", max_concurrency=2)
    async with scheduler.slot("p"):
        pass
    scheduler.configure("p", max_concurrency=1)
    in_flight = 0
    peak = 0

    async def worker():
        nonlocal in_flight, peak
        async with scheduler.slot("p"):
            in_flight += 1
            peak = max(peak, in_flight)
            await asyncio.sleep(0.02)
            in_flight -= 1

    await asyncio.gather(*[worker() for _ in range(3)])
    assert peak <= 1


@pytest.mark.asyncio
async def test_rate_limit_spaces_request_starts():
    scheduler = RequestScheduler().configure("p", rate_per_second=50)  # 20ms min interval
    starts = []

    async def worker():
        async with scheduler.slot("p"):
            starts.append(asyncio.get_event_loop().time())

    await asyncio.gather(*[worker() for _ in range(4)])
    starts.sort()
    gaps = [starts[i + 1] - starts[i] for i in range(len(starts) - 1)]
    # Each consecutive start is spaced by ~ the min interval (allow scheduling slack).
    assert all(gap >= 0.015 for gap in gaps)


@pytest.mark.asyncio
async def test_configure_from_settings_reads_provider_override():
    scheduler = RequestScheduler()
    settings = Settings(name="scheduler-test")
    settings.set("model_request.scheduler.max_concurrency", 4)
    settings.set("model_request.scheduler.providers", {"OpenAICompatible": {"max_concurrency": 1}})
    scheduler.configure_from_settings("OpenAICompatible", settings)
    assert scheduler.is_active("OpenAICompatible") is True
    # Provider override (1) is applied, not the global default (4).
    config = scheduler._configs["OpenAICompatible"]
    assert config.max_concurrency == 1
