from __future__ import annotations

import json
from dataclasses import FrozenInstanceError

import pytest
import agently.types.data as context_data

from agently.types.data import (
    ContextBlock,
    ContextBudget,
    ContextCandidate,
    ContextCompleteness,
    ContextConsumer,
    ContextConsumption,
    ContextDiagnostic,
    ContextOmission,
    ContextPackage,
    ContextReadIntent,
    ContextRole,
    ContextSourceBindingSnapshot,
    TaskContextEntrySnapshot,
    TaskContextSnapshot,
)


def _block(
    *,
    role: ContextRole = "information",
    completeness: ContextCompleteness = "complete",
) -> ContextBlock:
    return ContextBlock(
        block_id=f"block:{role}:{completeness}",
        block_key=f"key:{role}:{completeness}",
        source_id="source:docs",
        source_revision="rev:17",
        source_ref="docs/guide.md#overview",
        binding_id="binding:docs",
        role=role,
        content={"text": "bounded context", "labels": ["guide"]},
        completeness=completeness,
        content_chars=15,
        required=False,
        refs=("docs/guide.md",),
        metadata={"nested": {"trusted": True}},
    )


@pytest.mark.parametrize(
    "role",
    [
        "instruction",
        "information",
        "example",
        "state",
        "artifact",
        "capability",
        "index",
    ],
)
@pytest.mark.parametrize(
    "completeness",
    ["complete", "truncated", "ref_only", "empty", "failed", "lossy"],
)
def test_context_block_supports_every_role_and_completeness(
    role: ContextRole,
    completeness: ContextCompleteness,
) -> None:
    block = _block(role=role, completeness=completeness)

    assert block.role == role
    assert block.completeness == completeness


def test_context_contracts_deep_freeze_public_values() -> None:
    block = _block()
    package = ContextPackage(
        package_id="package:1",
        task_context_id="context:task-1",
        context_revision=3,
        consumer_id="planner",
        phase="planning",
        source_revisions={"source:docs": "rev:17"},
        blocks=(block,),
        omissions=(ContextOmission(block_key="key:cold", reason="not_selected"),),
        diagnostics=(
            ContextDiagnostic(
                code="context.optional_omitted",
                message="Optional content remained cold.",
                details={"keys": ["key:cold"]},
            ),
        ),
    )

    with pytest.raises(FrozenInstanceError):
        block.role = "state"  # type: ignore[misc]
    with pytest.raises(TypeError):
        block.content["text"] = "mutated"  # type: ignore[index]
    with pytest.raises(TypeError):
        block.metadata["nested"]["trusted"] = False  # type: ignore[index]
    with pytest.raises(TypeError):
        package.source_revisions["source:docs"] = "rev:18"  # type: ignore[index]
    assert isinstance(package.blocks, tuple)
    assert isinstance(package.omissions, tuple)
    assert isinstance(package.diagnostics, tuple)

    public = package.to_dict()
    public["source_revisions"]["source:docs"] = "detached"
    public["blocks"][0]["content"]["text"] = "detached"
    assert package.source_revisions["source:docs"] == "rev:17"
    assert package.blocks[0].content["text"] == "bounded context"


def test_context_identity_layers_remain_distinct() -> None:
    candidate = ContextCandidate(
        block_key="offered:k1",
        source_id="source:skill-library",
        source_revision="skill-revision:sha256",
        source_ref="skill:writing@sha256/SKILL.md",
        binding_id="binding:task-skill",
        role="instruction",
        summary="Writing procedure",
        estimated_chars=120,
        required=True,
    )
    block = ContextBlock(
        block_id="block:observed-read",
        block_key=candidate.block_key,
        source_id=candidate.source_id,
        source_revision=candidate.source_revision,
        source_ref=candidate.source_ref,
        binding_id=candidate.binding_id,
        role=candidate.role,
        content="Writing procedure",
        completeness="complete",
        content_chars=17,
        required=True,
    )
    consumption = ContextConsumption(
        consumption_id="consumption:1",
        package_id="package:1",
        request_id="request:1",
        consumer_id="worker",
        phase="execution",
        block_ids=(block.block_id,),
    )

    assert len(
        {
            candidate.source_id,
            candidate.source_revision,
            candidate.source_ref,
            candidate.binding_id,
            candidate.block_key,
            block.block_id,
            consumption.consumption_id,
        }
    ) == 7
    assert consumption.block_ids == (block.block_id,)


def test_package_disclosure_is_not_request_consumption() -> None:
    package = ContextPackage(
        package_id="package:disclosed",
        task_context_id="context:task-1",
        context_revision=1,
        consumer_id="worker",
        phase="execution",
        source_revisions={"source:docs": "rev:17"},
        blocks=(_block(),),
    )

    public = package.to_dict()
    assert "request_id" not in public
    assert "consumed" not in public
    assert "consumption_id" not in public


def test_context_package_exposes_coverage_without_cursor_capability() -> None:
    package = ContextPackage(
        package_id="package:coverage",
        task_context_id="context:task-1",
        context_revision=1,
        consumer_id="planner",
        phase="planning",
        source_revisions={"binding:repo": "commit:abc"},
        source_coverage={
            "binding:repo": {
                "scope": {"path": ".", "query": "execution owner"},
                "returned_candidates": 8,
                "exhaustive": False,
                "continuation_available": True,
            }
        },
    )

    public = package.to_dict()

    assert public["source_coverage"]["binding:repo"] == {
        "scope": {"path": ".", "query": "execution owner"},
        "returned_candidates": 8,
        "exhaustive": False,
        "continuation_available": True,
    }
    assert "cursor" not in json.dumps(public)
    with pytest.raises(TypeError):
        package.source_coverage["binding:repo"]["scope"]["path"] = "src"  # type: ignore[index]


def test_context_source_descriptor_values_replace_candidate_windows() -> None:
    descriptor_type = getattr(context_data, "ContextSourceDescriptor", None)
    page_type = getattr(context_data, "ContextSourceDescriptorPage", None)
    change_type = getattr(context_data, "ContextSourceChange", None)
    change_set_type = getattr(context_data, "ContextSourceChangeSet", None)
    read_type = getattr(context_data, "ContextSourceRead", None)

    assert descriptor_type is not None
    assert page_type is not None
    assert change_type is not None
    assert change_set_type is not None
    assert read_type is not None
    assert not hasattr(context_data, "ContextSourceCandidateWindow")

    descriptor = descriptor_type(
        descriptor_key="descriptor:1",
        source_id="source:repo",
        source_revision="commit:abc",
        source_ref="agently/core/Agent.py",
        role="information",
        title="Agent implementation",
        summary="Agent implementation",
        estimated_chars=100,
        index_text="Agent implementation owner",
        metadata={"path": "agently/core/Agent.py"},
    )
    page = page_type(
        source_id="source:repo",
        source_revision="commit:abc",
        descriptors=(descriptor,),
        next_cursor="page:2",
    )
    change = change_type(
        operation="upsert",
        descriptor_key=descriptor.descriptor_key,
        descriptor=descriptor,
    )
    changes = change_set_type(
        source_id="source:repo",
        from_revision="commit:old",
        to_revision="commit:abc",
        changes=(change,),
    )
    read = read_type(
        source_id="source:repo",
        source_revision="commit:abc",
        source_ref="agently/core/Agent.py",
        content="class Agent: ...",
        completeness="complete",
        content_digest="sha256:abc",
        refs=("agently/core/Agent.py",),
        metadata={"path": "agently/core/Agent.py"},
    )

    assert page.descriptors == (descriptor,)
    assert page.next_cursor == "page:2"
    assert changes.changes == (change,)
    assert read.content == "class Agent: ..."
    assert read.refs == ("agently/core/Agent.py",)
    with pytest.raises(TypeError):
        descriptor.metadata["path"] = "."  # type: ignore[index]


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"descriptors": (object(),)}, "descriptors"),
        ({"next_cursor": ""}, "next_cursor"),
        ({"next_cursor": "x" * 4097}, "next_cursor"),
    ],
)
def test_context_source_descriptor_page_rejects_invalid_values(
    overrides: dict[str, object],
    message: str,
) -> None:
    page_type = getattr(context_data, "ContextSourceDescriptorPage", None)
    assert page_type is not None
    values: dict[str, object] = {
        "source_id": "source:repo",
        "source_revision": "commit:abc",
        "descriptors": (),
    }
    values.update(overrides)

    with pytest.raises(ValueError, match=message):
        page_type(**values)


def test_budget_consumer_intent_and_snapshot_are_explicit_values() -> None:
    budget = ContextBudget(max_chars=4000, max_blocks=8, max_block_chars=1200)
    consumer = ContextConsumer(
        consumer_id="verifier",
        model="example-model",
        capabilities={"attachments": False},
    )
    intent = ContextReadIntent(
        query="Verify the artifact against the criteria.",
        explicit_refs=("artifact:report",),
        roles=("state", "artifact", "information"),
    )
    snapshot = TaskContextSnapshot(
        context_id="context:task-1",
        task_id="task-1",
        revision=2,
        bindings=(
            ContextSourceBindingSnapshot(
                binding_id="binding:docs",
                source_id="source:docs",
                source_kind="documentation",
                source_revision="rev:17",
                required=False,
                priority=2,
                scope="task",
            ),
        ),
        entries=(
            TaskContextEntrySnapshot(
                entry_id="entry:goal",
                role="state",
                content="Create a verified report.",
                required=True,
            ),
        ),
    )

    assert budget.max_chars == 4000
    assert consumer.capabilities["attachments"] is False
    assert intent.explicit_refs == ("artifact:report",)
    assert snapshot.bindings[0].source_revision == "rev:17"
    assert snapshot.entries[0].entry_id == "entry:goal"


@pytest.mark.parametrize(
    ("factory", "message"),
    [
        (lambda: ContextBudget(max_chars=0), "max_chars"),
        (lambda: ContextBudget(max_blocks=0), "max_blocks"),
        (lambda: ContextConsumer(consumer_id=""), "consumer_id"),
        (lambda: ContextReadIntent(query=""), "query"),
        (
            lambda: ContextPackage(
                package_id="",
                task_context_id="context:1",
                context_revision=0,
                consumer_id="worker",
                phase="execution",
                source_revisions={},
            ),
            "package_id",
        ),
    ],
)
def test_context_contracts_reject_invalid_required_values(factory, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        factory()
