from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import pytest

from agently.core.context import TaskContext
from agently.types.data import ContextBlock, ContextCandidate, ContextReadIntent


class RecordingSource:
    def __init__(self, source_id: str = "source:docs", revision: str = "rev:1") -> None:
        self.source_id = source_id
        self.source_revision = revision
        self.list_calls = 0
        self.read_calls = 0

    async def async_list_candidates(
        self,
        intent: ContextReadIntent,
        *,
        limit: int,
        filters: Mapping[str, Any] | None = None,
    ) -> Sequence[ContextCandidate]:
        self.list_calls += 1
        return []

    async def async_read(
        self,
        candidate: ContextCandidate,
        *,
        max_chars: int,
        representation: str | None = None,
    ) -> ContextBlock:
        self.read_calls += 1
        raise AssertionError("TaskContext must not read a source while binding or snapshotting it.")


def test_task_context_manages_revisioned_source_bindings_and_direct_entries() -> None:
    source = RecordingSource()
    context = TaskContext(task_id="task-1", context_id="context:task-1")

    binding_id = context.attach(
        source,
        binding_id="binding:docs",
        required=False,
        priority=3,
        scope="task",
        metadata={"trust": "authoritative"},
    )
    entry_id = context.put(
        role="state",
        content={"goal": "Produce a report."},
        entry_id="entry:goal",
        required=True,
        source_ref="request:goal",
        metadata={"owner": "user"},
    )
    snapshot = context.snapshot()

    assert binding_id == "binding:docs"
    assert entry_id == "entry:goal"
    assert snapshot.context_id == "context:task-1"
    assert snapshot.task_id == "task-1"
    assert snapshot.revision == 2
    assert snapshot.bindings[0].source_id == "source:docs"
    assert snapshot.bindings[0].source_revision == "rev:1"
    assert snapshot.bindings[0].priority == 3
    assert snapshot.bindings[0].metadata["trust"] == "authoritative"
    assert snapshot.entries[0].role == "state"
    assert snapshot.entries[0].required is True
    assert snapshot.entries[0].content["goal"] == "Produce a report."


def test_task_context_remove_updates_revision_only_when_something_is_removed() -> None:
    context = TaskContext(task_id="task-1", context_id="context:task-1")
    context.attach(RecordingSource(), binding_id="binding:docs")
    context.put(role="state", content="goal", entry_id="entry:goal")

    assert context.revision == 2
    assert context.remove("missing") is False
    assert context.revision == 2
    assert context.remove("entry:goal") is True
    assert context.revision == 3
    assert context.remove("binding:docs") is True
    assert context.revision == 4
    assert context.snapshot().bindings == ()
    assert context.snapshot().entries == ()


def test_task_context_rejects_duplicate_binding_entry_and_unknown_role() -> None:
    context = TaskContext(task_id="task-1", context_id="context:task-1")
    context.attach(RecordingSource("source:a"), binding_id="binding:duplicate")

    with pytest.raises(ValueError, match="binding_id"):
        context.attach(RecordingSource("source:b"), binding_id="binding:duplicate")

    context.put(role="state", content="first", entry_id="entry:duplicate")
    with pytest.raises(ValueError, match="entry_id"):
        context.put(role="state", content="second", entry_id="entry:duplicate")
    with pytest.raises(ValueError, match="Context role"):
        context.put(role="unknown", content="bad")  # type: ignore[arg-type]


def test_task_context_snapshots_are_detached_and_deeply_immutable() -> None:
    source = RecordingSource()
    context = TaskContext(task_id="task-1", context_id="context:task-1")
    context.attach(
        source,
        binding_id="binding:docs",
        metadata={"labels": ["source"]},
    )
    context.put(
        role="state",
        content={"items": ["one"]},
        entry_id="entry:state",
    )
    snapshot = context.snapshot()

    with pytest.raises(TypeError):
        snapshot.bindings[0].metadata["labels"][0] = "changed"  # type: ignore[index]
    with pytest.raises(TypeError):
        snapshot.entries[0].content["items"][0] = "changed"  # type: ignore[index]

    context.remove("entry:state")
    assert snapshot.revision == 2
    assert snapshot.entries[0].content["items"] == ("one",)


def test_task_context_detects_context_and_source_revision_staleness() -> None:
    source = RecordingSource(revision="rev:1")
    context = TaskContext(task_id="task-1", context_id="context:task-1")
    context.attach(source, binding_id="binding:docs")
    snapshot = context.snapshot()

    assert context.is_snapshot_current(snapshot) is True
    source.source_revision = "rev:2"
    assert context.is_snapshot_current(snapshot) is False

    fresh = context.snapshot()
    assert fresh.source_revisions["source:docs"] == "rev:2"
    context.put(role="state", content="new", entry_id="entry:new")
    assert context.is_snapshot_current(fresh) is False


def test_binding_and_snapshot_do_not_trigger_source_or_model_work() -> None:
    source = RecordingSource()
    context = TaskContext(task_id="task-1")

    context.attach(source)
    context.snapshot()

    assert source.list_calls == 0
    assert source.read_calls == 0
    assert not hasattr(context, "model")
    assert not hasattr(context, "request")


@pytest.mark.parametrize(
    ("factory", "message"),
    [
        (lambda: TaskContext(task_id=""), "task_id"),
        (lambda: TaskContext(task_id="task", context_id=""), "context_id"),
        (
            lambda: TaskContext(task_id="task").attach(
                RecordingSource(source_id=""),
            ),
            "source_id",
        ),
    ],
)
def test_task_context_rejects_invalid_identity(factory, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        factory()
