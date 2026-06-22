import pytest

from agently.core import (
    TaskBoard,
    TaskBoardContext,
    TaskBoardGraph,
    TaskBoardRevision,
    TaskBoardValidator,
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


@pytest.mark.asyncio
async def test_task_board_tick_runs_through_triggerflow_and_advances_revision():
    contexts: list[TaskBoardContext] = []

    async def handler(context: TaskBoardContext):
        contexts.append(context)
        assert context.model == "model-key"
        assert context.workspace == "workspace-ref"
        assert context.effort == "high"
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
