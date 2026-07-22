from __future__ import annotations

import pytest

from agently.core.application.AgentTask.TaskEvidenceContextSource import (
    TaskEvidenceContextSource,
)
from agently.core.application.AgentTask.TaskReferences import TaskReferenceCatalog


@pytest.mark.asyncio
async def test_task_evidence_context_source_exposes_only_body_bearing_canonical_evidence():
    catalog = TaskReferenceCatalog("task-evidence-source")
    quote = catalog.add_evidence(
        {
            "id": "quote-readback:1",
            "kind": "taskboard_action_artifact.readback",
            "status": "ok",
            "body_state": "bounded",
            "owner": "action_artifact",
            "selection_key": "sel_quote",
            "body": (
                'NVDA last_sale_price "$170.77"; AMD last_sale_price "$235.77"; '
                'AVGO last_sale_price "$385.91", net_change "+$7.75", percentage_change "+2.05%".'
            ),
        }
    )
    catalog.add_evidence(
        {
            "id": "pointer-only:1",
            "kind": "agent_task.action.result",
            "status": "ok",
            "body_state": "ref_only",
            "selection_key": "sel_pointer",
        }
    )
    source = TaskEvidenceContextSource(catalog)

    page = await source.async_enumerate_descriptors(
        profile={"projection_max_chars": 200},
        cursor=None,
        limit=10,
    )

    assert source.source_kind == "task_evidence"
    assert [item.source_ref for item in page.descriptors] == [quote["reference_id"]]
    assert "AVGO" in page.descriptors[0].index_text

    exact = await source.async_read_exact(
        quote["reference_id"],
        max_chars=32,
        range_start=0,
    )
    assert exact.completeness == "truncated"
    assert exact.next_range_start == 32

    scoped = await source.async_read_scoped(
        quote["reference_id"],
        query="AVGO last_sale_price",
        max_chars=80,
    )
    assert "AVGO last_sale_price" in scoped.content
    assert "$385.91" in scoped.content


def test_task_evidence_context_source_revision_changes_only_for_new_body_identity():
    catalog = TaskReferenceCatalog("task-evidence-revision")
    source = TaskEvidenceContextSource(catalog)
    empty_revision = source.source_revision
    catalog.add_evidence(
        {
            "id": "pointer-only",
            "kind": "agent_task.action.result",
            "status": "ok",
            "body_state": "ref_only",
        }
    )
    pointer_revision = source.source_revision
    catalog.add_evidence(
        {
            "id": "body-readback",
            "kind": "taskboard_action_artifact.readback",
            "status": "ok",
            "body_state": "bounded",
            "owner": "action_artifact",
            "body": "canonical body",
        }
    )

    assert pointer_revision == empty_revision
    assert source.source_revision != pointer_revision
