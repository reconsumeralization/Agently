"""Unit tests for staged and react strategy runners on TriggerFlow."""

from __future__ import annotations

from typing import Any

import pytest


class MockStrategyContext:
    """Minimal context stub for strategy runner unit tests."""

    execution_environment: Any = None

    def __init__(self):
        self.model_calls: list[dict[str, Any]] = []
        self.resource_reads: list[dict[str, Any]] = []
        self.stream_events: list[dict[str, Any]] = []
        self._model_response: Any = "mock result"
        self.tool_results: dict[str, Any] = {}
        self.action_results: dict[str, Any] = {}
        self.action_spec_batches: list[dict[str, Any]] = []

    async def async_request_model(self, **kwargs: Any) -> Any:
        self.model_calls.append(kwargs)
        sh = kwargs.get("stream_handler")
        if sh:
            await sh({"delta": "mock", "path": "output"})
        return self._model_response

    async def async_read_resource(self, *, skill_id: str, path: str, max_bytes: int = 65536) -> str:
        self.resource_reads.append({"skill_id": skill_id, "path": path, "max_bytes": max_bytes})
        return f"content of {path} (max {max_bytes} bytes)"

    async def async_emit_runtime_stream(self, item: dict[str, Any]) -> None:
        self.stream_events.append(item)

    async def async_call_tool(self, name: str, **kwargs: Any) -> Any:
        self.tool_results[name] = kwargs
        return {"status": "ok", "tool": name}

    async def async_call_action(self, name: str, **kwargs: Any) -> Any:
        self.action_results[name] = kwargs
        return {"status": "ok", "action": name}

    async def async_execute_action_specs(
        self,
        action_specs: list[dict[str, Any]],
        *,
        concurrency: int | None = None,
    ) -> list[dict[str, Any]]:
        self.action_spec_batches.append({"specs": action_specs, "concurrency": concurrency})
        return [
            {
                "status": "success",
                "action_id": spec["name"],
                "result": {"status": "ok", "tool": spec["name"]},
            }
            for spec in action_specs
        ]


class TestStagedStrategy:
    @pytest.mark.asyncio
    async def test_staged_executes_steps_in_order(self):
        from agently.builtins.plugins.SkillsExecutor.AgentlySkillsExecutor.modules.strategies.staged import run_staged_execution

        ctx = MockStrategyContext()
        plan = {
            "execution_stages": [
                {"description": "Analyze input data"},
                {"description": "Generate report"},
                {"description": "Format output"},
            ],
        }

        result = await run_staged_execution(
            task="test task",
            plan=plan,
            context=ctx,
        )

        # Should have 3 model calls (one per step) + 1 finalize
        assert len(ctx.model_calls) >= 3
        assert result is not None

    @pytest.mark.asyncio
    async def test_staged_emits_events(self):
        from agently.builtins.plugins.SkillsExecutor.AgentlySkillsExecutor.modules.strategies.staged import run_staged_execution

        ctx = MockStrategyContext()
        plan = {
            "execution_stages": [
                {"description": "Step 1"},
                {"description": "Step 2"},
            ],
        }

        await run_staged_execution(task="test", plan=plan, context=ctx)

        event_types = [e["type"] for e in ctx.stream_events]
        assert "skills.staged.start" in event_types
        assert "skills.staged.step_start" in event_types
        assert "skills.staged.step_done" in event_types
        assert "skills.staged.done" in event_types

    @pytest.mark.asyncio
    async def test_staged_handles_empty_stages(self):
        from agently.builtins.plugins.SkillsExecutor.AgentlySkillsExecutor.modules.strategies.staged import run_staged_execution

        ctx = MockStrategyContext()
        plan = {"execution_stages": []}

        result = await run_staged_execution(task="test", plan=plan, context=ctx)
        assert result["status"] == "error"

    @pytest.mark.asyncio
    async def test_staged_respects_step_budget(self):
        from agently.builtins.plugins.SkillsExecutor.AgentlySkillsExecutor.modules.strategies.staged import run_staged_execution

        ctx = MockStrategyContext()
        plan = {
            "execution_stages": [
                {"description": f"Step {i}"} for i in range(10)
            ],
        }

        await run_staged_execution(task="test", plan=plan, context=ctx, step_budget=3)
        # Should only execute 3 steps (budget cap), but it starts from step_budget limit applied to stages[:step_budget]
        # Actually the budget cuts the stages list: stages[:3]
        # So 3 reason calls + 1 finalize
        reason_calls = [c for c in ctx.model_calls if c.get("stream_handler")]
        assert len(reason_calls) <= 4  # 3 steps + finalize

    @pytest.mark.asyncio
    async def test_staged_folds_prior_outputs(self):
        from agently.builtins.plugins.SkillsExecutor.AgentlySkillsExecutor.modules.strategies.staged import run_staged_execution

        ctx = MockStrategyContext()
        ctx._model_response = "output from step"
        plan = {
            "execution_stages": [
                {"description": "Step 1"},
                {"description": "Step 2"},
                {"description": "Step 3"},
            ],
        }

        await run_staged_execution(task="test", plan=plan, context=ctx)

        # Step 2+ prompts should include prior outputs
        step_prompts = [c["prompt"] for c in ctx.model_calls]
        # Second prompt should reference Step 1's output
        assert "output from step" in str(step_prompts[1])
        # Third prompt should reference both prior outputs
        assert "output from step" in str(step_prompts[2])

    @pytest.mark.asyncio
    async def test_staged_applies_semantic_outputs_at_finalize(self):
        from agently.builtins.plugins.SkillsExecutor.AgentlySkillsExecutor.modules.strategies.staged import run_staged_execution

        ctx = MockStrategyContext()
        calls: list[dict[str, Any]] = []

        async def dynamic_response(**kwargs):
            calls.append(kwargs)
            if kwargs.get("output_schema"):
                return {"decision": "ship", "reason": "all checks passed"}
            return "step output"

        ctx.async_request_model = dynamic_response
        semantic_outputs = {
            "decision": (str, "final decision", True),
            "reason": (str, "short reason", True),
        }
        plan = {
            "execution_stages": [{"description": "Review the implementation"}],
        }

        result = await run_staged_execution(
            task="test",
            plan=plan,
            context=ctx,
            semantic_outputs=semantic_outputs,
        )

        assert result == {"decision": "ship", "reason": "all checks passed"}
        finalize_calls = [call for call in calls if call.get("output_schema")]
        assert len(finalize_calls) == 1
        assert finalize_calls[0]["output_schema"] == semantic_outputs


class TestReactStrategy:
    @pytest.mark.asyncio
    async def test_react_terminates_on_final(self):
        from agently.builtins.plugins.SkillsExecutor.AgentlySkillsExecutor.modules.strategies.react import run_react_execution

        ctx = MockStrategyContext()
        ctx._model_response = {"next_action": "done", "final": True}

        result = await run_react_execution(
            task="test task",
            plan={},
            context=ctx,
            allowed_tools=["search"],
        )

        assert result is not None
        # Should have 1 reason call (model said final=True immediately)
        reason_calls = [c for c in ctx.model_calls if c.get("output_format") == "json"]
        assert len(reason_calls) == 1

    @pytest.mark.asyncio
    async def test_react_loops_until_budget_exhausted(self):
        from agently.builtins.plugins.SkillsExecutor.AgentlySkillsExecutor.modules.strategies.react import run_react_execution

        ctx = MockStrategyContext()
        call_count = [0]

        async def dynamic_response(**kwargs):
            call_count[0] += 1
            sh = kwargs.get("stream_handler")
            if sh:
                await sh({"delta": "mock"})
            return {"next_action": f"step {call_count[0]}", "next_tool": "search", "final": False}

        ctx.async_request_model = dynamic_response

        await run_react_execution(
            task="test",
            plan={},
            context=ctx,
            step_budget=3,
            allowed_tools=["search"],
        )

        # Should stop at step_budget (3 reason calls)
        assert call_count[0] <= 3

    @pytest.mark.asyncio
    async def test_react_emits_events(self):
        from agently.builtins.plugins.SkillsExecutor.AgentlySkillsExecutor.modules.strategies.react import run_react_execution

        ctx = MockStrategyContext()
        ctx._model_response = {"next_action": "done", "final": True}

        await run_react_execution(
            task="test",
            plan={},
            context=ctx,
        )

        event_types = [e["type"] for e in ctx.stream_events]
        assert "skills.react.start" in event_types
        assert "skills.react.done" in event_types

    @pytest.mark.asyncio
    async def test_react_act_block_called_when_tool_specified(self):
        from agently.builtins.plugins.SkillsExecutor.AgentlySkillsExecutor.modules.strategies.react import run_react_execution

        ctx = MockStrategyContext()
        call_count = [0]

        async def dynamic_response(**kwargs):
            call_count[0] += 1
            sh = kwargs.get("stream_handler")
            if sh:
                await sh({"delta": "mock"})
            if call_count[0] == 1:
                return {"next_action": "search for data", "next_tool": "search", "final": False}
            return {"next_action": "done", "final": True}

        ctx.async_request_model = dynamic_response

        await run_react_execution(
            task="test",
            plan={},
            context=ctx,
            allowed_tools=["search"],
        )

        # Should have called search tool
        assert len(ctx.tool_results) >= 1
        assert "search" in ctx.tool_results

    @pytest.mark.asyncio
    async def test_react_empty_tools_list(self):
        from agently.builtins.plugins.SkillsExecutor.AgentlySkillsExecutor.modules.strategies.react import run_react_execution

        ctx = MockStrategyContext()
        ctx._model_response = {"next_action": "think about it", "final": True}

        result = await run_react_execution(
            task="test",
            plan={},
            context=ctx,
        )

        assert result is not None

    @pytest.mark.asyncio
    async def test_react_parallel_tools_use_action_runtime_surface(self):
        from agently.builtins.plugins.SkillsExecutor.AgentlySkillsExecutor.modules.strategies.react import run_react_execution

        ctx = MockStrategyContext()
        call_count = [0]

        async def dynamic_response(**kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                return {
                    "next_action": "look up independent facts",
                    "next_actions": [
                        {"next_tool": "search", "next_kwargs": {"q": "alpha"}},
                        {"next_tool": "lookup", "next_kwargs": {"id": "beta"}},
                    ],
                    "final": False,
                }
            return {"next_action": "done", "final": True}

        ctx.async_request_model = dynamic_response

        await run_react_execution(
            task="test",
            plan={},
            context=ctx,
            allowed_tools=["search", "lookup"],
        )

        assert len(ctx.action_spec_batches) == 1
        assert [spec["name"] for spec in ctx.action_spec_batches[0]["specs"]] == ["search", "lookup"]
        assert ctx.tool_results == {}
