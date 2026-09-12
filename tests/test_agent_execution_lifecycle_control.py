"""Host lifecycle tests; synthetic producers make no model-quality claims."""

from __future__ import annotations

import asyncio
import copy
import json
from typing import cast

import pytest

from agently import Agently
from agently.builtins.plugins.AgentExecution import AgentExecution, ProductionOptions
from agently.core import PluginManager
from agently.core.application.AgentExecution import AgentExecutionPaused
from agently.utils import Settings


class ControlledExecution(AgentExecution):
    name = "controlled-lifecycle-test"
    producer_route = "model_request"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.cleanup_entered = asyncio.Event()
        self.cleanup_release = asyncio.Event()
        self.cleanup_release.set()
        self.calls = 0
        self.cleaned = False
        self.failure: Exception | None = None

    async def _async_produce(self, options: ProductionOptions) -> tuple[str, object]:
        self.calls += 1
        self.entered.set()
        try:
            await self.release.wait()
            if self.failure is not None:
                raise self.failure
            return "model_request", {"value": self.calls}
        finally:
            self.cleanup_entered.set()
            await self.cleanup_release.wait()
            self.cleaned = True


def execution() -> ControlledExecution:
    settings = Settings(name="lifecycle-test", parent=Agently.settings)
    manager = PluginManager(settings, parent=Agently.plugin_manager)
    manager.register("AgentExecution", ControlledExecution, activate=False)
    agent = Agently.AgentType(manager, parent_settings=settings, name="lifecycle-test")
    return cast(ControlledExecution, agent.create_execution(ControlledExecution.name))


@pytest.mark.asyncio
async def test_cancel_draft_prevents_dispatch_and_mutation():
    run = execution()
    result = await run.async_cancel()
    assert result["status"] == "cancelled"
    assert run.calls == 0
    with pytest.raises(asyncio.CancelledError):
        await run.async_run()
    with pytest.raises(RuntimeError, match="already started"):
        run.input("new task")
    assert await run.async_cancel() == result


@pytest.mark.asyncio
async def test_cancel_waits_for_cleanup_without_waiting_for_callers_followup():
    run = execution()
    followup = asyncio.Event()

    async def caller():
        with pytest.raises(asyncio.CancelledError):
            await run.async_run()
        await followup.wait()

    caller_task = asyncio.create_task(caller())
    await run.entered.wait()
    result = await asyncio.wait_for(run.async_cancel(), timeout=2)
    assert result["status"] == "cancelled"
    assert run.cleaned
    assert not caller_task.done()
    followup.set()
    await caller_task


@pytest.mark.asyncio
async def test_cancel_timeout_does_not_claim_settlement_or_cancel_cleanup_again():
    run = execution()
    run.cleanup_release.clear()
    caller = asyncio.create_task(run.async_run())
    await run.entered.wait()
    with pytest.raises(asyncio.TimeoutError):
        await run.async_cancel(timeout=0.01)
    await run.cleanup_entered.wait()
    assert not run._completed
    assert run.status == "running"
    assert not run._closed
    second_cancel = asyncio.create_task(run.async_cancel())
    await asyncio.sleep(0)
    assert not second_cancel.done()
    run.cleanup_release.set()
    assert (await second_cancel)["status"] == "cancelled"
    assert run.cleaned
    with pytest.raises(asyncio.CancelledError):
        await caller


@pytest.mark.asyncio
async def test_close_drains_and_preserves_result_readers():
    run = execution()
    old_reader = run.get_result()
    caller = asyncio.create_task(old_reader.async_get_data())
    await run.entered.wait()
    with pytest.raises(asyncio.TimeoutError):
        await run.async_close(timeout=0.01)
    assert not run._cancel_requested
    assert not run._closed
    run.release.set()
    assert await caller == {"value": 1}
    result = await run.async_close()
    assert result["closed"] is True
    assert result["status"] == "success"
    assert await old_reader.async_get_data() == {"value": 1}
    assert await run.async_run() == {"value": 1}
    assert run.calls == 1
    assert await run.async_close() == result


@pytest.mark.asyncio
async def test_close_draft_prevents_dispatch():
    run = execution()
    assert (await run.async_close())["closed"] is True
    with pytest.raises(RuntimeError, match="closed"):
        await run.async_run()
    assert run.calls == 0


@pytest.mark.asyncio
async def test_concurrent_readers_share_once_only_production():
    run = execution()
    readers = [asyncio.create_task(run.async_run()) for _ in range(5)]
    await run.entered.wait()
    run.release.set()
    assert await asyncio.gather(*readers) == [{"value": 1}] * 5
    assert run.calls == 1


@pytest.mark.asyncio
async def test_producer_failure_is_preserved_by_close_and_readers():
    run = execution()
    run.failure = ValueError("producer failed")
    run.release.set()
    with pytest.raises(ValueError, match="producer failed"):
        await run.async_run()
    with pytest.raises(ValueError, match="producer failed"):
        await run.async_close()
    with pytest.raises(ValueError, match="producer failed"):
        await run.async_run()
    assert run.calls == 1
    assert run.cleaned


@pytest.mark.asyncio
async def test_close_explicit_cancellation_settles_run():
    run = execution()
    caller = asyncio.create_task(run.async_run())
    await run.entered.wait()
    result = await run.async_close(pending="cancel")
    assert result == {"execution_id": run.id, "status": "cancelled", "closed": True}
    assert run.cleaned
    with pytest.raises(asyncio.CancelledError):
        await caller


@pytest.mark.asyncio
async def test_sync_cancel_from_another_thread_joins_the_owner_loop():
    run = execution()
    caller = asyncio.create_task(run.async_run())
    await run.entered.wait()
    result = await asyncio.to_thread(run.cancel, timeout=2)
    assert result["status"] == "cancelled"
    assert run.cleaned
    with pytest.raises(asyncio.CancelledError):
        await caller


@pytest.mark.asyncio
async def test_cancel_during_context_preparation_never_enters_producer():
    run = execution()
    entered = asyncio.Event()
    cleaned = asyncio.Event()

    async def prepare():
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaned.set()

    run.async_prepare_task_context = prepare  # type: ignore[method-assign]
    caller = asyncio.create_task(run.async_run())
    await entered.wait()
    await run.async_cancel()
    assert cleaned.is_set()
    assert run.calls == 0
    with pytest.raises(asyncio.CancelledError):
        await caller


@pytest.mark.asyncio
async def test_cancellation_cleanup_failure_is_not_successful_cancellation():
    run = execution()

    async def fail_cleanup(options):
        run.entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            raise ValueError("cleanup failed")

    run._async_produce = fail_cleanup
    caller = asyncio.create_task(run.async_run())
    await run.entered.wait()
    with pytest.raises(ValueError, match="cleanup failed"):
        await run.async_cancel()
    assert run.status == "error"
    assert not run._closed
    with pytest.raises(ValueError, match="cleanup failed"):
        await caller


@pytest.mark.asyncio
async def test_pause_draft_is_a_triggerflow_wait_and_reader_does_not_resume():
    run = execution()
    receipt = await run.async_pause()
    assert receipt["status"] == "created"
    assert receipt.get("pause_requested") is True
    with pytest.raises(AgentExecutionPaused) as paused:
        await run.async_run()
    assert paused.value.boundary == "before_production"
    assert run.status == "paused"
    assert not run._completed
    assert run.calls == 0
    assert run._pause_flow is not None
    assert "execution-pause" in run._pause_flow.get_pending_interrupts()
    with pytest.raises(AgentExecutionPaused):
        await run.async_get_data()
    with pytest.raises(RuntimeError, match="unresolved waits"):
        await run.async_close()
    run.release.set()
    assert await run.async_resume() == {"value": 1}
    assert run.calls == 1
    with pytest.raises(RuntimeError, match="not paused"):
        await run.async_resume()


@pytest.mark.asyncio
async def test_pause_candidate_waits_for_producer_then_resumes_only_final_policies():
    run = execution()
    reviewed: list[object] = []

    def review(value, context):
        reviewed.append(value)
        return True

    run.review(review)
    caller = asyncio.create_task(run.async_run())
    await run.entered.wait()
    receipt = await run.async_pause()
    assert receipt["status"] == "running"
    assert not run.cleaned
    run.release.set()
    with pytest.raises(AgentExecutionPaused) as paused:
        await caller
    assert paused.value.boundary == "candidate_ready"
    assert run.cleaned
    assert reviewed == []
    assert await run.async_resume() == {"value": 1}
    assert reviewed == [{"value": 1}]
    assert run.calls == 1


@pytest.mark.asyncio
async def test_cancel_paused_execution_cancels_actual_graph_wait():
    run = execution()
    await run.async_pause()
    with pytest.raises(AgentExecutionPaused):
        await run.async_run()
    flow = run._pause_flow
    assert flow is not None
    result = await run.async_close(pending="cancel")
    assert result["closed"] is True
    assert result["status"] == "cancelled"
    assert flow.is_closed()
    assert not flow.get_pending_interrupts()
    assert run.calls == 0


@pytest.mark.asyncio
async def test_paused_execution_cannot_reset_cumulative_deadline_on_resume():
    run = execution()
    run.limits["max_seconds"] = 0.001
    await run.async_pause()
    with pytest.raises(AgentExecutionPaused):
        await run.async_run()
    await asyncio.sleep(0.01)
    with pytest.raises(TimeoutError):
        await run.async_resume()
    assert run.calls == 0


@pytest.mark.asyncio
async def test_save_load_draft_rebinds_identity_and_does_not_dispatch():
    original = execution()
    original.input("bound task")
    original.execution_context.consume_model_request(response_id="earlier-request")
    await original.async_pause()
    with pytest.raises(AgentExecutionPaused):
        await original.async_run()
    snapshot = json.loads(json.dumps(original.save()))
    restored = cast(ControlledExecution, original.agent.create_execution(ControlledExecution.name))
    restored.input("bound task")
    restored.load(snapshot)
    assert restored.id == original.id
    assert restored.calls == 0
    assert restored.execution_context.model_request_count == 1
    assert restored.execution_context.started_at == pytest.approx(
        original.execution_context.started_at, abs=0.001,
    )
    with pytest.raises(AgentExecutionPaused):
        await restored.async_get_data()
    restored.release.set()
    assert await restored.async_resume() == {"value": 1}
    await original.async_close(pending="cancel")


@pytest.mark.asyncio
async def test_save_load_candidate_runs_only_rebound_final_policies():
    original = execution()
    reviews: list[object] = []

    def review(value, context):
        reviews.append(value)
        return True

    original.review(review)
    caller = asyncio.create_task(original.async_run())
    await original.entered.wait()
    await original.async_pause()
    original.release.set()
    with pytest.raises(AgentExecutionPaused):
        await caller
    snapshot = original.save()
    restored = cast(ControlledExecution, original.agent.create_execution(ControlledExecution.name))
    restored.review(review)
    restored.load(snapshot)
    assert reviews == []
    assert restored.calls == 0
    assert await restored.async_resume() == {"value": 1}
    assert restored.calls == 0
    assert reviews == [{"value": 1}]
    await original.async_close(pending="cancel")


@pytest.mark.asyncio
async def test_snapshot_rejects_missing_policy_changed_draft_and_corrupt_candidate():
    original = execution()

    def review(value, context):
        return True

    original.input("source").review(review)
    await original.async_pause()
    with pytest.raises(AgentExecutionPaused):
        await original.async_run()
    snapshot = original.save()
    missing_policy = cast(ControlledExecution, original.agent.create_execution(ControlledExecution.name))
    missing_policy.input("source")
    with pytest.raises(ValueError, match="resource/policy"):
        missing_policy.load(snapshot)
    assert not missing_policy._started
    changed_draft = cast(ControlledExecution, original.agent.create_execution(ControlledExecution.name))
    changed_draft.input("changed").review(review)
    with pytest.raises(ValueError, match="draft or limits"):
        changed_draft.load(snapshot)
    corrupted = copy.deepcopy(snapshot)
    corrupted["candidate"] = "changed bytes"
    rebound = cast(ControlledExecution, original.agent.create_execution(ControlledExecution.name))
    rebound.input("source").review(review)
    with pytest.raises(ValueError, match="content identity"):
        rebound.load(corrupted)
    assert not rebound._started
    assert all(run.calls == 0 for run in (missing_policy, changed_draft, rebound))
    await original.async_close(pending="cancel")


@pytest.mark.asyncio
async def test_interrupt_is_context_information_and_has_a_distinct_consumption_receipt():
    run = execution()
    run.input("original task")
    receipt = await run.async_interrupt("additional source fact", author="host")
    assert receipt["status"] == "inserted"
    assert not run._cancel_requested
    assert run.prompt.get("input") == "original task"
    package = await run.async_read_task_context(consumer_id="synthetic-consumer", phase="direct")
    assert any(block.content == "additional source fact" for block in package.blocks)
    assert run.guidance_items[0]["status"] == "inserted"
    run.record_context_consumption(package, request_id="synthetic-request-identity")
    assert run.guidance_items[0]["status"] == "consumed"
    assert run.guidance_items[0]["request_id"] == "synthetic-request-identity"
    await run.async_close()


@pytest.mark.asyncio
async def test_interrupt_after_candidate_is_ignored_without_mutating_it():
    run = execution()
    caller = asyncio.create_task(run.async_run())
    await run.entered.wait()
    await run.async_pause()
    run.release.set()
    with pytest.raises(AgentExecutionPaused):
        await caller
    receipt = await run.async_interrupt("do something different")
    assert receipt["status"] == "ignored"
    assert run.result == {"value": 1}
    assert await run.async_resume() == {"value": 1}


@pytest.mark.asyncio
async def test_snapshot_preserves_pending_interrupt_information_without_model_dispatch():
    original = execution()
    await original.async_interrupt("source fact")
    await original.async_pause()
    with pytest.raises(AgentExecutionPaused):
        await original.async_run()
    snapshot = original.save()
    restored = cast(ControlledExecution, original.agent.create_execution(ControlledExecution.name))
    restored.load(snapshot)
    assert restored.guidance_items[0]["status"] == "inserted"
    assert restored.calls == 0
    assert any(entry.content == "source fact" for entry in restored.task_context.snapshot().entries)
    await restored.async_close(pending="cancel")
    await original.async_close(pending="cancel")


@pytest.mark.asyncio
async def test_loaded_pause_close_cancel_settles_retained_flow():
    original = execution()
    await original.async_pause()
    with pytest.raises(AgentExecutionPaused):
        await original.async_run()
    restored = cast(ControlledExecution, original.agent.create_execution(ControlledExecution.name)).load(original.save())
    flow = restored._pause_flow
    assert flow is not None
    receipt = await restored.async_close(pending="cancel")
    assert receipt["status"] == "cancelled"
    assert receipt["closed"] and flow.is_closed()
    assert restored.calls == 0
    await original.async_cancel()


@pytest.mark.asyncio
async def test_second_pause_retains_new_continuation():
    run = execution()
    await run.async_pause()
    with pytest.raises(AgentExecutionPaused):
        await run.async_run()
    first_flow = run._pause_flow
    resumed = asyncio.create_task(run.async_resume())
    await run.entered.wait()
    await run.async_pause()
    run.release.set()
    with pytest.raises(AgentExecutionPaused):
        await resumed
    assert first_flow is not None and first_flow.is_closed()
    assert run._pause_flow is not first_flow
    assert run.status == "paused" and run.calls == 1
    assert await run.async_resume() == {"value": 1}
    assert run.calls == 1


@pytest.mark.asyncio
async def test_paused_cancel_timeout_keeps_owned_cleanup_and_reader_failure(monkeypatch):
    run = execution()
    await run.async_pause()
    with pytest.raises(AgentExecutionPaused):
        await run.async_run()
    assert run._pause_flow is not None
    close_flow = run._pause_flow.async_close
    cleanup = asyncio.Event()
    close_calls = 0

    async def slow_close(*args, **kwargs):
        nonlocal close_calls
        close_calls += 1
        await cleanup.wait()
        return await close_flow(*args, **kwargs)

    monkeypatch.setattr(run._pause_flow, "async_close", slow_close)
    with pytest.raises(asyncio.TimeoutError):
        await run.async_cancel(timeout=0.01)
    assert run.status == "cancelling" and not run._completed
    again = asyncio.create_task(run.async_cancel())
    cleanup.set()
    assert (await again)["status"] == "cancelled"
    assert close_calls == 1
    with pytest.raises(asyncio.CancelledError):
        await run.async_get_data()


@pytest.mark.asyncio
async def test_stream_reader_exposes_pause_without_hanging_or_closing_owner():
    run = execution()
    await run.async_pause()

    async def consume():
        return [item async for item in run.get_async_generator(type="instant")]

    with pytest.raises(AgentExecutionPaused):
        await asyncio.wait_for(consume(), timeout=2)
    assert run.status == "paused" and not run._closed
    run.release.set()
    assert await run.async_resume() == {"value": 1}


class RevisingExecution(ControlledExecution):
    def _assert_rework_supported(self) -> None:
        # This synthetic producer explicitly declares rerun safety for this test.
        pass

    async def _async_produce(self, options: ProductionOptions) -> tuple[str, object]:
        self.execution_context.consume_model_request()
        return await super()._async_produce(options)


@pytest.mark.asyncio
async def test_rework_same_identity_preserves_captured_readers_and_budget():
    run = RevisingExecution(execution().agent, limits={"max_model_requests": 3})
    run.input("original contract")
    run.release.set()
    old = run.get_result()
    assert await old.async_get_data() == {"value": 1}
    first_meta = await old.async_get_meta()
    identity = run.id
    assert await run.async_rework("Revise the presentation") == {"value": 2}
    assert run.id == identity and run.revision == 1
    assert old.revision == 0 and await old.async_get_data() == {"value": 1}
    assert await old.async_get_meta() == first_meta
    assert await asyncio.to_thread(old.get_data) == {"value": 1}
    assert await run.get_result(revision=0).async_get_data() == {"value": 1}
    assert await run.get_result().async_get_data() == {"value": 2}
    assert run.execution_context.model_request_count == 2
    assert run.prompt_snapshot["input"] == "original contract"
    assert "Revise the presentation" in run.request.prompt.to_text()


@pytest.mark.asyncio
async def test_failed_rework_keeps_prior_candidate_and_consumes_attempt():
    run = RevisingExecution(execution().agent, limits={"max_model_requests": 1})
    run.release.set()
    old = run.get_result()
    await run.async_run()
    from agently.core.application.AgentExecution import AgentExecutionLimitExceeded
    with pytest.raises(AgentExecutionLimitExceeded):
        await run.async_rework("Change the draft", max_reworks=1)
    assert run.revision == 1 and run.status == "blocked"
    assert await old.async_get_data() == {"value": 1}
    with pytest.raises(AgentExecutionLimitExceeded):
        await run.async_run()


@pytest.mark.asyncio
async def test_rework_limit_cannot_be_raised_and_custom_producer_is_not_implicitly_safe():
    custom = execution()
    custom.release.set()
    await custom.async_run()
    with pytest.raises(NotImplementedError):
        await custom.async_rework("Change it")
    assert custom.revision == 0 and custom.calls == 1
    run = RevisingExecution(execution().agent)
    run.release.set()
    await run.async_run()
    await run.async_rework("First change", max_reworks=1)
    from agently.core.application.AgentExecution import AgentExecutionLimitExceeded
    with pytest.raises(AgentExecutionLimitExceeded, match="rework budget"):
        await run.async_rework("Second change", max_reworks=100)
    assert run.revision == 1 and run.calls == 2


@pytest.mark.asyncio
async def test_snapshot_restores_revisions_readers_and_replay_budget():
    run = RevisingExecution(execution().agent, limits={"max_model_requests": 3})
    run.input("original")
    run.release.set()
    await run.async_run()
    run.release.clear()
    run.entered.clear()
    revise = asyncio.create_task(run.async_rework("Revise", max_reworks=1))
    await run.entered.wait()
    await run.async_pause()
    run.release.set()
    with pytest.raises(AgentExecutionPaused):
        await revise
    run.execution_context._dispatched_action_ids.add("external_write")
    run.execution_context._rework_blocked_action_ids.add("external_write")
    saved = run.save()
    restored = RevisingExecution(run.agent, limits={"max_model_requests": 3}).input("original")
    restored.load(json.loads(json.dumps(saved)))
    assert restored.id == run.id and restored.revision == 1
    assert restored.execution_context.model_request_count == 2
    assert await restored.get_result(revision=0).async_get_data() == {"value": 1}
    assert await restored.async_resume() == {"value": 2}
    assert restored.calls == 0
    assert restored.execution_context._rework_blocked_action_ids == {"external_write"}
    from agently.core.application.AgentExecution import AgentExecutionLimitExceeded
    with pytest.raises(AgentExecutionLimitExceeded):
        await restored.async_rework("Again", max_reworks=3)
    await run.async_cancel()


@pytest.mark.asyncio
async def test_snapshot_rejects_live_resource_and_cancel_releases_only_owned_scope(monkeypatch):
    from agently import base
    run = execution()
    run.execution_context._resource_handle_ids.update({"owned", "external"})
    handles = {
        "owned": {"scope": "execution", "owner_id": run.id, "status": "ready"},
        "external": {"scope": "session", "owner_id": "shared-session", "status": "ready"},
    }
    released = []

    class Resources:
        def inspect(self, key):
            return handles.get(key)

        async def async_release_scope(self, scope, owner_id):
            released.append((scope, owner_id))
            handles["owned"]["status"] = "released"

    monkeypatch.setattr(base, "execution_resource", Resources())
    job = asyncio.create_task(run.async_run())
    await run.entered.wait()
    await run.async_pause()
    run.release.set()
    with pytest.raises(AgentExecutionPaused):
        await job
    with pytest.raises(RuntimeError, match="live ExecutionResource"):
        run.save()
    await run.async_cancel()
    assert released == [("execution", run.id)]
    assert handles["external"]["status"] == "ready"
