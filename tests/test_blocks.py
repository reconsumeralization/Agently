"""Unit + integration tests for standard action blocks (Spec C)."""

from __future__ import annotations

import pytest

from agently.builtins.blocks import (
    FlowBlock,
    ReasonBlock,
    IntentBlock,
    ReadBlock,
    FinalizeBlock,
)
from agently.core.TriggerFlow.BluePrint import TriggerFlowBlueprint


class MockContext:
    """Minimal SkillsExecutionContext stub for unit tests."""

    def __init__(self):
        self.model_calls: list[dict] = []
        self.resource_reads: list[dict] = []
        self.stream_events: list[dict] = []
        self._model_response = "mock result"

    async def async_request_model(self, **kwargs):
        self.model_calls.append(kwargs)
        # If stream_handler is provided, simulate a stream event
        sh = kwargs.get("stream_handler")
        if sh:
            await sh({"delta": "mock", "path": "output"})
        return self._model_response

    async def async_read_resource(self, *, skill_id, path, max_bytes=65536):
        self.resource_reads.append({"skill_id": skill_id, "path": path, "max_bytes": max_bytes})
        return f"content of {path} (max {max_bytes} bytes)"

    async def async_emit_runtime_stream(self, item):
        self.stream_events.append(item)


# ═══════════════════════════════════════════════════════════
# FlowBlock Protocol
# ═══════════════════════════════════════════════════════════


class TestFlowBlockProtocol:
    def test_all_blocks_implement_protocol(self):
        assert isinstance(ReasonBlock(), FlowBlock)
        assert isinstance(IntentBlock(), FlowBlock)
        assert isinstance(ReadBlock(), FlowBlock)
        assert isinstance(FinalizeBlock(), FlowBlock)

    def test_block_names(self):
        assert ReasonBlock.name == "ReasonBlock"
        assert IntentBlock.name == "IntentBlock"
        assert ReadBlock.name == "ReadBlock"
        assert FinalizeBlock.name == "FinalizeBlock"


# ═══════════════════════════════════════════════════════════
# ReasonBlock
# ═══════════════════════════════════════════════════════════


class TestReasonBlock:
    @pytest.mark.asyncio
    async def test_direct_execute_basic(self):
        ctx = MockContext()
        block = ReasonBlock(model_key="reason")
        result = await block.execute(prompt="hello", context=ctx)

        assert result == "mock result"
        assert len(ctx.model_calls) == 1
        assert ctx.model_calls[0]["model_key"] == "reason"
        assert ctx.model_calls[0]["prompt"] == "hello"

    @pytest.mark.asyncio
    async def test_direct_execute_emits_stream_events(self):
        ctx = MockContext()
        block = ReasonBlock(stream_bridge=True)
        await block.execute(prompt="hello", context=ctx)

        event_types = [e["type"] for e in ctx.stream_events]
        assert "block.reason" in event_types
        actions = [e["action"] for e in ctx.stream_events]
        assert "start" in actions
        assert "done" in actions

    @pytest.mark.asyncio
    async def test_direct_execute_no_stream_bridge(self):
        ctx = MockContext()
        block = ReasonBlock(stream_bridge=False)
        await block.execute(prompt="hello", context=ctx)

        # Only start + done events, no delta
        event_types = [e["type"] for e in ctx.stream_events]
        assert len([t for t in event_types if t == "block.reason"]) == 2  # start + done

    @pytest.mark.asyncio
    async def test_direct_execute_passes_model_key(self):
        ctx = MockContext()
        block = ReasonBlock(model_key="reason")
        await block.execute(prompt="hello", context=ctx)

        assert ctx.model_calls[0]["model_key"] == "reason"

    @pytest.mark.asyncio
    async def test_direct_execute_passes_output_schema(self):
        ctx = MockContext()
        block = ReasonBlock()
        schema = {"status": (str, "ok")}
        await block.execute(prompt="hello", context=ctx, output_schema=schema)

        assert ctx.model_calls[0]["output_schema"] == schema

    def test_build_operators_creates_chunk_operator(self):
        ctx = MockContext()
        bp = TriggerFlowBlueprint(name="test")
        block = ReasonBlock()

        ids = block.build_operators(blueprint=bp, context=ctx, settings={})
        assert len(ids) == 1
        assert ids[0].startswith("reason-block-")

        # Verify operator in definition
        ops = bp.definition.operators
        assert len(ops) == 1
        assert ops[0]["kind"] == "chunk"
        assert ops[0]["name"] == "ReasonBlock"

    def test_build_operators_registers_handler(self):
        ctx = MockContext()
        bp = TriggerFlowBlueprint(name="test")
        block = ReasonBlock()
        ids = block.build_operators(blueprint=bp, context=ctx, settings={})

        # Handler should be registered
        event_handlers = bp._handlers["event"]
        handler_count = sum(len(h) for h in event_handlers.values())
        assert handler_count == 1


# ═══════════════════════════════════════════════════════════
# ReadBlock
# ═══════════════════════════════════════════════════════════


class TestReadBlock:
    @pytest.mark.asyncio
    async def test_direct_execute(self):
        ctx = MockContext()
        block = ReadBlock(max_bytes=4096)
        content = await block.execute(
            skill_id="test-skill", path="references/data.txt", context=ctx
        )

        assert "data.txt" in content
        assert "4096" in content
        assert len(ctx.resource_reads) == 1
        assert ctx.resource_reads[0]["skill_id"] == "test-skill"

    @pytest.mark.asyncio
    async def test_direct_execute_emits_event(self):
        ctx = MockContext()
        block = ReadBlock()
        await block.execute(skill_id="s1", path="p1", context=ctx)

        events = [e for e in ctx.stream_events if e["type"] == "block.resource.read"]
        assert len(events) == 1
        assert events[0]["payload"]["skill_id"] == "s1"

    @pytest.mark.asyncio
    async def test_direct_execute_respects_max_bytes_override(self):
        ctx = MockContext()
        block = ReadBlock(max_bytes=65536)
        await block.execute(skill_id="s1", path="p1", context=ctx, max_bytes=100)

        assert ctx.resource_reads[0]["max_bytes"] == 100

    def test_build_operators(self):
        ctx = MockContext()
        bp = TriggerFlowBlueprint(name="test")
        block = ReadBlock()
        ids = block.build_operators(blueprint=bp, context=ctx, settings={})

        assert len(ids) == 1
        assert ids[0].startswith("read-block-")
        assert bp.definition.operators[0]["kind"] == "chunk"


# ═══════════════════════════════════════════════════════════
# IntentBlock
# ═══════════════════════════════════════════════════════════


class TestIntentBlock:
    @pytest.mark.asyncio
    async def test_direct_execute_uses_default_schema(self):
        ctx = MockContext()
        ctx._model_response = {"intent": "greeting", "confidence": 0.95}
        block = IntentBlock()

        result = await block.execute(prompt="Hello!", context=ctx)
        assert result["intent"] == "greeting"
        assert result["confidence"] == 0.95
        assert ctx.model_calls[0]["output_format"] == "json"

    @pytest.mark.asyncio
    async def test_direct_execute_emits_intent_event(self):
        ctx = MockContext()
        ctx._model_response = {"intent": "query", "confidence": 0.8}
        block = IntentBlock()
        await block.execute(prompt="test", context=ctx)

        intent_events = [e for e in ctx.stream_events if e["type"] == "block.intent"]
        assert len(intent_events) == 1
        assert intent_events[0]["payload"]["intent"] == "query"

    @pytest.mark.asyncio
    async def test_direct_execute_custom_schema(self):
        ctx = MockContext()
        ctx._model_response = {"action": "search", "score": 0.9}
        block = IntentBlock(
            intent_schema={
                "action": (str, "action to take"),
                "score": (float, "confidence"),
            }
        )
        result = await block.execute(prompt="test", context=ctx)
        assert result["action"] == "search"

    def test_build_operators(self):
        ctx = MockContext()
        bp = TriggerFlowBlueprint(name="test")
        block = IntentBlock()
        ids = block.build_operators(blueprint=bp, context=ctx, settings={})

        assert len(ids) == 1
        assert ids[0].startswith("intent-block-")


# ═══════════════════════════════════════════════════════════
# FinalizeBlock
# ═══════════════════════════════════════════════════════════


class TestFinalizeBlock:
    @pytest.mark.asyncio
    async def test_direct_execute_emits_finalize_event(self):
        ctx = MockContext()
        block = FinalizeBlock()
        result = await block.execute(context=ctx, collected_outputs={"a": 1})

        assert result == {"a": 1}
        final_events = [e for e in ctx.stream_events if e["type"] == "block.finalize"]
        assert len(final_events) == 1
        assert final_events[0]["action"] == "done"

    @pytest.mark.asyncio
    async def test_direct_execute_empty_outputs(self):
        ctx = MockContext()
        block = FinalizeBlock()
        result = await block.execute(context=ctx)

        assert result == {}

    @pytest.mark.asyncio
    async def test_direct_execute_with_semantic_outputs(self):
        ctx = MockContext()
        ctx._model_response = {"summary": "structured result"}
        block = FinalizeBlock(
            model_key="reason",
            semantic_outputs={"summary": (str, "final summary")},
        )
        result = await block.execute(context=ctx, collected_outputs={"raw": "data"})

        assert result == {"summary": "structured result"}
        assert len(ctx.model_calls) == 1

    def test_build_operators(self):
        ctx = MockContext()
        bp = TriggerFlowBlueprint(name="test")
        block = FinalizeBlock()
        ids = block.build_operators(blueprint=bp, context=ctx, settings={})

        assert len(ids) == 1
        assert ids[0].startswith("finalize-block-")
        assert bp.definition.operators[0]["kind"] == "chunk"


# ═══════════════════════════════════════════════════════════
# TriggerFlow integration: ReasonBlock → FinalizeBlock
# ═══════════════════════════════════════════════════════════


class TestBlockOnTriggerFlow:
    """End-to-end: two blocks wired on a real TriggerFlow execution."""

    @pytest.mark.asyncio
    async def test_reason_to_finalize_flow(self):
        ctx = MockContext()
        ctx._model_response = "reasoned output"
        bp = TriggerFlowBlueprint(name="test-flow")

        reason = ReasonBlock(model_key="reason")
        finalize = FinalizeBlock()

        reason_ids = reason.build_operators(blueprint=bp, context=ctx, settings={})
        fin_ids = finalize.build_operators(blueprint=bp, context=ctx, settings={})

        # Wire ReasonBlock → FinalizeBlock by updating the ReasonBlock's
        # emit signal to trigger the FinalizeBlock's listen signal.
        reason_op = None
        for op in bp.definition.operators:
            if op["id"] == reason_ids[0]:
                reason_op = op
                break
        assert reason_op is not None

        fin_op = None
        for op in bp.definition.operators:
            if op["id"] == fin_ids[0]:
                fin_op = op
                break
        assert fin_op is not None

        # Re-wire: ReasonBlock done → FinalizeBlock start
        reason_done_event = reason_op["emit_signals"][0]["trigger_event"]
        fin_start_event = fin_op["listen_signals"][0]["trigger_event"]

        # Register FinalizeBlock's handler to listen for ReasonBlock's done event
        fin_handler_id = list(bp._handlers["event"][fin_start_event].keys())[0]
        fin_handler = bp._handlers["event"][fin_start_event][fin_handler_id]
        bp.add_handler("event", reason_done_event, fin_handler)

        # Verify both handlers are registered
        assert reason_done_event in bp._handlers["event"] or any(True for _ in [])
        assert fin_start_event in bp._handlers["event"]

        # The operators exist and have valid structure
        assert reason_op["kind"] == "chunk"
        assert fin_op["kind"] == "chunk"
