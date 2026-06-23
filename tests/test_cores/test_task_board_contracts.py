import pytest

from agently.core import (
    TaskBoard,
    TaskBoardContext,
    TaskBoardGraph,
    TaskBoardRevision,
    TaskBoardValidator,
    build_task_board_evidence_view,
    coerce_task_board_planning_result,
    resolve_task_board_planning_policy,
)
from agently.types.data import TaskBoardCardResult, TaskBoardPatch


def _revision():
    return TaskBoardRevision.create(
        board_id="demo",
        graph=TaskBoardGraph.from_value(
            {
                "graph_id": "demo-graph",
                "cards": [
                    {"id": "collect", "objective": "Collect facts."},
                    {"id": "final", "objective": "Write final answer.", "depends_on": ["collect"]},
                ],
            }
        ),
    )


def test_task_board_validation_rejects_duplicate_ids():
    with pytest.raises(ValueError, match="Duplicate TaskBoardCard id"):
        TaskBoardValidator().validate(
            {
                "board_id": "duplicate",
                "revision_id": "rev-0",
                "graph": {
                    "graph_id": "duplicate-graph",
                    "cards": [
                        {"id": "a", "objective": "A"},
                        {"id": "a", "objective": "B"},
                    ],
                },
            }
        )


def test_task_board_validation_rejects_missing_dependency():
    with pytest.raises(ValueError, match="depends on missing card"):
        TaskBoardValidator().validate(
            {
                "board_id": "missing",
                "revision_id": "rev-0",
                "graph": {
                    "graph_id": "missing-graph",
                    "cards": [{"id": "a", "objective": "A", "depends_on": ["missing"]}],
                },
            }
        )


def test_task_board_validation_rejects_cycles():
    with pytest.raises(ValueError, match="root card|dependency cycle"):
        TaskBoardValidator().validate(
            {
                "board_id": "cycle",
                "revision_id": "rev-0",
                "graph": {
                    "graph_id": "cycle-graph",
                    "cards": [
                        {"id": "a", "objective": "A", "depends_on": ["b"]},
                        {"id": "b", "objective": "B", "depends_on": ["a"]},
                    ],
                },
            }
        )


def test_task_board_patch_base_revision_mismatch_fails_closed():
    revision = _revision()
    patch = TaskBoardPatch(
        base_revision="rev-stale",
        operations=(
            {
                "op": "record_card_result",
                "result": TaskBoardCardResult(card_id="collect", status="completed").to_dict(),
            },
        ),
    )

    with pytest.raises(ValueError, match="base_revision mismatch"):
        TaskBoardValidator().apply_patch(revision, patch)


def test_task_board_schedule_waits_for_completed_dependencies():
    revision = _revision()
    validator = TaskBoardValidator()

    first_schedule = validator.schedule(revision)
    assert first_schedule.runnable_card_ids == ("collect",)
    assert first_schedule.blocked_card_ids == ("final",)

    next_revision = validator.apply_patch(
        revision,
        TaskBoardPatch(
            base_revision="rev-0",
            operations=(
                {
                    "op": "record_card_result",
                    "result": {
                        "card_id": "collect",
                        "status": "completed",
                        "preview": "facts",
                        "file_refs": [{"path": "facts.md", "sha256": "abc"}],
                    },
                },
            ),
        ),
    )
    second_schedule = validator.schedule(next_revision)
    assert next_revision.revision_id == "rev-1"
    assert second_schedule.runnable_card_ids == ("final",)
    assert second_schedule.completed_card_ids == ("collect",)
    assert next_revision.card_results["collect"].file_refs[0]["path"] == "facts.md"


def test_task_board_evidence_view_uses_bounded_hot_preview_and_cold_refs():
    revision = _revision()
    cold_ref = {
        "path": "artifacts/collect.json",
        "sha256": "abc",
        "bytes": 1200,
        "preview": "ref preview must not enter hot path",
        "content": "full content must not enter hot path",
    }
    next_revision = TaskBoardValidator().apply_patch(
        revision,
        TaskBoardPatch(
            base_revision="rev-0",
            operations=(
                {
                    "op": "record_card_result",
                    "result": {
                        "card_id": "collect",
                        "status": "completed",
                        "preview": "x" * 1200,
                        "artifact_refs": [cold_ref],
                        "file_refs": [cold_ref],
                        "diagnostics": [{"kind": "probe", "content": "diagnostic body"}],
                    },
                },
            ),
        ),
    )

    view = build_task_board_evidence_view(next_revision, preview_chars=100).to_dict()

    collect = view["cards"][0]
    assert collect["card_id"] == "collect"
    assert collect["preview"]["content"] == "x" * 100
    assert collect["preview"]["truncated"] is True
    assert collect["preview"]["original_chars"] == 1200
    assert collect["has_cold_refs"] is True
    assert collect["artifact_refs"][0]["path"] == "artifacts/collect.json"
    assert "preview" not in collect["artifact_refs"][0]
    assert "content" not in collect["artifact_refs"][0]
    assert "content" not in collect["diagnostics"]["items"][0]
    assert view["truncated"] is True
    assert view["status_counts"]["completed"] == 1
    assert view["status_counts"]["pending"] == 1


def test_task_board_evidence_view_rejects_unknown_card_scope():
    with pytest.raises(ValueError, match="unknown card ids"):
        build_task_board_evidence_view(_revision(), card_ids=["missing"])


def test_task_board_effort_policy_does_not_define_hard_budgets_or_action_options():
    policy = resolve_task_board_planning_policy("high")
    payload = policy.to_prompt_payload()
    forbidden_keys = {
        "allowed_actions",
        "action_options",
        "max_cards",
        "max_model_requests",
        "max_steps",
        "required_actions",
        "step_count",
    }

    def walk_keys(value):
        if isinstance(value, dict):
            for key, item in value.items():
                yield key
                yield from walk_keys(item)
        elif isinstance(value, list):
            for item in value:
                yield from walk_keys(item)

    assert policy.effort_profile.name == "high"
    assert forbidden_keys.isdisjoint({str(key) for key in walk_keys(payload)})
    assert "not a target count" in policy.action_block_meaning
    assert "not an allowlist" in policy.action_block_meaning
    assert any("existing TaskBoard card results" in item for item in policy.evidence_reuse_guidance)
    assert any("Re-gather evidence only" in item for item in policy.evidence_reuse_guidance)
    assert any("localized defect" in item for item in policy.repair_orchestration_guidance)


def test_task_board_planning_result_builds_valid_revision():
    result = coerce_task_board_planning_result(
        {
            "board_goal": "Prepare a support refund decision.",
            "cards": [
                {
                    "id": "collect",
                    "action_block": "Collect ticket and invoice evidence.",
                    "objective": "Gather customer and billing facts.",
                    "depends_on": [],
                    "evidence_to_use": ["ticket_id", "invoice_id"],
                    "done_when": "Ticket and invoice evidence are available.",
                },
                {
                    "id": "decide",
                    "action_block": "Compare facts against refund policy.",
                    "objective": "Decide whether refund approval is justified.",
                    "depends_on": ["collect"],
                    "done_when": "Decision has evidence-backed reason.",
                    "allowed_execution_shape": "model",
                },
            ],
            "reflection_points": ["Check that billing status matches the ticket claim."],
            "completion_gate": "Final decision cites collected evidence.",
            "why_this_effort_shape": "Balanced evidence and decision separation.",
        },
        board_id="refund",
        effort="medium",
    )

    assert result.revision.board_id == "refund"
    assert result.revision.graph.graph_id == "refund.graph"
    assert [card.id for card in result.revision.graph.cards] == ["collect", "decide"]
    assert result.revision.graph.cards[0].input_refs == ("ticket_id", "invoice_id")
    assert result.revision.graph.cards[0].evidence_contract["action_block"] == "Collect ticket and invoice evidence."
    assert result.revision.graph.cards[1].depends_on == ("collect",)
    assert result.revision.graph.cards[1].allowed_execution_shape == "model"
    assert result.revision.metadata["completion_gate"] == "Final decision cites collected evidence."
    assert result.planning_policy.effort_profile.name == "medium"


def test_task_board_planning_result_rejects_effort_as_hard_control_keys():
    with pytest.raises(ValueError, match="forbidden effort-control key: max_cards"):
        coerce_task_board_planning_result(
            {
                "board_goal": "Invalid board.",
                "max_cards": 2,
                "cards": [{"id": "only", "objective": "Run.", "depends_on": []}],
                "completion_gate": "Done.",
                "why_this_effort_shape": "Invalid hard control.",
            },
            board_id="invalid",
        )


def test_task_board_planning_result_still_fails_closed_on_invalid_dependencies():
    with pytest.raises(ValueError, match="depends on missing card"):
        coerce_task_board_planning_result(
            {
                "board_goal": "Invalid dependency board.",
                "cards": [
                    {
                        "id": "final",
                        "action_block": "Finalize.",
                        "objective": "Write final.",
                        "depends_on": ["missing"],
                        "done_when": "Final exists.",
                    }
                ],
                "completion_gate": "Done.",
                "why_this_effort_shape": "Invalid dependency.",
            },
            board_id="missing-dependency",
        )


@pytest.mark.asyncio
async def test_task_board_tick_runs_through_triggerflow_and_advances_revision():
    contexts: list[TaskBoardContext] = []

    async def handler(context: TaskBoardContext):
        contexts.append(context)
        assert context.model == "model-key"
        assert context.workspace == "workspace-ref"
        assert context.effort == "high"
        assert context.planning_policy is not None
        assert context.planning_policy.effort_profile.name == "high"
        return {
            "status": "completed",
            "preview": f"done:{ context.card.id }",
            "artifact_refs": [{"card_id": context.card.id, "kind": "text"}],
        }

    board = TaskBoard(
        _revision(),
        handler=handler,
        model="model-key",
        workspace="workspace-ref",
        effort="high",
    )

    first_tick = await board.async_run_tick(timeout=1)
    assert first_tick.previous_revision.revision_id == "rev-0"
    assert first_tick.revision.revision_id == "rev-1"
    assert first_tick.schedule.runnable_card_ids == ("collect",)
    assert first_tick.revision.card_results["collect"].preview == "done:collect"
    assert first_tick.triggerflow_snapshot["revision"]["revision_id"] == "rev-1"

    second_tick = await board.async_run_tick(timeout=1)
    assert second_tick.revision.revision_id == "rev-2"
    assert second_tick.schedule.runnable_card_ids == ("final",)
    assert contexts[-1].dependency_results["collect"].preview == "done:collect"


@pytest.mark.asyncio
async def test_task_board_explicit_simple_task_still_uses_task_board_process():
    async def handler(context: TaskBoardContext):
        return f"simple:{ context.card.objective }"

    board = TaskBoard(
        TaskBoardRevision.create(
            board_id="simple",
            graph={"graph_id": "simple-graph", "cards": [{"id": "answer", "objective": "Answer directly."}]},
        ),
        handler=handler,
    )
    tick = await board.async_run_tick(timeout=1)

    assert tick.schedule.runnable_card_ids == ("answer",)
    assert tick.revision.revision_id == "rev-1"
    assert tick.revision.card_results["answer"].preview == "simple:Answer directly."


@pytest.mark.asyncio
async def test_task_board_handler_cannot_mutate_frozen_revision_directly():
    def handler(context: TaskBoardContext):
        with pytest.raises(Exception):
            setattr(context.revision, "revision_id", "mutated")
        return {"status": "completed", "preview": "ok"}

    board = TaskBoard(
        TaskBoardRevision.create(
            board_id="immutable",
            graph={"graph_id": "immutable-graph", "cards": [{"id": "card", "objective": "Run."}]},
        ),
        handler=handler,
    )
    tick = await board.async_run_tick(timeout=1)

    assert tick.previous_revision.revision_id == "rev-0"
    assert tick.revision.revision_id == "rev-1"
