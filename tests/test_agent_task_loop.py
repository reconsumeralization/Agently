from __future__ import annotations

import asyncio
import json
import os
import time
from collections.abc import AsyncGenerator, Mapping
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from agently import Agently
from agently.core import PluginManager
from agently.core.orchestration import TaskBoard
from agently.core.application.AgentTask.BlockCarrier import (
    WorkUnitIntent,
    WorkUnitResult,
    scoped_retrieval_policy,
    select_carrier_output_policy,
)
from agently.core.application.AgentTask import AgentTask
from agently.core.application.AgentExecution.Stream import project_agent_execution_text_delta
from agently.types.data import (
    AgentlyRequestData,
    AgentExecutionStreamData,
    ExecutionBlockGraph,
    TaskBoardCard,
    TaskBoardCardResult,
    TaskBoardGraph,
    TaskBoardRevision,
)
from agently.utils import DataFormatter, Settings
from examples.agent_task.interview_question_preparation import judge_interview_semantics


def test_task_shared_star_export_includes_evidence_ledger_helpers():
    namespace: dict[str, Any] = {}
    exec("from agently.core.application.AgentTask.TaskShared import *", namespace)

    for helper_name in (
        "acceptance_locator_view_from_ledger",
        "collect_evidence_use",
        "evidence_envelope_from_value",
        "evidence_ledger_view",
        "source_refs_from_ledger",
        "validate_evidence_use",
        "workspace_artifacts_from_ledger",
    ):
        assert callable(namespace.get(helper_name))


def test_agent_task_process_summary_is_compact_and_not_evidence():
    payload = {
        "decision_basis": ["Use the available bounded evidence."],
        "short_summary": "x" * 800,
        "progress_message": "Drafted a bounded artifact section for review.",
        "criterion_checks": [
            {
                "criterion": "Report includes sources.",
                "status": "partial",
                "summary": "One source remains unverified.",
                "api_key": "secret-value",
                "content": "large body should not be carried as process summary",
            }
        ],
        "evidence_use": [{"claim": "not a process field", "evidence_ids": ["e1"]}],
    }

    summary = AgentTask._process_summary_from_value(payload, stage="execution")
    next_step = AgentTask._combined_process_summary(
        plan=payload,
        execution_result=payload,
        verification=payload,
    )

    assert summary["stage"] == "execution"
    assert summary["criterion_checks"][0]["api_key"] == "[redacted]"
    assert "content" not in summary["criterion_checks"][0]
    assert len(summary["short_summary"]) <= 380
    assert "evidence_use" not in summary
    assert "decision_basis" not in next_step.get("plan", {})
    assert "progress_message" not in next_step["execution"]
    assert next_step["execution"]["short_summary"]


def test_agent_task_prompts_do_not_expose_workspace_streaming_mechanics():
    source_files = [
        "agently/core/application/AgentTask/TaskBoardCardExecution.py",
        "agently/core/application/AgentTask/ArtifactDelivery.py",
        "agently/core/application/AgentTask/FlatStrategy.py",
        "agently/core/application/AgentTask/TaskBoardFinalization.py",
    ]
    forbidden_phrases = [
        "AgentTask will stream",
        "will stream the long body",
        "will write/read back",
        "The framework will stream",
        "produce trusted file_refs",
        "Verify every success criterion",
    ]
    repo_root = Path(__file__).resolve().parents[1]
    offenders: list[str] = []
    for relative_path in source_files:
        text = (repo_root / relative_path).read_text(encoding="utf-8")
        for phrase in forbidden_phrases:
            if phrase in text:
                offenders.append(f"{relative_path}: {phrase}")
    assert offenders == []


def test_taskboard_prompts_keep_model_contract_surface_simple():
    source_files = [
        "agently/core/application/AgentTask/TaskBoardCardExecution.py",
        "agently/core/application/AgentTask/TaskBoardStrategy.py",
        "agently/core/orchestration/TaskBoard/TaskBoardPlanning.py",
        "agently/core/application/AgentTask/ArtifactDelivery.py",
    ]
    forbidden_phrases = [
        "AgentExecution step",
        "bounded AgentExecution",
        "framework-prefetched",
        "TaskBoardEvidenceView",
        "TaskBoard and AgentTask",
        "framework canonicalizes",
        "framework remaps",
        "for the AgentTask",
    ]
    repo_root = Path(__file__).resolve().parents[1]
    offenders: list[str] = []
    for relative_path in source_files:
        text = (repo_root / relative_path).read_text(encoding="utf-8")
        for phrase in forbidden_phrases:
            if phrase in text:
                offenders.append(f"{relative_path}: {phrase}")
    assert offenders == []


def test_verifier_prompt_keeps_optional_risk_sections_optional():
    source_path = Path(__file__).resolve().parents[1] / "agently/core/application/AgentTask/Verification.py"
    text = source_path.read_text(encoding="utf-8")

    assert "Do not require risk, uncertainty, limitation, or caveat sections" in text
    assert "unless the user task, output contract," in text
    assert "verifier-visible evidence limitations explicitly require them" in text
    assert "Output-contract section labels are content" in text
    assert "not exact heading-text mandates" in text
    assert "do not reject a " in text
    assert "long artifact solely because an exact locator label missed" in text


def test_agent_task_process_progress_delta_uses_only_explicit_progress_event():
    assert AgentTask._is_process_summary_stream_path("self_check")
    assert AgentTask._is_process_summary_stream_path("artifact.progress_message")

    suppressed_child_delta = AgentExecutionStreamData(
        path="agent_task.iteration.1.execution.self_check",
        value="internal self check",
        delta=None,
        event_type="delta",
        is_complete=False,
        source="agent_task",
        meta={"stream_kind": "child_execution"},
    )
    progress_item = AgentExecutionStreamData(
        path="agent_task.process.progress",
        value={"message": "Reading the bounded source evidence."},
        event_type="done",
        is_complete=True,
        source="agent_task",
        meta={"stream_kind": "progress", "progress_source": "process_summary"},
    )

    assert project_agent_execution_text_delta(suppressed_child_delta) is None
    assert project_agent_execution_text_delta(progress_item) == "Reading the bounded source evidence.\n\n"


def test_agent_task_action_observation_delta_projects_safe_progress_text():
    started = AgentExecutionStreamData(
        path="agent_task.action.started",
        value={
            "action_id": "grep_workspace",
            "status": "started",
            "kind": "shell_search",
            "input_summary": {"query": "deadline", "scope": "workspace"},
        },
        event_type="done",
        is_complete=True,
        source="agent_task",
        meta={"stream_kind": "action_observation", "phase": "started"},
    )
    completed = AgentExecutionStreamData(
        path="agent_task.action.completed",
        value={
            "action_id": "grep_workspace",
            "status": "success",
            "kind": "shell_search",
            "success": True,
            "output_summary": {"path": "notes.md", "content": "deadline is 2026-07-01"},
            "source_refs": [{"value": "notes.md"}],
        },
        event_type="done",
        is_complete=True,
        source="agent_task",
        meta={"stream_kind": "action_observation", "phase": "completed"},
    )
    failed = AgentExecutionStreamData(
        path="agent_task.action.failed",
        value={"action_id": "read_file", "status": "failed", "error": "file not found"},
        event_type="done",
        is_complete=True,
        source="agent_task",
        meta={"stream_kind": "action_observation", "phase": "failed"},
    )

    assert project_agent_execution_text_delta(started) == (
        'Action started: grep_workspace (shell_search). Input: {"query": "deadline", "scope": "workspace"}\n\n'
    )
    completed_text = project_agent_execution_text_delta(completed)
    assert completed_text is not None
    assert completed_text.endswith("\n\n")
    assert "Action completed: grep_workspace (shell_search)." in completed_text
    assert "Result:" in completed_text
    assert "deadline is 2026-07-01" in completed_text
    assert "Refs: notes.md" in completed_text
    assert project_agent_execution_text_delta(failed) == "Action failed: read_file. Error: file not found\n\n"


def test_evidence_ledger_guard_rejects_structurally_invalid_support():
    from agently.core.application.AgentTask.EvidenceLedger import validate_evidence_use

    ledger = {
        "evidence_items": [
            {"id": "quote.failed", "kind": "action_evidence", "status": "failed", "body_state": "bounded"},
            {"id": "repo.path", "kind": "locator_ref", "status": "ok", "body_state": "ref_only", "path": "src/app.py"},
        ]
    }

    guard = validate_evidence_use(
        [
            {"claim": "The quote was 123.45.", "evidence_ids": ["quote.failed"], "support_type": "content"},
            {"claim": "src/app.py defines Foo.", "evidence_ids": ["repo.path"], "support_type": "content"},
            {"claim": "Unknown.", "evidence_ids": ["missing"], "support_type": "content"},
        ],
        ledger,
    )

    assert guard["valid"] is False
    assert guard["blocking_count"] == 3
    assert {
        diagnostic["code"]
        for diagnostic in guard["diagnostics"]
        if diagnostic.get("blocking") is True
    } == {
        "evidence_ledger.unavailable_item_used_as_positive_support",
        "evidence_ledger.ref_only_item_used_as_content_support",
        "evidence_ledger.invalid_evidence_id",
    }


def test_evidence_ledger_acceptance_locator_is_ref_pointer_only():
    from agently.core.application.AgentTask.EvidenceLedger import (
        acceptance_locator_view_from_ledger,
        evidence_ledger_view,
        validate_evidence_use,
    )

    ledger = {
        "evidence_items": [
            {
                "id": "locator.final.middle",
                "kind": "workspace_artifact.acceptance_locator",
                "status": "ok",
                "body_state": "ref_only",
                "path": "reports/final.md",
                "criterion_id": "middle",
                "claim": "The middle section is present.",
                "heading": "Middle Section",
                "line_start": 42,
                "line_end": 48,
                "byte_offset": 16000,
                "byte_end": 17500,
                "source_evidence_ids": ["workspace_artifact_readback:test:reports/final.md"],
            }
        ]
    }

    view = evidence_ledger_view(ledger)
    locator_view = acceptance_locator_view_from_ledger(view)
    assert locator_view["items"][0]["line_start"] == 42
    assert locator_view["items"][0]["heading"] == "Middle Section"

    invalid_guard = validate_evidence_use(
        [{"claim": "The middle section is present.", "evidence_ids": ["locator.final.middle"], "support_type": "content"}],
        ledger,
    )
    assert invalid_guard["valid"] is False
    assert any(
        item["code"] == "evidence_ledger.ref_only_item_used_as_content_support"
        for item in invalid_guard["diagnostics"]
    )

    pointer_guard = validate_evidence_use(
        [
            {
                "claim": "reports/final.md has a middle-section locator.",
                "evidence_ids": ["locator.final.middle"],
                "support_type": "ref_pointer",
            }
        ],
        ledger,
    )
    assert pointer_guard["valid"] is True


def test_acceptance_locator_matches_unicode_dash_heading_variants():
    from agently.core.application.AgentTask.AcceptanceLocator import (
        build_workspace_artifact_acceptance_locator_items,
    )

    items = build_workspace_artifact_acceptance_locator_items(
        path="final.md",
        source="test",
        text=(
            "# Final\n\n"
            "## Source\u2011backed Evidence Table\n\n"
            "Evidence rows.\n\n"
            "## Implementation & Product Highlights\n\n"
            "Highlights.\n"
        ),
        manifest={
            "sections": [
                {
                    "id": "implementation_highlights",
                    "title": "Implementation / Product Highlights",
                }
            ]
        },
        acceptance_points=[
            {
                "criterion": "The final artifact includes the source-backed evidence table.",
                "expected_anchor": "Source-backed Evidence Table",
            }
        ],
    )

    locator = next(item for item in items if item.get("criterion_id") == "acceptance_point:0")
    assert locator["status"] == "ok"
    assert locator["heading"] == "Source\u2011backed Evidence Table"
    assert locator["line_start"] == 3
    assert locator["requirement_level"] == "advisory"

    required_locator = next(item for item in items if item.get("criterion_id") == "implementation_highlights")
    assert required_locator["status"] == "ok"
    assert required_locator["requirement_level"] == "required"
    assert required_locator["heading"] == "Implementation & Product Highlights"


def test_acceptance_locator_matches_cjk_numeric_spacing_variants():
    from agently.core.application.AgentTask.AcceptanceLocator import (
        build_workspace_artifact_acceptance_locator_items,
    )

    items = build_workspace_artifact_acceptance_locator_items(
        path="final.md",
        source="test",
        text=(
            "# LMCC Mock Exam\n\n"
            "### 第一大题：单选题（每题 3 分，共 60 分）\n\n"
            "**第 1-10 题：基础概念**\n\n"
            "题目内容。\n"
        ),
        acceptance_points=[
            {
                "criterion_id": "single_choice_heading",
                "criterion": "The single-choice section heading is present.",
                "expected_anchor": "### 第一大题：单选题（每题3分，共60分）",
            },
            {
                "criterion_id": "question_range",
                "criterion": "The first question range anchor is present.",
                "expected_anchor": "第1-10题：基础概念",
            },
        ],
    )

    statuses = {item.get("criterion_id"): item.get("status") for item in items}
    assert statuses == {
        "single_choice_heading": "ok",
        "question_range": "ok",
    }


def test_acceptance_locator_uses_manifest_outline_ordinal_for_heading_label_variants():
    from agently.core.application.AgentTask.AcceptanceLocator import (
        build_workspace_artifact_acceptance_locator_items,
    )

    items = build_workspace_artifact_acceptance_locator_items(
        path="final.md",
        source="test",
        text=(
            "# Final Report\n\n"
            "## 1. Scope\n\n"
            "Scope content.\n\n"
            "## 2. Details\n\n"
            "### Nested Detail A\n\n"
            "Nested content.\n\n"
            "### Nested Detail B\n\n"
            "Details content.\n\n"
            "## 3. Closing Boundary\n\n"
            "Closing content.\n"
        ),
        manifest={
            "section_outline": [
                "scope",
                "details",
                "closing statement",
            ]
        },
    )

    locator = next(item for item in items if item.get("criterion_id") == "section_outline:2")
    assert locator["status"] == "ok"
    assert locator["requirement_level"] == "required"
    assert locator["heading"] == "3. Closing Boundary"
    assert locator["line_start"] == 17
    assert locator["byte_offset"] < locator["byte_end"]


def test_evidence_ledger_guard_reconciles_visible_aliases_to_canonical_ids():
    from agently.core.application.AgentTask.EvidenceLedger import validate_evidence_use

    ledger = {
        "evidence_items": [
            {
                "id": "ledger.workspace.readme",
                "kind": "workspace_artifact.readback",
                "status": "ok",
                "body_state": "full",
                "path": "README.md",
                "record_id": "record-readme",
                "source_url": "https://example.test/readme",
                "body": "README content",
                "provenance": {"action_id": "repo_read", "action_call_id": "call-1"},
            },
            {
                "id": "ledger.workspace.init",
                "kind": "workspace_artifact.readback",
                "status": "ok",
                "body_state": "bounded",
                "path": "skillopt/__init__.py",
                "body": "__version__ = '1.0'",
            },
        ]
    }

    guard = validate_evidence_use(
        [
            {"claim": "The README content was read.", "evidence_ids": ["README.md"], "support_type": "content"},
            {
                "claim": "The readme record is the selected source.",
                "evidence_ids": ["record-readme"],
                "support_type": "content",
            },
            {
                "claim": "The readme URL is available.",
                "evidence_ids": ["https://example.test/readme"],
                "support_type": "content",
            },
            {
                "claim": "The repository read action produced this result.",
                "evidence_ids": ["action_result_repo_read"],
                "support_type": "content",
            },
            {
                "claim": "The package initializer was read.",
                "evidence_ids": ["skillopt/__init__.py"],
                "support_type": "content",
            },
        ],
        ledger,
    )

    assert guard["valid"] is True
    assert guard["blocking_count"] == 0
    assert [entry["evidence_ids"] for entry in guard["normalized_evidence_use"]] == [
        ["ledger.workspace.readme"],
        ["ledger.workspace.readme"],
        ["ledger.workspace.readme"],
        ["ledger.workspace.readme"],
        ["ledger.workspace.init"],
    ]
    assert any(item["code"] == "evidence_ledger.alias_resolved" for item in guard["diagnostics"])
    assert "available_evidence_refs" in guard


def test_evidence_ledger_guard_uses_item_declared_aliases():
    from agently.core.application.AgentTask.EvidenceLedger import validate_evidence_use

    ledger = {
        "evidence_items": [
            {
                "id": "ledger.source.guide",
                "kind": "action_evidence",
                "status": "ok",
                "body_state": "bounded",
                "aliases": ["load_source:docs/guide.md"],
                "body": "Guide content from the action result.",
            }
        ]
    }

    guard = validate_evidence_use(
        [
            {
                "claim": "The guide content was read.",
                "evidence_ids": ["load_source:docs/guide.md"],
                "support_type": "content",
            }
        ],
        ledger,
    )

    assert guard["valid"] is True
    assert guard["normalized_evidence_use"][0]["evidence_ids"] == ["ledger.source.guide"]
    assert any(item["code"] == "evidence_ledger.alias_resolved" for item in guard["diagnostics"])


def test_blocks_action_evidence_declares_generic_action_ref_aliases():
    from agently.builtins.plugins.Blocks.AgentlyBlocks import EvidenceMapperRegistry
    from agently.core.application.AgentTask.EvidenceLedger import validate_evidence_use

    graph = ExecutionBlockGraph.from_value({"graph_id": "graph-generic-alias", "source_plan_id": "plan-generic-alias"})
    envelope = EvidenceMapperRegistry().map_evidence(
        graph,
        {
            "blocks": {
                "execution_block_results": [
                    {
                        "kind": "agent_step",
                        "execution_block_id": "exec-load-source",
                        "status": "completed",
                        "output": {
                            "execution_meta": {
                                "execution_id": "run-generic-alias",
                                "logs": {
                                    "action_logs": [
                                        {
                                            "id": "load_source",
                                            "status": "success",
                                            "action_call_id": "call-load-source",
                                            "input_preview": {"path": "docs/guide.md"},
                                            "result_preview": {
                                                "path": "docs/guide.md",
                                                "content": "Guide content from the action result.",
                                            },
                                        }
                                    ]
                                },
                            }
                        },
                    }
                ]
            }
        },
    )

    action_items = [item for item in envelope.evidence_items if item.get("kind") == "action_evidence"]
    assert len(action_items) == 1
    action_item = action_items[0]
    assert "load_source:docs/guide.md" in action_item.get("aliases", [])

    guard = validate_evidence_use(
        [
            {
                "claim": "The guide content was read.",
                "evidence_ids": ["load_source:docs/guide.md"],
                "support_type": "content",
            }
        ],
        envelope,
    )

    assert guard["valid"] is True
    assert guard["normalized_evidence_use"][0]["evidence_ids"] == [action_item["id"]]


def test_evidence_ledger_guard_blocks_ambiguous_basename_aliases():
    from agently.core.application.AgentTask.EvidenceLedger import validate_evidence_use

    ledger = {
        "evidence_items": [
            {"id": "docs.readme", "kind": "workspace_artifact.readback", "status": "ok", "body_state": "full", "path": "docs/README.md"},
            {"id": "pkg.readme", "kind": "workspace_artifact.readback", "status": "ok", "body_state": "full", "path": "packages/README.md"},
        ]
    }

    guard = validate_evidence_use(
        [{"claim": "The README explains the package.", "evidence_ids": ["README.md"], "support_type": "content"}],
        ledger,
    )

    assert guard["valid"] is False
    assert guard["blocking_count"] == 1
    diagnostic = next(item for item in guard["diagnostics"] if item.get("blocking") is True)
    assert diagnostic["code"] == "evidence_ledger.ambiguous_evidence_alias"
    assert set(diagnostic["candidates"]) == {"docs.readme", "pkg.readme"}


def test_evidence_ledger_alias_reconciliation_preserves_status_guards():
    from agently.core.application.AgentTask.EvidenceLedger import validate_evidence_use

    ledger = {
        "evidence_items": [
            {
                "id": "quote.failed",
                "kind": "action_evidence",
                "status": "failed",
                "body_state": "ref_only",
                "action_id": "quote_lookup",
            }
        ]
    }

    guard = validate_evidence_use(
        [{"claim": "The quote was 123.45.", "evidence_ids": ["action_result_quote_lookup"], "support_type": "content"}],
        ledger,
    )

    assert guard["valid"] is False
    assert guard["normalized_evidence_use"][0]["evidence_ids"] == ["quote.failed"]
    assert any(item["code"] == "evidence_ledger.alias_resolved" for item in guard["diagnostics"])
    assert any(
        item["code"] == "evidence_ledger.unavailable_item_used_as_positive_support"
        and item.get("blocking") is True
        for item in guard["diagnostics"]
    )


def test_evidence_binding_repair_resolves_missing_id_from_unique_claim_body():
    from agently.core.application import AgentTask
    from agently.core.application.AgentTask.EvidenceLedger import validate_evidence_use

    ledger = {
        "evidence_items": [
            {
                "id": "search.amd",
                "kind": "agent_task.action.result",
                "status": "ok",
                "body_state": "bounded",
                "action_id": "web_search",
                "body": "AMD shares up 261% over past year and 132% YTD; hit 52-week high $564.76.",
            },
            {
                "id": "search.avgo",
                "kind": "agent_task.action.result",
                "status": "ok",
                "body_state": "bounded",
                "action_id": "web_search",
                "body": "AVGO approved a quarterly dividend.",
            },
        ]
    }
    guard = validate_evidence_use(
        [
            {
                "claim": "AMD shares up 261% over past year and 132% YTD; hit 52-week high $564.76",
                "evidence_ids": [],
                "support_type": "content",
            }
        ],
        ledger,
    )

    repaired = AgentTask._deterministic_evidence_binding_repair(guard, ledger)

    assert repaired == [
        {
            "claim_index": 0,
            "claim": "AMD shares up 261% over past year and 132% YTD; hit 52-week high $564.76",
            "evidence_ids": ["search.amd"],
            "support_type": "content",
        }
    ]
    merged = AgentTask._merge_repaired_evidence_use(guard["normalized_evidence_use"], repaired)
    assert validate_evidence_use(merged, ledger)["valid"] is True


def test_evidence_binding_repair_resolves_missing_unavailability_id_from_failed_action_body():
    from agently.core.application import AgentTask
    from agently.core.application.AgentTask.EvidenceLedger import validate_evidence_use

    ledger = {
        "evidence_items": [
            {
                "id": "browse.reuters.failed",
                "kind": "agent_task.action.result",
                "status": "failed",
                "body_state": "bounded",
                "action_id": "browse",
                "body": (
                    "Can not browse 'https://www.reuters.com/business/broadcom-tumbles-revenue-miss/'. "
                    "Fallback failed: Page.goto net::ERR_CONNECTION_CLOSED. Error: curl exited 35."
                ),
            }
        ]
    }
    guard = validate_evidence_use(
        [
            {
                "claim": "Reuters article browse returned error",
                "evidence_ids": [],
                "support_type": "unavailability",
            }
        ],
        ledger,
    )

    repaired = AgentTask._deterministic_evidence_binding_repair(guard, ledger)

    assert repaired == [
        {
            "claim_index": 0,
            "claim": "Reuters article browse returned error",
            "evidence_ids": ["browse.reuters.failed"],
            "support_type": "unavailability",
        }
    ]
    merged = AgentTask._merge_repaired_evidence_use(guard["normalized_evidence_use"], repaired)
    assert validate_evidence_use(merged, ledger)["valid"] is True


def test_evidence_binding_repair_leaves_missing_id_unresolved_when_claim_body_is_ambiguous():
    from agently.core.application import AgentTask
    from agently.core.application.AgentTask.EvidenceLedger import validate_evidence_use

    ledger = {
        "evidence_items": [
            {
                "id": "search.one",
                "kind": "agent_task.action.result",
                "status": "ok",
                "body_state": "bounded",
                "body": "The source says the project risk is material.",
            },
            {
                "id": "search.two",
                "kind": "agent_task.action.result",
                "status": "ok",
                "body_state": "bounded",
                "body": "Another source says the project risk is material.",
            },
        ]
    }
    guard = validate_evidence_use(
        [
            {
                "claim": "The project risk is material.",
                "evidence_ids": [],
                "support_type": "content",
            }
        ],
        ledger,
    )

    assert AgentTask._deterministic_evidence_binding_repair(guard, ledger) == []


def test_evidence_ledger_view_reassigns_unique_cite_as_on_remerge():
    # The cumulative ledger re-renders sub-ledger items that each carried their own
    # e1..eN handle. The view must own cite_as and reassign unique handles so a single
    # view never exposes duplicate cite_as (which would read as ambiguous aliases).
    from agently.core.application.AgentTask.EvidenceLedger import evidence_ledger_view

    merged = {
        "evidence_items": [
            {
                "id": "iter1.read",
                "kind": "workspace_artifact.readback",
                "status": "ok",
                "body_state": "bounded",
                "path": "report.md",
                "cite_as": "e1",
                "body": "report body",
            },
            {
                "id": "iter2.read",
                "kind": "workspace_artifact.readback",
                "status": "ok",
                "body_state": "bounded",
                "path": "data.csv",
                "cite_as": "e1",
                "body": "data body",
            },
            {
                "id": "iter3.read",
                "kind": "action_evidence",
                "status": "ok",
                "body_state": "bounded",
                "cite_as": "e1",
                "body": "action body",
            },
        ]
    }

    view = evidence_ledger_view(merged)
    cite_as_values = [item["cite_as"] for item in view["items"]]
    assert cite_as_values == ["e1", "e2", "e3"]
    assert len(set(cite_as_values)) == 3


def test_cite_as_handle_resolves_unambiguously_after_remerge():
    from agently.core.application.AgentTask.EvidenceLedger import evidence_ledger_view, validate_evidence_use

    merged = {
        "evidence_items": [
            {"id": "iter1.read", "kind": "workspace_artifact.readback", "status": "ok", "body_state": "bounded", "path": "report.md", "cite_as": "e1", "body": "a"},
            {"id": "iter2.read", "kind": "workspace_artifact.readback", "status": "ok", "body_state": "bounded", "path": "data.csv", "cite_as": "e1", "body": "b"},
            {"id": "iter3.read", "kind": "action_evidence", "status": "ok", "body_state": "bounded", "cite_as": "e1", "body": "c"},
        ]
    }
    view = evidence_ledger_view(merged)

    guard = validate_evidence_use(
        [{"claim": "The data file was read.", "evidence_ids": ["e2"], "support_type": "content"}],
        view,
    )

    assert guard["valid"] is True
    assert guard["normalized_evidence_use"][0]["evidence_ids"] == ["iter2.read"]


def test_evidence_guard_binds_composite_file_locator_to_matching_readback():
    # A composite/locator reference ("<file> <sub-locator>") that exact-alias cannot
    # match must bind to the readback whose path anchor it names -- and never another.
    from agently.core.application.AgentTask.EvidenceLedger import validate_evidence_use

    ledger = {
        "evidence_items": [
            {
                "id": "rb.report",
                "kind": "workspace_artifact.readback",
                "status": "ok",
                "body_state": "bounded",
                "path": "report.md",
                "body": "| project-a | 42 |",
            },
            {
                "id": "rb.data",
                "kind": "workspace_artifact.readback",
                "status": "ok",
                "body_state": "bounded",
                "path": "data.csv",
                "body": "raw rows",
            },
        ]
    }

    guard = validate_evidence_use(
        [
            {
                "claim": "project-a throughput is 42",
                "evidence_ids": ["report.md table row for project-a"],
                "support_type": "content",
            }
        ],
        ledger,
    )

    assert guard["valid"] is True
    assert guard["normalized_evidence_use"][0]["evidence_ids"] == ["rb.report"]
    assert any(item["code"] == "evidence_ledger.alias_resolved" for item in guard["diagnostics"])


def test_evidence_guard_narrows_composite_section_locator_by_heading():
    from agently.core.application.AgentTask.EvidenceLedger import validate_evidence_use

    ledger = {
        "evidence_items": [
            {
                "id": "rb.report.summary",
                "kind": "workspace_artifact.targeted_readback",
                "status": "ok",
                "body_state": "bounded",
                "path": "report.md",
                "heading": "Summary",
                "body": "Summary content",
            },
            {
                "id": "rb.report.risks",
                "kind": "workspace_artifact.targeted_readback",
                "status": "ok",
                "body_state": "bounded",
                "path": "report.md",
                "heading": "Risks",
                "body": "Risk content",
            },
        ]
    }

    guard = validate_evidence_use(
        [
            {
                "claim": "The risks are enumerated.",
                "evidence_ids": ["report.md Risks section"],
                "support_type": "content",
            }
        ],
        ledger,
    )

    assert guard["valid"] is True
    assert guard["normalized_evidence_use"][0]["evidence_ids"] == ["rb.report.risks"]


def test_resolve_evidence_reference_reports_tiers():
    from agently.core.application.AgentTask.EvidenceLedger import resolve_evidence_reference

    ledger = {
        "evidence_items": [
            {"id": "rb.report", "kind": "workspace_artifact.readback", "status": "ok", "body_state": "bounded", "path": "report.md", "body": "x"},
            {"id": "rb.data", "kind": "workspace_artifact.readback", "status": "ok", "body_state": "bounded", "path": "data.csv", "body": "y"},
        ]
    }

    assert resolve_evidence_reference("rb.report", ledger)["status"] == "resolved"
    anchor = resolve_evidence_reference("report.md table row for project-a", ledger)
    assert anchor["status"] == "resolved"
    assert anchor["id"] == "rb.report"
    assert resolve_evidence_reference("some opaque handle that names nothing", ledger)["status"] == "unresolved"


def test_resolve_evidence_reference_reports_ambiguous_basename():
    from agently.core.application.AgentTask.EvidenceLedger import resolve_evidence_reference

    ledger = {
        "evidence_items": [
            {"id": "docs.readme", "kind": "workspace_artifact.readback", "status": "ok", "body_state": "full", "path": "docs/README.md"},
            {"id": "pkg.readme", "kind": "workspace_artifact.readback", "status": "ok", "body_state": "full", "path": "pkg/README.md"},
        ]
    }

    resolution = resolve_evidence_reference("README.md", ledger)
    assert resolution["status"] == "ambiguous"
    assert set(resolution["candidates"]) == {"docs.readme", "pkg.readme"}


def test_execution_meta_action_results_enter_canonical_evidence_ledger():
    execution_meta = {
        "status": "completed",
        "logs": {
            "action_logs": [
                {
                    "action_id": "market_quotes",
                    "status": "partial_success",
                    "action_call_id": "call-quotes",
                    "raw": {
                        "kwargs": {"symbols": ["NVDA", "AMD", "AVGO"]},
                        "data": {
                            "quotes": [
                                {"symbol": "NVDA", "last": "194.97", "as_of": "2026-06-29"},
                                {"symbol": "AMD", "last": "539.49", "as_of": "2026-06-29"},
                            ],
                            "history_status": "unavailable",
                        },
                    },
                }
            ],
            "route_logs": {},
        },
    }

    ledger = AgentTask._evidence_ledger_from_execution_meta(execution_meta)
    action_items = [
        item
        for item in ledger["items"]
        if item.get("kind") == "agent_task.action.result"
        and item.get("action_id") == "market_quotes"
    ]

    assert len(action_items) == 1
    item = action_items[0]
    assert item["status"] == "ok"
    assert item["body_state"] == "bounded"
    assert item["action_call_id"] == "call-quotes"
    assert "action_result_market_quotes" in item["aliases"]
    assert "NVDA" in json.dumps(item.get("body") or item.get("preview"), ensure_ascii=False)

    from agently.core.application.AgentTask.EvidenceLedger import validate_evidence_use

    guard = validate_evidence_use(
        [
            {
                "claim": "NVDA quote data was retrieved.",
                "evidence_ids": ["action_result_market_quotes"],
                "support_type": "content",
            }
        ],
        ledger,
    )

    assert guard["valid"] is True
    assert guard["normalized_evidence_use"][0]["evidence_ids"] == [item["id"]]


def test_block_carrier_output_policy_selects_schema_and_body_transport():
    flat_text = WorkUnitIntent(
        id="flat-text",
        origin="flat_step",
        objective="Return separately addressable prose fields.",
        delivery_contract={
            "execution_prompt": {
                "output": {"summary": (str,), "notes": (str,)},
                "output_format": "auto",
            }
        },
    )
    mixed = WorkUnitIntent(
        id="mixed",
        origin="flat_step",
        objective="Return prose plus typed status.",
        delivery_contract={
            "execution_prompt": {
                "output": {"summary": (str,), "accepted": (bool,)},
                "output_format": "auto",
            }
        },
    )
    workspace_artifact = WorkUnitIntent(
        id="workspace-artifact",
        origin="taskboard_card",
        objective="Create a trusted file-backed deliverable.",
        delivery_contract={"deliverable_mode": "sectioned_workspace_artifact"},
    )
    plain_text = WorkUnitIntent(
        id="plain-text",
        origin="flat_step",
        objective="Write one natural-language body.",
        runtime_preferences={"deliverable_mode": "freeform_text"},
    )

    assert select_carrier_output_policy(flat_text).control_format == "xml_field"
    assert select_carrier_output_policy(mixed).control_format == "hybrid"

    artifact_policy = select_carrier_output_policy(workspace_artifact)
    assert artifact_policy.control_format == "json"
    assert artifact_policy.body_transport == "workspace_artifact"
    assert artifact_policy.body_uses_output is False
    assert artifact_policy.requires_workspace_readback is True

    plain_text_policy = select_carrier_output_policy(plain_text)
    assert plain_text_policy.control_format is None
    assert plain_text_policy.body_transport == "plain_text"
    assert plain_text_policy.body_uses_output is False
    assert plain_text_policy.requires_structured_judge is True


def test_block_carrier_exposes_compact_scoped_retrieval_policy():
    intent = WorkUnitIntent(
        id="retrieval-policy",
        origin="flat_step",
        objective="Find scoped evidence before reading large files.",
    )

    policy = scoped_retrieval_policy()
    serialized = intent.to_dict()

    assert serialized["retrieval_policy"] == policy
    assert policy["schema_version"] == "agent_task_scoped_retrieval/v1"
    assert policy["roles"]["locator_ref"] == "discovered target; content not read"
    assert policy["roles"]["evidence_snippet"] == "bounded readable excerpt"
    assert policy["query_owner"] == "planner_or_control_model"
    assert policy["executor_owner"] == "Workspace search/read actions or Blocks workspace_operation"


def test_flat_step_plan_preserves_scoped_retrieval_query_groups(tmp_path):
    agent = _create_agent("agent-task-scoped-retrieval-plan").use_workspace(tmp_path / "workspace")
    task = AgentTask(
        agent,
        task_id="scoped-retrieval-plan",
        goal="Use scoped search before reading large files.",
        success_criteria=["Evidence is grounded."],
    )

    plan = task._normalize_step_plan(
        {
            "execution_shape": "actions",
            "step_instruction": "Search scoped notes.",
            "scoped_retrieval": {
                "queries": [
                    {
                        "query": "deadline",
                        "expected_role": "evidence_snippet",
                        "path": "notes",
                        "pattern": "*.md",
                    },
                    {
                        "query": "final.md",
                        "expected_role": "locator_ref",
                    },
                ],
                "fallback_order": ["next_query", "bounded_read"],
            },
        }
    )

    assert plan["scoped_retrieval"] == {
        "query_groups": [
            {
                "query": "deadline",
                "expected_role": "evidence_snippet",
                "path": "notes",
                "pattern": "*.md",
            },
            {
                "query": "final.md",
                "expected_role": "locator_ref",
            },
        ],
        "fallback_order": ["next_query", "bounded_read"],
    }


def test_scoped_retrieval_normalizes_structured_content_contains_and_globs(tmp_path):
    agent = _create_agent("agent-task-scoped-retrieval-structured-fields").use_workspace(tmp_path / "workspace")
    task = AgentTask(
        agent,
        task_id="scoped-retrieval-structured-fields",
        goal="Use structured retrieval fields.",
        success_criteria=["Evidence is grounded."],
    )

    plan = task._normalize_step_plan(
        {
            "execution_shape": "actions",
            "step_instruction": "Find Atlas evidence.",
            "expected_evidence": "Atlas evidence",
            "scoped_retrieval": {
                "query_groups": [
                    {
                        "query": "read all retained files that mention Atlas or owner",
                        "expected_role": "content retrieval for source facts",
                        "search_surface": "workspace_files",
                        "path": "retained/",
                        "pattern": "*.txt,*.md,*.json",
                        "filters": {"content_contains": ["Atlas", "owner"]},
                    }
                ]
            },
        }
    )

    query_groups = plan["scoped_retrieval"]["query_groups"]
    assert [group["query"] for group in query_groups] == ["Atlas", "owner"]
    assert all(group["pattern"] == "**" for group in query_groups)
    assert all("content_contains" not in group.get("filters", {}) for group in query_groups)
    assert query_groups[0]["search_surface"] == "workspace_files"


def test_taskboard_source_ref_policy_reuses_scoped_retrieval_policy():
    policy = AgentTask._taskboard_source_ref_policy()

    assert policy["scoped_retrieval_policy"] == scoped_retrieval_policy()
    assert "locator_ref" in policy["scoped_retrieval_policy"]["roles"]
    assert "evidence_snippet" in policy["scoped_retrieval_policy"]["roles"]
    assert any("filters.collection" in rule for rule in policy["scoped_retrieval_policy"]["rules"])
    assert any("never infer a generic kind" in rule for rule in policy["scoped_retrieval_policy"]["rules"])
    assert any("truncated evidence snippets" in rule for rule in policy["scoped_retrieval_policy"]["rules"])
    assert any("filters.collection" in rule for rule in policy["rules"])
    assert any("never infer a generic kind" in rule for rule in policy["rules"])
    assert any("truncated evidence snippets" in rule for rule in policy["rules"])


def test_block_carrier_compiles_scoped_retrieval_before_agent_step(tmp_path):
    agent = _create_agent("agent-task-scoped-retrieval-block-plan").use_workspace(tmp_path / "workspace")
    task = AgentTask(
        agent,
        task_id="scoped-retrieval-block-plan",
        goal="Use scoped search before reading large files.",
        success_criteria=["Evidence is grounded."],
    )
    plan = task._normalize_step_plan(
        {
            "execution_shape": "actions",
            "step_instruction": "Use the retrieved evidence.",
            "expected_evidence": "deadline evidence",
            "scoped_retrieval": {
                "query_groups": [
                    {
                        "query": "alpha deadline",
                        "expected_role": "evidence_snippet",
                        "filters": {"scope.case_id": "alpha"},
                        "search_surface": "workspace_files",
                        "path": "notes",
                        "pattern": "*.md",
                        "snippet_limit": 64,
                        "max_file_bytes": 4096,
                    }
                ]
            },
        }
    )
    context_pack: dict[str, Any] = {
        "goal": task.goal,
        "items": [],
        "omitted": [],
        "diagnostics": {},
        "profile": "test",
    }
    work_unit = task._build_flat_work_unit_intent(1, plan, cast(Any, context_pack))

    execution_plan = task._build_blocks_execution_plan(work_unit, plan, cast(Any, context_pack))

    assert [block.kind for block in execution_plan.plan_blocks] == ["workspace_operation", "agent_step"]
    assert execution_plan.plan_blocks[0].bound_inputs["operation"] == "search"
    assert execution_plan.plan_blocks[0].bound_inputs["query"] == "alpha deadline"
    assert execution_plan.plan_blocks[0].bound_inputs["include_snippets"] is True
    assert execution_plan.plan_blocks[0].bound_inputs["search_surface"] == "workspace_files"
    assert execution_plan.plan_blocks[0].bound_inputs["path"] == "notes"
    assert execution_plan.plan_blocks[0].bound_inputs["pattern"] == "*.md"
    assert execution_plan.plan_blocks[0].bound_inputs["max_file_bytes"] == 4096
    compact_plan = task._compact_execution_plan_for_meta(execution_plan)
    assert compact_plan["plan_blocks"][0]["bound_inputs"]["operation"] == "search"
    assert compact_plan["plan_blocks"][0]["bound_inputs"]["query"] == "alpha deadline"
    assert compact_plan["plan_blocks"][0]["bound_inputs"]["filters"] == {
        "scope.case_id": "alpha",
        "path": "notes",
    }
    assert execution_plan.edges[0].from_plan_block == execution_plan.plan_blocks[0].id
    assert execution_plan.edges[0].to_plan_block == execution_plan.plan_blocks[1].id
    assert execution_plan.edges[0].binding["target_input"] == "scoped_retrieval_results"


def test_block_carrier_normalizes_singleton_record_filters(tmp_path):
    agent = _create_agent("agent-task-scoped-retrieval-filter-normalization").use_workspace(tmp_path / "workspace")
    task = AgentTask(
        agent,
        task_id="scoped-retrieval-filter-normalization",
        goal="Use record filters.",
        success_criteria=["Evidence is grounded."],
    )
    plan = task._normalize_step_plan(
        {
            "execution_shape": "actions",
            "step_instruction": "Search retained notes.",
            "scoped_retrieval": {
                "query_groups": [
                    {
                        "query": "Project Atlas",
                        "expected_role": "evidence_snippet",
                        "search_surface": "workspace_index",
                        "filters": {"collection": ["retained-notes"]},
                    }
                ]
            },
        }
    )
    context_pack: dict[str, Any] = {
        "goal": task.goal,
        "items": [],
        "omitted": [],
        "diagnostics": {},
        "profile": "test",
    }
    work_unit = task._build_flat_work_unit_intent(1, plan, cast(Any, context_pack))

    execution_plan = task._build_blocks_execution_plan(work_unit, plan, cast(Any, context_pack))

    assert execution_plan.plan_blocks[0].bound_inputs["filters"]["collection"] == "retained-notes"


@pytest.mark.asyncio
async def test_block_carrier_executes_scoped_retrieval_and_injects_results(tmp_path):
    agent = _create_agent("agent-task-scoped-retrieval-block-exec").use_workspace(tmp_path / "workspace")
    task = AgentTask(
        agent,
        task_id="scoped-retrieval-block-exec",
        goal="Use scoped search before reading large files.",
        success_criteria=["Evidence is grounded."],
    )
    await task.workspace.ingest(
        content="Alpha deadline is 2026-07-01. Use this bounded evidence.",
        collection="observations",
        kind="note",
        summary="alpha deadline note",
        scope={"case_id": "alpha"},
    )
    await task.workspace.ingest(
        content="Beta deadline is unrelated.",
        collection="observations",
        kind="note",
        summary="beta deadline note",
        scope={"case_id": "beta"},
    )
    plan = task._normalize_step_plan(
        {
            "execution_shape": "actions",
            "step_instruction": "Use the retrieved evidence.",
            "expected_evidence": "deadline evidence",
            "scoped_retrieval": {
                "query_groups": [
                    {
                        "query": "deadline",
                        "expected_role": "evidence_snippet",
                        "filters": {"scope.case_id": "alpha"},
                        "snippet_limit": 48,
                    }
                ]
            },
        }
    )
    context_pack: dict[str, Any] = {
        "goal": task.goal,
        "items": [],
        "omitted": [],
        "diagnostics": {},
        "profile": "test",
    }
    work_unit = task._build_flat_work_unit_intent(1, plan, cast(Any, context_pack))
    seen: dict[str, Any] = {}

    async def handler(block_context: Mapping[str, Any]) -> dict[str, Any]:
        scoped_results = task._scoped_retrieval_results_from_block_context(block_context)
        evidence_ledger = task._evidence_ledger_from_block_context(block_context)
        seen["scoped_results"] = scoped_results
        seen["evidence_ledger"] = evidence_ledger
        return {
            "execution_result": {
                "candidate_final_result": "Alpha deadline found.",
                "scoped_retrieval_results": scoped_results,
                "evidence_use": [
                    {
                        "claim": "Alpha deadline found.",
                        "evidence_ids": [evidence_ledger["items"][2]["id"]],
                        "support_type": "content",
                    }
                ],
            },
            "execution_meta": {
                "execution_id": "scoped-retrieval-child",
                "status": "completed",
                "route": {"selected_route": "test", "status": "completed"},
                "logs": {"action_logs": [], "route_logs": {}, "errors": []},
            },
        }

    execution_result, execution_meta, _work_unit_result = await task._run_work_unit_through_blocks(
        work_unit=work_unit,
        plan=plan,
        context_pack=cast(Any, context_pack),
        execution_id="scoped-retrieval-block-exec-run",
        handler=handler,
        start_payload={"test": True},
    )

    scoped_results = seen["scoped_results"]
    evidence_ledger = seen["evidence_ledger"]
    assert len(scoped_results) == 1
    assert scoped_results[0]["query"] == "deadline"
    assert scoped_results[0]["bounded"]["returned_results"] == 1
    assert [item["kind"] for item in evidence_ledger["items"][:3]] == [
        "workspace_operation.search",
        "locator_ref",
        "evidence_snippet",
    ]
    assert evidence_ledger["items"][1]["body_state"] == "ref_only"
    assert evidence_ledger["items"][2]["body_state"] in {"bounded", "truncated"}
    snippet = scoped_results[0]["evidence_snippets"][0]
    assert snippet["role"] == "evidence_snippet"
    assert snippet["content"].startswith("Alpha deadline")
    assert "semantically_relevant" not in scoped_results[0]
    assert execution_result["scoped_retrieval_results"][0]["evidence_snippets"][0]["content"].startswith(
        "Alpha deadline"
    )
    block_kinds = [
        block["kind"]
        for block in execution_meta["blocks"]["execution_block_graph"]["execution_blocks"]
    ]
    assert block_kinds == ["workspace_operation", "agent_step"]
    block_evidence_items = execution_meta["blocks"]["evidence"]["evidence_items"]
    assert evidence_ledger["items"][2]["id"] in {item["id"] for item in block_evidence_items}
    compact_search_output = execution_meta["blocks"]["evidence"]["execution_block_results"][0]["output"]
    assert compact_search_output["operation"] == "search"
    assert compact_search_output["query"] == "deadline"
    assert compact_search_output["bounded"]["returned_results"] == 1
    assert compact_search_output["evidence_snippet_count"] == 1


@pytest.mark.asyncio
async def test_block_carrier_executes_file_scoped_retrieval_and_injects_results(tmp_path):
    agent = _create_agent("agent-task-file-scoped-retrieval").use_workspace(tmp_path / "workspace")
    task = AgentTask(
        agent,
        task_id="file-scoped-retrieval-block-exec",
        goal="Use scoped file search before reading broad files.",
        success_criteria=["Evidence is grounded."],
    )
    await task.workspace.write_file("notes/alpha.md", "alpha\nrelease deadline is 2026-07-01\n")
    plan = task._normalize_step_plan(
        {
            "execution_shape": "actions",
            "step_instruction": "Use the retrieved file evidence.",
            "expected_evidence": "deadline evidence",
            "scoped_retrieval": {
                "query_groups": [
                    {
                        "query": "deadline",
                        "expected_role": "evidence_snippet",
                        "search_surface": "workspace_files",
                        "path": "notes",
                        "pattern": "*.md",
                        "max_file_bytes": 1024,
                    }
                ]
            },
        }
    )
    context_pack: dict[str, Any] = {
        "goal": task.goal,
        "items": [],
        "omitted": [],
        "diagnostics": {},
        "profile": "test",
    }
    work_unit = task._build_flat_work_unit_intent(1, plan, cast(Any, context_pack))
    seen: dict[str, Any] = {}

    async def handler(block_context: Mapping[str, Any]) -> dict[str, Any]:
        scoped_results = task._scoped_retrieval_results_from_block_context(block_context)
        seen["scoped_results"] = scoped_results
        return {
            "execution_result": {
                "candidate_final_result": "File deadline found.",
                "scoped_retrieval_results": scoped_results,
            },
            "execution_meta": {
                "execution_id": "file-scoped-retrieval-child",
                "status": "completed",
                "route": {"selected_route": "test", "status": "completed"},
                "logs": {"action_logs": [], "route_logs": {}, "errors": []},
            },
        }

    execution_result, execution_meta, _work_unit_result = await task._run_work_unit_through_blocks(
        work_unit=work_unit,
        plan=plan,
        context_pack=cast(Any, context_pack),
        execution_id="file-scoped-retrieval-block-exec-run",
        handler=handler,
        start_payload={"test": True},
    )

    scoped_results = seen["scoped_results"]
    assert scoped_results[0]["bounded"]["search_surface"] == "workspace_files"
    assert "search_engines" not in scoped_results[0]["bounded"]
    assert scoped_results[0]["bounded"]["file_returned_results"] == 1
    assert scoped_results[0]["bounded"]["context_lines"] == 3
    assert scoped_results[0]["evidence_snippets"][0]["content"] == "alpha\nrelease deadline is 2026-07-01"
    assert scoped_results[0]["locator_refs"][0]["content_state"] == "ref_only"
    assert execution_result["scoped_retrieval_results"][0]["bounded"]["returned_results"] == 1
    compact_search_output = execution_meta["blocks"]["evidence"]["execution_block_results"][0]["output"]
    assert compact_search_output["operation"] == "search"
    assert compact_search_output["bounded"]["search_surface"] == "workspace_files"
    assert compact_search_output["evidence_snippet_count"] == 1
    compact_operations = execution_meta["block_carrier"]["workspace_operations"]
    assert compact_operations[0]["kind"] == "workspace_operation"
    assert compact_operations[0]["output"]["operation"] == "search"
    assert compact_operations[0]["output"]["bounded"]["returned_results"] == 1
    assert compact_operations[0]["output"]["bounded"]["search_engines"] in (
        ["workspace_file_grep"],
        ["workspace_file_scan"],
    )


@pytest.mark.asyncio
async def test_taskboard_card_scoped_retrieval_uses_block_carrier(tmp_path):
    agent = _create_agent("agent-task-taskboard-scoped-retrieval").use_workspace(tmp_path / "workspace")
    task = AgentTask(
        agent,
        task_id="taskboard-scoped-retrieval-block-exec",
        goal="Use scoped retrieval inside a TaskBoard card.",
        success_criteria=["Evidence is grounded."],
    )
    await task.workspace.write_file("retained/ops-note.md", "Project Atlas owner is Priya Shah.\n")
    card = TaskBoardCard(
        id="collect",
        objective="Find the Atlas owner evidence.",
        allowed_execution_shape="actions",
        metadata={
            "scoped_retrieval": {
                "query_groups": [
                    {
                        "query": "Priya Shah",
                        "expected_role": "evidence_snippet",
                        "search_surface": "workspace_files",
                        "path": "retained",
                        "pattern": "**",
                        "max_results": 2,
                    }
                ]
            }
        },
    )
    plan = task._taskboard_card_carrier_plan(card)
    work_unit = WorkUnitIntent(
        id="taskboard:collect:attempt:1",
        origin="taskboard_card",
        objective=card.objective,
        input_payload={
            "card": card.to_dict(),
            "scoped_retrieval": task._taskboard_card_scoped_retrieval(card),
            "retrieval_policy": scoped_retrieval_policy(),
        },
        delivery_contract={"card": card.to_dict()},
        runtime_preferences={
            "handler": "agent_task_bounded_step",
            "preferred_execution_shape": "taskboard_card",
            "strategy": "taskboard",
        },
    )
    context_pack: dict[str, Any] = {
        "goal": task.goal,
        "items": [],
        "omitted": [],
        "diagnostics": {},
        "profile": "test",
    }
    seen: dict[str, Any] = {}

    async def handler(block_context: Mapping[str, Any]) -> dict[str, Any]:
        payload = task._taskboard_card_payload_with_scoped_retrieval_results(
            work_unit.input_payload,
            block_context,
        )
        seen["payload"] = payload
        return {
            "execution_result": {
                "answer": "TaskBoard card used scoped retrieval.",
                "scoped_retrieval_results": payload.get("scoped_retrieval_results", []),
            },
            "execution_meta": {
                "execution_id": "taskboard-scoped-retrieval-child",
                "status": "completed",
                "route": {"selected_route": "test", "status": "completed"},
                "logs": {"action_logs": [], "route_logs": {}, "errors": []},
            },
        }

    execution_result, execution_meta, _work_unit_result = await task._run_work_unit_through_blocks(
        work_unit=work_unit,
        plan=plan,
        context_pack=cast(Any, context_pack),
        execution_id="taskboard-scoped-retrieval-block-exec-run",
        handler=handler,
        start_payload={"test": True},
    )

    assert plan["scoped_retrieval"]["query_groups"][0]["pattern"] == "**"
    scoped_results = seen["payload"]["scoped_retrieval_results"]
    assert scoped_results[0]["bounded"]["search_surface"] == "workspace_files"
    assert scoped_results[0]["bounded"]["returned_results"] == 1
    assert scoped_results[0]["evidence_snippets"][0]["content"] == "Project Atlas owner is Priya Shah."
    assert execution_result["scoped_retrieval_results"][0]["bounded"]["returned_results"] == 1
    block_kinds = [
        block["kind"]
        for block in execution_meta["blocks"]["execution_block_graph"]["execution_blocks"]
    ]
    assert block_kinds == ["workspace_operation", "agent_step"]
    compact_operations = execution_meta["block_carrier"]["workspace_operations"]
    assert compact_operations[0]["kind"] == "workspace_operation"
    assert compact_operations[0]["output"]["bounded"]["returned_results"] == 1
    taskboard_compact = task._compact_block_carrier_for_taskboard_meta(
        execution_meta["block_carrier"],
        blocks=execution_meta["blocks"],
    )
    assert taskboard_compact["workspace_operations"][0]["kind"] == "workspace_operation"
    assert taskboard_compact["workspace_operations"][0]["output"]["bounded"]["returned_results"] == 1
    prompt_view = task._compact_taskboard_evidence_view_for_prompt(
        {
            "cards": [
                {
                    "card_id": "collect",
                    "status": "completed",
                    "diagnostics": [{"block_carrier": taskboard_compact}],
                }
            ]
        }
    )
    prompt_operation = prompt_view["cards"][0]["workspace_operations"][0]
    assert "Project Atlas owner is Priya Shah." in prompt_operation["output"]["first_evidence_snippet"]["content"]


def test_workspace_artifact_bounded_step_schema_excludes_long_body_fields():
    schema = AgentTask._bounded_step_output_schema(
        {
            "body_transport": "workspace_artifact",
            "body_uses_output": False,
            "control_format": "json",
        }
    )

    assert "artifact_manifest" in schema
    assert schema["artifact_manifest"][2] is False
    assert schema["evidence"][2] is False
    assert "candidate_final_result" not in schema
    assert "artifact_markdown" not in schema
    assert "file_refs" not in schema
    keys = list(schema)
    assert keys.index("self_check") > keys.index("acceptance_points")
    assert keys.index("short_summary") > keys.index("self_check")
    assert keys.index("progress_message") > keys.index("short_summary")


def test_agent_task_defaults_do_not_apply_resource_caps(tmp_path):
    agent = _create_agent("agent-task-default-resource-caps").use_workspace(tmp_path / "workspace")
    task = AgentTask(
        agent,
        task_id="default-resource-caps",
        goal="Complete the task.",
        success_criteria=["The task is complete."],
    )

    assert task.max_iterations is None
    assert task.limits == {}
    assert task._taskboard_max_ticks() is None
    assert task._taskboard_max_ticks_source() == "unbounded_default"


@pytest.mark.skipif(not hasattr(time, "tzset"), reason="local timezone switching requires time.tzset")
def test_task_context_contract_includes_utc_and_local_time_when_timezone_known(tmp_path):
    previous_tz = os.environ.get("TZ")
    os.environ["TZ"] = "Asia/Shanghai"
    time.tzset()
    try:
        agent = _create_agent("agent-task-context-local-time").use_workspace(tmp_path / "workspace")
        task = AgentTask(
            agent,
            task_id="context-local-time",
            goal="Complete the task.",
            success_criteria=["The task is complete."],
        )
        task.created_at = 0
        task.started_at = None

        contract = task._task_context_contract()

        current_time = contract["current_time"]
        assert current_time["utc"] == "1970-01-01T00:00:00Z"
        assert current_time["local"] == "1970-01-01T08:00:00+08:00"
        assert current_time["timezone"] == "Asia/Shanghai"
        assert "run_date_utc" not in contract
        assert "run_time_utc" not in contract
        assert "run_date_local" not in contract
        assert "run_time_local" not in contract
        assert "model decisions broadly" in contract["temporal_policy"]["general_decision_context"]
    finally:
        if previous_tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = previous_tz
        time.tzset()


@pytest.mark.asyncio
async def test_record_observation_projects_action_logs_to_normalized_action_events(tmp_path):
    agent = _create_agent("agent-task-action-observation-events").use_workspace(tmp_path / "workspace")
    task = AgentTask(
        agent,
        task_id="action-observation-events",
        goal="Inspect Workspace evidence with actions.",
        success_criteria=["Action facts are observable."],
    )
    decision_ref = await task._record_decision(
        1,
        {"step_instruction": "Search and read scoped evidence."},
        {
            "goal": task.goal,
            "items": [],
            "omitted": [],
            "diagnostics": {},
            "profile": "test",
        },
    )
    execution_meta = {
        "execution_id": "exec-action-events",
        "status": "completed",
        "route": {"selected_route": "actions"},
        "block_carrier": {
            "work_unit": {
                "id": "iter-1:flat-step",
                "origin": "flat_step",
                "runtime_preferences": {"strategy": "flat"},
            }
        },
        "logs": {
            "action_logs": [
                {
                    "action_id": "grep_workspace",
                    "status": "success",
                    "action_call_id": "call-grep",
                    "kind": "shell_search",
                    "raw": {
                        "kwargs": {"query": "deadline", "scope": "workspace"},
                        "error": "one search backend failed after another backend returned results",
                    },
                    "elapsed_ms": 12,
                    "model_digest": {
                        "result_preview": {
                            "path": "notes.md",
                            "content": "deadline is 2026-07-01",
                        },
                        "result_preview_meta": {"bytes": 24, "truncated": False},
                        "file_refs": [{"path": "notes.md", "sha256": "abc"}],
                    },
                },
                {
                    "action_id": "read_file",
                    "status": "failed",
                    "action_call_id": "call-read",
                    "raw": {"kwargs": {"path": "missing.md"}},
                    "error": "file not found",
                    "retryable": False,
                },
            ],
            "route_logs": {},
        },
    }

    await task._record_observation(
        1,
        plan={"step_instruction": "Search and read scoped evidence."},
        decision_ref=decision_ref,
        execution_result={"step_result": "searched workspace"},
        execution_meta=execution_meta,
    )
    await task._record_observation(
        1,
        plan={"step_instruction": "Search and read scoped evidence."},
        decision_ref=decision_ref,
        execution_result={"step_result": "searched workspace"},
        execution_meta=execution_meta,
    )

    action_items = [item for item in task._stream_items if item.path.startswith("agent_task.action.")]
    started_items = [item for item in action_items if item.path == "agent_task.action.started"]
    completed_items = [item for item in action_items if item.path == "agent_task.action.completed"]
    failed_items = [item for item in action_items if item.path == "agent_task.action.failed"]

    assert len(started_items) == 2
    assert len(completed_items) == 1
    assert len(failed_items) == 1
    assert all((item.meta or {}).get("stream_kind") == "action_observation" for item in action_items)
    grep_started = next(item for item in started_items if item.value["action_id"] == "grep_workspace")
    assert grep_started.value["input_summary"] == {"query": "deadline", "scope": "workspace"}
    assert grep_started.value["work_unit_id"] == "iter-1:flat-step"
    grep_completed = completed_items[0]
    assert grep_completed.value["output_summary"]["path"] == "notes.md"
    assert grep_completed.value["file_refs"][0]["path"] == "notes.md"
    assert grep_completed.value["success"] is True
    assert "error" not in grep_completed.value
    assert any(ref["value"] == "notes.md" for ref in grep_completed.value["source_refs"])
    read_failed = failed_items[0]
    assert read_failed.value["action_id"] == "read_file"
    assert read_failed.value["error"] == "file not found"
    assert read_failed.value["failure_category"] == "execution"
    assert read_failed.value["retryable"] is False


def test_agent_task_explicit_resource_caps_remain_effective(tmp_path):
    agent = _create_agent("agent-task-explicit-resource-caps").use_workspace(tmp_path / "workspace")
    task = AgentTask(
        agent,
        task_id="explicit-resource-caps",
        goal="Complete the task.",
        success_criteria=["The task is complete."],
        max_iterations=2,
        limits={"max_model_requests": 1},
    )
    taskboard_task = AgentTask(
        agent,
        task_id="explicit-taskboard-tick-cap",
        goal="Complete the board task.",
        success_criteria=["The task is complete."],
        max_iterations=2,
        options={"agent_task": {"taskboard_max_ticks": 4}},
    )

    assert task.max_iterations == 2
    assert task.limits["max_model_requests"] == 1
    assert task._taskboard_max_ticks() == 2
    assert task._taskboard_max_ticks_source() == "explicit_max_iterations"
    assert taskboard_task._taskboard_max_ticks() == 4
    assert taskboard_task._taskboard_max_ticks_source() == "taskboard_option"


def test_flat_step_plan_infers_workspace_artifact_mode_from_required_deliverables():
    task = AgentTask.__new__(AgentTask)
    task.options = {
        "execution_prompt_snapshot": {
            "input": {
                "case": {
                    "output_contract": {
                        "required_deliverables": [{"path": "final.md"}],
                    }
                }
            }
        }
    }

    plan = task._normalize_step_plan(
        {
            "execution_shape": "direct",
            "step_instruction": "write the final file",
            "expected_evidence": "final.md exists",
            "rationale": "caller requires a file deliverable",
            "deliverable_mode": "inline_final",
        }
    )

    assert plan["deliverable_mode"] == "sectioned_workspace_artifact"
    assert plan["deliverable_mode_source"] == "required_workspace_deliverables"
    assert plan["required_workspace_deliverables"] == ["final.md"]
    assert plan["prefer_stream_draft"] is True


def test_flat_step_plan_normalizes_expected_evidence_duplicate_prefix_alias():
    task = AgentTask.__new__(AgentTask)
    task.options = {}

    plan = task._normalize_step_plan(
        {
            "execution_shape": "actions",
            "step_instruction": "write the final file",
            "expected_expected_evidence": "final.md exists after the Workspace write action",
            "rationale": "the previous step gathered the evidence",
        }
    )

    assert plan["expected_evidence"] == "final.md exists after the Workspace write action"
    assert "expected_expected_evidence" not in plan
    assert plan["normalization_diagnostics"][0]["code"] == "agent_task.flat_plan.expected_evidence_alias"


@pytest.mark.asyncio
async def test_carrier_control_policy_reaches_child_execution_output_format():
    task = AgentTask.__new__(AgentTask)
    emitted: list[tuple[str, dict[str, Any]]] = []

    async def emit(event: str, payload: dict[str, Any]) -> None:
        emitted.append((event, payload))

    setattr(task, "_emit", emit)

    class FakeExecution:
        id = "fake-carrier-format-execution"

        def __init__(self, data: Any | None = None) -> None:
            self.data = {"summary": "ok"} if data is None else data
            self.output_format: str | None = None
            self.output_called = False

        def input(self, payload: dict[str, Any]) -> None:
            self.input_payload = payload

        def language(self, language: str) -> None:
            self.language_value = language

        def instruct(self, instruction: str) -> None:
            self.instruction = instruction

        def output(self, schema: dict[str, Any], *, format: str) -> None:
            self.output_called = True
            self.output_schema = schema
            self.output_format = format

        async def async_get_data(self) -> Any:
            return self.data

        async def async_get_meta(self) -> dict[str, Any]:
            return {"status": "success"}

    execution = FakeExecution()
    carrier_policy = task._carrier_output_policy_from_block_context(
        {"input": {"carrier_output_policy": {"control_format": "xml_field"}}}
    )
    result, meta = await task._run_bounded_child_execution(
        execution=execution,
        language_policy={"language": "en"},
        input_payload={"task_id": "carrier-format"},
        instruction="Return a bounded summary.",
        output_schema={"summary": (str, "bounded summary", True)},
        output_format=task._carrier_control_output_format(carrier_policy),
        started_event="agent_task.test.execution.started",
        started_payload={},
        stream_bridge=lambda _execution: asyncio.sleep(0),
    )

    assert execution.output_format == "xml_field"
    assert execution.output_called is True
    assert result == {"summary": "ok"}
    assert meta["status"] == "success"
    assert emitted[0][0] == "agent_task.test.execution.started"

    free_text_execution = FakeExecution("natural-language body")
    free_text_policy = {"control_format": None, "body_uses_output": False, "body_transport": "plain_text"}
    result, meta = await task._run_bounded_child_execution(
        execution=free_text_execution,
        language_policy={"language": "en"},
        input_payload={"task_id": "carrier-free-text"},
        instruction="Write the report body.",
        output_schema={"summary": (str, "bounded summary", True)},
        output_format="json",
        use_output=task._carrier_uses_control_output(free_text_policy),
        carrier_output_policy=free_text_policy,
        started_event="agent_task.test.free_text.started",
        started_payload={},
        stream_bridge=lambda _execution: asyncio.sleep(0),
    )

    assert free_text_execution.output_called is False
    assert free_text_execution.output_format is None
    assert free_text_execution.input_payload["carrier_output_policy"]["body_transport"] == "plain_text"
    assert "return the natural-language body directly as plain text" in free_text_execution.instruction
    assert result == "natural-language body"
    assert meta["status"] == "success"


def test_taskboard_prompt_compaction_drops_recursive_block_carrier_payload():
    huge = "x" * 20000
    block_carrier = {
        "work_unit": {
            "id": "taskboard:deliver:attempt:1",
            "origin": "taskboard_card",
            "objective": huge,
            "input_payload": {
                "taskboard_evidence_view": {
                    "cards": [{"diagnostics": [{"block_carrier": {"recursive": huge}}]}],
                }
            },
            "input_refs": [{"artifact_id": "a1", "bytes": 100}],
            "expected_deliverable": {"required_outputs": ["final.md"]},
            "evidence_requirements": [{"required_output": "final.md"}],
        },
        "work_unit_result": {
            "id": "taskboard:deliver:attempt:1",
            "status": "completed",
            "summary": huge,
            "carrier_meta": {
                "snapshot_status": "completed",
                "execution_plan": {"id": "plan", "large": huge},
                "execution_block_graph": {"id": "graph", "large": huge},
            },
        },
        "output_policy": {
            "body_transport": "structured_control",
            "control_format": "json",
        },
    }
    compact_carrier = AgentTask._compact_block_carrier_for_taskboard_meta(block_carrier)
    compact_carrier_text = json.dumps(compact_carrier, ensure_ascii=False)

    assert compact_carrier["work_unit"]["origin"] == "taskboard_card"
    assert compact_carrier["work_unit_result"]["id"] == compact_carrier["work_unit"]["id"]
    assert "input_payload" not in compact_carrier_text
    assert len(compact_carrier_text) < 8000

    evidence_view = {
        "schema_version": "task_board_evidence_view/v1",
        "revision_id": "rev-1",
        "status_counts": {"completed": 1},
        "source_refs": [
            {
                "source_url": "https://example.test/source",
                "title": "Official source",
                "content": huge,
            }
        ],
        "file_refs": [
            {
                "path": "final.md",
                "sha256": "abc123",
                "preview": huge,
                "bytes": len(huge),
            }
        ],
        "cards": [
            {
                "card_id": "deliver",
                "status": "completed",
                "preview": {"answer": huge},
                "source_refs": [
                    {
                        "url": "https://example.test/card-source",
                        "label": "Card source",
                        "content": huge,
                    }
                ],
                "file_refs": [
                    {
                        "path": "evidence/source.md",
                        "sha256": "def456",
                        "preview": huge,
                    }
                ],
                "diagnostics": [{"block_carrier": block_carrier}],
                "metadata": {"block_carrier": block_carrier},
            }
        ],
    }
    compact_view = AgentTask._compact_taskboard_evidence_view_for_prompt(evidence_view)
    compact_view_text = json.dumps(compact_view, ensure_ascii=False)

    assert len(compact_view_text) < 10000
    assert len(huge) > len(compact_view_text)
    assert "taskboard:deliver:attempt:1" in compact_view_text
    assert "https://example.test/source" in compact_view_text
    assert "https://example.test/card-source" in compact_view_text
    assert "final.md" in compact_view_text
    assert "evidence/source.md" in compact_view_text
    assert "input_payload" not in compact_view_text
    assert huge not in compact_view_text


def test_block_carrier_hot_metadata_compacts_recursive_payload():
    huge = "x" * 50000
    work_unit = WorkUnitIntent(
        id="iter-1:flat-step",
        origin="flat_step",
        objective=huge,
        input_payload={"recursive": {"execution_meta": {"block_carrier": {"large": huge}}}},
        delivery_contract={
            "deliverable_mode": "workspace_artifact",
            "execution_prompt": {"output": {"final": (str,)}, "output_format": "json"},
        },
        runtime_preferences={"handler": "agent_task_bounded_step", "strategy": "flat", "step_plan": "direct"},
    )
    work_unit_result = WorkUnitResult(
        id=work_unit.id,
        status="completed",
        summary={"large": huge},
        artifact_manifest={"path": "final.md", "sha256": "abc"},
        evidence=(huge,),
        carrier_meta={
            "execution_plan": {"large": huge},
            "execution_block_graph": {"large": huge},
            "snapshot": {"large": huge},
        },
    )
    output_policy = select_carrier_output_policy(work_unit)
    snapshot = {
        "status": "completed",
        "blocks": {
            "status": "completed",
            "replan_signals": [],
            "execution_block_results": [{"output": {"execution_meta": {"recursive": huge}}}],
        },
    }
    block_result = {"semantic_outputs": {"step": {"large": huge}}, "status": "completed"}
    block_carrier = AgentTask._compact_block_carrier_for_meta(
        work_unit=work_unit,
        work_unit_result=work_unit_result,
        output_policy=output_policy,
        block_result=block_result,
        snapshot=snapshot,
    )

    class FakeExecutionPlan:
        def to_dict(self) -> dict[str, Any]:
            return {
                "plan_id": "plan-1",
                "task_frame_id": "frame-1",
                "plan_blocks": [
                    {
                        "id": "agent-step",
                        "plan_block_id": "agent_step",
                        "kind": "agent_step",
                        "intent": huge,
                        "bound_inputs": {
                            "task_id": "task-1",
                            "step_plan": "direct",
                            "work_unit": work_unit.to_dict(),
                            "plan": {"large": huge},
                        },
                    }
                ],
            }

    class FakeExecutionGraph:
        def to_dict(self) -> dict[str, Any]:
            return {
                "execution_id": "exec-1",
                "execution_blocks": [
                    {
                        "id": "agent-step",
                        "kind": "agent_step",
                        "bound_inputs": {"large": huge},
                    }
                ],
            }

    class FakeEvidence:
        def to_dict(self) -> dict[str, Any]:
            return {
                "execution_block_results": [
                    {
                        "id": "agent-step",
                        "kind": "agent_step",
                        "status": "completed",
                        "output": {
                            "execution_result": {"large": huge},
                            "execution_meta": {
                                "execution_id": "child-1",
                                "status": "completed",
                                "route": {"selected_route": "model_request", "status": "completed"},
                                "block_carrier": {"recursive": huge},
                            },
                        },
                    }
                ],
            }

    execution_meta: dict[str, Any] = {}
    AgentTask._attach_blocks_evidence(
        execution_meta,
        execution_plan=FakeExecutionPlan(),
        execution_graph=FakeExecutionGraph(),
        evidence=FakeEvidence(),
        block_result=block_result,
        snapshot=snapshot,
    )
    execution_meta["block_carrier"] = block_carrier
    hot_text = json.dumps(execution_meta, ensure_ascii=False)

    assert execution_meta["block_carrier"]["work_unit"]["origin"] == "flat_step"
    assert execution_meta["block_carrier"]["work_unit_result"]["id"] == work_unit.id
    assert execution_meta["blocks"]["execution_plan"]["plan_blocks"][0]["kind"] == "agent_step"
    assert execution_meta["blocks"]["execution_plan"]["plan_blocks"][0]["bound_inputs"]["step_plan"] == "direct"
    assert execution_meta["blocks"]["execution_block_graph"]["execution_blocks"][0]["kind"] == "agent_step"
    assert execution_meta["blocks"]["evidence"]["execution_block_results"][0]["kind"] == "agent_step"
    assert execution_meta["blocks"]["result"]["semantic_outputs"]
    assert "input_payload" not in hot_text
    assert "carrier_meta" not in hot_text
    assert len(hot_text) < 20000


def test_agent_task_hot_path_compaction_omits_provider_request_payloads():
    secret = "SECRET_REQUEST_DATA_SHOULD_STAY_COLD"
    request_payload = {
        "messages": [{"role": "user", "content": secret}],
        "tools": [{"name": "oversized_tool_schema", "description": secret}],
    }

    meta_value = AgentTask._compact_value_for_meta(
        {
            "status": "failed",
            "request_data": request_payload,
            "nested": {
                "provider_request": {
                    "request_payload": request_payload,
                }
            },
        }
    )
    verifier_value = AgentTask._compact_verifier_prompt_value(
        {
            "status": "failed",
            "raw_request": request_payload,
            "message": f"Status Code: 403\nRequest Data: {json.dumps(request_payload)}",
        }
    )
    action_preview = AgentTask._compact_action_preview_value(
        {
            "ok": False,
            "prompt_data": request_payload,
        },
        max_chars=1200,
    )
    hot_text = json.dumps(
        {
            "meta": meta_value,
            "verifier": verifier_value,
            "action_preview": action_preview,
        },
        ensure_ascii=False,
    )

    assert secret not in hot_text
    assert "messages" not in json.dumps(meta_value["request_data"], ensure_ascii=False)
    assert meta_value["request_data"]["reason"] == "provider_request_payload_hot_path"
    assert meta_value["nested"]["provider_request"]["reason"] == "provider_request_payload_hot_path"
    assert verifier_value["raw_request"]["reason"] == "provider_request_payload_hot_path"
    assert verifier_value["message"]["reason"] == "provider_request_payload_hot_path"
    assert action_preview["prompt_data"]["reason"] == "provider_request_payload_hot_path"


class MockAgentTaskRequester:
    name = "MockAgentTaskRequester"
    DEFAULT_SETTINGS: dict[str, object] = {}
    calls: list[str] = []
    verification_calls = 0

    def __init__(self, prompt, settings):
        self.prompt = prompt
        self.settings = settings

    @staticmethod
    def reset():
        MockAgentTaskRequester.calls = []
        MockAgentTaskRequester.verification_calls = 0

    @staticmethod
    def _on_register():
        MockAgentTaskRequester.reset()

    @staticmethod
    def _on_unregister():
        pass

    def generate_request_data(self):
        return AgentlyRequestData(
            client_options={},
            headers={},
            data={"messages": self.prompt.to_messages(), "output": self.prompt.get("output")},
            request_options={"stream": True},
            request_url="mock://agent-task",
        )

    async def request_model(self, request_data: AgentlyRequestData):
        text = json.dumps(DataFormatter.sanitize(request_data.data), ensure_ascii=False)
        MockAgentTaskRequester.calls.append(text)
        if "Summarize AgentTask progress" in text:
            payload = {
                "message": "Progress model summarized the current snapshot.",
            }
            payload_text = json.dumps(payload, ensure_ascii=False)
            midpoint = max(1, len(payload_text) // 2)
            yield "message", payload_text[:midpoint]
            yield "message", payload_text[midpoint:]
            return
        elif "Analyze this task's execution shape for AgentTaskLoop strategy resolution" in text:
            payload = {
                "analysis": "This mock task is linear and can stay in the flat loop.",
                "execution_hint": {
                    "recommended_shape": "flat",
                    "confidence": "medium",
                    "reasons": ["one bounded repair loop is enough"],
                    "linear_evidence": ["single deliverable"],
                    "branching_evidence": [],
                    "uncertainty": "",
                },
            }
        elif "Verify the task against every success criterion" in text:
            MockAgentTaskRequester.verification_calls += 1
            if MockAgentTaskRequester.verification_calls == 1:
                payload = {
                    "is_complete": False,
                    "requires_block": False,
                    "reason": "verification evidence is incomplete",
                    "missing_criteria": ["script does not run yet"],
                    "replan_instruction": "run the repair step again with the recorded failure evidence",
                    "final_result": "",
                }
            else:
                final_result = "legacy script upgraded and verified"
                if "summary" in text:
                    final_result = json.dumps(
                        {"summary": "Operator summary for INC-4242."},
                        ensure_ascii=False,
                    )
                payload = {
                    "is_complete": True,
                    "requires_block": False,
                    "reason": "all success criteria are now satisfied",
                    "missing_criteria": [],
                    "replan_instruction": "",
                    "final_result": final_result,
                }
        elif "Plan the next bounded AgentExecution step" in text:
            payload = {
                "step_instruction": "repair the legacy script using current Agently APIs",
                "expected_evidence": "script execution succeeds",
                "rationale": "the prior failure must be fixed before final verification",
            }
        elif "Execute exactly one bounded step" in text:
            payload = {
                "step_result": "patched script and ran verification",
                "evidence": ["python legacy_script.py exited with status 0"],
                "remaining_work": [],
            }
        else:
            payload = {"answer": "ok"}
        yield "message", json.dumps(payload, ensure_ascii=False)

    async def broadcast_response(
        self,
        response_generator: AsyncGenerator[tuple[str, object], None],
    ):
        response_text = ""
        async for event, data in response_generator:
            if event == "message":
                response_text += str(data)
                yield "delta", str(data)
        yield "done", response_text


def _create_agent(name: str = "agent-task-loop-test"):
    settings = Settings(name=f"{name}-settings", parent=Agently.settings)
    plugin_manager = PluginManager(settings, parent=Agently.plugin_manager, name=f"{name}-plugins")
    plugin_manager.register("ModelRequester", MockAgentTaskRequester, activate=True)
    return Agently.AgentType(plugin_manager, parent_settings=settings, name=name)


@pytest.mark.asyncio
async def test_agent_task_workspace_artifact_delivery_writes_and_readbacks(tmp_path):
    workspace = Agently.create_workspace(tmp_path / "workspace-artifact-helper")
    task = AgentTask.__new__(AgentTask)
    task.id = "workspace-artifact-helper"
    task.workspace = workspace
    task.diagnostics = {}

    execution_meta = {"logs": {}}
    delivered = await task._deliver_workspace_artifact(
        {
            "artifact_markdown": "# Actual Report\n\nThis content must exist on disk.",
            "artifact_manifest": {
                "path": "reports/final.md",
                "file_refs": [{"path": "fake-final.md", "sha256": "fake"}],
            },
            "file_refs": [{"path": "model-claimed.md", "sha256": "fake"}],
        },
        plan={"deliverable_mode": "workspace_artifact"},
        execution_meta=execution_meta,
        source="test.workspace_artifact",
    )

    assert (workspace.files_root / "reports/final.md").read_text(encoding="utf-8").startswith("# Actual Report")
    assert delivered["file_refs"][0]["path"] == "reports/final.md"
    assert delivered["file_refs"][0]["source"] == "test.workspace_artifact"
    assert delivered["artifact_manifest"]["sha256"] == delivered["file_refs"][0]["sha256"]
    assert delivered["diagnostics"][0]["code"] == "agent_task.workspace_artifact.untrusted_model_file_refs"
    assert delivered["artifact_markdown"].startswith("Workspace artifact delivered at reports/final.md")
    assert delivered["artifact_preview"].startswith("# Actual Report")
    assert delivered["workspace_artifact_content_omitted"][0]["field"] == "artifact_markdown"
    assert execution_meta["logs"]["artifact_refs"][0]["path"] == "reports/final.md"
    assert execution_meta["workspace_refs"]["agent_task_artifacts"][0]["path"] == "reports/final.md"
    ledger_items = execution_meta["blocks"]["evidence"]["evidence_items"]
    artifact_item = next(item for item in ledger_items if item["kind"] == "workspace_artifact.readback")
    assert artifact_item["status"] == "ok"
    assert artifact_item["path"] == "reports/final.md"
    assert artifact_item["body"].startswith("# Actual Report")


@pytest.mark.asyncio
async def test_workspace_artifact_delivery_records_acceptance_locators_from_actual_artifact(tmp_path):
    workspace = Agently.create_workspace(tmp_path / "workspace-artifact-locator")
    task = AgentTask.__new__(AgentTask)
    task.id = "workspace-artifact-locator"
    task.workspace = workspace
    task.diagnostics = {}
    task.success_criteria = ["The final report includes the middle evidence section."]

    execution_meta = {"logs": {}}
    delivered = await task._deliver_workspace_artifact(
        {
            "artifact_markdown": "# Report\n\nIntro.\n\n## Middle Evidence\n\nActual middle content.",
            "artifact_manifest": {"path": "reports/final.md"},
            "acceptance_points": [
                {
                    "criterion": "Middle evidence section is present.",
                    "expected_anchor": "Middle Evidence",
                    "line_start": 999,
                    "evidence_ids": ["source.evidence"],
                }
            ],
        },
        plan={"deliverable_mode": "workspace_artifact"},
        execution_meta=execution_meta,
        source="test.workspace_artifact",
    )

    assert delivered["workspace_artifact_delivery"]["acceptance_locator_count"] >= 1
    ledger_items = execution_meta["blocks"]["evidence"]["evidence_items"]
    locator = next(
        item
        for item in ledger_items
        if item["kind"] == "workspace_artifact.acceptance_locator" and item.get("anchor_text") == "Middle Evidence"
    )
    assert locator["status"] == "ok"
    assert locator["body_state"] == "ref_only"
    assert locator["line_start"] == 5
    assert locator["byte_offset"] < locator["byte_end"]
    assert "body" not in locator
    assert any("workspace_artifact_readback" in item for item in locator["source_evidence_ids"])


@pytest.mark.asyncio
async def test_verifier_workspace_artifact_readback_targets_required_sections(tmp_path):
    agent = _create_agent("agent-task-verifier-targeted-artifact").use_workspace(tmp_path / "workspace")
    task = AgentTask(
        agent,
        task_id="verifier-targeted-artifact",
        goal="Produce a long source-grounded final artifact.",
        success_criteria=["The final artifact includes a source list with concrete refs."],
        workspace=tmp_path / "workspace",
        options={
            "execution_prompt_snapshot": {
                "input": {
                    "case": {
                        "output_contract": {
                            "deliverables": [{"path": "final.md", "media_type": "text/markdown"}],
                            "sections": ["overview", "analysis", "source list"],
                        }
                    }
                }
            }
        },
    )
    filler = "\n".join(f"## Analysis {index}\n\nFiller paragraph {index}." for index in range(650))
    source_section = "\n\n## Source List\n\n- https://example.test/source-a\n- workspace://evidence/ref-a\n"
    body = "# Long Artifact\n\n" + filler + source_section
    write_result = await task.workspace.write_file("final.md", body)
    read_result = await task.workspace.read_file("final.md", max_bytes=4000)
    ref = {
        "path": "final.md",
        "bytes": int(read_result["bytes"]),
        "sha256": read_result["sha256"],
        "media_type": write_result.get("media_type"),
        "content_kind": "text",
        "role": "workspace_artifact",
        "source": "test",
        "preview": str(read_result["content"]),
        "truncated": True,
        "read_bytes": int(read_result["read_bytes"]),
    }

    artifacts = await task._trusted_workspace_artifacts_for_verifier({"artifact_refs": [ref]})

    assert artifacts[0]["readback"]["truncated"] is True
    assert "https://example.test/source-a" not in artifacts[0]["readback"]["content"]
    targeted = artifacts[0]["targeted_readbacks"]
    assert any(item["kind"] == "section_search" and item["query"] == "source list" for item in targeted)
    assert any("https://example.test/source-a" in item.get("content", "") for item in targeted)

    execution_meta = {
        "blocks": {
            "evidence": {
                "evidence_items": [task._workspace_artifact_readback_evidence_item(ref)],
            }
        }
    }
    await task._ensure_workspace_artifact_targeted_readback_evidence(
        execution_meta,
        task._cumulative_evidence_ledger(execution_meta),
    )
    ledger_items = execution_meta["blocks"]["evidence"]["evidence_items"]
    targeted_items = [item for item in ledger_items if item["kind"] == "workspace_artifact.targeted_readback"]
    assert targeted_items
    assert any("https://example.test/source-a" in item.get("body", "") for item in targeted_items)

    small_body = "# Small Artifact\n\n## Source List\n\n- https://example.test/source-a\n"
    small_write_result = await task.workspace.write_file("small.md", small_body)
    full_read_result = await task.workspace.read_file("small.md", max_bytes=12000)
    full_ref = {
        "path": "small.md",
        "bytes": int(full_read_result["bytes"]),
        "sha256": full_read_result["sha256"],
        "media_type": small_write_result.get("media_type"),
        "content_kind": "text",
        "role": "workspace_artifact",
        "source": "test",
        "truncated": False,
        "read_bytes": int(full_read_result["read_bytes"]),
    }
    full_artifacts = await task._trusted_workspace_artifacts_for_verifier({"artifact_refs": [full_ref]})
    assert "targeted_readbacks" not in full_artifacts[0]


@pytest.mark.asyncio
async def test_verifier_workspace_artifact_readback_uses_acceptance_locator_for_middle_section(tmp_path):
    agent = _create_agent("agent-task-verifier-acceptance-locator").use_workspace(tmp_path / "workspace")
    task = AgentTask(
        agent,
        task_id="verifier-acceptance-locator",
        goal="Produce a long final artifact with a verifiable middle section.",
        success_criteria=["The final artifact includes the target middle section."],
        workspace=tmp_path / "workspace",
    )
    filler = "\n".join(f"Filler paragraph {index}: " + ("x" * 80) for index in range(220))
    marker = "TARGET_MIDDLE_SECTION_MARKER"
    body = f"# Long Report\n\n{filler}\n\n## Target Middle Section\n\n{marker}\n"
    write_result = await task.workspace.write_file("final.md", body)
    read_result = await task.workspace.read_file("final.md", max_bytes=4000)
    ref = {
        "path": "final.md",
        "bytes": int(read_result["bytes"]),
        "sha256": read_result["sha256"],
        "media_type": write_result.get("media_type"),
        "content_kind": "text",
        "role": "workspace_artifact",
        "source": "test",
        "preview": str(read_result["content"]),
        "truncated": True,
        "read_bytes": int(read_result["read_bytes"]),
    }
    locator_items = await task._workspace_artifact_acceptance_locator_evidence_items(
        ref=ref,
        result={
            "acceptance_points": [
                {
                    "criterion": "Target middle section is present.",
                    "expected_anchor": "Target Middle Section",
                }
            ]
        },
        manifest={"path": "final.md"},
        source="test",
        content=body,
    )
    locator = next(item for item in locator_items if item["status"] == "ok")
    assert locator["byte_offset"] > 4000

    execution_meta = {
        "blocks": {
            "evidence": {
                "evidence_items": [
                    task._workspace_artifact_readback_evidence_item(ref),
                    locator,
                ],
            }
        }
    }
    await task._ensure_workspace_artifact_targeted_readback_evidence(
        execution_meta,
        task._cumulative_evidence_ledger(execution_meta),
    )
    targeted_items = [
        item
        for item in execution_meta["blocks"]["evidence"]["evidence_items"]
        if item["kind"] == "workspace_artifact.targeted_readback"
    ]
    assert any(
        item.get("provenance", {}).get("source_evidence_id") == locator["id"] and marker in item.get("body", "")
        for item in targeted_items
    )


@pytest.mark.asyncio
async def test_workspace_intermediate_artifact_stays_ref_backed_without_satisfying_final_contract(tmp_path):
    workspace = Agently.create_workspace(tmp_path / "workspace-artifact-intermediate")
    task = AgentTask.__new__(AgentTask)
    task.id = "workspace-artifact-intermediate"
    task.workspace = workspace
    task.diagnostics = {}
    task.options = {"agent_task": {"required_deliverables": [{"path": "deliverables/final.md"}]}}

    notes_body = "# Search Notes\n\n" + "\n".join(
        f"- Source note {index}: keep this large intermediate evidence cold." for index in range(20)
    )
    execution_meta = {"logs": {}}
    delivered = await task._deliver_workspace_artifact(
        {
            "artifact_markdown": notes_body,
            "artifact_manifest": {"path": "working/search-notes.md"},
            "evidence": ["Downloaded source snapshot and summarized it into working notes."],
        },
        plan={"deliverable_mode": "workspace_artifact"},
        execution_meta=execution_meta,
        source="test.workspace_artifact.intermediate",
    )

    bounded_read = await workspace.read_file("working/search-notes.md", max_bytes=80)

    assert delivered["file_refs"][0]["path"] == "working/search-notes.md"
    assert execution_meta["logs"]["artifact_refs"][0]["path"] == "working/search-notes.md"
    assert execution_meta["workspace_refs"]["agent_task_artifacts"][0]["path"] == "working/search-notes.md"
    assert bounded_read["ok"] is True
    assert bounded_read["truncated"] is True
    assert bounded_read["content"].startswith("# Search Notes")
    assert task._required_workspace_deliverables() == ["deliverables/final.md"]
    assert await task._missing_required_workspace_deliverables() == ["deliverables/final.md"]


@pytest.mark.asyncio
async def test_agent_task_workspace_artifact_delivery_reports_readback_failure(tmp_path):
    workspace = Agently.create_workspace(tmp_path / "workspace-artifact-readback-failure")

    class ReadbackFailingWorkspace:
        files_root = workspace.files_root

        async def write_file(self, *args: Any, **kwargs: Any) -> Any:
            return await workspace.write_file(*args, **kwargs)

        async def read_file(self, *args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("readback unavailable")

    task = AgentTask.__new__(AgentTask)
    task.id = "workspace-artifact-readback-failure"
    task.workspace = ReadbackFailingWorkspace()
    task.diagnostics = {}
    execution_meta = {"logs": {}}

    delivered = await task._deliver_workspace_artifact(
        {
            "artifact_markdown": "# Actual Report\n\nThis content was written but cannot be read back.",
            "artifact_manifest": {"path": "reports/final.md"},
        },
        plan={"deliverable_mode": "workspace_artifact"},
        execution_meta=execution_meta,
        source="test.workspace_artifact",
    )

    assert (workspace.files_root / "reports/final.md").is_file()
    assert delivered["file_refs"] == []
    assert "artifact_refs" not in execution_meta["logs"]
    assert delivered["workspace_artifact_delivery"]["status"] == "readback_failed"
    diagnostic = delivered["diagnostics"][0]
    assert diagnostic["code"] == "agent_task.workspace_artifact.readback_failed"
    assert "readback failed" in diagnostic["message"]
    assert "write_failed" not in json.dumps(DataFormatter.sanitize(delivered), ensure_ascii=False)
    assert task.diagnostics["workspace_artifact_delivery"][0]["status"] == "readback_failed"


@pytest.mark.asyncio
async def test_agent_task_workspace_artifact_delivery_prefers_complete_body(tmp_path):
    workspace = Agently.create_workspace(tmp_path / "workspace-artifact-complete-body")
    task = AgentTask.__new__(AgentTask)
    task.id = "workspace-artifact-complete-body"
    task.workspace = workspace
    task.diagnostics = {}

    full_body = "# Complete Report\n\n" + "\n".join(f"Section {index}: complete content." for index in range(20))
    delivered = await task._deliver_workspace_artifact(
        {
            "answer": full_body,
            "artifact_markdown": "# Short Report\n\nSee candidate_final_result for details.",
            "artifact_manifest": {"path": "reports/final.md"},
        },
        plan={"deliverable_mode": "workspace_artifact"},
        execution_meta={"logs": {}},
        source="test.workspace_artifact",
    )

    written = (workspace.files_root / "reports/final.md").read_text(encoding="utf-8")
    assert written == full_body
    assert delivered["workspace_artifact_delivery"]["content_key"] == "answer"
    assert delivered["file_refs"][0]["bytes"] == len(full_body.encode("utf-8"))
    assert delivered["answer"].startswith("Workspace artifact delivered at reports/final.md")
    assert delivered["artifact_preview"].startswith("# Complete Report")


@pytest.mark.asyncio
async def test_agent_task_workspace_artifact_delivery_compacts_manifest_sections(tmp_path):
    workspace = Agently.create_workspace(tmp_path / "workspace-artifact-compact-manifest")
    task = AgentTask.__new__(AgentTask)
    task.id = "workspace-artifact-compact-manifest"
    task.workspace = workspace
    task.diagnostics = {}

    long_section = "Section body.\n" * 500
    delivered = await task._deliver_workspace_artifact(
        {
            "artifact_manifest": {
                "path": "reports/final.md",
                "sections": [
                    {"id": "overview", "title": "Overview", "content": long_section},
                    "raw section text\n" * 200,
                ],
            },
        },
        plan={"deliverable_mode": "sectioned_workspace_artifact"},
        execution_meta={"logs": {}},
        source="test.workspace_artifact",
    )

    written = (workspace.files_root / "reports/final.md").read_text(encoding="utf-8")
    assert "Section body." in written
    first_section = delivered["artifact_manifest"]["sections"][0]
    second_section = delivered["artifact_manifest"]["sections"][1]
    assert "content" not in first_section
    assert first_section["omitted_content"][0]["field"] == "content"
    assert second_section["content_omitted"] is True
    assert delivered["artifact_preview"].startswith("## Overview")


@pytest.mark.asyncio
async def test_agent_task_workspace_artifact_delivery_does_not_write_plain_answer_without_mode(tmp_path):
    workspace = Agently.create_workspace(tmp_path / "workspace-artifact-no-implicit-answer")
    task = AgentTask.__new__(AgentTask)
    task.id = "workspace-artifact-no-implicit-answer"
    task.workspace = workspace
    task.diagnostics = {}

    delivered = await task._deliver_workspace_artifact(
        {"answer": "This is a control-card summary, not a deliverable body."},
        plan={},
        execution_meta={"logs": {}},
        source="test.workspace_artifact",
    )

    assert delivered["answer"] == "This is a control-card summary, not a deliverable body."
    assert delivered.get("file_refs") == []
    assert not (workspace.files_root / "final.md").exists()


@pytest.mark.asyncio
async def test_agent_task_workspace_artifact_delivery_waits_for_remaining_work(tmp_path):
    workspace = Agently.create_workspace(tmp_path / "workspace-artifact-remaining-work")
    task = AgentTask.__new__(AgentTask)
    task.id = "workspace-artifact-remaining-work"
    task.workspace = workspace
    task.diagnostics = {}

    delivered = await task._deliver_workspace_artifact(
        {
            "artifact_manifest": {"path": "reports/final.md"},
            "remaining_work": ["Read README.md before writing the final report."],
            "step_result": "Repository cloned; detailed source reading remains.",
        },
        plan={"deliverable_mode": "sectioned_workspace_artifact"},
        execution_meta={"logs": {}},
        source="test.workspace_artifact",
    )

    assert delivered["artifact_manifest"]["path"] == "reports/final.md"
    assert delivered["remaining_work"] == ["Read README.md before writing the final report."]
    assert delivered.get("file_refs") == []
    assert "workspace_artifact_delivery" not in delivered
    assert not (workspace.files_root / "reports/final.md").exists()


@pytest.mark.asyncio
async def test_agent_task_workspace_artifact_delivery_adopts_successful_action_written_file(tmp_path):
    workspace = Agently.create_workspace(tmp_path / "workspace-artifact-action-adopt")
    task = AgentTask.__new__(AgentTask)
    task.id = "workspace-artifact-action-adopt"
    task.workspace = workspace
    task.diagnostics = {}
    task.success_criteria = ["The final artifact is available through trusted Workspace readback."]
    task.options = {}

    body = "# Existing Action Report\n\nThis file was written by a Workspace action before execution stalled.\n"
    await workspace.write_file("final.md", body, append=False)
    execution_meta = {
        "status": "failed",
        "logs": {
            "action_logs": [
                {
                    "action_id": "write_file",
                    "status": "success",
                    "result_preview": {
                        "ok": True,
                        "mode": "write",
                        "path": "final.md",
                        "file_refs": [{"path": "final.md", "role": "output"}],
                    },
                    "file_refs": [{"path": "final.md", "role": "output"}],
                }
            ]
        },
        "diagnostics": {
            "errors": [
                {
                    "error_type": "RuntimeStageStallError",
                    "message": "AgentExecution made no progress before idle deadline.",
                    "stage": "action_loop_close",
                }
            ]
        },
    }

    delivered = await task._deliver_workspace_artifact(
        {
            "artifact_manifest": {"path": "final.md"},
            "remaining_work": ["Retry or replan after execution failure."],
            "ready_for_final_verification": False,
            "step_result": "",
        },
        plan={"deliverable_mode": "workspace_artifact"},
        execution_meta=execution_meta,
        source="agent_task.iteration.2.workspace_artifact",
    )

    assert delivered["workspace_artifact_delivery"]["status"] == "adopted_existing"
    assert delivered["workspace_artifact_delivery"]["content_key"] == "action_file_ref"
    assert delivered["workspace_artifact_delivery"]["readback"]["path"] == "final.md"
    assert delivered["file_refs"][0]["path"] == "final.md"
    assert delivered["file_refs"][0]["sha256"]
    assert delivered["artifact_preview"].startswith("# Existing Action Report")
    assert delivered["remaining_work"] == []
    assert delivered["ready_for_final_verification"] is True
    assert delivered["workspace_artifact_remaining_work_handoff"]["status"] == "handed_to_terminal_verification"
    assert delivered["diagnostics"][-1]["code"] == "agent_task.workspace_artifact.action_file_adopted"
    assert execution_meta["logs"]["artifact_refs"][0]["path"] == "final.md"
    ledger_items = execution_meta["blocks"]["evidence"]["evidence_items"]
    assert any(item["kind"] == "workspace_artifact.readback" and item["path"] == "final.md" for item in ledger_items)


@pytest.mark.asyncio
async def test_agent_task_workspace_artifact_delivery_does_not_adopt_missing_action_file(tmp_path):
    workspace = Agently.create_workspace(tmp_path / "workspace-artifact-action-missing")
    task = AgentTask.__new__(AgentTask)
    task.id = "workspace-artifact-action-missing"
    task.workspace = workspace
    task.diagnostics = {}
    task.options = {}

    execution_meta = {
        "logs": {
            "action_logs": [
                {
                    "action_id": "write_file",
                    "status": "success",
                    "result_preview": {
                        "ok": True,
                        "mode": "write",
                        "path": "final.md",
                        "file_refs": [{"path": "final.md", "role": "output"}],
                    },
                }
            ]
        }
    }

    delivered = await task._deliver_workspace_artifact(
        {
            "artifact_manifest": {"path": "final.md"},
            "remaining_work": ["Retry or replan after execution failure."],
            "step_result": "",
        },
        plan={"deliverable_mode": "workspace_artifact"},
        execution_meta=execution_meta,
        source="agent_task.iteration.2.workspace_artifact",
    )

    assert delivered.get("file_refs") == []
    assert delivered["remaining_work"] == ["Retry or replan after execution failure."]
    assert "workspace_artifact_delivery" not in delivered
    diagnostic_codes = [item["code"] for item in delivered["diagnostics"]]
    assert "agent_task.workspace_artifact.action_file_readback_failed" in diagnostic_codes
    assert "agent_task.workspace_artifact.awaiting_body" in diagnostic_codes


@pytest.mark.asyncio
async def test_agent_task_workspace_artifact_delivery_uses_full_markdown_body_from_evidence(tmp_path):
    workspace = Agently.create_workspace(tmp_path / "workspace-artifact-evidence-body")
    task = AgentTask.__new__(AgentTask)
    task.id = "workspace-artifact-evidence-body"
    task.workspace = workspace
    task.diagnostics = {}

    full_body = "# Corrected Report\n\nThis is the complete file body from structured evidence.\n"
    delivered = await task._deliver_workspace_artifact(
        {
            "artifact_manifest": {"path": "reports/final.md", "sections": [{"id": "report"}]},
            "evidence": [
                "Source facts were gathered.",
                f"Corrected reports/final.md content (full body):\n\n{full_body}",
            ],
            "remaining_work": [
                "Write the corrected reports/final.md Workspace artifact using the full body content provided in evidence."
            ],
            "ready_for_final_verification": False,
        },
        plan={"deliverable_mode": "sectioned_workspace_artifact"},
        execution_meta={"logs": {}},
        source="test.workspace_artifact.evidence_body",
    )

    written = (workspace.files_root / "reports/final.md").read_text(encoding="utf-8")
    assert written == full_body.strip()
    assert delivered["workspace_artifact_delivery"]["status"] == "delivered"
    assert delivered["workspace_artifact_delivery"]["content_key"] == "evidence[1]"
    assert delivered["workspace_artifact_delivery"]["remaining_work_handoff"]["status"] == (
        "handed_to_terminal_verification"
    )
    assert delivered["remaining_work"] == []
    assert delivered["ready_for_final_verification"] is True
    assert delivered["evidence"][1].startswith("Workspace artifact delivered at reports/final.md")
    assert delivered["workspace_artifact_content_omitted"][0]["field"] == "evidence[1]"
    assert delivered["diagnostics"][-1]["code"] == (
        "agent_task.workspace_artifact.remaining_work_handed_to_verifier"
    )


@pytest.mark.asyncio
async def test_agent_task_workspace_artifact_delivery_ignores_non_body_evidence_snippets(tmp_path):
    workspace = Agently.create_workspace(tmp_path / "workspace-artifact-evidence-snippet")
    task = AgentTask.__new__(AgentTask)
    task.id = "workspace-artifact-evidence-snippet"
    task.workspace = workspace
    task.diagnostics = {}

    delivered = await task._deliver_workspace_artifact(
        {
            "artifact_manifest": {"path": "reports/final.md", "sections": [{"id": "report"}]},
            "evidence": [
                "Source excerpt:\n\n# Not The Deliverable\n\nThis is a source page title, not an artifact body.",
                {"content": "# Still only an untyped snippet\n\nNo artifact role or path marks this as a body."},
            ],
            "remaining_work": ["Read README.md before writing the final report."],
        },
        plan={"deliverable_mode": "sectioned_workspace_artifact"},
        execution_meta={"logs": {}},
        source="test.workspace_artifact.evidence_snippet",
    )

    assert delivered["artifact_manifest"]["path"] == "reports/final.md"
    assert delivered["remaining_work"] == ["Read README.md before writing the final report."]
    assert delivered.get("file_refs") == []
    assert "workspace_artifact_delivery" not in delivered
    assert not (workspace.files_root / "reports/final.md").exists()


@pytest.mark.asyncio
async def test_agent_task_workspace_artifact_delivery_preserves_existing_full_body(tmp_path):
    workspace = Agently.create_workspace(tmp_path / "workspace-artifact-preserve-body")
    task = AgentTask.__new__(AgentTask)
    task.id = "workspace-artifact-preserve-body"
    task.workspace = workspace
    task.diagnostics = {}

    full_body = "# Complete Report\n\n" + "\n".join(f"Section {index}: complete content." for index in range(80))
    first = await task._deliver_workspace_artifact(
        {
            "answer": full_body,
            "artifact_manifest": {"path": "reports/final.md"},
        },
        plan={"deliverable_mode": "workspace_artifact"},
        execution_meta={"logs": {}},
        source="test.workspace_artifact.initial",
    )
    short_control_note = "Existing final.md already satisfies the output contract."
    second = await task._deliver_workspace_artifact(
        {
            "artifact_markdown": short_control_note,
            "artifact_manifest": {"path": "reports/final.md"},
        },
        plan={"deliverable_mode": "workspace_artifact"},
        execution_meta={"logs": {}},
        source="test.workspace_artifact.followup",
    )

    written = (workspace.files_root / "reports/final.md").read_text(encoding="utf-8")
    assert written == full_body
    assert first["file_refs"][0]["sha256"] == second["file_refs"][0]["sha256"]
    assert second["workspace_artifact_delivery"]["status"] == "preserved_existing"
    assert second["workspace_artifact_delivery"]["content_key"] == "artifact_markdown"
    assert second["diagnostics"][0]["code"] == "agent_task.workspace_artifact.preserved_existing"


@pytest.mark.asyncio
async def test_agent_task_workspace_artifact_outline_sections_trigger_stream_draft(tmp_path):
    workspace = Agently.create_workspace(tmp_path / "workspace-artifact-outline-sections")
    task = AgentTask.__new__(AgentTask)
    task.id = "workspace-artifact-outline-sections"
    task.goal = "Revise a source-grounded artifact."
    task.success_criteria = ["The final artifact cites only supported sources."]
    task.execution_strategy = "flat"
    task.workspace = workspace
    task.diagnostics = {}

    old_body = "# Complete Report\n\nUnsupported source: https://example.test/old\n" + (
        "Existing body.\n" * 200
    )
    replacement_body = "# Complete Report\n\nSupported source: https://example.test/source\n"
    await workspace.write_file("reports/final.md", old_body)

    async def fake_stream_workspace_artifact_draft(**kwargs: Any) -> dict[str, Any]:
        path = kwargs["path"]
        await workspace.write_file(path, replacement_body, append=False)
        readback = await workspace.read_file(path)
        ref = {
            "path": readback["path"],
            "bytes": readback["bytes"],
            "sha256": readback["sha256"],
            "media_type": readback.get("media_type"),
            "content_kind": readback.get("content_kind", "text"),
            "role": "workspace_artifact",
            "source": kwargs.get("source"),
            "preview": readback["content"],
            "truncated": False,
            "read_bytes": readback["bytes"],
            "handler_id": readback.get("handler_id"),
        }
        return {
            "source": kwargs.get("source"),
            "path": path,
            "status": "delivered",
            "mode": "streamed_workspace_artifact",
            "file_refs": [ref],
        }

    task._stream_workspace_artifact_draft = fake_stream_workspace_artifact_draft

    delivered = await task._deliver_workspace_artifact(
        {
            "artifact_manifest": {
                "path": "reports/final.md",
                "sections": [
                    "official source references",
                    "syllabus boundary",
                    "mock questions",
                    "answer key",
                ],
            },
            "evidence": ["Outline is ready; framework should draft the body."],
            "remaining_work": [],
        },
        plan={"deliverable_mode": "workspace_artifact"},
        execution_meta={"logs": {}},
        source="test.workspace_artifact.outline",
    )

    written = (workspace.files_root / "reports/final.md").read_text(encoding="utf-8")
    assert written == replacement_body
    assert delivered["workspace_artifact_delivery"]["mode"] == "streamed_workspace_artifact"
    assert delivered["workspace_artifact_delivery"]["status"] == "delivered"
    assert delivered["file_refs"][0]["bytes"] == len(replacement_body.encode("utf-8"))


@pytest.mark.asyncio
async def test_workspace_artifact_outline_with_remaining_verification_still_drafts(tmp_path):
    workspace = Agently.create_workspace(tmp_path / "workspace-artifact-outline-remaining")
    task = AgentTask.__new__(AgentTask)
    task.id = "workspace-artifact-outline-remaining"
    task.goal = "Write a file-backed report."
    task.success_criteria = ["The final artifact is written and read back."]
    task.execution_strategy = "flat"
    task.workspace = workspace
    task.diagnostics = {}

    body = "# Final Report\n\nThe body was drafted from the section outline.\n"

    async def fake_stream_workspace_artifact_draft(**kwargs: Any) -> dict[str, Any]:
        path = kwargs["path"]
        await workspace.write_file(path, body, append=False)
        readback = await workspace.read_file(path)
        return {
            "source": kwargs.get("source"),
            "path": path,
            "status": "delivered",
            "mode": "streamed_workspace_artifact",
            "file_refs": [
                {
                    "path": readback["path"],
                    "bytes": readback["bytes"],
                    "sha256": readback["sha256"],
                    "media_type": readback.get("media_type"),
                    "content_kind": readback.get("content_kind", "text"),
                    "role": "workspace_artifact",
                    "source": kwargs.get("source"),
                    "preview": readback["content"],
                    "truncated": False,
                    "read_bytes": readback["bytes"],
                    "handler_id": readback.get("handler_id"),
                }
            ],
        }

    task._stream_workspace_artifact_draft = fake_stream_workspace_artifact_draft

    delivered = await task._deliver_workspace_artifact(
        {
            "artifact_manifest": {
                "path": "final.md",
                "section_outline": [
                    "data boundary",
                    "ticker snapshot",
                    "source list",
                ],
            },
            "evidence": ["The required evidence has already been collected."],
            "remaining_work": ["Verify final.md content via readback."],
            "ready_for_final_verification": False,
        },
        plan={"deliverable_mode": "sectioned_workspace_artifact"},
        execution_meta={"logs": {}},
        source="test.workspace_artifact.outline_remaining",
    )

    written = (workspace.files_root / "final.md").read_text(encoding="utf-8")
    assert written == body
    assert delivered["workspace_artifact_delivery"]["mode"] == "streamed_workspace_artifact"
    assert delivered["workspace_artifact_delivery"]["remaining_work_handoff"]["status"] == (
        "handed_to_terminal_verification"
    )
    assert delivered["remaining_work"] == []
    assert delivered["ready_for_final_verification"] is True


@pytest.mark.asyncio
async def test_agent_task_flat_workspace_artifact_delivery_before_verification(tmp_path):
    class WorkspaceArtifactRequester(MockAgentTaskRequester):
        name = "WorkspaceArtifactRequester"
        verify_text = ""

        async def request_model(self, request_data: AgentlyRequestData):
            text = json.dumps(DataFormatter.sanitize(request_data.data), ensure_ascii=False)
            if "Verify the task against every success criterion" in text:
                self.__class__.verify_text = text
                assert "reports/final.md" in text
                assert "file_refs" in text
                assert "artifact_preview" in text
                assert "Delivered Report" in text
                assert "capability_evidence" in text
                assert "artifacts" in text
                assert "readback" in text
                assert "sha256" not in text
                assert "agent_task.workspace_artifact.untrusted_model_file_refs" in text
                payload = {
                    "is_complete": True,
                    "requires_block": False,
                    "reason": "trusted Workspace readback evidence is present",
                    "missing_criteria": [],
                    "replan_instruction": "",
                    "final_result_required": True,
                    "final_result": "Delivered final report at reports/final.md.",
                }
            elif "Plan the next bounded AgentExecution step" in text:
                payload = {
                    "execution_shape": "direct",
                    "step_instruction": "produce the final report body for framework Workspace delivery",
                    "expected_evidence": "trusted Workspace artifact readback",
                    "rationale": "the task requires a file deliverable",
                    "deliverable_mode": "workspace_artifact",
                }
            elif "Execute exactly one bounded step" in text:
                payload = {
                    "step_result": "prepared final report body",
                    "artifact_markdown": "# Delivered Report\n\nThe framework must write this report.",
                    "artifact_manifest": {
                        "path": "reports/final.md",
                        "file_refs": [{"path": "test fake ref"}],
                    },
                    "file_refs": [{"path": "test fake ref"}],
                    "evidence": ["report body is ready for framework delivery"],
                    "remaining_work": [],
                }
            else:
                payload = {
                    "step_result": "Direct bounded step returned value ok.",
                    "candidate_final_result": "Bounded step completed with value ok.",
                    "evidence": ["value ok was produced by the direct bounded step."],
                    "remaining_work": [],
                }
            yield "message", json.dumps(payload, ensure_ascii=False)

    settings = Settings(name="agent-task-workspace-artifact-settings", parent=Agently.settings)
    plugin_manager = PluginManager(
        settings, parent=Agently.plugin_manager, name="agent-task-workspace-artifact-plugins"
    )
    plugin_manager.register("ModelRequester", WorkspaceArtifactRequester, activate=True)
    agent = Agently.AgentType(plugin_manager, parent_settings=settings, name="agent-task-workspace-artifact")
    task = agent.create_task(
        task_id="workspace-artifact-flat",
        goal="Create the final report as a Workspace artifact.",
        success_criteria=["A final report file is written and read back."],
        workspace=tmp_path / "task-workspace",
        max_iterations=1,
    )

    result = await task.run()
    meta = await task.meta()

    assert result["status"] == "completed"
    task_scoped_workspace = Agently.create_workspace(tmp_path / "task-workspace").with_scope_node(
        "tasks",
        "workspace-artifact-flat",
    )
    assert (await task_scoped_workspace.read_file("reports/final.md"))["content"].startswith("# Delivered Report")
    delivery = meta["diagnostics"]["workspace_artifact_delivery"][0]
    assert delivery["status"] == "delivered"
    assert delivery["file_refs"][0]["path"] == "reports/final.md"
    assert delivery["file_refs"][0]["bytes"] > 0
    assert delivery["file_refs"][0]["sha256"]
    assert delivery["file_refs"][0]["preview"].startswith("# Delivered Report")
    assert meta["iterations"][0]["verification"]["reason"] == "trusted Workspace readback evidence is present"
    assert delivery["file_refs"][0]["sha256"][:12] not in WorkspaceArtifactRequester.verify_text
    assert "reports/final.md#" not in WorkspaceArtifactRequester.verify_text


def test_artifact_readback_evidence_ids_omit_sha_from_model_hot_key():
    readback_ids = AgentTask._artifact_readback_evidence_ids(
        [
            {
                "path": "reports/final.md",
                "bytes": 120,
                "sha256": "a" * 64,
                "role": "workspace_artifact",
            }
        ]
    )

    assert readback_ids == ["reports/final.md"]


@pytest.mark.asyncio
async def test_verification_accepts_trusted_workspace_artifact_without_inline_final_result(tmp_path):
    class WorkspaceArtifactPointerRequester(MockAgentTaskRequester):
        name = "WorkspaceArtifactPointerRequester"
        tail_marker = "TRUSTED_WORKSPACE_ARTIFACT_TAIL_MARKER"
        verify_text = ""

        async def request_model(self, request_data: AgentlyRequestData):
            text = json.dumps(DataFormatter.sanitize(request_data.data), ensure_ascii=False)
            if "Verify the task against every success criterion" in text:
                self.__class__.verify_text = text
                assert "trusted_workspace_artifacts" in text
                assert "source-grounded Workspace artifacts" in text
                assert "trusted_workspace_artifacts.readback.content" in text
                assert "acceptance-point evidence review" in text
                assert "whole-document editorial review" in text
                assert "evidence_ledger" in text
                assert "reports/final.md" in text
                assert "https://example.test/source" in text
                payload = {
                    "is_complete": True,
                    "requires_block": False,
                    "reason": "trusted Workspace artifact readback satisfies the deliverable",
                    "missing_criteria": [],
                    "replan_instruction": "",
                    "final_result_required": True,
                    "final_result": "",
                }
            elif "Plan the next bounded AgentExecution step" in text:
                payload = {
                    "execution_shape": "direct",
                    "step_instruction": "produce the long, sectioned deliverable as a Workspace artifact",
                    "expected_evidence": "trusted Workspace artifact readback",
                    "rationale": "the task requires a file-backed deliverable",
                    "deliverable_mode": "workspace_artifact",
                }
            elif "Execute exactly one bounded step" in text:
                long_body = (
                    "# Delivered Report\n\n"
                    + "Source: https://example.test/source\n\n"
                    + ("This section is intentionally long so the default hot preview is insufficient.\n" * 90)
                    + f"\n{self.__class__.tail_marker}\n"
                )
                payload = {
                    "step_result": "prepared long, sectioned deliverable body",
                    "artifact_markdown": long_body,
                    "artifact_manifest": {"path": "reports/final.md"},
                    "evidence": ["long, sectioned deliverable body is ready for framework delivery"],
                    "remaining_work": [],
                }
            else:
                payload = {"answer": "ok"}
            yield "message", json.dumps(payload, ensure_ascii=False)

    settings = Settings(name="agent-task-workspace-artifact-pointer-settings", parent=Agently.settings)
    plugin_manager = PluginManager(
        settings,
        parent=Agently.plugin_manager,
        name="agent-task-workspace-artifact-pointer-plugins",
    )
    plugin_manager.register("ModelRequester", WorkspaceArtifactPointerRequester, activate=True)
    agent = Agently.AgentType(
        plugin_manager,
        parent_settings=settings,
        name="agent-task-workspace-artifact-pointer",
    )
    task = agent.create_task(
        task_id="workspace-artifact-pointer",
        goal="Create the final report as a Workspace artifact.",
        success_criteria=[
            "A final report file is written and read back.",
            "The final artifact includes concrete source URLs for source-grounded claims.",
        ],
        workspace=tmp_path / "task-workspace",
        max_iterations=1,
    )

    result = await task.run()
    meta = await task.meta()

    assert result["status"] == "completed"
    assert result["final_result"].startswith("Workspace artifact delivered at reports/final.md")
    verification = meta["iterations"][0]["verification"]
    assert verification["is_complete"] is True
    assert verification["final_result_via_workspace_artifact"] is True
    assert "final_result_missing" not in verification.get("guard_reasons", [])
    assert WorkspaceArtifactPointerRequester.tail_marker not in WorkspaceArtifactPointerRequester.verify_text


@pytest.mark.asyncio
async def test_agent_task_workspace_artifact_refs_survive_incomplete_verification(tmp_path):
    class WorkspaceArtifactPartialRequester(MockAgentTaskRequester):
        name = "WorkspaceArtifactPartialRequester"

        async def request_model(self, request_data: AgentlyRequestData):
            text = json.dumps(DataFormatter.sanitize(request_data.data), ensure_ascii=False)
            if "Verify the task against every success criterion" in text:
                assert "reports/partial.md" in text
                payload = {
                    "is_complete": False,
                    "requires_block": False,
                    "reason": "real Workspace artifact exists but source coverage is incomplete",
                    "failure_analysis": "The artifact was written, but verification still needs stronger evidence.",
                    "acceptance_delta": ["Add stronger cited evidence before final acceptance."],
                    "missing_criteria": ["stronger cited evidence"],
                    "replan_instruction": "Gather stronger cited evidence and update the artifact.",
                    "final_result_required": True,
                    "final_result": "",
                }
            elif "Plan the next bounded AgentExecution step" in text:
                payload = {
                    "execution_shape": "direct",
                    "step_instruction": "draft the report as a sectioned Workspace artifact manifest",
                    "expected_evidence": "Workspace write/readback and cited evidence",
                    "rationale": "the task requires a file deliverable",
                    "deliverable_mode": "sectioned_workspace_artifact",
                }
            elif "Execute exactly one bounded step" in text:
                payload = {
                    "step_result": "prepared a sectioned artifact manifest",
                    "artifact_manifest": {
                        "path": "reports/partial.md",
                        "sections": [
                            {
                                "title": "概览",
                                "content": "这是一份仍需补证据的报告草稿。",
                            },
                            {
                                "title": "待补充证据",
                                "content": "后续步骤需要补充真实引用后才能验收。",
                            },
                        ],
                        "file_refs": [{"path": "fake-partial.md", "sha256": "fake"}],
                    },
                    "file_refs": [{"path": "model-claimed.md", "sha256": "fake"}],
                    "evidence": ["draft content is ready for framework delivery"],
                    "remaining_work": ["stronger cited evidence"],
                    "ready_for_final_verification": True,
                }
            else:
                payload = {"answer": "ok"}
            yield "message", json.dumps(payload, ensure_ascii=False)

    settings = Settings(name="agent-task-workspace-artifact-partial-settings", parent=Agently.settings)
    plugin_manager = PluginManager(
        settings, parent=Agently.plugin_manager, name="agent-task-workspace-artifact-partial-plugins"
    )
    plugin_manager.register("ModelRequester", WorkspaceArtifactPartialRequester, activate=True)
    agent = Agently.AgentType(plugin_manager, parent_settings=settings, name="agent-task-workspace-artifact-partial")
    task = agent.create_task(
        task_id="workspace-artifact-partial",
        goal="Create a report file and verify it against cited evidence.",
        success_criteria=["A final report file is written.", "The report has stronger cited evidence."],
        workspace=tmp_path / "task-workspace",
        max_iterations=1,
    )

    result = await task.run()
    meta = await task.meta()

    assert result["status"] == "max_iterations"
    assert result["accepted"] is False
    assert result["artifact_status"] == "partial"
    delivery = meta["diagnostics"]["workspace_artifact_delivery"][0]
    assert delivery["status"] == "delivered"
    assert delivery["file_refs"][0]["path"] == "reports/partial.md"
    assert (
        meta["iterations"][0]["verification"]["reason"]
        == "real Workspace artifact exists but source coverage is incomplete"
    )
    task_scoped_workspace = Agently.create_workspace(tmp_path / "task-workspace").with_scope_node(
        "tasks",
        "workspace-artifact-partial",
    )
    readback = await task_scoped_workspace.read_file("reports/partial.md")
    assert "这是一份仍需补证据的报告草稿" in readback["content"]
    assert "## 待补充证据" in readback["content"]


@pytest.mark.asyncio
async def test_agent_task_workspace_artifact_stream_draft_when_step_returns_no_body(tmp_path):
    class WorkspaceArtifactStreamDraftRequester(MockAgentTaskRequester):
        name = "WorkspaceArtifactStreamDraftRequester"

        async def request_model(self, request_data: AgentlyRequestData):
            text = json.dumps(DataFormatter.sanitize(request_data.data), ensure_ascii=False)
            if "Write only the final Markdown artifact body." in text:
                yield "message", "# Partial attempt that must be discarded\n\n"
                yield "status", {
                    "status": "failed",
                    "attempt_index": 1,
                    "retry": True,
                    "next_attempt_index": 2,
                    "reason": "transient provider disconnect",
                }
                yield "message", "# Streamed Report\n\n"
                yield "message", "This body was written through the framework artifact draft stream.\n"
                return
            if "Verify the task against every success criterion" in text:
                assert "reports/final.md" in text
                assert "agent_task.workspace_artifact.stream_drafted" in text
                payload = {
                    "is_complete": True,
                    "requires_block": False,
                    "reason": "trusted streamed Workspace artifact readback is present",
                    "missing_criteria": [],
                    "replan_instruction": "",
                    "final_result": "Delivered final report at reports/final.md.",
                }
            elif "Plan the next bounded AgentExecution step" in text:
                payload = {
                    "execution_shape": "direct",
                    "step_instruction": "prepare the final report; framework may stream-draft the file body if needed",
                    "expected_evidence": "trusted Workspace artifact readback",
                    "rationale": "the task requires a file deliverable",
                    "deliverable_mode": "sectioned_workspace_artifact",
                }
            elif "Execute exactly one bounded step" in text:
                payload = {
                    "step_result": "ready to generate reports/final.md as a Workspace artifact",
                    "answer": "A short control summary; the artifact_manifest describes the real deliverable.",
                    "candidate_final_result": "STRUCTURED BODY SHOULD NOT BE WRITTEN",
                    "artifact_manifest": {
                        "path": "final.md",
                        "sections": [
                            {"id": "report", "title": "Final report", "intent": "Write the complete deliverable."}
                        ],
                    },
                    "evidence": ["source evidence has been collected"],
                    "remaining_work": [],
                }
            else:
                payload = {"answer": "ok"}
            yield "message", json.dumps(payload, ensure_ascii=False)

        async def broadcast_response(
            self,
            response_generator: AsyncGenerator[tuple[str, object], None],
        ):
            response_text = ""
            async for event, data in response_generator:
                if event == "message":
                    response_text += str(data)
                    yield "delta", str(data)
                elif event == "status":
                    yield "status", data
            yield "done", response_text

    settings = Settings(name="agent-task-workspace-artifact-stream-draft-settings", parent=Agently.settings)
    plugin_manager = PluginManager(
        settings, parent=Agently.plugin_manager, name="agent-task-workspace-artifact-stream-draft-plugins"
    )
    plugin_manager.register("ModelRequester", WorkspaceArtifactStreamDraftRequester, activate=True)
    agent = Agently.AgentType(
        plugin_manager, parent_settings=settings, name="agent-task-workspace-artifact-stream-draft"
    )
    task = agent.create_task(
        task_id="workspace-artifact-stream-draft",
        goal="Create the final report as a Workspace artifact.",
        success_criteria=["A final report file is written and read back."],
        workspace=tmp_path / "task-workspace",
        max_iterations=1,
    )

    result = await task.run()
    meta = await task.meta()

    assert result["status"] == "completed"
    task_scoped_workspace = Agently.create_workspace(tmp_path / "task-workspace").with_scope_node(
        "tasks",
        "workspace-artifact-stream-draft",
    )
    readback = await task_scoped_workspace.read_file("final.md")
    assert "Streamed Report" in readback["content"]
    assert "<$retry>" not in readback["content"]
    assert "Partial attempt" not in readback["content"]
    assert "STRUCTURED BODY SHOULD NOT BE WRITTEN" not in readback["content"]
    delivery = meta["diagnostics"]["workspace_artifact_delivery"][0]
    assert delivery["status"] == "delivered"
    assert delivery["mode"] == "streamed_workspace_artifact"
    assert delivery["retry_boundaries"][0]["source"] == "structured_status"
    assert delivery["retry_boundaries"][0]["reason"] == "transient provider disconnect"
    assert delivery["file_refs"][0]["path"] == "final.md"
    assert meta["iterations"][0]["verification"]["reason"] == "trusted streamed Workspace artifact readback is present"


@pytest.mark.asyncio
async def test_workspace_artifact_stream_draft_consumes_public_retry_marker_without_writing(tmp_path):
    class WorkspaceArtifactDraftPublicMarkerRequester(MockAgentTaskRequester):
        name = "WorkspaceArtifactDraftPublicMarkerRequester"

        async def request_model(self, request_data: AgentlyRequestData):
            text = json.dumps(DataFormatter.sanitize(request_data.data), ensure_ascii=False)
            if "Write only the final Markdown artifact body." in text:
                yield "message", "# Partial attempt that must be discarded\n\n"
                yield "message", "<$retry>transient provider disconnect</$retry>"
                yield "message", "# Streamed Report\n\n"
                yield "message", "This body was written after the public retry marker.\n"
                return
            yield "message", json.dumps({"answer": "unused"}, ensure_ascii=False)

    settings = Settings(name="agent-task-workspace-artifact-public-marker-settings", parent=Agently.settings)
    plugin_manager = PluginManager(
        settings,
        parent=Agently.plugin_manager,
        name="agent-task-workspace-artifact-public-marker-plugins",
    )
    plugin_manager.register("ModelRequester", WorkspaceArtifactDraftPublicMarkerRequester, activate=True)
    agent = Agently.AgentType(
        plugin_manager,
        parent_settings=settings,
        name="agent-task-workspace-artifact-public-marker",
    )
    task = AgentTask(
        agent,
        task_id="workspace-artifact-public-marker",
        goal="Create the final report as a Workspace artifact.",
        success_criteria=["A final report file is written and read back."],
        workspace=tmp_path / "task-workspace",
    )

    delivery = await task._stream_workspace_artifact_draft(
        path="final.md",
        plan={"deliverable_mode": "workspace_artifact"},
        execution_result={"artifact_manifest": {"path": "final.md"}},
        execution_meta={"status": "completed", "logs": {"action_logs": [], "route_logs": {}}},
        source="test.workspace_artifact_draft.public_marker",
        context_pack=None,
        iteration_index=1,
    )

    assert delivery is not None
    assert delivery["status"] == "delivered"
    assert delivery.get("retry_boundaries", []) == []
    assert delivery["public_replay_markers"][0]["source"] == "delta_replay_marker"
    assert delivery["public_replay_markers"][0]["reason"] == "transient provider disconnect"
    readback = await task.workspace.read_file("final.md")
    assert "Streamed Report" in readback["content"]
    assert "This body was written after the public retry marker." in readback["content"]
    assert "<$retry>" not in readback["content"]
    assert "Partial attempt" not in readback["content"]


@pytest.mark.asyncio
async def test_workspace_artifact_stream_draft_receives_cumulative_evidence_anchors(tmp_path):
    class WorkspaceArtifactDraftEvidenceRequester(MockAgentTaskRequester):
        name = "WorkspaceArtifactDraftEvidenceRequester"
        draft_text = ""

        async def request_model(self, request_data: AgentlyRequestData):
            text = json.dumps(DataFormatter.sanitize(request_data.data), ensure_ascii=False)
            if "Write only the final Markdown artifact body." in text:
                self.__class__.draft_text = text
                assert "cumulative_evidence_anchors" in text
                assert "https://example.test/exact-source" in text
                assert "Evidence-backed source snippet" in text
                yield "message", "# Evidence Draft\n\nSource: https://example.test/exact-source\n"
                return
            yield "message", json.dumps({"answer": "unused"}, ensure_ascii=False)

    settings = Settings(name="agent-task-workspace-artifact-draft-evidence-settings", parent=Agently.settings)
    plugin_manager = PluginManager(
        settings,
        parent=Agently.plugin_manager,
        name="agent-task-workspace-artifact-draft-evidence-plugins",
    )
    plugin_manager.register("ModelRequester", WorkspaceArtifactDraftEvidenceRequester, activate=True)
    agent = Agently.AgentType(
        plugin_manager,
        parent_settings=settings,
        name="agent-task-workspace-artifact-draft-evidence",
    )
    task = AgentTask(
        agent,
        task_id="workspace-artifact-draft-evidence",
        goal="Write a source-grounded Workspace artifact.",
        success_criteria=["The final artifact cites exact source URLs."],
        workspace=tmp_path / "task-workspace",
    )
    task.iterations.append(
        {
            "iteration": 1,
            "execution_meta": {
                "status": "completed",
                "logs": {
                    "action_logs": [
                        {
                            "action_id": "web_search",
                            "status": "success",
                            "action_call_id": "call-source",
                            "model_digest": {
                                "result_preview": [
                                    {
                                        "title": "Exact source",
                                        "href": "https://example.test/exact-source",
                                        "body": "Evidence-backed source snippet.",
                                    }
                                ],
                                "result_preview_meta": {"truncated": False},
                            },
                        }
                    ],
                    "route_logs": {},
                },
            },
        }
    )

    delivery = await task._stream_workspace_artifact_draft(
        path="final.md",
        plan={"deliverable_mode": "workspace_artifact"},
        execution_result={"artifact_manifest": {"path": "final.md"}},
        execution_meta={"status": "completed", "logs": {"action_logs": [], "route_logs": {}}},
        source="test.workspace_artifact_draft.evidence",
        context_pack=None,
        iteration_index=2,
    )

    assert delivery is not None
    assert delivery["status"] == "delivered"
    assert "https://example.test/exact-source" in WorkspaceArtifactDraftEvidenceRequester.draft_text
    readback = await task.workspace.read_file("final.md")
    assert "https://example.test/exact-source" in readback["content"]


@pytest.mark.asyncio
async def test_workspace_artifact_stream_draft_disables_action_loop(tmp_path, monkeypatch):
    class WorkspaceArtifactDraftNoActionRequester(MockAgentTaskRequester):
        name = "WorkspaceArtifactDraftNoActionRequester"

        async def request_model(self, request_data: AgentlyRequestData):
            text = json.dumps(DataFormatter.sanitize(request_data.data), ensure_ascii=False)
            if "Write only the final Markdown artifact body." in text:
                yield "message", "# Draft Without Actions\n\nThe draft used existing evidence only.\n"
                return
            yield "message", json.dumps({"answer": "unused"}, ensure_ascii=False)

    settings = Settings(name="agent-task-workspace-artifact-draft-no-action-settings", parent=Agently.settings)
    plugin_manager = PluginManager(
        settings,
        parent=Agently.plugin_manager,
        name="agent-task-workspace-artifact-draft-no-action-plugins",
    )
    plugin_manager.register("ModelRequester", WorkspaceArtifactDraftNoActionRequester, activate=True)
    agent = Agently.AgentType(
        plugin_manager,
        parent_settings=settings,
        name="agent-task-workspace-artifact-draft-no-action",
    )

    def browse(query: str = "") -> str:
        return f"unexpected browse: {query}"

    agent.use_actions(browse, always=True)
    calls = 0

    async def fail_if_action_loop_runs(**kwargs: Any):
        nonlocal calls
        _ = kwargs
        calls += 1
        raise AssertionError("workspace artifact draft must not run the action loop")

    monkeypatch.setattr(agent.action, "async_plan_and_execute", fail_if_action_loop_runs)

    task = AgentTask(
        agent,
        task_id="workspace-artifact-draft-no-action",
        goal="Write a Workspace artifact from existing evidence.",
        success_criteria=["The final artifact is written without extra actions."],
        workspace=tmp_path / "task-workspace",
    )

    delivery = await task._stream_workspace_artifact_draft(
        path="final.md",
        plan={"deliverable_mode": "workspace_artifact"},
        execution_result={"artifact_manifest": {"path": "final.md"}},
        execution_meta={"status": "completed", "logs": {"action_logs": [], "route_logs": {}}},
        source="test.workspace_artifact_draft.no_action_loop",
        context_pack=None,
        iteration_index=1,
    )

    assert delivery is not None
    assert delivery["status"] == "delivered"
    assert calls == 0
    assert agent.settings.get("action.loop.enabled", True) is True
    readback = await task.workspace.read_file("final.md")
    assert "Draft Without Actions" in readback["content"]


@pytest.mark.asyncio
async def test_workspace_artifact_stream_draft_receives_active_repair_context(tmp_path):
    class WorkspaceArtifactDraftRepairRequester(MockAgentTaskRequester):
        name = "WorkspaceArtifactDraftRepairRequester"
        draft_text = ""

        async def request_model(self, request_data: AgentlyRequestData):
            text = json.dumps(DataFormatter.sanitize(request_data.data), ensure_ascii=False)
            if "Write only the final Markdown artifact body." in text:
                self.__class__.draft_text = text
                assert "repair_context" in text
                assert "Replace the unsupported legacy claim." in text
                assert "https://example.test/exact-source" in text
                yield "message", "# Repaired Draft\n\nSource: https://example.test/exact-source\n\nNo unsupported claim remains.\n"
                return
            yield "message", json.dumps({"answer": "unused"}, ensure_ascii=False)

    settings = Settings(name="agent-task-workspace-artifact-draft-repair-settings", parent=Agently.settings)
    plugin_manager = PluginManager(
        settings,
        parent=Agently.plugin_manager,
        name="agent-task-workspace-artifact-draft-repair-plugins",
    )
    plugin_manager.register("ModelRequester", WorkspaceArtifactDraftRepairRequester, activate=True)
    agent = Agently.AgentType(
        plugin_manager,
        parent_settings=settings,
        name="agent-task-workspace-artifact-draft-repair",
    )
    task = AgentTask(
        agent,
        task_id="workspace-artifact-draft-repair",
        goal="Repair a source-grounded Workspace artifact.",
        success_criteria=["The final artifact removes unsupported claims."],
        workspace=tmp_path / "task-workspace",
    )
    task.iterations.append(
        {
            "iteration": 1,
            "plan": {"step_instruction": "Draft the first artifact.", "execution_shape": "direct"},
            "execution_meta": {
                "status": "completed",
                "logs": {
                    "action_logs": [
                        {
                            "action_id": "browse",
                            "status": "success",
                            "action_call_id": "call-source",
                            "model_digest": {
                                "result_preview": {
                                    "selected_url": "https://example.test/exact-source",
                                    "content": "Exact source says the corrected claim.",
                                },
                                "result_preview_meta": {"truncated": False},
                            },
                        }
                    ],
                    "route_logs": {},
                },
            },
            "verification": {
                "is_complete": False,
                "reason": "Unsupported claim remains.",
                "failure_analysis": "The draft kept a claim that the source does not support.",
                "acceptance_delta": ["Replace the unsupported legacy claim."],
                "missing_criteria": ["Unsupported claim remains."],
                "repair_constraints": ["Use exact source URLs from available evidence."],
                "next_step_requirements": ["Rewrite the affected artifact section."],
                "replan_instruction": "Repair the artifact using verifier feedback.",
            },
        }
    )

    delivery = await task._stream_workspace_artifact_draft(
        path="final.md",
        plan={
            "deliverable_mode": "workspace_artifact",
            "step_instruction": "Repair the artifact.",
        },
        execution_result={"artifact_manifest": {"path": "final.md"}},
        execution_meta={"status": "completed", "logs": {"action_logs": [], "route_logs": {}}},
        source="test.workspace_artifact_draft.repair",
        context_pack=None,
        iteration_index=2,
    )

    assert delivery is not None
    assert delivery["status"] == "delivered"
    assert "active correction contract" in WorkspaceArtifactDraftRepairRequester.draft_text
    readback = await task.workspace.read_file("final.md")
    assert "No unsupported claim remains." in readback["content"]


def test_agent_language_policy_normalizes_and_reaches_execution_prompt():
    agent = _create_agent("agent-language-policy")

    agent.language("简体中文")
    execution = agent.create_execution().language("chinese")

    agent_policy = agent.agent_prompt.get("options.language_policy")
    execution_policy = execution.prompt_snapshot.get("options", {}).get("language_policy")

    assert agent_policy is not None
    assert execution_policy is not None
    assert agent_policy["language"] == "zh-CN"
    assert agent_policy["search_region"] == "cn-zh"
    assert execution_policy["language"] == "zh-CN"
    assert execution_policy["accept_language"].startswith("zh-CN")
    assert "Language policy" in execution.request.prompt.to_text()


@pytest.mark.asyncio
async def test_agent_create_task_exposes_scoped_workspace_readback_actions(tmp_path):
    task_id = "workspace-readback-task"
    agent = _create_agent("agent-task-scoped-workspace-actions")

    execution = agent.create_task(
        task_id=task_id,
        goal="Create and verify a workspace deliverable.",
        success_criteria=["final.md can be read back."],
        workspace=tmp_path / "task-workspace",
        max_iterations=1,
        options={"agent_task": {"enable_workspace_readback_actions": True}},
    )

    assert {"list_files", "read_file", "search_files"}.issubset(set(execution.local_action_ids))

    scoped_workspace = agent.workspace.with_scope_node(
        "tasks",
        task_id,
        scope={"task_id": task_id},
        search_scope={"task_id": task_id},
    )
    await scoped_workspace.write_file("final.md", "# Scoped Deliverable\n")

    read_result = await agent.action.async_execute_action("read_file", {"path": "final.md"})

    data = read_result.get("data")
    assert read_result.get("status") == "success"
    assert isinstance(data, dict)
    assert data.get("path") == "final.md"
    assert "Scoped Deliverable" in str(data.get("content") or "")


@pytest.mark.asyncio
async def test_agent_create_task_exposes_scoped_workspace_coding_actions(tmp_path):
    task_id = "workspace-coding-task"
    agent = _create_agent("agent-task-scoped-workspace-coding-actions")

    execution = agent.create_task(
        task_id=task_id,
        goal="Create and repair a workspace deliverable.",
        success_criteria=["final.md can be edited and read back."],
        workspace=tmp_path / "task-workspace",
        max_iterations=1,
        options={
            "agent_task": {
                "enable_workspace_readback_actions": True,
                "enable_workspace_coding_actions": True,
            }
        },
    )

    expected_actions = {
        "list_files",
        "read_file",
        "search_files",
        "glob_files",
        "grep_files",
        "write_file",
        "edit_file",
        "apply_patch",
    }
    assert expected_actions.issubset(set(execution.local_action_ids))

    scoped_workspace = agent.workspace.with_scope_node(
        "tasks",
        task_id,
        scope={"task_id": task_id},
        search_scope={"task_id": task_id},
    )
    await scoped_workspace.write_file("final.md", "# Draft\nold wording\n")

    read_result = await agent.action.async_execute_action("read_file", {"path": "final.md"})
    assert read_result.get("status") == "success"
    expected_sha = read_result.get("data", {}).get("sha256")

    edit_result = await agent.action.async_execute_action(
        "edit_file",
        {
            "path": "final.md",
            "old_string": "old wording",
            "new_string": "corrected wording",
            "expected_sha256": expected_sha,
        },
    )
    assert edit_result.get("status") == "success"

    updated = await scoped_workspace.read_file("final.md")
    assert "corrected wording" in str(updated.get("content") or "")


def test_agent_task_child_execution_sets_task_local_action_loop_guard(tmp_path):
    agent = _create_agent("agent-task-action-loop-guard").use_workspace(tmp_path / "task-workspace")
    task = AgentTask(
        agent,
        goal="Use actions inside a bounded task step.",
        success_criteria=["The child action loop has a task-local safety guard."],
        execution="flat",
    )

    execution = task._create_bounded_child_execution(
        lineage={
            "task_id": task.id,
            "iteration_id": "iter-1",
            "step_id": "execute",
        }
    )

    assert agent.settings.get("action.loop.max_rounds") is None
    assert execution.request.settings.get("action.loop.max_rounds") == 2
    assert execution.request.settings.get("tool.loop.max_rounds") == 2


def test_agent_task_child_execution_respects_explicit_action_loop_guard(tmp_path):
    agent = _create_agent("agent-task-action-loop-explicit").use_workspace(tmp_path / "task-workspace")
    task = AgentTask(
        agent,
        goal="Use actions inside a bounded task step.",
        success_criteria=["The child action loop honors explicit task options."],
        execution="flat",
        options={"agent_task": {"action_loop_max_rounds": 3}},
    )
    disabled_task = AgentTask(
        agent,
        goal="Use actions without task-local loop guard.",
        success_criteria=["The explicit None option disables the task guard."],
        execution="flat",
        options={"agent_task": {"action_loop_max_rounds": None}},
    )

    execution = task._create_bounded_child_execution(
        lineage={"task_id": task.id, "iteration_id": "iter-1", "step_id": "execute"}
    )
    disabled_execution = disabled_task._create_bounded_child_execution(
        lineage={"task_id": disabled_task.id, "iteration_id": "iter-1", "step_id": "execute"}
    )

    assert execution.request.settings.get("action.loop.max_rounds") == 3
    assert execution.request.settings.get("tool.loop.max_rounds") == 3
    assert disabled_execution.request.settings.get("action.loop.max_rounds") is None
    assert disabled_execution.request.settings.get("tool.loop.max_rounds") is None


@pytest.mark.asyncio
async def test_agent_goal_success_criteria_uses_task_execution_path(tmp_path):
    MockAgentTaskRequester.reset()
    agent = _create_agent("agent-goal-task-path").use_workspace(tmp_path / "task-workspace")

    execution = agent.goal(
        "Repair a legacy Agently script so it runs on the current API.",
        ["The script runs successfully."],
    ).strategy("task", max_iterations=2)

    result = await execution.async_start()
    meta = await execution.async_get_meta()

    assert result["status"] == "completed"
    assert meta["route"]["selected_route"] == "agent_task"
    assert meta["task_refs"]["task_id"]
    assert meta["task_refs"]["status"] == "completed"
    assert meta["success_criteria"] == ["The script runs successfully."]


@pytest.mark.asyncio
async def test_agent_task_loop_receives_execution_prompt_snapshot(tmp_path):
    MockAgentTaskRequester.reset()
    agent = _create_agent("agent-task-loop-execution-prompt").use_workspace(tmp_path / "task-workspace")

    execution = (
        agent.goal(
            "Prepare an operator summary from caller-provided facts.",
            ["The summary uses the supplied incident id."],
        )
        .effort("low", budget={"iteration_limit": 2})
        .input({"incident_id": "INC-4242", "severity": "SEV2"})
        .output({"summary": (str, "Operator summary that includes the incident id.", True)}, format="json")
        .strategy("task", max_iterations=2)
    )

    result = await execution.async_start()
    meta = await execution.async_get_meta()
    calls = "\n".join(MockAgentTaskRequester.calls)
    task = cast(Any, execution).task_record

    assert result["status"] == "completed"
    assert meta["route"]["selected_route"] == "agent_task"
    assert "INC-4242" in calls
    assert "severity" in calls
    assert "summary" in calls
    assert task.options["execution_prompt_snapshot"]["input"]["incident_id"] == "INC-4242"


@pytest.mark.asyncio
async def test_public_task_strategy_spellings_share_agent_task_lifecycle(tmp_path):
    for label, build in (
        (
            "create_task_loop",
            lambda agent, workspace: agent.create_task_loop(
                task_id="task-loop-spelling",
                goal="Repair a legacy Agently script so it runs on the current API.",
                success_criteria=["The script runs successfully."],
                workspace=workspace,
                max_iterations=2,
                limits={"max_model_requests": 1},
            ),
        ),
        (
            "strategy_task_loop",
            lambda agent, workspace: (
                agent.create_execution(
                    options={
                        "task": {
                            "task_id": "strategy-task-loop-spelling",
                            "workspace": workspace,
                            "max_iterations": 2,
                            "limits": {"max_model_requests": 1},
                        }
                    }
                )
                .goal(
                    "Repair a legacy Agently script so it runs on the current API.",
                    ["The script runs successfully."],
                )
                .strategy("task_loop")
            ),
        ),
    ):
        MockAgentTaskRequester.reset()
        agent = _create_agent(f"agent-{label}").use_workspace(tmp_path / label)
        execution = build(agent, tmp_path / label)

        result = await execution.async_start()
        meta = await execution.async_get_meta()

        assert result["status"] == "completed"
        assert meta["route"]["selected_route"] == "agent_task"
        assert meta["route"]["options"]["strategy"] == "task_loop"
        assert meta["task_refs"]["status"] == "completed"


@pytest.mark.asyncio
async def test_strategy_shape_analysis_is_hint_not_hard_route(tmp_path):
    agent = _create_agent("agent-task-shape-analysis").use_workspace(tmp_path / "workspace")
    task = AgentTask(
        agent,
        task_id="shape-analysis-policy-gate",
        goal="Plan a multi-angle launch review.",
        success_criteria=["The review is planned."],
        execution="auto",
        options={"agent_task": {"execution_strategy_policy": {"allow_taskboard": False}}},
    )

    async def taskboard_hint():
        return {
            "analysis": "Several workstreams could benefit from a board.",
            "execution_hint": {
                "recommended_shape": "taskboard",
                "confidence": "high",
                "reasons": ["parallel evidence streams"],
                "linear_evidence": [],
                "branching_evidence": ["marketing, operations, and finance perspectives"],
                "uncertainty": "",
            },
        }

    cast(Any, task)._request_task_shape_analysis = taskboard_hint

    effective = await task._resolve_effective_execution_strategy()

    assert effective == "flat"
    assert task.execution_strategy == "auto"
    assert task.task_shape_analysis["execution_hint"]["recommended_shape"] == "taskboard"
    assert task.diagnostics["execution_strategy"]["effective"] == "flat"
    assert task.workspace_refs["strategy"]


@pytest.mark.asyncio
async def test_auto_strategy_can_select_taskboard_when_policy_allows(tmp_path):
    agent = _create_agent("agent-task-shape-taskboard").use_workspace(tmp_path / "workspace")
    task = AgentTask(
        agent,
        task_id="shape-analysis-taskboard",
        goal="Plan multiple parallel research tracks.",
        success_criteria=["Each track has evidence."],
        execution="auto",
        options={"agent_task": {"execution_strategy_policy": {"allow_taskboard": True}}},
    )

    async def taskboard_hint():
        return {
            "analysis": "The task has multiple independent tracks.",
            "execution_hint": {
                "recommended_shape": "taskboard",
                "confidence": "medium",
                "reasons": ["independent evidence tracks"],
                "linear_evidence": [],
                "branching_evidence": ["track A", "track B"],
                "uncertainty": "",
            },
        }

    cast(Any, task)._request_task_shape_analysis = taskboard_hint

    assert await task._resolve_effective_execution_strategy() == "taskboard"
    assert task.diagnostics["execution_strategy"]["source"] == "task_shape_analysis"


@pytest.mark.asyncio
async def test_explicit_flat_strategy_skips_shape_analysis(tmp_path):
    agent = _create_agent("agent-explicit-flat-strategy").use_workspace(tmp_path / "workspace")
    task = AgentTask(
        agent,
        task_id="explicit-flat-no-analysis",
        goal="Run a linear task.",
        success_criteria=["The task is done."],
        execution="flat",
    )

    async def fail_if_called():
        raise AssertionError("explicit flat must not request task-shape analysis")

    cast(Any, task)._request_task_shape_analysis = fail_if_called

    assert await task._resolve_effective_execution_strategy() == "flat"
    assert task.task_shape_analysis == {}


def test_strategy_method_maps_execution_shapes_and_nested_inheritance(tmp_path):
    from agently.core.application.AgentExecution import AgentExecutionContext
    from agently.core.runtime.RuntimeContext import bind_runtime_context

    agent = _create_agent("agent-strategy-method").use_workspace(tmp_path / "workspace")

    flat_execution = agent.goal("Do a flat task.", ["Done."]).strategy("flat", max_iterations=1)
    assert flat_execution.strategy_name == "flat"
    assert flat_execution.task_options["execution"] == "flat"
    assert flat_execution.task_strategy_options()["execution"] == "flat"

    parent_context = AgentExecutionContext(
        execution_id="parent-exec",
        lineage={},
        limits={},
        task_execution_strategy="auto",
        effective_task_execution_strategy="taskboard",
        strategy_context_source="task_shape_analysis",
    )
    with bind_runtime_context(agent_execution_context=parent_context):
        inherited = agent.create_execution().goal("Nested task.", ["Done."])
        overridden = agent.create_execution().goal("Nested override.", ["Done."]).strategy("flat")

    assert inherited.task_strategy_options()["execution"] == "taskboard"
    assert inherited.task_strategy_options()["_execution_strategy_source"] == "inherited_agent_execution_context"
    assert overridden.task_strategy_options()["execution"] == "flat"

    inherited_task = AgentTask(
        agent,
        task_id="nested-inherited-taskboard",
        goal="Nested inherited task.",
        success_criteria=["Done."],
        execution=inherited.task_strategy_options()["execution"],
    )

    async def fail_if_called():
        raise AssertionError("inherited effective strategy must not request task-shape analysis")

    cast(Any, inherited_task)._request_task_shape_analysis = fail_if_called
    assert inherited_task.execution_strategy == "taskboard"
    assert inherited_task.effective_execution_strategy == "taskboard"
    assert asyncio.run(inherited_task._resolve_effective_execution_strategy()) == "taskboard"


@pytest.mark.asyncio
async def test_agent_execution_runtime_observes_flat_agent_task_stream(tmp_path):
    MockAgentTaskRequester.reset()
    captured = []

    async def capture(event):
        captured.append(event)

    hook_name = "test_agent_task_loop.agent_execution_runtime_stream_capture"
    Agently.event_center.register_hook(capture, hook_name=hook_name)
    try:
        agent = _create_agent("agent-flat-runtime-observation").use_workspace(tmp_path / "workspace")
        execution = agent.goal(
            "Repair a legacy Agently script.",
            [
                "The original failure is recorded.",
                "The script runs successfully.",
            ],
        ).strategy("flat", max_iterations=2)

        result = await execution.async_get_data()

        assert result["status"] == "completed"
        execution_events = [
            event
            for event in captured
            if event.run is not None
            and event.run.run_kind == "agent_execution"
            and event.run.execution_id == execution.id
        ]
        event_types = [event.event_type for event in execution_events]
        assert event_types[0] == "agent_execution.started"
        assert event_types[-1] == "agent_execution.completed"
        stream_events = [event for event in execution_events if event.event_type == "agent_execution.stream"]
        stream_paths = [event.payload.get("path") for event in stream_events if isinstance(event.payload, dict)]
        assert "route.selected" in stream_paths
        assert "agent_task.created" in stream_paths
        assert any(str(path).startswith("agent_task.iteration.") for path in stream_paths)
        assert any(str(path).endswith(".execution.completed") for path in stream_paths)
        task_stream_event = next(
            event
            for event in stream_events
            if isinstance(event.payload, dict) and event.payload.get("path") == "agent_task.created"
        )
        assert task_stream_event.payload["execution_id"] == execution.id
        assert task_stream_event.payload["task_id"] == execution.task_refs["task_id"]
        assert task_stream_event.payload["execution_strategy"] == "flat"
        assert task_stream_event.payload["effective_execution_strategy"] == "flat"
    finally:
        Agently.event_center.unregister_hook(hook_name)


@pytest.mark.asyncio
async def test_effort_reflection_density_records_expected_points(tmp_path):
    async def run_with_effort(label: str, effort: dict[str, Any]):
        agent = _create_agent(f"agent-reflection-{label}").use_workspace(tmp_path / label)
        task = AgentTask(
            agent,
            task_id=f"reflection-{label}",
            goal="Complete one bounded step.",
            success_criteria=["Verifier accepts the result."],
            execution="flat",
            max_iterations=1,
            options={"agent_task": {"effort": effort}},
        )

        async def request_plan(_iteration_index, _context_pack):
            return {
                "execution_shape": "direct",
                "step_instruction": "produce evidence",
                "expected_evidence": "evidence",
                "rationale": "one step",
            }

        async def execute_step(_iteration_index, _plan, _context_pack):
            return (
                {"step_result": "done", "evidence": ["ok"], "remaining_work": []},
                {"execution_id": f"exec-{label}", "status": "success", "route": {"selected_route": "model_request"}},
            )

        async def request_verification(_iteration_index, **_kwargs):
            return {
                "is_complete": True,
                "requires_block": False,
                "reason": "accepted",
                "missing_criteria": [],
                "replan_instruction": "",
                "final_result_required": True,
                "final_result": "done",
            }

        cast(Any, task)._request_plan = request_plan
        cast(Any, task)._execute_step = execute_step
        cast(Any, task)._request_verification = request_verification
        await task.async_run()
        return [item["phase"] for item in task.reflections]

    low = await run_with_effort("low", {"name": "low", "reflection_density": "final"})
    medium = await run_with_effort("medium", {"name": "medium", "reflection_density": "major_node"})
    high = await run_with_effort("high", {"name": "high", "reflection_density": "action"})

    assert low == ["final"]
    assert "major_node" in medium and "bounded_step" not in medium and "final" in medium
    assert {"bounded_step", "major_node", "final"}.issubset(set(high))


@pytest.mark.asyncio
async def test_acp_recovery_policy_uses_registered_action_after_exhaustion(tmp_path, monkeypatch):
    agent = _create_agent("agent-acp-recovery").use_workspace(tmp_path / "workspace")
    calls: list[tuple[str, dict[str, Any]]] = []

    async def fake_execute_action(action_id: str, payload: dict[str, Any]):
        calls.append((action_id, payload))
        return {
            "ok": True,
            "status": "success",
            "agent_id": payload.get("agent_id"),
            "result": {"final_result": "recovered by acp"},
            "diagnostics": [],
        }

    monkeypatch.setattr(agent.action, "async_execute_action", fake_execute_action)

    task = AgentTask(
        agent,
        task_id="acp-recovery-task",
        goal="Recover a failed bounded step.",
        success_criteria=["ACP recovery evidence is accepted."],
        execution="flat",
        max_iterations=1,
        options={"agent_task": {"acp_recovery": {"enabled": True, "agent_id": "codex"}}},
    )

    async def request_plan(_iteration_index, _context_pack):
        return {
            "execution_shape": "direct",
            "step_instruction": "fail first",
            "expected_evidence": "failure",
            "rationale": "exercise recovery",
        }

    async def execute_step(_iteration_index, _plan, _context_pack):
        return (
            {"step_result": "", "evidence": ["failed"], "remaining_work": ["recover"]},
            {"execution_id": "failed-step", "status": "failed", "route": {"selected_route": "model_request"}},
        )

    async def request_verification(_iteration_index, *, execution_meta, **_kwargs):
        assert execution_meta["route"]["selected_route"] == "acp_recovery"
        return {
            "is_complete": True,
            "requires_block": False,
            "reason": "ACP recovery accepted",
            "missing_criteria": [],
            "replan_instruction": "",
            "final_result_required": True,
            "final_result": "recovered by acp",
        }

    cast(Any, task)._request_plan = request_plan
    cast(Any, task)._execute_step = execute_step
    cast(Any, task)._request_verification = request_verification

    result = await task.async_run()

    assert result["status"] == "completed"
    assert calls and calls[0][0] == "acp_run_task"
    assert calls[0][1]["agent_id"] == "codex"
    assert task.workspace_refs["acp_recovery"]


@pytest.mark.asyncio
async def test_taskboard_card_failure_uses_acp_recovery_after_card_attempts(tmp_path, monkeypatch):
    agent = _create_agent("agent-taskboard-acp-recovery").use_workspace(tmp_path / "workspace")
    calls: list[tuple[str, dict[str, Any]]] = []

    async def fake_execute_action(action_id: str, payload: dict[str, Any]):
        calls.append((action_id, payload))
        return {
            "ok": True,
            "status": "success",
            "agent_id": payload.get("agent_id"),
            "result": {"final_result": "taskboard card recovered by acp"},
            "diagnostics": [],
        }

    monkeypatch.setattr(agent.action, "async_execute_action", fake_execute_action)
    task = AgentTask(
        agent,
        task_id="taskboard-acp-recovery",
        goal="Recover a failed TaskBoard card.",
        success_criteria=["ACP recovery evidence is accepted."],
        execution="taskboard",
        max_iterations=1,
        options={"agent_task": {"acp_recovery": {"enabled": True, "agent_id": "codex"}}},
    )
    card = TaskBoardCard.from_value(
        {
            "id": "collect",
            "objective": "Collect evidence for the final answer.",
            "required_outputs": ["Recovered evidence"],
        }
    )
    revision = TaskBoardRevision.create(
        board_id="taskboard-acp-recovery",
        graph=TaskBoardGraph.from_value({"graph_id": "taskboard-acp-recovery-graph", "cards": [card.to_dict()]}),
    )
    context = SimpleNamespace(
        card=card,
        revision=revision,
        dependency_results={},
        planning_policy=None,
    )

    async def failing_card(_context, _context_pack):
        return TaskBoardCardResult(
            card_id=card.id,
            status="failed",
            preview={"error": "missing dynamic handler"},
            diagnostics=({"code": "taskboard.card.missing_handler", "card_id": card.id},),
            metadata={"execution_kind": "taskboard_agent_card"},
        )

    monkeypatch.setattr(cast(Any, task), "_run_taskboard_agent_card", failing_card)
    monkeypatch.setattr(cast(Any, task), "_should_record_process_reflection", lambda *_args, **_kwargs: False)

    result = await task._run_taskboard_card(
        context,
        {
            "goal": task.goal,
            "profile": "",
            "items": [],
            "omitted": [],
            "diagnostics": {},
        },
    )

    assert result.status == "completed"
    assert result.metadata["acp_recovery"] is True
    assert result.metadata["acp_recovered"] is True
    assert any(item.get("code") == "taskboard.card.acp_recovery" for item in result.diagnostics)
    assert calls and calls[0][0] == "acp_run_task"
    payload = calls[0][1]
    assert payload["agent_id"] == "codex"
    assert payload["context"]["plan"]["taskboard_card_id"] == "collect"
    assert payload["context"]["failed_execution_result"]["taskboard_card_result"]["status"] == "failed"
    assert task.workspace_refs["acp_recovery"]


@pytest.mark.asyncio
async def test_taskboard_action_card_retries_retryable_result_protocol_failure(tmp_path, monkeypatch):
    agent = _create_agent("agent-taskboard-card-result-retry").use_workspace(tmp_path / "workspace")
    task = AgentTask(
        agent,
        task_id="taskboard-card-result-retry",
        goal="Write the final report.",
        success_criteria=["final.md is written."],
        execution="taskboard",
        options={
            "agent_task": {
                "required_deliverables": [{"path": "final.md", "media_type": "text/markdown"}],
                "taskboard_card_max_attempts": 2,
            }
        },
    )
    card = TaskBoardCard.from_value(
        {
            "id": "draft",
            "objective": "Draft final.md.",
            "allowed_execution_shape": "actions",
            "required_outputs": ["final.md exists with a non-empty body"],
        }
    )
    revision = TaskBoardRevision.create(
        board_id="taskboard-card-result-retry",
        graph=TaskBoardGraph.from_value({"graph_id": "taskboard-card-result-retry-graph", "cards": [card.to_dict()]}),
    )
    context = SimpleNamespace(
        card=card,
        revision=revision,
        dependency_results={},
        planning_policy=None,
    )
    attempts: list[int] = []

    async def fake_run_work_unit_through_blocks(*_args, **kwargs):
        attempt_index = kwargs["start_payload"]["attempt_index"]
        attempts.append(attempt_index)
        meta = {
            "execution_id": f"exec-{attempt_index}",
            "status": "success",
            "route": {"selected_route": "model_request", "status": "completed"},
            "logs": {"action_logs": {}, "route_logs": {}, "errors": []},
            "diagnostics": [],
        }
        if attempt_index == 1:
            return (
                {
                    "status": "completed",
                    "answer": "A final.md artifact was produced.",
                    "artifact_manifest": {"path": "final.md", "sections": [{}]},
                    "remaining_work": [],
                },
                meta,
                {},
            )
        return (
            {
                "status": "completed",
                "artifact_markdown": "# Final Report\n\nRecovered body.",
                "artifact_manifest": {"path": "final.md", "sections": [{"title": "Final Report"}]},
                "remaining_work": [],
            },
            meta,
            {},
        )

    async def noop(*_args, **_kwargs):
        return None

    monkeypatch.setattr(cast(Any, task), "_run_work_unit_through_blocks", fake_run_work_unit_through_blocks)
    monkeypatch.setattr(cast(Any, task), "_emit", noop)
    monkeypatch.setattr(cast(Any, task), "_emit_action_observation_events", noop)

    result = await task._run_taskboard_agent_card(
        context,
        {
            "goal": task.goal,
            "profile": "",
            "items": [],
            "omitted": [],
            "diagnostics": {},
        },
    )

    assert attempts == [1, 2]
    assert result.status == "completed"
    retry_diagnostics = task.diagnostics["taskboard_card_retries"]
    assert retry_diagnostics[0]["code"] == "taskboard.card.result_protocol_retry"
    assert "agent_task.workspace_artifact.empty_body" in retry_diagnostics[0]["retryable_codes"]
    assert result.metadata["attempt_index"] == 2
    readback = await task.workspace.read_file("final.md")
    assert readback["content"] == "# Final Report\n\nRecovered body."


@pytest.mark.asyncio
async def test_taskboard_action_card_retries_blocking_evidence_use_guard(tmp_path, monkeypatch):
    agent = _create_agent("agent-taskboard-card-evidence-use-retry").use_workspace(tmp_path / "workspace")
    task = AgentTask(
        agent,
        task_id="taskboard-card-evidence-use-retry",
        goal="Collect source evidence.",
        success_criteria=["The card reports collected evidence without invalid evidence bindings."],
        execution="taskboard",
        options={"agent_task": {"taskboard_card_max_attempts": 2}},
    )
    card = TaskBoardCard.from_value(
        {
            "id": "collect",
            "objective": "Collect source evidence for the final answer.",
            "allowed_execution_shape": "actions",
            "required_outputs": ["Collected source evidence summary"],
        }
    )
    revision = TaskBoardRevision.create(
        board_id="taskboard-card-evidence-use-retry",
        graph=TaskBoardGraph.from_value(
            {"graph_id": "taskboard-card-evidence-use-retry-graph", "cards": [card.to_dict()]}
        ),
    )
    context = SimpleNamespace(
        card=card,
        revision=revision,
        dependency_results={},
        planning_policy=None,
    )
    attempts: list[int] = []

    async def fake_run_work_unit_through_blocks(*_args, **kwargs):
        attempt_index = kwargs["start_payload"]["attempt_index"]
        attempts.append(attempt_index)
        meta = {
            "execution_id": f"exec-{attempt_index}",
            "status": "success",
            "route": {"selected_route": "model_request", "status": "completed"},
            "logs": {"action_logs": {}, "route_logs": {}, "errors": []},
            "diagnostics": [],
        }
        if attempt_index == 1:
            return (
                {
                    "status": "completed",
                    "answer": "Collected source evidence.",
                    "evidence_use": [
                        {
                            "claim": "Collected source evidence.",
                            "evidence_ids": ["missing-evidence-id"],
                            "support_type": "content",
                        }
                    ],
                    "remaining_work": [],
                },
                meta,
                {},
            )
        return (
            {
                "status": "completed",
                "answer": "Collected source evidence; no unsupported evidence binding remains.",
                "evidence_use": [],
                "remaining_work": [],
            },
            meta,
            {},
        )

    async def noop(*_args, **_kwargs):
        return None

    monkeypatch.setattr(cast(Any, task), "_run_work_unit_through_blocks", fake_run_work_unit_through_blocks)
    monkeypatch.setattr(cast(Any, task), "_emit", noop)
    monkeypatch.setattr(cast(Any, task), "_emit_action_observation_events", noop)

    result = await task._run_taskboard_agent_card(
        context,
        {
            "goal": task.goal,
            "profile": "",
            "items": [],
            "omitted": [],
            "diagnostics": {},
        },
    )

    assert attempts == [1, 2]
    assert result.status == "completed"
    retry_diagnostics = task.diagnostics["taskboard_card_retries"]
    assert retry_diagnostics[0]["code"] == "taskboard.card.result_protocol_retry"
    assert "taskboard.card.evidence_use_guard_blocking" in retry_diagnostics[0]["retryable_codes"]
    assert result.metadata["attempt_index"] == 2


def test_taskboard_final_verification_failure_creates_repair_revision(tmp_path):
    agent = _create_agent("agent-taskboard-final-repair").use_workspace(tmp_path / "workspace")
    task = AgentTask(
        agent,
        task_id="taskboard-final-repair",
        goal="Produce a source-grounded final deliverable.",
        success_criteria=["Unsupported facts are removed or backed by evidence."],
        execution="taskboard",
        max_iterations=2,
        options={"agent_task": {"required_deliverables": [{"path": "final.md"}]}},
    )
    collect = TaskBoardCard.from_value(
        {
            "id": "collect",
            "objective": "Collect official source evidence.",
            "required_outputs": ["Official source evidence"],
        }
    )
    draft = TaskBoardCard.from_value(
        {
            "id": "draft",
            "objective": "Draft the final deliverable.",
            "depends_on": ["collect"],
            "required_outputs": ["Final deliverable"],
            "allowed_execution_shape": "control",
        }
    )
    revision = TaskBoardRevision.from_value(
        {
            "board_id": "taskboard-final-repair",
            "revision_id": "rev-1",
            "graph": {
                "graph_id": "taskboard-final-repair-graph",
                "cards": [collect.to_dict(), draft.to_dict()],
            },
            "card_results": {
                "collect": TaskBoardCardResult(card_id="collect", status="completed").to_dict(),
                "draft": TaskBoardCardResult(card_id="draft", status="completed").to_dict(),
            },
        }
    )

    repaired = task._taskboard_final_verification_repair_revision(
        revision,
        final={"accepted": False, "reason": "unsupported labels remain", "final_result": "draft"},
        final_verification={
            "is_complete": False,
            "requires_block": False,
            "reason": "unsupported sub-section labels are not in evidence",
            "missing_criteria": ["Remove unsupported sub-section labels."],
            "next_step_requirements": ["Use only verifier-visible source titles."],
            "acceptance_delta": ["Preserve source URLs."],
        },
    )

    assert repaired is not None
    cards = repaired.graph.card_by_id()
    repair_ids = [
        card_id
        for card_id, card in cards.items()
        if card.metadata.get("generated_by") == "agent_task.taskboard.final_verification_repair"
    ]
    assert len(repair_ids) == 1
    repair = cards[repair_ids[0]]
    assert repair.allowed_execution_shape == "control"
    assert set(repair.depends_on) == {"collect", "draft"}
    assert "Remove unsupported sub-section labels." in repair.evidence_contract["missing_criteria"]
    assert repair.metadata["final_workspace_deliverables"] == ["final.md"]
    assert any("final.md" in item for item in repair.required_outputs)
    assert any(item.get("code") == "taskboard.final_verification.repair_patch" for item in repaired.diagnostics)
    schedule = TaskBoard(repaired, handler=lambda _context: None).schedule()
    assert schedule.runnable_card_ids == (repair.id,)
    assert task.diagnostics["taskboard_final_repair_patches"][0]["repair_card_id"] == repair.id


@pytest.mark.asyncio
async def test_taskboard_finalization_repairs_repairable_requires_block_verdict(tmp_path, monkeypatch):
    agent = _create_agent("agent-taskboard-final-repairable-block").use_workspace(tmp_path / "workspace")
    task = AgentTask(
        agent,
        task_id="taskboard-final-repairable-block",
        goal="Return a complete final report.",
        success_criteria=["The final report includes required sections."],
        execution="taskboard",
        options={"agent_task": {"required_deliverables": [{"path": "final.md"}]}},
    )
    card = TaskBoardCard.from_value(
        {
            "id": "draft",
            "objective": "Draft the final report.",
            "required_outputs": ["Final report"],
            "allowed_execution_shape": "control",
        }
    )
    revision = TaskBoardRevision.from_value(
        {
            "board_id": "taskboard-final-repairable-block",
            "revision_id": "rev-1",
            "graph": {"graph_id": "taskboard-final-repairable-block-graph", "cards": [card.to_dict()]},
            "card_results": {
                "draft": TaskBoardCardResult(
                    card_id="draft",
                    status="completed",
                    preview={
                        "status": "completed",
                        "final_result": "Draft body missing a required section.",
                        "remaining_work": [],
                    },
                ).to_dict()
            },
        }
    )

    async def verifier_requires_block_for_repairable_gap(*_args, **_kwargs):
        return {
            "is_complete": False,
            "requires_block": True,
            "reason": "The final deliverable is missing a required section.",
            "failure_analysis": "This is a repairable artifact gap.",
            "acceptance_delta": ["Add the missing required section."],
            "missing_criteria": ["Missing required section."],
            "replan_instruction": "Repair final.md by adding the missing section.",
            "repair_constraints": ["Preserve existing evidence."],
            "next_step_requirements": ["Return verifier-visible readback for final.md."],
            "final_result_required": True,
            "final_result": "",
        }

    async def fail_finalizer(*_args, **_kwargs):
        raise AssertionError("Promotable terminal candidate should skip the model finalizer.")

    async def noop(*_args, **_kwargs):
        return None

    monkeypatch.setattr(cast(Any, task), "_request_taskboard_final", fail_finalizer)
    monkeypatch.setattr(cast(Any, task), "_request_verification", verifier_requires_block_for_repairable_gap)
    monkeypatch.setattr(cast(Any, task), "_record_phase", noop)
    monkeypatch.setattr(cast(Any, task), "_emit", noop)

    result = await task._finalize_taskboard(
        revision,
        context_pack={
            "goal": task.goal,
            "profile": "",
            "items": [],
            "omitted": [],
            "diagnostics": {},
        },
    )

    assert result["terminal"] is False
    assert result["status"] == "repair_requested"
    repair_revision = TaskBoardRevision.from_value(result["revision"])
    repair_cards = [
        card
        for card in repair_revision.graph.cards
        if card.metadata.get("generated_by") == "agent_task.taskboard.final_verification_repair"
    ]
    assert len(repair_cards) == 1
    assert "Missing required section." in repair_cards[0].evidence_contract["missing_criteria"]


@pytest.mark.asyncio
async def test_taskboard_finalization_promotes_single_terminal_candidate_without_finalizer(tmp_path, monkeypatch):
    agent = _create_agent("agent-taskboard-final-promotion").use_workspace(tmp_path / "workspace")
    task = AgentTask(
        agent,
        task_id="taskboard-final-promotion",
        goal="Return the final report.",
        success_criteria=["The completed card result is returned."],
        execution="taskboard",
    )
    card = TaskBoardCard.from_value(
        {
            "id": "draft",
            "objective": "Draft the final report.",
            "required_outputs": ["Final report"],
        }
    )
    revision = TaskBoardRevision.from_value(
        {
            "board_id": "taskboard-final-promotion",
            "revision_id": "rev-1",
            "graph": {"graph_id": "taskboard-final-promotion-graph", "cards": [card.to_dict()]},
            "card_results": {
                "draft": TaskBoardCardResult(
                    card_id="draft",
                    status="completed",
                    preview={
                        "status": "completed",
                        "final_result": "Final report body from the completed terminal card.",
                        "remaining_work": [],
                    },
                ).to_dict()
            },
        }
    )
    calls = {"finalizer": 0, "verifier": 0}

    async def fail_finalizer(*_args, **_kwargs):
        calls["finalizer"] += 1
        raise AssertionError("TaskBoard finalizer should be skipped for promotable terminal candidate.")

    async def complete_verifier(*_args, **kwargs):
        calls["verifier"] += 1
        execution_result = kwargs["execution_result"]
        assert execution_result["final_result"] == "Final report body from the completed terminal card."
        return {
            "is_complete": True,
            "requires_block": False,
            "reason": "complete",
            "failure_analysis": "",
            "acceptance_delta": [],
            "missing_criteria": [],
            "replan_instruction": "",
            "repair_constraints": [],
            "next_step_requirements": [],
            "final_result_required": True,
            "final_result": execution_result["final_result"],
        }

    async def noop(*_args, **_kwargs):
        return None

    monkeypatch.setattr(cast(Any, task), "_request_taskboard_final", fail_finalizer)
    monkeypatch.setattr(cast(Any, task), "_request_verification", complete_verifier)
    monkeypatch.setattr(cast(Any, task), "_record_phase", noop)
    monkeypatch.setattr(cast(Any, task), "_emit", noop)

    result = await task._finalize_taskboard(
        revision,
        context_pack={
            "goal": task.goal,
            "profile": "",
            "items": [],
            "omitted": [],
            "diagnostics": {},
        },
    )

    assert result == {"terminal": True, "status": "completed"}
    assert calls == {"finalizer": 0, "verifier": 1}
    assert task.result["taskboard"]["finalization_source"] == "candidate_promotion"


@pytest.mark.asyncio
async def test_taskboard_final_gate_blocks_only_explicit_dirty_state_facts(tmp_path, monkeypatch):
    agent = _create_agent("agent-taskboard-final-dirty-state").use_workspace(tmp_path / "workspace")
    task = AgentTask(
        agent,
        task_id="taskboard-final-dirty-state",
        goal="Return the final report.",
        success_criteria=["The completed card result is returned."],
        execution="taskboard",
    )
    card = TaskBoardCard.from_value(
        {
            "id": "draft",
            "objective": "Draft the final report.",
            "required_outputs": ["Final report"],
        }
    )
    revision = TaskBoardRevision.from_value(
        {
            "board_id": "taskboard-final-dirty-state",
            "revision_id": "rev-1",
            "graph": {"graph_id": "taskboard-final-dirty-state-graph", "cards": [card.to_dict()]},
            "card_results": {
                "draft": TaskBoardCardResult(
                    card_id="draft",
                    status="completed",
                    preview={
                        "status": "completed",
                        "final_result": "Final report body from the completed terminal card.",
                        "remaining_work": [],
                    },
                ).to_dict()
            },
            "diagnostics": [
                {
                    "kind": "explicit_state_fact",
                    "code": "task_repo.dirty",
                    "scope": "task",
                    "status": "dirty",
                    "blocking": True,
                    "reason": "Task-scoped repository files are still dirty.",
                    "source": "action:git_status",
                }
            ],
        }
    )
    calls = {"finalizer": 0, "verifier": 0}

    async def fail_finalizer(*_args, **_kwargs):
        calls["finalizer"] += 1
        raise AssertionError("TaskBoard finalizer should be skipped for promotable terminal candidate.")

    async def complete_verifier(*_args, **kwargs):
        calls["verifier"] += 1
        execution_result = kwargs["execution_result"]
        assert execution_result["final_result"] == "Final report body from the completed terminal card."
        return {
            "is_complete": True,
            "requires_block": False,
            "reason": "complete before explicit state gate",
            "failure_analysis": "",
            "acceptance_delta": [],
            "missing_criteria": [],
            "replan_instruction": "",
            "repair_constraints": [],
            "next_step_requirements": [],
            "final_result_required": True,
            "final_result": execution_result["final_result"],
        }

    async def noop(*_args, **_kwargs):
        return None

    monkeypatch.setattr(cast(Any, task), "_request_taskboard_final", fail_finalizer)
    monkeypatch.setattr(cast(Any, task), "_request_verification", complete_verifier)
    monkeypatch.setattr(cast(Any, task), "_record_phase", noop)
    monkeypatch.setattr(cast(Any, task), "_emit", noop)

    result = await task._finalize_taskboard(
        revision,
        context_pack={
            "goal": task.goal,
            "profile": "",
            "items": [],
            "omitted": [],
            "diagnostics": {},
        },
    )

    assert result == {"terminal": True, "status": "blocked"}
    assert calls == {"finalizer": 0, "verifier": 1}
    assert task.result["accepted"] is False
    assert task.result["artifact_status"] == "partial"
    assert task.result["taskboard"]["explicit_state_facts"][0]["code"] == "task_repo.dirty"
    assert "Task-scoped repository files are still dirty." in task.result["reason"]


def test_taskboard_auto_reuses_initial_plan_and_falls_back_for_small_linear_board(tmp_path):
    agent = _create_agent("agent-taskboard-auto-plan-reuse").use_workspace(tmp_path / "workspace")
    task = AgentTask(
        agent,
        task_id="taskboard-auto-plan-reuse",
        goal="Answer a simple question.",
        success_criteria=["Return the answer."],
        execution="auto",
    )
    task.task_shape_analysis = task._normalize_task_shape_analysis(
        {
            "analysis": "A tiny board would be enough.",
            "execution_hint": {"recommended_shape": "taskboard", "confidence": "medium"},
            "initial_taskboard_plan": {
                "board_goal": "Answer a simple question.",
                "cards": [
                    {
                        "id": "answer",
                        "action_block": "Answer directly.",
                        "objective": "Return the answer.",
                        "depends_on": [],
                        "done_when": "Answer is returned.",
                        "allowed_execution_shape": "auto",
                    }
                ],
                "reflection_points": [],
                "completion_gate": "The answer is returned.",
                "why_this_effort_shape": "Single card.",
            },
        }
    )

    planning_result = task._initial_taskboard_plan_from_shape_analysis()

    assert planning_result is not None
    assert [card.id for card in planning_result.revision.graph.cards] == ["answer"]
    assert task._taskboard_should_fallback_to_flat(planning_result.revision) is True

    readback_revision = TaskBoardRevision.from_value(
        {
            "board_id": "readback-board",
            "revision_id": "rev-readback",
            "graph": {
                "graph_id": "readback-graph",
                "cards": [
                    TaskBoardCard.from_value(
                        {
                            "id": "readback",
                            "objective": "Read a required artifact.",
                            "allowed_execution_shape": "readback",
                        }
                    ).to_dict()
                ],
            },
        }
    )
    assert task._taskboard_should_fallback_to_flat(readback_revision) is False

    explicit_task = AgentTask(
        agent,
        task_id="taskboard-explicit-no-fallback",
        goal="Answer a simple question.",
        success_criteria=["Return the answer."],
        execution="taskboard",
    )
    assert explicit_task._taskboard_should_fallback_to_flat(planning_result.revision) is False


def test_output_contract_guards_invalid_final_result_after_json_fallback(tmp_path):
    agent = _create_agent("agent-output-final-guard").use_workspace(tmp_path / "workspace")
    task = AgentTask(
        agent,
        task_id="output-final-guard",
        goal="Return structured output.",
        success_criteria=["Final result must be valid JSON."],
        options={
            "execution_prompt_snapshot": {
                "output": {"answer": (str, "Answer", True)},
                "output_format": "hybrid",
            }
        },
    )

    verification = task._normalize_verification(
        {
            "is_complete": True,
            "requires_block": False,
            "reason": "looks complete",
            "missing_criteria": [],
            "final_result_required": True,
            "final_result": '{"answer": "bad quote”}',
        },
        execution_evidence_summary={"status": "success"},
    )

    assert verification["is_complete"] is False
    assert "final_result_output_parse_failed" in verification["guard_reasons"]
    assert "parse as a dict" in verification["missing_criteria"][0]
    assert "hybrid -> json" in verification["missing_criteria"][0]


def test_verification_accepts_file_backed_result_despite_soft_liveness_failure(tmp_path):
    agent = _create_agent("agent-soft-liveness-final").use_workspace(tmp_path / "workspace")
    task = AgentTask(
        agent,
        task_id="soft-liveness-final",
        goal="Produce a file-backed report.",
        success_criteria=["The report exists and satisfies every criterion."],
    )
    idle_error = "AgentExecution made no progress before idle deadline: max_no_progress_seconds=90.0."

    verification = task._normalize_verification(
        {
            "is_complete": True,
            "requires_block": False,
            "reason": "All success criteria are satisfied.",
            "failure_analysis": "All success criteria are satisfied.",
            "acceptance_delta": [],
            "missing_criteria": [],
            "replan_instruction": "Task complete, no replan needed.",
            "next_step_requirements": ["Task complete, no replan needed."],
            "progress_message": "Verification successful: deliverable complete and accepted.",
            "final_result_required": True,
            "final_result": "final.md",
            "criterion_checks": [
                {"criterion": "sections", "status": "satisfied", "summary": "All required sections are present."},
                {"criterion": "grounding", "status": "satisfied", "summary": "Grounding guard is clear."},
            ],
        },
        execution_evidence_summary={
            "status": "failed",
            "errors": [
                {
                    "error_type": "RuntimeStageStallError",
                    "stage": "action_planning",
                    "status": "stalled",
                    "message": idle_error,
                    "last_progress_event": "action_planning.started",
                }
            ],
            "action_ids": ["list_files", "read_file"],
            "action_statuses": {"list_files": "success", "read_file": "success"},
            "failed_actions": [],
            "blocked_actions": [],
            "approval_required_actions": [],
            "artifact_refs": [
                {
                    "path": "final.md",
                    "role": "workspace_artifact",
                    "source": "agent_task.workspace_artifact.stream_drafted",
                    "readback": {"content": "# Report\n\nDone.", "truncated": False},
                }
            ],
        },
        grounding_guard={
            "valid": True,
            "blocking_count": 0,
            "checked_claims": [],
            "diagnostics": [],
        },
    )

    assert verification["is_complete"] is True
    assert "execution_status_failed" not in verification.get("guard_reasons", [])
    assert "Execution step status is failed" not in " ".join(verification.get("missing_criteria", []))
    assert "Execution step status is failed" not in " ".join(verification.get("repair_constraints", []))
    assert verification["final_result_via_workspace_artifact"] is True
    assert verification["non_blocking_execution_status"]["error_type"] == "RuntimeStageStallError"


def test_verification_keeps_liveness_failure_blocking_without_criterion_checks(tmp_path):
    agent = _create_agent("agent-soft-liveness-needs-checks").use_workspace(tmp_path / "workspace")
    task = AgentTask(
        agent,
        task_id="soft-liveness-needs-checks",
        goal="Produce a file-backed report.",
        success_criteria=["The report exists and satisfies every criterion."],
    )

    verification = task._normalize_verification(
        {
            "is_complete": True,
            "requires_block": False,
            "reason": "All success criteria are satisfied.",
            "acceptance_delta": [],
            "missing_criteria": [],
            "replan_instruction": "Task complete, no replan needed.",
            "final_result_required": True,
            "final_result": "final.md",
        },
        execution_evidence_summary={
            "status": "failed",
            "errors": [
                {
                    "error_type": "RuntimeStageStallError",
                    "stage": "action_planning",
                    "status": "stalled",
                    "message": "AgentExecution made no progress before idle deadline.",
                }
            ],
            "action_statuses": {"read_file": "success"},
            "failed_actions": [],
            "blocked_actions": [],
            "approval_required_actions": [],
            "artifact_refs": [
                {
                    "path": "final.md",
                    "role": "workspace_artifact",
                    "source": "agent_task.workspace_artifact.stream_drafted",
                    "readback": {"content": "# Report\n\nDone.", "truncated": False},
                }
            ],
        },
        grounding_guard={
            "valid": True,
            "blocking_count": 0,
            "checked_claims": [],
            "diagnostics": [],
        },
    )

    assert verification["is_complete"] is False
    assert "execution_status_failed" in verification["guard_reasons"]
    assert "non_blocking_execution_status" not in verification


def test_verification_guard_rewrites_conflicting_completion_fields(tmp_path):
    agent = _create_agent("agent-guard-field-alignment").use_workspace(tmp_path / "workspace")
    task = AgentTask(
        agent,
        task_id="guard-field-alignment",
        goal="Produce a grounded report.",
        success_criteria=["Every claim is grounded in bounded readback evidence."],
    )

    verification = task._normalize_verification(
        {
            "is_complete": True,
            "requires_block": False,
            "reason": "All success criteria are satisfied.",
            "failure_analysis": "All success criteria are satisfied.",
            "acceptance_delta": [],
            "missing_criteria": [],
            "replan_instruction": "Task complete, no replan needed.",
            "next_step_requirements": ["Task complete, no replan needed."],
            "progress_message": "Verification successful: deliverable complete and accepted.",
            "final_result_required": True,
            "final_result": "final.md",
        },
        execution_evidence_summary={"status": "completed"},
        grounding_guard={
            "valid": False,
            "blocking_count": 1,
            "checked_claims": [
                {
                    "claim": "A factual claim.",
                    "evidence_ids": ["action_evidence:ref-only"],
                    "support_type": "content",
                }
            ],
            "diagnostics": [
                {
                    "blocking": True,
                    "code": "evidence_ledger.ref_only_item_used_as_content_support",
                    "message": "ref_only evidence supports only discovery/ref-pointer claims until readback evidence exists.",
                }
            ],
        },
    )

    assert verification["is_complete"] is False
    assert "evidence_ledger_grounding_guard_failed" in verification["guard_reasons"]
    assert "Verification successful" not in verification.get("progress_message", "")
    assert "Task complete" not in verification.get("replan_instruction", "")
    assert "Task complete" not in " ".join(verification.get("next_step_requirements", []))
    assert "ref_only evidence" in " ".join(verification.get("missing_criteria", []))


def test_output_contract_accepts_declared_hybrid_final_result(tmp_path):
    agent = _create_agent("agent-hybrid-final-guard").use_workspace(tmp_path / "workspace")
    task = AgentTask(
        agent,
        task_id="hybrid-final-guard",
        goal="Return structured output.",
        success_criteria=["Final result must match the declared hybrid output contract."],
        options={
            "execution_prompt_snapshot": {
                "output": {"answer": (str, "Answer", True), "items": ([str], "Items", True)},
                "output_format": "hybrid",
            }
        },
    )

    verification = task._normalize_verification(
        {
            "is_complete": True,
            "requires_block": False,
            "reason": "looks complete",
            "missing_criteria": [],
            "final_result_required": True,
            "final_result": '### answer\nDone.\n\n### items\n```json\n["a", "b"]\n```',
        },
        execution_evidence_summary={"status": "success"},
    )

    assert verification["is_complete"] is True
    assert "guard_reasons" not in verification


def test_output_contract_accepts_declared_xml_field_final_result(tmp_path):
    agent = _create_agent("agent-xml-field-final-guard").use_workspace(tmp_path / "workspace")
    task = AgentTask(
        agent,
        task_id="xml-field-final-guard",
        goal="Return XML-like structured output.",
        success_criteria=["Final result must match the declared xml_field output contract."],
        options={
            "execution_prompt_snapshot": {
                "output": {
                    "lesson_script": (str, "Long lesson script", True),
                    "review_note": (str, "Review note", True),
                },
                "output_format": "xml_field",
            }
        },
    )

    verification = task._normalize_verification(
        {
            "is_complete": True,
            "requires_block": False,
            "reason": "looks complete",
            "missing_criteria": [],
            "final_result_required": True,
            "final_result": (
                "<agently_output>"
                '<field name="lesson_script" type="text"># Lesson\n\nUse natural prose.</field>'
                '<field name="review_note" type="text">Structured review is separate.</field>'
                "</agently_output>"
            ),
        },
        execution_evidence_summary={"status": "success"},
    )

    assert verification["is_complete"] is True
    assert "guard_reasons" not in verification


def test_output_contract_falls_back_to_json_when_declared_format_fails(tmp_path):
    agent = _create_agent("agent-hybrid-json-final-fallback").use_workspace(tmp_path / "workspace")
    task = AgentTask(
        agent,
        task_id="hybrid-json-final-fallback",
        goal="Return structured output.",
        success_criteria=["Final result must match the declared output contract."],
        options={
            "execution_prompt_snapshot": {
                "output": {"answer": (str, "Answer", True), "items": ([str], "Items", True)},
                "output_format": "hybrid",
            }
        },
    )

    verification = task._normalize_verification(
        {
            "is_complete": True,
            "requires_block": False,
            "reason": "looks complete",
            "missing_criteria": [],
            "final_result_required": True,
            "final_result": '{"answer": "Done.", "items": ["a", "b"]}',
        },
        execution_evidence_summary={"status": "success"},
    )

    assert verification["is_complete"] is True
    assert "guard_reasons" not in verification
    diagnostics = task.diagnostics["final_result_output_contract"][0]
    assert diagnostics["declared_format"] == "hybrid"
    assert diagnostics["resolved_format"] == "json"


@pytest.mark.asyncio
async def test_agent_task_loop_replans_and_records_workspace(tmp_path):
    MockAgentTaskRequester.reset()
    agent = _create_agent()

    task = agent.create_task(
        task_id="legacy-script-upgrade",
        goal="Repair a legacy Agently script so it runs on the current API.",
        success_criteria=[
            "The original failure is recorded.",
            "The script runs successfully.",
            "Verification evidence is stored.",
        ],
        workspace=tmp_path / "task-workspace",
        max_iterations=2,
        limits={"max_model_requests": 1},
        options={"agent_task": {"stream_progress": True}},
    )

    result_facade = task.get_result()
    stream_items = [item async for item in result_facade.get_async_generator(type="instant")]
    result = await result_facade.async_get_data()
    execution_meta = await result_facade.async_get_meta()
    meta = await task.meta()
    delta_text = "".join([chunk async for chunk in task.get_async_generator(type="delta")])
    resumed_execution = await result_facade.async_resume()
    resumed_result = await resumed_execution.async_start()
    resumed_meta = await resumed_execution.async_get_meta()

    assert result["status"] == "completed"
    assert result["iterations"] == 2
    assert result_facade.task_refs["task_id"] == "legacy-script-upgrade"
    assert result_facade.task_refs["status"] == "completed"
    assert execution_meta["task_refs"]["task_id"] == "legacy-script-upgrade"
    assert execution_meta["task_refs"]["status"] == "completed"
    assert resumed_execution.task_refs["task_id"] == "legacy-script-upgrade"
    assert resumed_execution.task_refs["resume"] is True
    assert resumed_result["status"] == "completed"
    assert resumed_result["resumed"] is True
    assert resumed_meta["task_refs"]["status"] == "completed"
    assert meta["status"] == "completed"
    assert len(meta["iterations"]) == 2
    for iteration in meta["iterations"]:
        block_carrier = iteration["execution_meta"]["block_carrier"]
        assert block_carrier["work_unit"]["origin"] == "flat_step"
        assert block_carrier["work_unit_result"]["id"] == block_carrier["work_unit"]["id"]
        assert block_carrier["output_policy"]["body_transport"] == "structured_control"
        blocks = iteration["execution_meta"]["blocks"]
        assert blocks["execution_plan"]["plan_blocks"][0]["kind"] == "agent_step"
        assert blocks["execution_block_graph"]["execution_blocks"][0]["kind"] == "agent_step"
        assert blocks["evidence"]["execution_block_results"][0]["kind"] == "agent_step"
        assert blocks["result"]["semantic_outputs"]
    assert MockAgentTaskRequester.verification_calls == 2
    assert any(item.path == "agent_task.started" for item in stream_items)
    assert any((item.meta or {}).get("stream_kind") == "progress" for item in stream_items)
    assert any((item.meta or {}).get("stream_kind") == "snapshot" for item in stream_items)
    progress_messages = [
        item.value.get("message")
        for item in stream_items
        if (item.meta or {}).get("stream_kind") == "progress" and isinstance(item.value, dict)
    ]
    snapshot_values = [
        item.value
        for item in stream_items
        if (item.meta or {}).get("stream_kind") == "snapshot" and isinstance(item.value, dict)
    ]
    assert any("building a Workspace context pack" in str(message) for message in progress_messages)
    assert any(value.get("stage") == "plan" for value in snapshot_values)
    assert any(value.get("stage") == "verification" for value in snapshot_values)
    child_execution_items = [item for item in stream_items if (item.meta or {}).get("stream_kind") == "child_execution"]
    assert any(item.path == "agent_task.iteration.1.execution.route.selected" for item in child_execution_items)
    assert any(item.path.endswith(".execution.step_result") for item in child_execution_items)
    assert all((item.meta or {}).get("child_execution_id") for item in child_execution_items)
    assert any(item.path.endswith(".replan") for item in stream_items)
    assert any(item.path == "result" for item in stream_items)
    assert "building a Workspace context pack" in delta_text
    assert "plan ready" in delta_text
    assert "bounded step finished" in delta_text
    assert "all success criteria are satisfied" in delta_text
    assert "Final result:" in delta_text
    assert "Operator summary for INC-4242." in delta_text
    phase_names = [item["phase"] for item in meta["diagnostics"]["phases"]]
    assert "configured" in phase_names
    assert "planned" in phase_names
    assert "executing" in phase_names
    assert "evidence_recorded" in phase_names
    assert "verified" in phase_names
    assert "guarded" in phase_names
    assert "replanned" in phase_names
    assert "terminal" in phase_names
    assert any(item.path == "agent_task.phase.verified" for item in stream_items)
    assert any((item.meta or {}).get("stream_kind") == "phase" for item in stream_items)
    assert len(meta["workspace_refs"]["observations"]) == 2
    assert len(meta["workspace_refs"]["decisions"]) == 2
    assert len(meta["workspace_refs"]["verification"]) == 2
    assert len(meta["workspace_refs"]["checkpoints"]) == 2
    assert len(meta["workspace_refs"]["evidence_links"]) >= 6
    assert meta["workspace_refs"]["reflections"]
    workspace = agent.workspace
    assert workspace is not None
    assert len(await workspace.checkpoint_history("legacy-script-upgrade")) == 2
    verifies_links = await workspace.links(relation="verifies_observation")
    decision_links = await workspace.links(relation="implements_decision")
    checkpoint_links = await workspace.links(relation="checkpointed_by")
    reflection_links = await workspace.links(relation="reflects_on")
    assert len(verifies_links) == 2
    assert len(decision_links) == 2
    assert len(checkpoint_links) == 2
    assert reflection_links
    assert all(link["meta"]["evidence"] for link in [*verifies_links, *decision_links, *checkpoint_links])


@pytest.mark.asyncio
async def test_agent_task_loop_progress_stream_is_opt_in(tmp_path):
    MockAgentTaskRequester.reset()
    agent = _create_agent("agent-task-loop-progress-opt-in")

    task = agent.create_task(
        task_id="progress-opt-in",
        goal="Repair a legacy Agently script so it runs on the current API.",
        success_criteria=["The script runs successfully."],
        workspace=tmp_path / "task-workspace",
        max_iterations=1,
        limits={"max_model_requests": 1},
    )

    stream_items = [item async for item in task.get_async_generator(type="instant")]

    assert not any((item.meta or {}).get("stream_kind") == "progress" for item in stream_items)
    assert any((item.meta or {}).get("stream_kind") == "snapshot" for item in stream_items)


@pytest.mark.asyncio
async def test_agent_task_loop_progress_model_uses_snapshot_background(tmp_path):
    MockAgentTaskRequester.reset()
    agent = _create_agent("agent-task-loop-progress-model")

    task = agent.create_task(
        task_id="progress-model",
        goal="Repair a legacy Agently script so it runs on the current API.",
        success_criteria=["The script runs successfully."],
        workspace=tmp_path / "task-workspace",
        max_iterations=1,
        limits={"max_model_requests": 1},
        options={
            "agent_task": {
                "stream_progress": True,
                "progress_model_key": "progress-narrator",
                "progress_timeout_seconds": 5,
            },
        },
    )

    stream_items = [item async for item in task.get_async_generator(type="instant")]
    progress_items = [item for item in stream_items if (item.meta or {}).get("stream_kind") == "progress"]
    progress_delta_items = [item for item in stream_items if (item.meta or {}).get("stream_kind") == "progress_delta"]

    assert progress_items
    assert all((item.meta or {}).get("progress_source") == "model" for item in progress_items)
    assert any("Progress model summarized" in item.value.get("message", "") for item in progress_items)
    assert progress_delta_items
    assert all(item.event_type == "delta" for item in progress_delta_items)
    assert all(item.is_complete is False for item in progress_delta_items)
    assert "Progress model summarized" in "".join(item.delta or "" for item in progress_delta_items)
    assert not any("building a Workspace context pack" in item.value.get("message", "") for item in progress_items)
    assert any("Summarize AgentTask progress" in call for call in MockAgentTaskRequester.calls)


@pytest.mark.asyncio
async def test_agent_task_loop_progress_model_uses_configured_language(tmp_path):
    MockAgentTaskRequester.reset()
    agent = _create_agent("agent-task-loop-progress-language")
    agent.settings.set("agent_task.progress.language", "zh-CN")

    task = agent.create_task(
        task_id="progress-language",
        goal="Repair a legacy Agently script so it runs on the current API.",
        success_criteria=["The script runs successfully."],
        workspace=tmp_path / "task-workspace",
        max_iterations=1,
        limits={"max_model_requests": 1},
        options={
            "agent_task": {
                "stream_progress": True,
                "progress_model_key": "progress-narrator",
                "progress_timeout_seconds": 5,
            },
        },
    )

    stream_items = [item async for item in task.get_async_generator(type="instant")]
    progress_items = [item for item in stream_items if (item.meta or {}).get("stream_kind") == "progress"]

    assert any("progress_language: zh-CN" in call for call in MockAgentTaskRequester.calls)
    assert progress_items
    assert all((item.meta or {}).get("progress_language") == "zh-CN" for item in progress_items)


@pytest.mark.asyncio
async def test_agent_task_loop_uses_agent_language_policy(tmp_path):
    MockAgentTaskRequester.reset()
    agent = _create_agent("agent-task-loop-language-policy")
    agent.language("中文")

    task = agent.create_task(
        task_id="language-policy-task",
        goal="Repair a legacy Agently script so it runs on the current API.",
        success_criteria=["The script runs successfully."],
        workspace=tmp_path / "task-workspace",
        max_iterations=1,
        limits={"max_model_requests": 1},
        options={
            "agent_task": {
                "stream_progress": True,
                "progress_model_key": "progress-narrator",
                "progress_timeout_seconds": 5,
            },
        },
    )

    stream_items = [item async for item in task.get_async_generator(type="instant")]
    progress_items = [item for item in stream_items if (item.meta or {}).get("stream_kind") == "progress"]

    assert any("language_policy" in call and "search_region: cn-zh" in call for call in MockAgentTaskRequester.calls)
    assert progress_items
    assert all((item.meta or {}).get("progress_language") == "zh-CN" for item in progress_items)


@pytest.mark.asyncio
async def test_agent_task_loop_progress_model_omits_developer_diagnostics(tmp_path, monkeypatch):
    MockAgentTaskRequester.reset()
    agent = _create_agent("agent-task-loop-progress-safe-diagnostics")

    task = agent.create_task(
        task_id="progress-safe-diagnostics",
        goal="Repair a legacy Agently script so it runs on the current API.",
        success_criteria=["The script runs successfully."],
        workspace=tmp_path / "task-workspace",
        max_iterations=1,
        limits={"max_model_requests": 1},
        options={
            "agent_task": {
                "stream_progress": True,
                "progress_model_key": "progress-narrator",
                "progress_timeout_seconds": 5,
            },
        },
    )

    async def noisy_context_pack(**_kwargs):
        return {
            "goal": "Repair a legacy Agently script so it runs on the current API.",
            "profile": "auto",
            "items": [],
            "omitted": [],
            "diagnostics": {
                "fallback_reason": {
                    "type": "OperationalError",
                    "message": 'fts5: syntax error near "."; no such column: question',
                },
                "builder": "default",
            },
        }

    assert task.workspace is not None
    monkeypatch.setattr(task.workspace, "build_context", noisy_context_pack)

    stream_items = [item async for item in task.get_async_generator(type="instant")]
    progress_calls = [call for call in MockAgentTaskRequester.calls if "Summarize AgentTask progress" in call]
    meta = await task.meta()

    assert progress_calls
    assert not any("fts5" in call for call in progress_calls)
    assert not any("no such column" in call for call in progress_calls)
    assert not any("fallback_reason" in call for call in progress_calls)
    assert any((item.meta or {}).get("stream_kind") == "snapshot" for item in stream_items)
    assert "progress_errors" not in meta["diagnostics"]


@pytest.mark.asyncio
async def test_agent_task_loop_progress_model_does_not_delay_stream_close(tmp_path):
    class SlowProgressRequester(MockAgentTaskRequester):
        name = "SlowProgressRequester"

        async def request_model(self, request_data: AgentlyRequestData):
            text = json.dumps(DataFormatter.sanitize(request_data.data), ensure_ascii=False)
            if "Summarize AgentTask progress" in text:
                await asyncio.sleep(10)
                yield "message", json.dumps(
                    {"message": "late progress summary"},
                    ensure_ascii=False,
                )
                return
            async for event in super().request_model(request_data):
                yield event

    settings = Settings(name="agent-task-slow-progress-settings", parent=Agently.settings)
    plugin_manager = PluginManager(settings, parent=Agently.plugin_manager, name="agent-task-slow-progress-plugins")
    plugin_manager.register("ModelRequester", SlowProgressRequester, activate=True)
    agent = Agently.AgentType(plugin_manager, parent_settings=settings, name="agent-task-slow-progress")

    task = agent.create_task(
        task_id="slow-progress",
        goal="Repair a legacy Agently script so it runs on the current API.",
        success_criteria=["The script runs successfully."],
        workspace=tmp_path / "task-workspace",
        max_iterations=1,
        limits={"max_model_requests": 1},
        options={
            "agent_task": {
                "stream_progress": True,
                "progress_model_key": "slow-progress-narrator",
                "progress_timeout_seconds": 30,
            },
        },
    )

    stream_items = await asyncio.wait_for(
        _collect_stream(task),
        timeout=5,
    )

    assert any((item.meta or {}).get("stream_kind") == "snapshot" for item in stream_items)
    assert not any((item.meta or {}).get("progress_source") == "model" for item in stream_items)


def test_agent_execution_dynamic_task_candidate_route_is_removed():
    agent = Agently.create_agent("execution-local-dynamic-task")
    execution = agent.create_execution().input("run a local dynamic task graph")

    with pytest.raises(ValueError, match=r"AgentExecution\.use_dynamic_task.*independent DAG workflows"):
        execution.use_dynamic_task(
            mode="submitted",
            plan={
                "graph_id": "execution-local-dynamic-task",
                "task_schema_version": "task_dag/v1",
                "tasks": [{"id": "extract", "kind": "local", "binding": "local_handler"}],
            },
            handlers={"local_handler": lambda context: context.task.id},
        )

    assert not hasattr(execution, "dynamic_task_candidates")
    assert not hasattr(agent, "_dynamic_task_candidates")


@pytest.mark.asyncio
async def test_agent_task_loop_rejects_dag_shaped_step_without_global_candidate_leak(tmp_path):
    class DagStepVerificationRequester(MockAgentTaskRequester):
        name = "DagStepVerificationRequester"

        async def request_model(self, request_data: AgentlyRequestData):
            text = json.dumps(DataFormatter.sanitize(request_data.data), ensure_ascii=False)
            if "Verify the task against every success criterion" in text:
                payload = {
                    "is_complete": True,
                    "requires_block": False,
                    "reason": "the bounded step returned the required evidence",
                    "missing_criteria": [],
                    "replan_instruction": "",
                    "final_result": "Bounded step completed with value ok.",
                }
            else:
                payload = {
                    "step_result": "Direct bounded step returned value ok.",
                    "candidate_final_result": "Bounded step completed with value ok.",
                    "evidence": ["value ok was produced by the direct bounded step."],
                    "remaining_work": [],
                }
            yield "message", json.dumps(payload, ensure_ascii=False)

    async def run_task(context):
        return {"task_id": context.task.id, "value": context.graph_input["value"]}

    settings = Settings(name="agent-task-dag-step-settings", parent=Agently.settings)
    plugin_manager = PluginManager(settings, parent=Agently.plugin_manager, name="agent-task-dag-step-plugins")
    plugin_manager.register("ModelRequester", DagStepVerificationRequester, activate=True)
    agent = Agently.AgentType(plugin_manager, parent_settings=settings, name="agent-task-dag-step")
    graph = {
        "graph_id": "agent-task-loop-dag-step",
        "task_schema_version": "task_dag/v1",
        "tasks": [{"id": "extract", "kind": "local", "binding": "local_handler"}],
        "semantic_outputs": {"final": "extract"},
    }
    task = agent.create_task(
        task_id="dag-shaped-step",
        goal="Return the final DAG result.",
        success_criteria=["The final result includes value ok."],
        workspace=tmp_path / "task-workspace",
        max_iterations=1,
        options={"agent_task": {"effort": {"execution": {"step_plan": "dag"}}}},
    )

    async def request_plan(_iteration_index, _context_pack):
        return {
            "execution_shape": "dynamic_task",
            "step_instruction": "Run the DAG-shaped extraction step.",
            "expected_evidence": "TaskDAG semantic output includes value ok.",
            "rationale": "The step has a clear bounded DAG contract.",
            "dynamic_task": {
                "mode": "submitted",
                "plan": graph,
                "handlers": {"local_handler": run_task},
                "graph_input": {"value": "ok"},
            },
        }

    cast(Any, task)._agent_task_step_overrides = {"_request_plan": request_plan}

    result = await task.async_run()
    meta = await task.async_meta()
    first_iteration = meta["iterations"][0]

    assert result["status"] == "completed"
    assert first_iteration["plan"]["execution_shape"] == "dynamic_task"
    assert first_iteration["plan"]["effective_execution_shape"] == "direct"
    assert first_iteration["plan"]["step_execution"]["dag_shape_degraded"] is True
    assert first_iteration["plan"]["step_execution"]["warning"] == "dag_shape_not_agent_execution_strategy"
    assert first_iteration["plan"]["step_execution"]["policy"]["step_plan"] == "direct"
    assert first_iteration["plan"]["step_execution"]["policy"]["step_plan_degraded_from"] == "dag"
    assert first_iteration["plan"]["step_execution"]["policy"]["allow_dag_steps"] is False
    assert first_iteration["execution_meta"]["route_plan"]["selected_route"] == "model_request"
    blocks = first_iteration["execution_meta"]["blocks"]
    assert blocks["execution_plan"]["plan_blocks"][0]["kind"] == "agent_step"
    assert blocks["execution_plan"]["plan_blocks"][0]["bound_inputs"]["step_plan"] == "direct"
    assert blocks["execution_block_graph"]["execution_blocks"][0]["kind"] == "agent_step"
    assert not hasattr(agent, "_dynamic_task_candidates")


@pytest.mark.asyncio
async def test_agent_task_loop_actions_step_route_policy_prevents_skill_takeover(tmp_path):
    class ActionStepRequester(MockAgentTaskRequester):
        name = "ActionStepRequester"

        async def request_model(self, request_data: AgentlyRequestData):
            text = json.dumps(DataFormatter.sanitize(request_data.data), ensure_ascii=False)
            if "Verify the task against every success criterion" in text:
                payload = {
                    "is_complete": True,
                    "requires_block": False,
                    "reason": "the action-shaped step returned bounded evidence",
                    "missing_criteria": [],
                    "replan_instruction": "",
                    "final_result": "source evidence collected",
                }
            elif "Execute exactly one bounded step" in text:
                payload = {
                    "step_result": "collected repository source evidence",
                    "evidence": ["fetch_agently_architecture_sources returned bounded excerpts"],
                    "remaining_work": [],
                }
            else:
                payload = {"answer": "ok"}
            yield "message", json.dumps(payload, ensure_ascii=False)

    settings = Settings(name="agent-task-action-step-route-settings", parent=Agently.settings)
    plugin_manager = PluginManager(settings, parent=Agently.plugin_manager, name="agent-task-action-step-route-plugins")
    plugin_manager.register("ModelRequester", ActionStepRequester, activate=True)
    agent = Agently.AgentType(plugin_manager, parent_settings=settings, name="agent-task-action-step-route")

    def fetch_agently_architecture_sources():
        return {"status": "ok", "sources": ["architecture evidence"]}

    skill_source = tmp_path / "skill-source" / "architecture-diagram"
    skill_source.mkdir(parents=True)
    (skill_source / "SKILL.md").write_text(
        """---
name: architecture-diagram
description: Use for architecture diagram rendering.
---

# architecture-diagram

Render the final diagram only after source evidence is collected.
""",
        encoding="utf-8",
    )
    Agently.skills_executor.configure(
        registry_root=tmp_path / "skills-registry",
        allowed_trust_levels=["local"],
    )
    Agently.skills_executor.install_skills(skill_source, trust_level="local")

    agent.use_actions(fetch_agently_architecture_sources, always=True)
    agent.use_skills(["architecture-diagram"], mode="model_decision", always=True)

    task = agent.create_task(
        task_id="action-step-route-policy",
        goal="Gather source evidence before rendering with a skill.",
        success_criteria=["Source evidence is collected."],
        workspace=tmp_path / "task-workspace",
        max_iterations=1,
    )

    async def request_plan(_iteration_index, _context_pack):
        return {
            "execution_shape": "actions",
            "step_instruction": "Call fetch_agently_architecture_sources before using any Skill.",
            "expected_evidence": "Repository source evidence is collected.",
            "rationale": "The task must gather source evidence before rendering.",
        }

    cast(Any, task)._agent_task_step_overrides = {"_request_plan": request_plan}

    result = await task.async_run()
    meta = await task.async_meta()
    first_iteration = meta["iterations"][0]

    assert result["status"] == "completed"
    assert first_iteration["plan"]["execution_shape"] == "actions"
    assert first_iteration["plan"]["step_execution"]["route_policy"]["allowed_routes"] == ["model_request"]
    assert first_iteration["execution_meta"]["route_plan"]["selected_route"] == "model_request"
    assert first_iteration["execution_meta"]["route_plan"]["candidates"]["skills"]["model_decision"] is True


@pytest.mark.asyncio
async def test_flat_intermediate_work_unit_skips_independent_verifier(tmp_path):
    agent = _create_agent("agent-task-flat-consumer-driven-sufficiency").use_workspace(tmp_path / "workspace")
    task = agent.create_task(
        task_id="flat-consumer-driven-sufficiency",
        goal="Gather evidence before writing the final answer.",
        success_criteria=["The final answer uses gathered evidence."],
        workspace=tmp_path / "task-workspace",
        max_iterations=1,
    )

    async def request_plan(_iteration_index, _context_pack):
        return {
            "execution_shape": "direct",
            "step_instruction": "Gather intermediate evidence.",
            "expected_evidence": "Intermediate evidence for a later answer.",
            "rationale": "The next step should consume this evidence.",
        }

    async def execute_step(_iteration_index, _plan, _context_pack):
        return (
            {
                "step_result": "Evidence note was captured.",
                "evidence": ["source note"],
                "remaining_work": ["Use the evidence to write the final answer."],
                "ready_for_final_verification": False,
            },
            {
                "execution_id": "exec-intermediate",
                "status": "completed",
                "route": {"selected_route": "model_request"},
                "logs": {},
            },
        )

    async def fail_if_called(*_args, **_kwargs):
        raise AssertionError("intermediate Flat work unit should not call independent verifier")

    cast(Any, task)._agent_task_step_overrides = {
        "_request_plan": request_plan,
        "_execute_step": execute_step,
    }
    cast(Any, task)._request_verification = fail_if_called

    result = await task.async_run()
    meta = await task.async_meta()
    iteration = meta["iterations"][0]

    assert result["status"] == "max_iterations"
    assert iteration["verification_source"] == "consumer_driven_continuation"
    assert iteration["verification"]["is_complete"] is False
    assert iteration["verification"]["consumer_driven_sufficiency"]["consumer"] == "next_flat_iteration"
    assert "Use the evidence" in " ".join(iteration["verification"]["next_step_requirements"])
    assert len(meta["workspace_refs"]["verification"]) == 1


@pytest.mark.asyncio
async def test_flat_remaining_work_without_ready_flag_skips_independent_verifier(tmp_path):
    agent = _create_agent("agent-task-flat-remaining-work-consumer").use_workspace(tmp_path / "workspace")
    task = agent.create_task(
        task_id="flat-remaining-work-consumer",
        goal="Gather evidence before writing the final answer.",
        success_criteria=["The final answer uses gathered evidence."],
        workspace=tmp_path / "task-workspace",
        max_iterations=1,
    )

    async def request_plan(_iteration_index, _context_pack):
        return {
            "execution_shape": "direct",
            "step_instruction": "Gather intermediate evidence.",
            "expected_evidence": "Intermediate evidence for a later answer.",
            "rationale": "The next step should consume this evidence.",
        }

    async def execute_step(_iteration_index, _plan, _context_pack):
        return (
            {
                "step_result": "Evidence note was captured.",
                "evidence": ["source note"],
                "remaining_work": ["Use the evidence to write the final answer."],
            },
            {
                "execution_id": "exec-remaining-work",
                "status": "completed",
                "route": {"selected_route": "model_request"},
                "logs": {},
            },
        )

    async def fail_if_called(*_args, **_kwargs):
        raise AssertionError("non-empty remaining_work should defer independent verifier")

    cast(Any, task)._agent_task_step_overrides = {
        "_request_plan": request_plan,
        "_execute_step": execute_step,
    }
    cast(Any, task)._request_verification = fail_if_called

    result = await task.async_run()
    meta = await task.async_meta()
    iteration = meta["iterations"][0]

    assert result["status"] == "max_iterations"
    assert iteration["verification_source"] == "consumer_driven_continuation"
    assert iteration["verification"]["consumer_driven_sufficiency"]["decision"]["reason"] == (
        "work_unit_reports_remaining_work"
    )
    assert "Use the evidence" in " ".join(iteration["verification"]["next_step_requirements"])
    assert len(meta["workspace_refs"]["verification"]) == 1


@pytest.mark.asyncio
async def test_agent_task_loop_stops_at_max_iterations(tmp_path):
    class NeverCompleteRequester(MockAgentTaskRequester):
        name = "NeverCompleteRequester"

        async def request_model(self, request_data: AgentlyRequestData):
            text = json.dumps(DataFormatter.sanitize(request_data.data), ensure_ascii=False)
            if "Verify the task against every success criterion" in text:
                payload: dict[str, Any] = {
                    "is_complete": False,
                    "requires_block": False,
                    "reason": "still incomplete",
                    "missing_criteria": ["final answer missing"],
                    "replan_instruction": "try one more step",
                    "final_result": "",
                }
            elif "Plan the next bounded AgentExecution step" in text:
                payload = {
                    "step_instruction": "continue analysis",
                    "expected_evidence": "final answer",
                    "rationale": "more evidence needed",
                }
            else:
                payload = {"step_result": "partial", "evidence": ["partial"], "remaining_work": ["final"]}
            yield "message", json.dumps(payload, ensure_ascii=False)

    settings = Settings(name="agent-task-max-settings", parent=Agently.settings)
    plugin_manager = PluginManager(settings, parent=Agently.plugin_manager, name="agent-task-max-plugins")
    plugin_manager.register("ModelRequester", NeverCompleteRequester, activate=True)
    agent = Agently.AgentType(plugin_manager, parent_settings=settings, name="agent-task-max")

    task = agent.create_task(
        task_id="survey-analysis",
        goal="Analyze customer interview responses.",
        success_criteria=["pain points are identified"],
        workspace=tmp_path / "task-workspace",
        max_iterations=1,
    )

    result = await task.async_run()
    meta = await task.async_meta()

    assert result["status"] == "max_iterations"
    assert result["accepted"] is False
    assert result["artifact_status"] == "partial"
    assert meta["status"] == "max_iterations"
    assert len(meta["iterations"]) == 1
    assert len(meta["workspace_refs"]["decisions"]) == 1
    assert len(meta["workspace_refs"]["verification"]) == 1


@pytest.mark.asyncio
async def test_agent_task_loop_blocks_when_verifier_requires_block(tmp_path):
    class BlockedRequester(MockAgentTaskRequester):
        name = "BlockedRequester"

        async def request_model(self, request_data: AgentlyRequestData):
            text = json.dumps(DataFormatter.sanitize(request_data.data), ensure_ascii=False)
            if "Verify the task against every success criterion" in text:
                payload: dict[str, Any] = {
                    "is_complete": True,
                    "requires_block": True,
                    "reason": "external approval is required before continuing",
                    "missing_criteria": [],
                    "replan_instruction": "",
                    "final_result": "draft result should not be accepted",
                }
            elif "Plan the next bounded AgentExecution step" in text:
                payload = {
                    "step_instruction": "prepare the approval-bound change",
                    "expected_evidence": "approval state",
                    "rationale": "the task cannot safely continue without approval",
                }
            else:
                payload = {
                    "step_result": "approval is still pending",
                    "evidence": ["approval_required"],
                    "remaining_work": ["wait for approval"],
                    "ready_for_final_verification": True,
                }
            yield "message", json.dumps(payload, ensure_ascii=False)

    settings = Settings(name="agent-task-blocked-settings", parent=Agently.settings)
    plugin_manager = PluginManager(settings, parent=Agently.plugin_manager, name="agent-task-blocked-plugins")
    plugin_manager.register("ModelRequester", BlockedRequester, activate=True)
    agent = Agently.AgentType(plugin_manager, parent_settings=settings, name="agent-task-blocked")

    task = agent.create_task(
        task_id="blocked-approval",
        goal="Produce the final remediation report after external approval.",
        success_criteria=["The final report is returned only after approval is available."],
        workspace=tmp_path / "task-workspace",
        max_iterations=2,
    )

    result = await task.async_run()
    meta = await task.async_meta()

    assert result["status"] == "blocked"
    assert result["accepted"] is False
    assert result["artifact_status"] == "blocked"
    assert meta["status"] == "blocked"
    assert len(meta["iterations"]) == 1
    verification = meta["iterations"][0]["verification"]
    assert verification["is_complete"] is False
    assert verification["requires_block"] is True
    assert "requires_block_true" in verification["guard_reasons"]
    assert meta["diagnostics"]["verification_guards"][0]["guard_reasons"] == ["requires_block_true"]
    assert meta["diagnostics"]["phases"][-1]["phase"] == "terminal"
    assert meta["diagnostics"]["phases"][-1]["diagnostics"]["artifact_status"] == "blocked"


def test_agent_task_verifier_block_continues_when_untried_read_action_exists(tmp_path):
    agent = _create_agent("agent-task-block-continuation").use_workspace(tmp_path / "task-workspace")
    task = AgentTask(
        agent,
        goal="Collect official source evidence and produce a report.",
        success_criteria=["The report is grounded in source evidence."],
        execution="flat",
        max_iterations=2,
        options={
            "planner_capabilities": [
                {
                    "id": "web_search",
                    "kind": "action",
                    "side_effect_level": "read",
                    "replay_safe": True,
                },
                {
                    "id": "browse",
                    "kind": "action",
                    "side_effect_level": "read",
                    "replay_safe": True,
                },
            ]
        },
    )

    verification = task._normalize_verification(
        {
            "is_complete": False,
            "requires_block": True,
            "reason": "Search failed to locate source evidence.",
            "failure_analysis": "The current evidence-gathering step failed.",
            "acceptance_delta": ["Official source evidence is still missing."],
            "missing_criteria": ["Official source evidence is missing."],
            "replan_instruction": "",
            "final_result_required": True,
            "final_result": "",
        },
        execution_evidence_summary={
            "status": "completed",
            "action_ids": ["web_search"],
            "failed_actions": ["web_search"],
            "blocked_actions": [],
            "approval_required_actions": [],
        },
    )

    assert verification["is_complete"] is False
    assert verification["requires_block"] is False
    assert "untried_read_action_available" in verification["guard_reasons"]
    assert "requires_block_true" not in verification["guard_reasons"]
    assert verification["continuation_opportunities"]["untried_action_ids"] == ["browse"]
    assert verification["replan_instruction"] == "Plan another bounded evidence-gathering step before blocking."
    assert task.diagnostics["verification_continuations"][0]["untried_action_ids"] == ["browse"]


def test_agent_task_verifier_block_does_not_expand_artifact_readback_into_all_read_actions(tmp_path):
    agent = _create_agent("agent-task-artifact-block-no-generic-continuation").use_workspace(
        tmp_path / "task-workspace"
    )
    task = AgentTask(
        agent,
        goal="Verify the final Workspace artifact.",
        success_criteria=["The final report has verifier-readable evidence."],
        execution="taskboard",
        max_iterations=2,
        options={
            "planner_capabilities": [
                {"id": "read_file", "kind": "action", "side_effect_level": "read", "replay_safe": True},
                {"id": "search_files", "kind": "action", "side_effect_level": "read", "replay_safe": True},
                {"id": "write_file", "kind": "action", "side_effect_level": "write", "replay_safe": True},
            ]
        },
    )

    verification = task._normalize_verification(
        {
            "is_complete": False,
            "requires_block": True,
            "reason": "The artifact evidence is still insufficient.",
            "failure_analysis": "A specific section needs scoped readback.",
            "acceptance_delta": ["Scoped artifact evidence is missing."],
            "missing_criteria": ["Scoped artifact evidence is missing."],
            "replan_instruction": "",
            "final_result_required": True,
            "final_result": "Workspace artifact delivered at final.md",
        },
        execution_evidence_summary={
            "status": "completed",
            "action_ids": [],
            "failed_actions": [],
            "blocked_actions": [],
            "approval_required_actions": [],
            "missing_required_actions": [],
            "capability_evidence": {
                "actions": {"succeeded": [], "failed": []},
                "artifacts": {"readback": ["workspace_artifact_readback:test:final.md"]},
            },
        },
    )

    assert verification["requires_block"] is True
    assert "untried_read_action_available" not in verification.get("guard_reasons", [])
    assert "continuation_opportunities" not in verification
    assert "write_file" not in " ".join(verification.get("missing_criteria", []))


def test_optional_failed_read_action_does_not_force_execution_risk_guard(tmp_path):
    agent = _create_agent("agent-task-optional-read-action").use_workspace(tmp_path / "task-workspace")
    task = AgentTask(
        agent,
        goal="Produce a source-grounded brief.",
        success_criteria=["The brief is complete and cites available evidence."],
        execution="flat",
        options={
            "planner_capabilities": [
                {
                    "id": "read_skill_guidance",
                    "kind": "action",
                    "side_effect_level": "read",
                    "replay_safe": True,
                }
            ]
        },
    )

    verification = task._normalize_verification(
        {
            "is_complete": True,
            "requires_block": False,
            "reason": "The final brief is complete; optional guidance was unavailable and disclosed.",
            "failure_analysis": "",
            "acceptance_delta": [],
            "missing_criteria": [],
            "replan_instruction": "",
            "final_result_required": True,
            "final_result": "final.md",
        },
        execution_evidence_summary={
            "status": "completed",
            "action_ids": ["read_skill_guidance"],
            "failed_actions": ["read_skill_guidance"],
            "blocked_actions": [],
            "approval_required_actions": [],
            "required_actions": [],
        },
    )

    assert verification["is_complete"] is True
    assert "execution_risk_actions_present" not in verification.get("guard_reasons", [])
    assert verification["non_blocking_failed_actions"] == ["read_skill_guidance"]
    assert "Unresolved execution risk actions" not in " ".join(verification.get("missing_criteria", []))


def test_required_failed_read_action_still_blocks_execution_risk_guard(tmp_path):
    agent = _create_agent("agent-task-required-read-action").use_workspace(tmp_path / "task-workspace")
    task = AgentTask(
        agent,
        goal="Produce a source-grounded brief.",
        success_criteria=["The required read action succeeds."],
        execution="flat",
        options={
            "planner_capabilities": [
                {
                    "id": "read_skill_guidance",
                    "kind": "action",
                    "side_effect_level": "read",
                    "replay_safe": True,
                }
            ],
            "capability_evidence_requirements": [
                {"capability_id": "read_skill_guidance", "kind": "action_succeeded"}
            ],
        },
    )

    verification = task._normalize_verification(
        {
            "is_complete": True,
            "requires_block": False,
            "reason": "The final brief is complete.",
            "failure_analysis": "",
            "acceptance_delta": [],
            "missing_criteria": [],
            "replan_instruction": "",
            "final_result_required": True,
            "final_result": "final.md",
        },
        execution_evidence_summary={
            "status": "completed",
            "action_ids": ["read_skill_guidance"],
            "failed_actions": ["read_skill_guidance"],
            "blocked_actions": [],
            "approval_required_actions": [],
            "required_actions": [],
        },
    )

    assert verification["is_complete"] is False
    assert "execution_risk_actions_present" in verification["guard_reasons"]
    assert "read_skill_guidance" in " ".join(verification.get("missing_criteria", []))
    assert verification.get("non_blocking_failed_actions") in (None, [])


def test_framework_action_loop_guard_diagnostic_does_not_force_execution_risk_guard(tmp_path):
    agent = _create_agent("agent-task-action-loop-diagnostic").use_workspace(tmp_path / "task-workspace")
    task = AgentTask(
        agent,
        goal="Produce a verified report.",
        success_criteria=["The report is complete and grounded."],
        execution="flat",
    )

    verification = task._normalize_verification(
        {
            "is_complete": True,
            "requires_block": False,
            "reason": "The final report is complete.",
            "failure_analysis": "",
            "acceptance_delta": [],
            "missing_criteria": [],
            "replan_instruction": "",
            "final_result_required": True,
            "final_result": "final.md",
        },
        execution_evidence_summary={
            "status": "completed",
            "action_ids": ["read_file"],
            "failed_actions": [],
            "blocked_actions": ["action_loop"],
            "approval_required_actions": [],
            "required_actions": [],
        },
    )

    assert verification["is_complete"] is True
    assert "execution_risk_actions_present" not in verification.get("guard_reasons", [])
    assert verification["non_blocking_failed_actions"] == ["action_loop"]
    assert "Unresolved execution risk actions" not in " ".join(verification.get("missing_criteria", []))


def test_unknown_failed_action_still_blocks_execution_risk_guard(tmp_path):
    agent = _create_agent("agent-task-unsafe-action").use_workspace(tmp_path / "task-workspace")
    task = AgentTask(
        agent,
        goal="Produce a report.",
        success_criteria=["The report is complete."],
        execution="flat",
    )

    verification = task._normalize_verification(
        {
            "is_complete": True,
            "requires_block": False,
            "reason": "The final report is complete.",
            "failure_analysis": "",
            "acceptance_delta": [],
            "missing_criteria": [],
            "replan_instruction": "",
            "final_result_required": True,
            "final_result": "final.md",
        },
        execution_evidence_summary={
            "status": "completed",
            "action_ids": ["write_file"],
            "failed_actions": ["write_file"],
            "blocked_actions": [],
            "approval_required_actions": [],
        },
    )

    assert verification["is_complete"] is False
    assert "execution_risk_actions_present" in verification["guard_reasons"]
    assert "write_file" in " ".join(verification.get("missing_criteria", []))


@pytest.mark.asyncio
async def test_agent_task_loop_verification_guard_replans_when_missing_criteria_is_present(tmp_path):
    class CompleteWithMissingRequester(MockAgentTaskRequester):
        name = "CompleteWithMissingRequester"
        verification_calls = 0

        async def request_model(self, request_data: AgentlyRequestData):
            text = json.dumps(DataFormatter.sanitize(request_data.data), ensure_ascii=False)
            if "Verify the task against every success criterion" in text:
                CompleteWithMissingRequester.verification_calls += 1
                if CompleteWithMissingRequester.verification_calls == 1:
                    payload = {
                        "is_complete": True,
                        "requires_block": False,
                        "reason": "looks complete but readback is missing",
                        "missing_criteria": ["file readback missing"],
                        "replan_instruction": "",
                        "final_result": "done",
                    }
                else:
                    payload = {
                        "is_complete": True,
                        "requires_block": False,
                        "reason": "readback evidence is now present",
                        "missing_criteria": [],
                        "replan_instruction": "",
                        "final_result": "legacy script upgraded and verified",
                    }
            elif "Plan the next bounded AgentExecution step" in text:
                payload = {
                    "step_instruction": "repair the legacy script using current Agently APIs",
                    "expected_evidence": "script execution succeeds and file is read back",
                    "rationale": "the prior verification gap must be closed",
                }
            elif "Execute exactly one bounded step" in text:
                payload = {
                    "step_result": "patched script and ran verification",
                    "evidence": ["python legacy_script.py exited with status 0", "file readback succeeded"],
                    "remaining_work": [],
                }
            else:
                payload = {"answer": "ok"}
            yield "message", json.dumps(payload, ensure_ascii=False)

    settings = Settings(name="agent-task-guard-missing-settings", parent=Agently.settings)
    plugin_manager = PluginManager(settings, parent=Agently.plugin_manager, name="agent-task-guard-missing-plugins")
    plugin_manager.register("ModelRequester", CompleteWithMissingRequester, activate=True)
    agent = Agently.AgentType(plugin_manager, parent_settings=settings, name="agent-task-guard-missing")

    task = agent.create_task(
        task_id="guard-missing",
        goal="Repair a legacy Agently script so it runs on the current API.",
        success_criteria=["The final file readback evidence is included."],
        workspace=tmp_path / "task-workspace",
        max_iterations=2,
    )

    stream_items = [item async for item in task.get_async_generator(type="instant")]
    result = await task.run()
    meta = await task.meta()

    assert result["status"] == "completed"
    assert result["iterations"] == 2
    assert any(item.path.endswith(".replan") for item in stream_items)
    assert meta["iterations"][0]["verification"]["is_complete"] is False
    assert "missing_criteria_present" in meta["iterations"][0]["verification"]["guard_reasons"]
    assert meta["diagnostics"]["verification_guards"]


@pytest.mark.asyncio
async def test_agent_task_loop_verification_guard_replans_when_final_result_missing(tmp_path):
    class CompleteWithoutFinalResultRequester(MockAgentTaskRequester):
        name = "CompleteWithoutFinalResultRequester"
        verification_calls = 0

        async def request_model(self, request_data: AgentlyRequestData):
            text = json.dumps(DataFormatter.sanitize(request_data.data), ensure_ascii=False)
            if "Verify the task against every success criterion" in text:
                CompleteWithoutFinalResultRequester.verification_calls += 1
                if CompleteWithoutFinalResultRequester.verification_calls == 1:
                    payload = {
                        "is_complete": True,
                        "requires_block": False,
                        "reason": "all evidence is present but no final deliverable was returned",
                        "missing_criteria": [],
                        "replan_instruction": "",
                        "final_result_required": True,
                        "final_result": "",
                    }
                else:
                    payload = {
                        "is_complete": True,
                        "requires_block": False,
                        "reason": "final deliverable is now included",
                        "missing_criteria": [],
                        "replan_instruction": "",
                        "final_result_required": True,
                        "final_result": "Final remediation report with verification evidence.",
                    }
            elif "Plan the next bounded AgentExecution step" in text:
                payload = {
                    "step_instruction": "produce the final report artifact",
                    "expected_evidence": "final report text",
                    "rationale": "the verifier requires the final deliverable before acceptance",
                }
            elif "Execute exactly one bounded step" in text:
                payload = {
                    "step_result": "prepared final report artifact",
                    "evidence": ["final report content is present"],
                    "remaining_work": [],
                }
            else:
                payload = {"answer": "ok"}
            yield "message", json.dumps(payload, ensure_ascii=False)

    settings = Settings(name="agent-task-final-result-settings", parent=Agently.settings)
    plugin_manager = PluginManager(settings, parent=Agently.plugin_manager, name="agent-task-final-result-plugins")
    plugin_manager.register("ModelRequester", CompleteWithoutFinalResultRequester, activate=True)
    agent = Agently.AgentType(plugin_manager, parent_settings=settings, name="agent-task-final-result")

    task = agent.create_task(
        task_id="guard-final-result",
        goal="Return the final remediation report.",
        success_criteria=["The final report artifact is returned."],
        workspace=tmp_path / "task-workspace",
        max_iterations=2,
    )

    stream_items = [item async for item in task.get_async_generator(type="instant")]
    result = await task.run()
    meta = await task.meta()

    assert result["status"] == "completed"
    assert result["accepted"] is True
    assert result["artifact_status"] == "accepted"
    assert result["iterations"] == 2
    assert any(item.path.endswith(".replan") for item in stream_items)
    first_verification = meta["iterations"][0]["verification"]
    assert first_verification["is_complete"] is False
    assert "final_result_missing" in first_verification["guard_reasons"]
    assert "Final result is missing." in first_verification["missing_criteria"]
    assert first_verification["replan_instruction"]
    assert (
        meta["iterations"][1]["verification"]["final_result"] == "Final remediation report with verification evidence."
    )


@pytest.mark.asyncio
async def test_agent_task_loop_verification_guard_replans_on_failed_action_evidence(tmp_path):
    class AlwaysCompleteRequester(MockAgentTaskRequester):
        name = "AlwaysCompleteRequester"

        async def request_model(self, request_data: AgentlyRequestData):
            text = json.dumps(DataFormatter.sanitize(request_data.data), ensure_ascii=False)
            if "Verify the task against every success criterion" in text:
                payload = {
                    "is_complete": True,
                    "requires_block": False,
                    "reason": "all criteria are satisfied",
                    "missing_criteria": [],
                    "replan_instruction": "",
                    "final_result": "legacy script upgraded and verified",
                }
            elif "Plan the next bounded AgentExecution step" in text:
                payload = {
                    "step_instruction": "run the verification command",
                    "expected_evidence": "command succeeds",
                    "rationale": "the task needs command evidence",
                }
            else:
                payload = {"answer": "ok"}
            yield "message", json.dumps(payload, ensure_ascii=False)

    settings = Settings(name="agent-task-guard-action-settings", parent=Agently.settings)
    plugin_manager = PluginManager(settings, parent=Agently.plugin_manager, name="agent-task-guard-action-plugins")
    plugin_manager.register("ModelRequester", AlwaysCompleteRequester, activate=True)
    agent = Agently.AgentType(plugin_manager, parent_settings=settings, name="agent-task-guard-action")
    task = agent.create_task(
        task_id="guard-action",
        goal="Repair a legacy Agently script and return the final verified result.",
        success_criteria=["The verification command succeeds.", "The final result is returned."],
        workspace=tmp_path / "task-workspace",
        max_iterations=2,
    )

    async def fake_execute(iteration_index, plan, context_pack):
        _ = (plan, context_pack)
        status = "failed" if iteration_index == 1 else "success"
        return (
            {"step_result": f"iteration {iteration_index}", "evidence": [status], "remaining_work": []},
            {
                "execution_id": f"exec-{iteration_index}",
                "status": "completed",
                "route": {"selected_route": "model_request"},
                "logs": {
                    "action_logs": {
                        "run_task_command": {
                            "name": "run_task_command",
                            "status": status,
                            "action_type": "shell",
                        }
                    }
                },
            },
        )

    cast(Any, task)._agent_task_step_overrides = {"_execute_step": fake_execute}

    result = await task.run()
    meta = await task.meta()

    assert result["status"] == "completed"
    assert result["iterations"] == 2
    assert any(phase["phase"] == "replanned" for phase in meta["diagnostics"]["phases"])
    first_verification = meta["iterations"][0]["verification"]
    assert first_verification["is_complete"] is False
    assert "execution_risk_actions_present" in first_verification["guard_reasons"]
    second_verification = meta["iterations"][1]["verification"]
    assert second_verification["is_complete"] is True
    assert "execution_risk_actions_present" not in second_verification.get("guard_reasons", [])
    second_logs = meta["iterations"][1]["execution_meta"]["logs"]["action_logs"]
    assert second_logs["run_task_command"]["status"] == "success"


@pytest.mark.asyncio
async def test_agent_task_loop_replans_on_structured_blocks_replan_signal(tmp_path):
    class AlwaysCompleteRequester(MockAgentTaskRequester):
        name = "AlwaysCompleteForReplanSignalRequester"

        async def request_model(self, request_data: AgentlyRequestData):
            text = json.dumps(DataFormatter.sanitize(request_data.data), ensure_ascii=False)
            if "Verify the task against every success criterion" in text:
                payload = {
                    "is_complete": True,
                    "requires_block": False,
                    "reason": "model thinks the task is complete",
                    "missing_criteria": [],
                    "replan_instruction": "",
                    "final_result_required": True,
                    "final_result": "final report",
                }
            elif "Plan the next bounded AgentExecution step" in text:
                payload = {
                    "step_instruction": "collect structured evidence",
                    "expected_evidence": "valid upstream evidence",
                    "rationale": "the step needs trusted evidence",
                }
            else:
                payload = {"answer": "ok"}
            yield "message", json.dumps(payload, ensure_ascii=False)

    settings = Settings(name="agent-task-replan-signal-settings", parent=Agently.settings)
    plugin_manager = PluginManager(settings, parent=Agently.plugin_manager, name="agent-task-replan-signal-plugins")
    plugin_manager.register("ModelRequester", AlwaysCompleteRequester, activate=True)
    agent = Agently.AgentType(plugin_manager, parent_settings=settings, name="agent-task-replan-signal")
    task = agent.create_task(
        task_id="structured-replan-signal",
        goal="Produce a final report only after structured execution evidence is valid.",
        success_criteria=["The upstream evidence is valid.", "The final report is returned."],
        workspace=tmp_path / "task-workspace",
        max_iterations=2,
    )

    async def fake_execute(iteration_index, plan, context_pack):
        _ = (plan, context_pack)
        replan_diagnostics = []
        if iteration_index == 1:
            replan_diagnostics.append(
                {
                    "kind": "replan_signal",
                    "status": "replan_goal",
                    "reason": "upstream evidence invalidates the current goal plan",
                    "affected_plan_block_ids": ["collect"],
                    "affected_execution_block_ids": ["collect:model_request"],
                }
            )
        return (
            {"step_result": f"iteration {iteration_index}", "evidence": ["candidate"], "remaining_work": []},
            {
                "execution_id": f"exec-{iteration_index}",
                "status": "completed",
                "route": {"selected_route": "model_request"},
                "logs": {"action_logs": {}},
                "blocks": {
                    "evidence": {"diagnostics": replan_diagnostics},
                    "snapshot": {"blocks": {"replan_signals": replan_diagnostics}},
                },
            },
        )

    cast(Any, task)._agent_task_step_overrides = {"_execute_step": fake_execute}

    result = await task.async_run()
    meta = await task.async_meta()

    assert result["status"] == "completed"
    assert result["iterations"] == 2
    first_verification = meta["iterations"][0]["verification"]
    assert first_verification["is_complete"] is False
    assert first_verification["replan_signals"][0]["status"] == "replan_goal"
    assert "structured_replan_signal" in first_verification["guard_reasons"]
    assert any(
        phase["phase"] == "replanned" and phase["diagnostics"]["replan_signals"][0]["status"] == "replan_goal"
        for phase in meta["diagnostics"]["phases"]
    )


@pytest.mark.asyncio
async def test_required_capabilities_satisfied_cumulatively_across_iterations(tmp_path):
    """ISSUE-012: required actions and skills can be satisfied in different steps."""

    class CumulativeRequester(MockAgentTaskRequester):
        name = "CumulativeRequester"

        async def request_model(self, request_data: AgentlyRequestData):
            text = json.dumps(DataFormatter.sanitize(request_data.data), ensure_ascii=False)
            if "Verify the task against every success criterion" in text:
                payload = {
                    "is_complete": True,
                    "requires_block": False,
                    "reason": "evidence is present",
                    "missing_criteria": [],
                    "replan_instruction": "",
                    "final_result_required": False,
                    "final_result": "done",
                }
            elif "Plan the next bounded AgentExecution step" in text:
                payload = {
                    "step_instruction": "produce capability evidence",
                    "expected_evidence": "x",
                    "rationale": "y",
                }
            else:
                payload = {"answer": "ok"}
            yield "message", json.dumps(payload, ensure_ascii=False)

    settings = Settings(name="agent-task-cumulative-settings", parent=Agently.settings)
    plugin_manager = PluginManager(settings, parent=Agently.plugin_manager, name="agent-task-cumulative-plugins")
    plugin_manager.register("ModelRequester", CumulativeRequester, activate=True)
    agent = Agently.AgentType(plugin_manager, parent_settings=settings, name="agent-task-cumulative")

    constraints = {"capability_constraints": {"actions": {"required": ["act_x"]}, "skills": {"required": ["skill_y"]}}}

    async def step_with_capability(iteration_index, plan, context_pack):
        # iteration 1 satisfies the required action, iteration 2 the required skill.
        if iteration_index == 1:
            logs = {"action_logs": {"act_x": {"name": "act_x", "status": "success"}}, "route_logs": {}}
        else:
            logs = {"action_logs": {}, "route_logs": {"plan": {"selected_skills": [{"skill_id": "skill_y"}]}}}
        return (
            {"step_result": f"iteration {iteration_index}", "evidence": ["ok"], "remaining_work": []},
            {
                "execution_id": f"exec-{iteration_index}",
                "status": "completed",
                "route": {"selected_route": "model_request" if iteration_index == 1 else "skills"},
                "logs": logs,
                "effective_options": constraints,
            },
        )

    task = agent.create_task(
        task_id="cumulative-capabilities",
        goal="Satisfy required action and skill across steps.",
        success_criteria=["The required action and skill are both used."],
        workspace=tmp_path / "task-workspace",
        max_iterations=3,
        options={"capability_constraints": constraints["capability_constraints"]},
    )
    cast(Any, task)._agent_task_step_overrides = {"_execute_step": step_with_capability}

    result = await task.async_run()
    meta = await task.async_meta()

    # Iteration 1 cannot complete (skill_y still missing); iteration 2 completes.
    assert result["status"] == "completed"
    assert result["iterations"] == 2
    first = meta["iterations"][0]["verification"]
    assert first["is_complete"] is False
    assert "skill_y" in " ".join(first.get("missing_required_capabilities", []))


@pytest.mark.asyncio
async def test_agent_task_resumes_from_checkpoint_after_crash(tmp_path):
    """ISSUE-005: a task continues from its last durable snapshot in a fresh task object."""
    MockAgentTaskRequester.reset()
    workspace_dir = tmp_path / "task-workspace"

    # First run: iteration 1 replans (verifier incomplete), then a simulated crash
    # before iteration 2 by raising inside the step of iteration 2.
    agent = _create_agent("agent-task-resume-1").use_workspace(workspace_dir)
    task = agent.create_task(
        task_id="resumable-task",
        goal="Repair a legacy Agently script so it runs on the current API.",
        success_criteria=["The script runs successfully."],
        workspace=workspace_dir,
        max_iterations=3,
    )

    async def crash_on_second_iteration(iteration_index, plan, context_pack):
        if iteration_index >= 2:
            raise RuntimeError("simulated process crash")
        return (
            {"step_result": "iteration 1 partial", "evidence": ["progress"], "remaining_work": ["finish"]},
            {"execution_id": "exec-1", "status": "completed", "route": {"selected_route": "model_request"}, "logs": {}},
        )

    cast(Any, task)._agent_task_step_overrides = {"_execute_step": crash_on_second_iteration}
    with pytest.raises(RuntimeError):
        await task.async_run()

    # A resume snapshot for iteration 1 must have been persisted (namespaced so
    # it does not mix with the task's per-step observation checkpoints).
    snapshot = await agent.workspace.get_snapshot("resumable-task::resume")
    assert snapshot is not None
    assert snapshot["iteration"] == 1
    assert snapshot["manifest"]["goal"].startswith("Repair a legacy")
    # The bare task_id checkpoint history is unaffected by resume snapshots.
    assert len(await agent.workspace.checkpoint_history("resumable-task")) == 1

    # Second run: a fresh AgentExecution resumes from the snapshot and completes.
    MockAgentTaskRequester.reset()
    agent2 = _create_agent("agent-task-resume-2").use_workspace(workspace_dir)
    resumed = await agent2.async_resume("resumable-task", workspace=workspace_dir)

    async def finish_step(iteration_index, plan, context_pack):
        return (
            {"step_result": "completed", "evidence": ["done"], "remaining_work": []},
            {
                "execution_id": f"exec-{iteration_index}",
                "status": "completed",
                "route": {"selected_route": "model_request"},
                "logs": {},
            },
        )

    cast(Any, resumed)._agent_task_step_overrides = {"_execute_step": finish_step}
    result = await resumed.async_start()
    execution_meta = await resumed.async_get_meta()
    meta = execution_meta["logs"]["route_logs"]["agent_task"]

    assert resumed.task_refs["resume"] is True
    assert resumed.task_refs["resumed_from_iteration"] == 1
    assert meta["resumed_from_iteration"] == 1
    # Continued from iteration 2 (did not re-run iteration 1).
    assert meta["iterations"][0]["iteration"] == 2
    assert result["status"] == "completed"


@pytest.mark.asyncio
async def test_agent_task_resume_without_snapshot_raises(tmp_path):
    """ISSUE-005: resuming an unknown task id is an explicit error."""
    agent = _create_agent("agent-task-resume-missing").use_workspace(tmp_path / "task-workspace")
    with pytest.raises(ValueError):
        await agent.async_resume("does-not-exist", workspace=tmp_path / "task-workspace")


@pytest.mark.asyncio
async def test_task_wall_clock_budget_surfaces_timed_out(tmp_path):
    """ISSUE-010: max_seconds is a task wall-clock deadline across task stages."""
    agent = _create_agent("agent-task-deadline").use_workspace(tmp_path / "task-workspace")
    task = agent.create_task(
        task_id="deadline",
        goal="Repair a legacy Agently script so it runs on the current API.",
        success_criteria=["The script runs successfully."],
        workspace=tmp_path / "task-workspace",
        max_iterations=3,
        limits={"max_seconds": 0.2},
    )

    async def slow_request_plan(_iteration_index, _context_pack):
        await asyncio.sleep(0.6)
        return {
            "step_instruction": "repair the script",
            "expected_evidence": "script execution succeeds",
            "rationale": "the task should not reach this plan after the deadline",
        }

    cast(Any, task)._agent_task_step_overrides = {"_request_plan": slow_request_plan}

    result = await task.async_run()
    assert result["status"] == "timed_out"
    assert task.status == "timed_out"
    assert "plan stage" in result["reason"]


def test_action_final_status_exempts_recovered_actions():
    """ISSUE-012: an action that failed then succeeded is not a risk action."""
    from agently.core.application.AgentTask.Task import AgentTask

    statuses = {"act_a": "success", "act_b": "failed"}
    failed = AgentTask._action_ids_by_final_status(statuses, {"failed", "failure", "error"})
    assert failed == ["act_b"]
    assert "act_a" not in failed


@pytest.mark.asyncio
async def test_interview_semantic_judge_returns_structured_rule_fields():
    class SemanticJudgeRequester(MockAgentTaskRequester):
        name = "SemanticJudgeRequester"

        async def request_model(self, request_data: AgentlyRequestData):
            text = json.dumps(DataFormatter.sanitize(request_data.data), ensure_ascii=False)
            assert "Judge the candidate interview brief semantically" in text
            payload = {
                "accepted": False,
                "source_specificity_ok": False,
                "target_coverage_ok": True,
                "conflict_handling_ok": False,
                "low_evidence_handling_ok": False,
                "blog_interview_quality_ok": True,
                "not_hiring_framed_ok": True,
                "reason": "Sources are too generic and uncertainty is not handled.",
                "rule_evidence": [
                    {
                        "rule": "source_specificity",
                        "ok": False,
                        "evidence": "The brief says sources exist but gives no URL or title.",
                    }
                ],
            }
            yield "message", json.dumps(payload, ensure_ascii=False)

    settings = Settings(name="interview-semantic-judge-settings", parent=Agently.settings)
    plugin_manager = PluginManager(settings, parent=Agently.plugin_manager, name="interview-semantic-judge-plugins")
    plugin_manager.register("ModelRequester", SemanticJudgeRequester, activate=True)
    agent = Agently.AgentType(plugin_manager, parent_settings=settings, name="interview-semantic-judge")

    result = await judge_interview_semantics(
        agent,
        file_text="# Interview brief\n\nSources: public web.\n\nQuestions?\n",
        interview_input={
            "targets": [
                {
                    "raw_input": "Karpathy from Anthropic",
                    "original_name": "Karpathy",
                    "organization_or_work": "Anthropic",
                    "aliases": [],
                }
            ],
            "interview_goal": "Prepare a blog interview brief.",
        },
        success_criteria=["The brief handles source evidence and target ambiguity."],
        action_summary={"action_log_count": 1, "action_log_ids": ["web_search"]},
    )

    assert result["accepted"] is False
    assert result["source_specificity_ok"] is False
    assert result["target_coverage_ok"] is True
    assert result["rule_evidence"]


@pytest.mark.asyncio
async def test_agent_task_loop_progress_model_failure_is_side_channel(tmp_path):
    MockAgentTaskRequester.reset()

    class FailingProgressRequester(MockAgentTaskRequester):
        name = "FailingProgressRequester"

        async def request_model(self, request_data: AgentlyRequestData):
            text = json.dumps(DataFormatter.sanitize(request_data.data), ensure_ascii=False)
            if "Summarize AgentTask progress" in text:
                raise RuntimeError("progress model unavailable")
            async for event in super().request_model(request_data):
                yield event

    settings = Settings(name="agent-task-failing-progress-settings", parent=Agently.settings)
    plugin_manager = PluginManager(settings, parent=Agently.plugin_manager, name="agent-task-failing-progress-plugins")
    plugin_manager.register("ModelRequester", FailingProgressRequester, activate=True)
    agent = Agently.AgentType(plugin_manager, parent_settings=settings, name="agent-task-failing-progress")
    captured = []

    async def capture(event):
        captured.append(event)

    hook_name = "test_agent_task_loop_progress_model_failure_is_side_channel.capture"
    Agently.event_center.register_hook(capture, hook_name=hook_name)
    try:
        task = agent.create_task(
            task_id="failing-progress",
            goal="Repair a legacy Agently script so it runs on the current API.",
            success_criteria=["The script runs successfully."],
            workspace=tmp_path / "task-workspace",
            max_iterations=1,
            options={
                "agent_task": {
                    "stream_progress": True,
                    "progress_model_key": "progress-narrator",
                    "progress_timeout_seconds": 5,
                },
            },
        )

        result = await task.async_run()
        meta = await task.async_meta()
    finally:
        Agently.event_center.unregister_hook(hook_name)

    event_types = [event.event_type for event in captured]
    side_channel_events = [
        event
        for event in captured
        if event.event_type in {"model.side_channel_request_failed", "request.side_channel_failed"}
    ]

    assert result["status"] == "max_iterations"
    assert side_channel_events
    assert "model.request_failed" not in event_types
    assert "request.failed" not in event_types
    assert all(event.level == "WARNING" for event in side_channel_events)
    assert meta["diagnostics"]["progress_errors"]


async def _collect_stream(task) -> list[Any]:
    return [item async for item in task.get_async_generator(type="instant")]


def _capability_gate_agent(name: str):
    """Agent whose verifier always claims completion (drives the gate tests)."""

    class AlwaysCompleteVerifier(MockAgentTaskRequester):
        name = "CapabilityGateVerifier"

        async def request_model(self, request_data: AgentlyRequestData):
            text = json.dumps(DataFormatter.sanitize(request_data.data), ensure_ascii=False)
            MockAgentTaskRequester.calls.append(text)
            if "Verify the task against every success criterion" in text:
                payload = {
                    "is_complete": True,
                    "requires_block": False,
                    "reason": "looks done from the verifier's view",
                    "missing_criteria": [],
                    "replan_instruction": "",
                    "final_result_required": False,
                    "final_result": "done",
                }
            elif "Plan the next bounded AgentExecution step" in text:
                payload = {
                    "execution_shape": "direct",
                    "step_instruction": "produce the artifact directly",
                    "expected_evidence": "x",
                    "rationale": "y",
                }
            elif "Execute exactly one bounded step" in text:
                payload = {
                    "step_result": "produced the artifact",
                    "evidence": ["artifact written"],
                    "remaining_work": [],
                }
            else:
                payload = {"answer": "ok"}
            yield "message", json.dumps(payload, ensure_ascii=False)

    settings = Settings(name=f"{name}-settings", parent=Agently.settings)
    plugin_manager = PluginManager(settings, parent=Agently.plugin_manager, name=f"{name}-plugins")
    plugin_manager.register("ModelRequester", AlwaysCompleteVerifier, activate=True)
    return Agently.AgentType(plugin_manager, parent_settings=settings, name=name)


@pytest.mark.asyncio
async def test_capability_evidence_gate_blocks_bypass_when_capability_unused(tmp_path):
    """AGENT_TASK_CAPABILITY_AWARE_EXECUTION_QUALITY_SPEC (load-bearing gate).

    The verifier claims completion, but the required capability never appears in
    execution evidence. The structured capability-evidence requirement must turn
    this into a hard verification failure, so 'accepted with the capability
    bypassed' becomes impossible — regardless of the capability kind.
    """
    agent = _capability_gate_agent("agent-task-capability-bypass")

    async def bypass_step(iteration_index, plan, context_pack):
        # model_request route, no skill selected -> the capability was bypassed.
        return (
            {"step_result": "did it without the capability", "evidence": ["wrote a file"], "remaining_work": []},
            {
                "execution_id": f"exec-{iteration_index}",
                "status": "completed",
                "route": {"selected_route": "model_request"},
                "logs": {
                    "action_logs": {"write_file": {"name": "write_file", "status": "success"}},
                    "route_logs": {},
                },
            },
        )

    task = agent.create_task(
        task_id="capability-evidence-bypass",
        goal="Produce the artifact using the intended capability.",
        success_criteria=["The artifact reflects the intended capability."],
        workspace=tmp_path / "task-workspace",
        max_iterations=1,
        options={
            "capability_evidence_requirements": [
                {"capability_id": "design-skill", "capability_kind": "skill", "kind": "capability_used"}
            ]
        },
    )
    cast(Any, task)._agent_task_step_overrides = {"_execute_step": bypass_step}

    result = await task.async_run()
    meta = await task.async_meta()

    assert result["status"] != "completed"
    assert result["accepted"] is False
    verification = meta["iterations"][0]["verification"]
    assert verification["is_complete"] is False
    assert "capability_evidence_missing" in verification.get("guard_reasons", [])
    assert "design-skill" in " ".join(verification.get("missing_capability_evidence", []))
    assert "design-skill" in " ".join(verification.get("missing_required_capabilities", []))


@pytest.mark.asyncio
async def test_execution_exception_becomes_verifier_visible_failed_evidence(tmp_path, monkeypatch):
    """A bounded step runtime error must not crash AgentTask before the
    observation/verifier path can see it. The deterministic guard blocks a
    verifier that incorrectly claims completion over failed execution evidence."""
    agent = _capability_gate_agent("agent-task-execution-failure-evidence")

    class FailingExecution:
        id = "exec-failed-route"

        def __init__(self):
            self.local_action_ids: list[str] = []

        def input(self, value):
            return self

        def instruct(self, value):
            return self

        def output(self, value, *, format=None):
            return self

        def language(self, value):
            return self

        async def async_get_data(self):
            raise RuntimeError("runtime placeholder path is invalid")

        async def async_get_meta(self):
            return {"execution_id": self.id, "status": "failed", "route": {"selected_route": "direct"}, "logs": {}}

    task = agent.create_task(
        task_id="execution-failure-evidence",
        goal="Produce the artifact using a bounded execution step.",
        success_criteria=["The artifact is produced."],
        workspace=tmp_path / "task-workspace",
        max_iterations=1,
    )
    monkeypatch.setattr(agent, "create_execution", lambda **kwargs: FailingExecution())

    result = await task.async_run()
    meta = await task.async_meta()

    assert result["status"] == "max_iterations"
    assert result["accepted"] is False
    execution_meta = meta["iterations"][0]["execution_meta"]
    assert execution_meta["status"] == "failed"
    assert execution_meta["logs"]["errors"][0]["message"] == "runtime placeholder path is invalid"
    verification = meta["iterations"][0]["verification"]
    assert verification["is_complete"] is False
    assert "execution_status_failed" in verification.get("guard_reasons", [])
    assert "runtime placeholder path is invalid" in " ".join(verification.get("missing_criteria", []))
    assert meta["diagnostics"]["execution_errors"][0]["message"] == "runtime placeholder path is invalid"


@pytest.mark.asyncio
async def test_execution_exception_compacts_provider_request_payload_for_hot_paths(tmp_path, monkeypatch):
    """Provider errors may mention request payloads, but AgentTask hot paths
    must keep only a compact error fact for verifier/planner input."""
    agent = _capability_gate_agent("agent-task-provider-error-compaction")
    huge_request_payload = (
        'Request Data: {"messages": ["SECRET_PROMPT_SHOULD_NOT_ENTER_VERIFIER", "'
        + ("large-token " * 500)
        + '"], "tools": ["SECRET_TOOL_SCHEMA_SHOULD_NOT_ENTER_VERIFIER"]}'
    )
    provider_error = (
        "Status Code: 403\n"
        "Response: {'code': 'AllocationQuota.FreeTierOnly', 'message': 'quota exhausted'}\n"
        + huge_request_payload
    )

    class FailingExecution:
        id = "exec-provider-error"

        def __init__(self):
            self.local_action_ids: list[str] = []

        def input(self, value):
            return self

        def instruct(self, value):
            return self

        def output(self, value, *, format=None):
            return self

        def language(self, value):
            return self

        async def async_get_data(self):
            raise RuntimeError(provider_error)

        async def async_get_meta(self):
            return {"execution_id": self.id, "status": "failed", "route": {"selected_route": "direct"}, "logs": {}}

    task = agent.create_task(
        task_id="provider-error-compaction",
        goal="Produce the artifact using a bounded execution step.",
        success_criteria=["The artifact is produced."],
        workspace=tmp_path / "task-workspace",
        max_iterations=1,
    )
    monkeypatch.setattr(agent, "create_execution", lambda **kwargs: FailingExecution())

    result = await task.async_run()
    meta = await task.async_meta()

    assert result["status"] == "max_iterations"
    hot_path_text = json.dumps(
        {
            "diagnostics": meta["diagnostics"],
            "execution_meta": meta["iterations"][0]["execution_meta"],
            "verification": meta["iterations"][0]["verification"],
        },
        ensure_ascii=False,
    )
    assert "Status Code: 403" in hot_path_text
    assert "AllocationQuota.FreeTierOnly" in hot_path_text
    assert "Request Data" not in hot_path_text
    assert "SECRET_PROMPT_SHOULD_NOT_ENTER_VERIFIER" not in hot_path_text
    assert "SECRET_TOOL_SCHEMA_SHOULD_NOT_ENTER_VERIFIER" not in hot_path_text
    error_messages = [
        meta["diagnostics"]["execution_errors"][0]["message"],
        meta["iterations"][0]["execution_meta"]["logs"]["errors"][0]["message"],
        meta["iterations"][0]["execution_meta"]["diagnostics"]["execution_error"]["message"],
    ]
    assert all(len(message) < 2500 for message in error_messages)


@pytest.mark.asyncio
async def test_capability_evidence_gate_passes_when_capability_used(tmp_path):
    """The same requirement passes when the capability genuinely shows up in
    execution evidence (here, a skills-route selected_skills record)."""
    agent = _capability_gate_agent("agent-task-capability-present")

    async def skills_step(iteration_index, plan, context_pack):
        return (
            {"step_result": "used the capability", "evidence": ["rendered via capability"], "remaining_work": []},
            {
                "execution_id": f"exec-{iteration_index}",
                "status": "completed",
                "route": {"selected_route": "skills"},
                "logs": {
                    "action_logs": {},
                    "route_logs": {"plan": {"selected_skills": [{"skill_id": "design-skill"}]}},
                },
            },
        )

    task = agent.create_task(
        task_id="capability-evidence-present",
        goal="Produce the artifact using the intended capability.",
        success_criteria=["The artifact reflects the intended capability."],
        workspace=tmp_path / "task-workspace",
        max_iterations=2,
        options={"capability_evidence_requirements": ["design-skill"]},
    )
    cast(Any, task)._agent_task_step_overrides = {"_execute_step": skills_step}

    result = await task.async_run()
    meta = await task.async_meta()

    assert result["status"] == "completed"
    assert result["accepted"] is True
    verification = meta["iterations"][0]["verification"]
    assert verification["is_complete"] is True
    assert verification.get("missing_capability_evidence", []) == []


@pytest.mark.asyncio
async def test_capability_evidence_satisfied_cumulatively_across_iterations(tmp_path):
    """Capability evidence accumulates across iterations: iteration 1 lacks it
    (gate fails -> replan), iteration 2 supplies it (task accepted)."""
    agent = _capability_gate_agent("agent-task-capability-cumulative")

    async def step(iteration_index, plan, context_pack):
        if iteration_index == 1:
            logs = {"action_logs": {"prep": {"name": "prep", "status": "success"}}, "route_logs": {}}
        else:
            logs = {"action_logs": {}, "route_logs": {"plan": {"selected_skills": [{"skill_id": "design-skill"}]}}}
        return (
            {"step_result": f"iteration {iteration_index}", "evidence": ["ok"], "remaining_work": []},
            {
                "execution_id": f"exec-{iteration_index}",
                "status": "completed",
                "route": {"selected_route": "model_request" if iteration_index == 1 else "skills"},
                "logs": logs,
            },
        )

    task = agent.create_task(
        task_id="capability-evidence-cumulative",
        goal="Produce the artifact using the intended capability.",
        success_criteria=["The artifact reflects the intended capability."],
        workspace=tmp_path / "task-workspace",
        max_iterations=3,
        options={"capability_evidence_requirements": ["design-skill"]},
    )
    cast(Any, task)._agent_task_step_overrides = {"_execute_step": step}

    result = await task.async_run()
    meta = await task.async_meta()

    assert result["status"] == "completed"
    assert result["iterations"] == 2
    first = meta["iterations"][0]["verification"]
    assert first["is_complete"] is False
    assert "design-skill" in " ".join(first.get("missing_capability_evidence", []))


@pytest.mark.asyncio
async def test_step_planner_prompt_exposes_capability_candidates_of_all_kinds(tmp_path):
    """Planner visibility: action, skill, and skill_pack capability candidates
    reach the planner prompt as one typed snapshot in options, with guidance_access
    so the planner knows which capabilities need their own route."""
    MockAgentTaskRequester.reset()
    agent = _capability_gate_agent("agent-task-capability-visibility")

    task = agent.create_task(
        task_id="capability-visibility",
        goal="Produce the artifact, choosing the right execution shape.",
        success_criteria=["An artifact is produced."],
        workspace=tmp_path / "task-workspace",
        max_iterations=1,
        options={
            "planner_capabilities": [
                {
                    "id": "fetch_sources",
                    "kind": "action",
                    "route": "model_request",
                    "guidance_access": "none",
                    "description": "gather",
                },
                {
                    "id": "design-skill",
                    "kind": "skill",
                    "route": "skills",
                    "guidance_access": "route_context",
                    "mode": "model_decision",
                    "description": "design",
                },
                {
                    "id": "report-pack",
                    "kind": "skill_pack",
                    "route": "skills",
                    "guidance_access": "route_context",
                    "description": "pack",
                },
            ]
        },
    )

    await task.async_run()

    plan_calls = [text for text in MockAgentTaskRequester.calls if "Plan the next bounded AgentExecution step" in text]
    assert plan_calls, "planner was never invoked"
    plan_text = plan_calls[0]
    assert "planner_capabilities" in plan_text
    for capability_id in ("fetch_sources", "design-skill", "report-pack"):
        assert capability_id in plan_text
    for kind in ("action", "skill", "skill_pack"):
        assert kind in plan_text
    assert "guidance_access" in plan_text


def test_step_scope_restricts_step_actions_from_structured_field(tmp_path):
    """Step scope comes from the structured step_scope field (not prose): an
    allowed_capability_ids list narrows the bounded step's action candidates via
    the execution-local action-id seam."""
    from agently.core.application import AgentTask

    agent = _capability_gate_agent("agent-task-step-scope")
    task = AgentTask(
        agent,
        goal="Gather evidence in a scoped step.",
        success_criteria=["Evidence gathered."],
        workspace=tmp_path / "task-workspace",
        max_iterations=1,
    )

    class _FakeExecution:
        def __init__(self):
            self.local_action_ids: list[str] = []
            self.applied_route_policy: dict[str, Any] | None = None

        def route_policy(self, value):
            self.applied_route_policy = value

        def record_consumed_option(self, *args, **kwargs):
            pass

    execution = _FakeExecution()
    plan = cast(Any, task)._normalize_step_plan(
        {
            "execution_shape": "direct",
            "step_instruction": "gather only",
            "expected_evidence": "x",
            "rationale": "y",
            "step_scope": {"allowed_capability_ids": ["fetch_sources"]},
        }
    )
    step_execution = cast(Any, task)._configure_step_execution(execution, plan)

    assert execution.local_action_ids == ["fetch_sources"]
    assert step_execution["step_scope"]["allowed_capability_ids"] == ["fetch_sources"]


def test_auto_step_plan_suppresses_dag_after_prior_dag_failure(tmp_path):
    from agently.core.application import AgentTask

    agent = _capability_gate_agent("agent-task-auto-dag-suppression")
    task = AgentTask(
        agent,
        goal="Run the next useful step.",
        success_criteria=["The result is produced."],
        workspace=tmp_path / "task-workspace",
        max_iterations=2,
        options={"agent_task": {"effort": {"execution": {"step_plan": "auto"}}}},
    )
    cast(Any, task)._failed_execution_shapes.add("dynamic_task")

    class _FakeExecution:
        def __init__(self):
            self.local_action_ids: list[str] = []

    execution = _FakeExecution()
    plan = cast(Any, task)._normalize_step_plan(
        {
            "execution_shape": "execution_dag",
            "step_instruction": "try a DAG",
            "expected_evidence": "result",
            "rationale": "has substeps",
            "dynamic_task": {"plan": {"tasks": []}},
        }
    )

    step_execution = cast(Any, task)._configure_step_execution(execution, plan)

    assert step_execution["effective_shape"] == "direct"
    assert step_execution["warning"] == "dag_shape_not_agent_execution_strategy"
    assert step_execution["policy"]["allow_dag_steps"] is False
    assert step_execution["policy"]["suppressed_execution_shapes"] == ["dynamic_task"]
    assert not hasattr(execution, "_add_dynamic_task_candidate")


def test_direct_step_plan_rejects_model_generated_dynamic_task_shape(tmp_path):
    from agently.core.application import AgentTask

    agent = _capability_gate_agent("agent-task-direct-dag-rejected")
    task = AgentTask(
        agent,
        goal="Run one bounded AgentExecution step.",
        success_criteria=["The result is produced."],
        workspace=tmp_path / "task-workspace",
        max_iterations=1,
        options={"agent_task": {"effort": {"execution": {"step_plan": "direct"}}}},
    )

    class _FakeExecution:
        def __init__(self):
            self.local_action_ids: list[str] = []

    execution = _FakeExecution()
    plan = cast(Any, task)._normalize_step_plan(
        {
            "execution_shape": "execution_dag",
            "step_instruction": "try a DAG anyway",
            "expected_evidence": "result",
            "rationale": "model proposed a DAG",
            "dynamic_task": {"plan": {"tasks": []}},
        }
    )

    step_execution = cast(Any, task)._configure_step_execution(execution, plan)

    assert step_execution["effective_shape"] == "direct"
    assert step_execution["warning"] == "dag_shape_not_agent_execution_strategy"
    assert not hasattr(execution, "_add_dynamic_task_candidate")


def test_explicit_dag_step_plan_is_not_agent_task_strategy(tmp_path):
    from agently.core.application import AgentTask

    agent = _capability_gate_agent("agent-task-explicit-dag-kept")
    task = AgentTask(
        agent,
        goal="Run the requested DAG step.",
        success_criteria=["The result is produced."],
        workspace=tmp_path / "task-workspace",
        max_iterations=1,
        options={"agent_task": {"effort": {"execution": {"step_plan": "dag"}}}},
    )
    cast(Any, task)._failed_execution_shapes.add("dynamic_task")

    class _FakeExecution:
        def __init__(self):
            self.local_action_ids: list[str] = []

    execution = _FakeExecution()
    plan = cast(Any, task)._normalize_step_plan(
        {
            "execution_shape": "dynamic_task",
            "step_instruction": "run the explicit DAG",
            "expected_evidence": "result",
            "rationale": "caller asked for dag",
            "dynamic_task": {"plan": {"tasks": []}},
        }
    )

    step_execution = cast(Any, task)._configure_step_execution(execution, plan)

    assert step_execution["effective_shape"] == "direct"
    assert step_execution["dag_shape_degraded"] is True
    assert step_execution["warning"] == "dag_shape_not_agent_execution_strategy"
    assert step_execution["policy"]["step_plan"] == "direct"
    assert step_execution["policy"]["step_plan_degraded_from"] == "dag"
    assert step_execution["policy"]["allow_dag_steps"] is False
    assert not hasattr(execution, "_add_dynamic_task_candidate")


def test_execution_log_summary_infers_action_success_from_route_history():
    """Older or nested route histories may expose action result records without
    a status field. AgentTask still needs a deterministic evidence projection."""
    from agently.core.application import AgentTask

    summary = AgentTask._execution_log_summary(
        {
            "status": "success",
            "logs": {
                "route_logs": {
                    "output": {
                        "history": [
                            {"name": "write_file", "result": {"path": "out.html"}},
                            {"name": "read_file", "error": "not found"},
                        ]
                    }
                }
            },
        }
    )

    assert summary["action_statuses"]["write_file"] == "success"
    assert summary["action_statuses"]["read_file"] == "failed"
    assert summary["capability_evidence"]["actions"]["succeeded"] == ["write_file"]
    assert summary["capability_evidence"]["actions"]["failed"] == ["read_file"]


def test_execution_log_summary_treats_partial_success_search_as_succeeded_action():
    from agently.core.application import AgentTask

    summary = AgentTask._execution_log_summary(
        {
            "status": "completed",
            "logs": {
                "action_logs": [
                    {
                        "action_id": "web_search",
                        "status": "partial_success",
                        "action_call_id": "call-search",
                        "model_digest": {
                            "result_preview": [
                                {
                                    "title": "Official release note",
                                    "href": "https://example.test/release",
                                    "body": "A successful backend returned useful source evidence.",
                                }
                            ],
                            "result_preview_meta": {"truncated": False},
                        },
                        "diagnostics": [
                            {
                                "code": "search_backend_failed",
                                "backend": "yahoo",
                                "message": "transient backend failure",
                            }
                        ],
                    }
                ],
                "route_logs": {},
            },
        }
    )

    assert summary["failed_actions"] == []
    assert summary["action_statuses"]["web_search"] == "partial_success"
    assert summary["capability_evidence"]["actions"]["succeeded"] == ["web_search"]
    assert summary["capability_evidence"]["actions"]["failed"] == []


def test_execution_log_summary_infers_nested_partial_success_result_as_succeeded_action():
    from agently.core.application import AgentTask

    summary = AgentTask._execution_log_summary(
        {
            "status": "completed",
            "logs": {
                "route_logs": {
                    "output": {
                        "history": [
                            {
                                "name": "web_search",
                                "result": {
                                    "status": "partial_success",
                                    "ok": True,
                                    "success": True,
                                    "data": [
                                        {
                                            "title": "Official release note",
                                            "href": "https://example.test/release",
                                            "body": "A recovered backend returned usable evidence.",
                                        }
                                    ],
                                    "diagnostics": [
                                        {
                                            "code": "search_backend_failed",
                                            "backend": "yahoo",
                                            "message": "transient backend failure",
                                        }
                                    ],
                                },
                            }
                        ]
                    }
                }
            },
        }
    )

    assert summary["failed_actions"] == []
    assert summary["action_statuses"]["web_search"] == "partial_success"
    assert summary["capability_evidence"]["actions"]["succeeded"] == ["web_search"]
    assert summary["capability_evidence"]["actions"]["failed"] == []


def test_execution_log_summary_includes_nested_action_result_previews():
    """AgentTask verifier evidence must include bounded Action observations
    produced inside the TriggerFlow/Blocks bounded-step execution wrapper."""
    from agently.core.application import AgentTask

    summary = AgentTask._execution_log_summary(
        {
            "status": "completed",
            "logs": {"action_logs": {}, "route_logs": {}},
            "blocks": {
                "evidence": {
                    "execution_block_results": [
                        {
                            "output": {
                                "execution_meta": {
                                    "status": "completed",
                                    "logs": {
                                        "action_logs": [
                                            {
                                                "action_id": "browse",
                                                "status": "success",
                                                "action_call_id": "call-1",
                                                "model_digest": {
                                                    "result_preview": {
                                                        "selected_url": "https://example.test/syllabus",
                                                        "content": (
                                                            ("Navigation link " * 160)
                                                            + "Official syllabus sections: "
                                                            + "1. Foundations; 2. Model architecture."
                                                        ),
                                                    },
                                                    "result_preview_meta": {
                                                        "original_size": 120,
                                                        "preview_size": 80,
                                                        "truncated": False,
                                                    },
                                                },
                                            }
                                        ],
                                        "route_logs": {},
                                    },
                                },
                                "execution_result": {"step_result": "ok"},
                            }
                        }
                    ]
                }
            },
        }
    )

    assert summary["action_ids"] == ["browse"]
    assert summary["action_statuses"]["browse"] == "success"
    assert summary["capability_evidence"]["actions"]["succeeded"] == ["browse"]
    action = summary["actions"][0]
    assert action["action_call_id"] == "call-1"
    assert action["result_preview"]["selected_url"] == "https://example.test/syllabus"
    assert "Official syllabus sections" in action["result_preview"]["content"]
    assert action["result_preview_meta"]["truncated"] is False

    verifier_summary = AgentTask._compact_verifier_evidence_summary(summary)
    verifier_action = verifier_summary["actions"][0]
    assert verifier_action["result_preview"]["selected_url"] == "https://example.test/syllabus"
    assert "Official syllabus sections" in verifier_action["result_preview"]["content"]
    assert "Model architecture" in verifier_action["result_preview"]["content"]


def test_cumulative_verifier_evidence_keeps_previous_iteration_action_previews(tmp_path):
    agent = _create_agent("agent-task-cumulative-evidence").use_workspace(tmp_path / "task-workspace")
    task = AgentTask(
        agent,
        goal="Create a source-grounded report.",
        success_criteria=["The report uses the most specific official source evidence."],
        execution="flat",
    )
    task.iterations.append(
        {
            "iteration": 1,
            "execution_meta": {
                "status": "completed",
                "logs": {"action_logs": {}, "route_logs": {}},
                "blocks": {
                    "evidence": {
                        "execution_block_results": [
                            {
                                "output": {
                                    "execution_meta": {
                                        "status": "completed",
                                        "logs": {
                                            "action_logs": [
                                                {
                                                    "action_id": "browse",
                                                    "status": "success",
                                                    "action_call_id": "call-specific",
                                                    "model_digest": {
                                                        "result_preview": {
                                                            "selected_url": "https://example.test/specific",
                                                            "content": (
                                                                "Specific official syllabus: "
                                                                "1. Foundations; 2. Model architecture."
                                                            ),
                                                        },
                                                        "result_preview_meta": {"truncated": False},
                                                    },
                                                }
                                            ],
                                            "route_logs": {},
                                        },
                                    }
                                }
                            }
                        ]
                    }
                },
            },
        }
    )

    cumulative = task._cumulative_execution_evidence_summary(
        {
            "status": "completed",
            "logs": {
                "action_logs": [
                    {
                        "action_id": "write_file",
                        "status": "success",
                        "action_call_id": "call-write",
                    }
                ],
                "route_logs": {},
            },
        }
    )
    verifier_summary = AgentTask._compact_verifier_evidence_summary(cumulative)

    actions = verifier_summary["actions"]
    assert [action["id"] for action in actions] == ["browse", "write_file"]
    browse_preview = actions[0]["result_preview"]
    assert browse_preview["selected_url"] == "https://example.test/specific"
    assert "Specific official syllabus" in browse_preview["content"]
    assert any(
        ref["field"] == "selected_url" and ref["value"] == "https://example.test/specific"
        for ref in verifier_summary["source_refs"]
    )


def test_cumulative_evidence_ledger_keeps_current_action_result_when_old_items_overflow(tmp_path):
    from agently.core.application.AgentTask.EvidenceLedger import validate_evidence_use

    agent = _create_agent("agent-task-current-evidence-priority").use_workspace(tmp_path / "task-workspace")
    task = AgentTask(
        agent,
        goal="Create a source-grounded report.",
        success_criteria=["Current source evidence remains verifier-visible."],
        execution="flat",
    )
    for index in range(150):
        task.iterations.append(
            {
                "iteration": index + 1,
                "execution_meta": {
                    "status": "completed",
                    "logs": {
                        "action_logs": [
                            {
                                "action_id": "browse",
                                "status": "success",
                                "action_call_id": f"act_call_old_{index}",
                                "model_digest": {
                                    "result_preview": {
                                        "selected_url": f"https://example.test/old/{index}",
                                        "content": f"Old bounded evidence {index}",
                                    },
                                    "result_preview_meta": {"truncated": False},
                                },
                            }
                        ],
                        "route_logs": {},
                    },
                },
            }
        )

    current_meta = {
        "status": "completed",
        "logs": {
            "action_logs": [
                {
                    "action_id": "browse",
                    "status": "success",
                    "action_call_id": "act_call_current",
                    "model_digest": {
                        "result_preview": {
                            "selected_url": "https://example.test/current",
                            "content": "Current official syllabus evidence.",
                        },
                        "result_preview_meta": {"truncated": False},
                    },
                }
            ],
            "route_logs": {},
        },
    }

    ledger = task._cumulative_evidence_ledger(current_meta)
    current_id = "agent_task_action_result:browse:act_call_current"

    assert current_id in {item.get("id") for item in ledger.get("items", [])}
    guard = validate_evidence_use(
        [{"claim": "Current syllabus evidence.", "evidence_ids": [current_id], "support_type": "content"}],
        ledger,
    )
    assert guard["valid"] is True


def test_cumulative_verifier_evidence_uses_raw_action_data_when_digest_missing(tmp_path):
    from agently.core.application import AgentTask

    agent = _create_agent("agent-task-raw-action-data-evidence").use_workspace(tmp_path / "task-workspace")
    task = AgentTask(
        agent,
        goal="Create a source-grounded repository report.",
        success_criteria=["The report grounds claims in files that were read."],
        execution="flat",
    )
    task.iterations.append(
        {
            "iteration": 1,
            "execution_meta": {
                "status": "completed",
                "logs": {
                    "action_logs": [
                        {
                            "action_id": "read_repo_file",
                            "status": "success",
                            "action_call_id": "call-config",
                            "model_digest": {},
                            "raw": {
                                "kwargs": {"path": "configs/_base_/default.yaml", "max_chars": 8000},
                                "data": {
                                    "path": "configs/_base_/default.yaml",
                                    "content": "model:\n  rewrite_max_completion_tokens: 64000\n",
                                    "sha256": "abc123",
                                    "truncated": False,
                                },
                            },
                        }
                    ],
                    "route_logs": {},
                },
            },
        }
    )

    cumulative = task._cumulative_execution_evidence_summary(
        {"status": "completed", "logs": {"action_logs": [], "route_logs": {}}}
    )
    verifier_summary = AgentTask._compact_verifier_evidence_summary(cumulative)

    action = verifier_summary["actions"][0]
    assert action["id"] == "read_repo_file"
    assert action["input_preview"]["path"] == "configs/_base_/default.yaml"
    assert action["result_preview"]["path"] == "configs/_base_/default.yaml"
    assert "rewrite_max_completion_tokens" in action["result_preview"]["content"]
    assert action["result_preview_meta"]["truncated"] is False
    assert any(
        ref["field"] == "path" and ref["value"] == "configs/_base_/default.yaml"
        for ref in verifier_summary["source_refs"]
    )
    assert any(
        ref["field"] == "path"
        and ref["value"] == "configs/_base_/default.yaml"
        and ref["content_state"] == "bounded_readback_available"
        for ref in verifier_summary["source_refs"]
    )


def test_source_refs_treat_excerpt_and_snippet_as_bounded_readback():
    action_refs = AgentTask._collect_source_refs_from_action_records(
        [
            {
                "id": "read_action_artifact",
                "status": "success",
                "action_call_id": "call-artifact",
                "result_preview": {
                    "key_files": [
                        {
                            "path": "docs/guide/configuration.md",
                            "excerpt": "Configuration excerpt read from the cloned repository artifact.",
                        },
                        {
                            "path": "docs/guide/dl-analogy.md",
                            "snippet": "Bounded snippet from the deep learning analogy guide.",
                        },
                    ]
                },
            }
        ]
    )
    action_ref_states = {
        ref["value"]: ref["content_state"]
        for ref in action_refs
        if ref.get("field") == "path"
    }
    assert action_ref_states["docs/guide/configuration.md"] == "bounded_readback_available"
    assert action_ref_states["docs/guide/dl-analogy.md"] == "bounded_readback_available"

    taskboard_refs = AgentTask._collect_taskboard_source_refs(
        {
            "cards": [
                {
                    "preview": {
                        "path": "docs/guide/skill-document.md",
                        "excerpt": "Bounded excerpt from the skill document guide.",
                    }
                }
            ]
        }
    )

    assert taskboard_refs[0]["path"] == "docs/guide/skill-document.md"
    assert taskboard_refs[0]["content_state"] == "bounded_readback_available"


def test_taskboard_final_source_refs_are_visible_to_verifier_summary(tmp_path):
    agent = _create_agent("agent-task-taskboard-final-source-refs").use_workspace(tmp_path / "task-workspace")
    task = AgentTask(
        agent,
        goal="Create a source-grounded repository report.",
        success_criteria=["The report grounds claims in files that were read."],
        execution="taskboard",
    )
    evidence_view = {
        "source_refs": [
            {
                "path": "docs/guide/configuration.md",
                "content_state": "bounded_readback_available",
                "excerpt": "Configuration guide excerpt.",
            },
            {
                "path": "docs/guide/unread.md",
                "content_state": "ref_only",
            },
        ]
    }

    final_source_refs = task._taskboard_final_source_refs_from_evidence_view(evidence_view)
    summary = task._execution_log_summary(
        {
            "status": "completed",
            "logs": {
                "artifact_refs": [{"path": "final.md", "role": "workspace_artifact"}],
                "source_refs": final_source_refs,
            },
        }
    )
    verifier_summary = AgentTask._compact_verifier_evidence_summary(summary)
    planner_anchors = AgentTask._planner_evidence_anchors_from_summary(summary)

    verifier_states = {ref["path"]: ref["content_state"] for ref in verifier_summary["source_refs"]}
    planner_states = {ref["value"]: ref["content_state"] for ref in planner_anchors["source_refs"]}
    assert verifier_states["docs/guide/configuration.md"] == "bounded_readback_available"
    assert verifier_states["docs/guide/unread.md"] == "ref_only"
    assert planner_states["docs/guide/configuration.md"] == "bounded_readback_available"
    assert planner_states["docs/guide/unread.md"] == "ref_only"


def test_flat_source_refs_distinguish_ref_only_repo_manifest_paths(tmp_path):
    agent = _create_agent("agent-task-ref-only-repo-manifest").use_workspace(tmp_path / "task-workspace")
    task = AgentTask(
        agent,
        goal="Create a source-grounded repository report.",
        success_criteria=["The report grounds claims in files that were read."],
        execution="flat",
    )
    task.iterations.append(
        {
            "iteration": 1,
            "execution_meta": {
                "status": "completed",
                "logs": {
                    "action_logs": [
                        {
                            "action_id": "clone_repo",
                            "status": "success",
                            "action_call_id": "call-clone",
                            "model_digest": {
                                "result_preview": {
                                    "repo": "example/repo",
                                    "files": [
                                        {"path": "README.md"},
                                        {"path": "docs/guide.md"},
                                    ],
                                },
                                "result_preview_meta": {"truncated": False},
                            },
                        },
                        {
                            "action_id": "read_repo_file",
                            "status": "success",
                            "action_call_id": "call-read",
                            "model_digest": {
                                "result_preview": {
                                    "path": "README.md",
                                    "content": "# SkillOpt\n\nThe README content was actually read.",
                                    "sha256": "read-sha",
                                    "truncated": False,
                                },
                                "result_preview_meta": {"truncated": False},
                            },
                        },
                    ],
                    "route_logs": {},
                },
            },
        }
    )

    cumulative = task._cumulative_execution_evidence_summary(
        {"status": "completed", "logs": {"action_logs": [], "route_logs": {}}}
    )
    verifier_summary = AgentTask._compact_verifier_evidence_summary(cumulative)
    planner_anchors = task._iteration_prompt_summaries()[0]["evidence_anchors"]

    ref_states = {
        (ref["action_call_id"], ref["value"]): ref["content_state"]
        for ref in verifier_summary["source_refs"]
        if ref["field"] == "path"
    }
    assert ref_states[("call-clone", "README.md")] == "ref_only"
    assert ref_states[("call-clone", "docs/guide.md")] == "ref_only"
    assert ref_states[("call-read", "README.md")] == "bounded_readback_available"
    assert any(
        ref["value"] == "README.md" and ref["content_state"] == "bounded_readback_available"
        for ref in planner_anchors["source_refs"]
    )
    assert any(
        ref["value"] == "docs/guide.md" and ref["content_state"] == "ref_only"
        for ref in planner_anchors["source_refs"]
    )


def test_planner_repair_context_keeps_previous_exact_evidence_anchors(tmp_path):
    agent = _create_agent("agent-task-planner-evidence-anchors").use_workspace(tmp_path / "task-workspace")
    task = AgentTask(
        agent,
        goal="Repair a source-grounded report without inventing source URLs.",
        success_criteria=["The report cites exact source URLs from action evidence."],
        execution="flat",
    )
    task.iterations.append(
        {
            "iteration": 1,
            "plan": {"step_instruction": "Search for source evidence.", "execution_shape": "actions"},
            "execution_meta": {
                "status": "failed",
                "logs": {
                    "action_logs": [
                        {
                            "action_id": "web_search",
                            "status": "partial_success",
                            "action_call_id": "call-search",
                            "model_digest": {
                                "result_preview": [
                                    {
                                        "title": "NVDA: NVIDIA Corp - Stock Price, Quote and News - CNBC",
                                        "href": "https://www.cnbc.com/quotes/NVDA",
                                        "body": "Nvidia stock coverage and related news snippets.",
                                    },
                                    {
                                        "title": "NVDA Stock Quote Price and Forecast | CNN",
                                        "href": "https://www.cnn.com/markets/stocks/NVDA",
                                        "body": "NVIDIA BioNeMo and Halos announcement snippets.",
                                    },
                                ],
                                "result_preview_meta": {"truncated": False},
                            },
                        }
                    ],
                    "route_logs": {},
                },
            },
            "verification": {
                "is_complete": False,
                "reason": "Source URLs must be exact.",
                "missing_criteria": ["Generated report cited fabricated article URLs."],
                "replan_instruction": "Rewrite using exact URLs from evidence.",
            },
        }
    )
    task.iterations.append(
        {
            "iteration": 2,
            "plan": {"step_instruction": "Rewrite the report.", "execution_shape": "direct"},
            "execution_meta": {"status": "completed", "logs": {"action_logs": [], "route_logs": {}}},
            "verification": {
                "is_complete": False,
                "reason": "Latest rewrite still missed exact evidence refs.",
                "missing_criteria": ["Exact source URLs are still missing."],
                "replan_instruction": "Repair citations from available evidence.",
            },
        }
    )

    summaries = task._iteration_prompt_summaries()
    first_anchors = summaries[0]["evidence_anchors"]
    assert any(
        ref["field"] == "href" and ref["value"] == "https://www.cnbc.com/quotes/NVDA"
        for ref in first_anchors["source_refs"]
    )
    assert first_anchors["action_result_previews"][0]["result_preview"][1]["href"] == (
        "https://www.cnn.com/markets/stocks/NVDA"
    )

    repair_context = task._planner_repair_context(summaries)
    available = repair_context["available_evidence_anchors"]
    assert any(
        ref["field"] == "href" and ref["value"] == "https://www.cnbc.com/quotes/NVDA"
        for ref in available["source_refs"]
    )
    assert available["action_result_previews"][0]["result_preview"][0]["href"] == "https://www.cnbc.com/quotes/NVDA"


@pytest.mark.asyncio
async def test_action_succeeded_evidence_satisfied_in_earlier_iteration(tmp_path):
    """action_succeeded evidence accumulates across iterations: the action
    succeeds in iteration 1, and a later iteration must not false-fail the
    requirement just because the action did not re-run."""
    agent = _capability_gate_agent("agent-task-action-succeeded-cumulative")

    async def step(iteration_index, plan, context_pack):
        if iteration_index == 1:
            # The required action succeeds here; iteration 2 omits it but the
            # capability_used requirement for "later-skill" is still unmet.
            logs = {
                "action_logs": {"build_action": {"name": "build_action", "status": "success"}},
                "route_logs": {},
            }
            route = "model_request"
        else:
            logs = {"action_logs": {}, "route_logs": {"plan": {"selected_skills": [{"skill_id": "later-skill"}]}}}
            route = "skills"
        return (
            {"step_result": f"iteration {iteration_index}", "evidence": ["ok"], "remaining_work": []},
            {
                "execution_id": f"exec-{iteration_index}",
                "status": "completed",
                "route": {"selected_route": route},
                "logs": logs,
            },
        )

    task = agent.create_task(
        task_id="action-succeeded-cumulative",
        goal="Use an action and a skill across steps.",
        success_criteria=["The action ran and the skill was used."],
        workspace=tmp_path / "task-workspace",
        max_iterations=3,
        options={
            "capability_evidence_requirements": [
                {"capability_id": "build_action", "kind": "action_succeeded"},
                {"capability_id": "later-skill", "capability_kind": "skill", "kind": "capability_used"},
            ]
        },
    )
    cast(Any, task)._agent_task_step_overrides = {"_execute_step": step}

    result = await task.async_run()
    meta = await task.async_meta()

    assert result["status"] == "completed"
    assert result["iterations"] == 2
    # Iteration 2 must not report build_action as missing — it succeeded in iter 1.
    second = meta["iterations"][1]["verification"]
    assert "build_action" not in " ".join(second.get("missing_capability_evidence", []))


@pytest.mark.asyncio
async def test_unenforced_evidence_kind_is_recorded_not_silently_blocking(tmp_path):
    """A reserved/not-yet-wired evidence kind (e.g. artifact_readback) does not
    block acceptance but is surfaced as an unenforced requirement rather than
    silently dropped."""
    agent = _capability_gate_agent("agent-task-unenforced-kind")

    async def step(iteration_index, plan, context_pack):
        return (
            {"step_result": "done", "evidence": ["ok"], "remaining_work": []},
            {
                "execution_id": f"exec-{iteration_index}",
                "status": "completed",
                "route": {"selected_route": "model_request"},
                "logs": {"action_logs": {}, "route_logs": {}},
            },
        )

    task = agent.create_task(
        task_id="unenforced-kind",
        goal="Produce an artifact.",
        success_criteria=["An artifact exists."],
        workspace=tmp_path / "task-workspace",
        max_iterations=1,
        options={"capability_evidence_requirements": [{"capability_id": "the-artifact", "kind": "artifact_readback"}]},
    )
    cast(Any, task)._agent_task_step_overrides = {"_execute_step": step}

    result = await task.async_run()
    meta = await task.async_meta()

    # Unenforced kind must not block; it is recorded for visibility.
    assert result["status"] == "completed"
    verification = meta["iterations"][0]["verification"]
    assert verification.get("missing_capability_evidence", []) == []
    unenforced = verification.get("unenforced_evidence_requirements", [])
    assert any(item.get("capability_id") == "the-artifact" for item in unenforced)


def test_agent_task_module_does_not_import_orchestrator_internals():
    """BUG_FIX 4.1 layering: AgentTask consumes the inert options snapshot and
    must not import AgentOrchestrator / HybridRoutePlanner internals."""
    import ast
    import inspect

    from agently.core.application import AgentTask as AgentTaskClass

    source_file = inspect.getsourcefile(AgentTaskClass)
    assert source_file is not None
    source = Path(source_file).read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.append(node.module or "")
    joined = " ".join(imported)
    assert "AgentOrchestrator" not in joined
    assert "HybridRoutePlanner" not in joined


def test_example_design_system_fingerprint_smoke(tmp_path):
    """BUG_FIX 4.4: design-system fingerprint smoke fails the 2026-06-12 light
    bypass artifact and passes a skill-template-derived artifact. Fingerprints
    are read from the installed skill, not hand-written into core."""
    from examples.agent_task.agently_architecture_diagram_cocoon_skill_task import (
        _design_system_fingerprint_hits,
        _design_system_fingerprints,
    )

    skill_dir = tmp_path / "architecture-diagram"
    (skill_dir / "resources").mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        'Use #020617 background, JetBrains Mono font, <pattern id="grid"> and stroke-dasharray.',
        encoding="utf-8",
    )
    (skill_dir / "resources" / "template.html").write_text("<svg></svg>", encoding="utf-8")

    fingerprints = _design_system_fingerprints(skill_dir)
    assert "#020617" in fingerprints
    assert "JetBrains Mono" in fingerprints

    light_bypass_artifact = (
        "<html><body style=\"background:#f5f5f5;font-family:'Segoe UI'\">" "<svg></svg></body></html>"
    )
    assert _design_system_fingerprint_hits(light_bypass_artifact, fingerprints) == []

    template_derived_artifact = (
        "<html><style>body{background:#020617;font-family:'JetBrains Mono'}</style>"
        '<svg><pattern id="grid"></pattern><rect stroke-dasharray="4,4"/></svg></html>'
    )
    hits = _design_system_fingerprint_hits(template_derived_artifact, fingerprints)
    assert "#020617" in hits
    assert "JetBrains Mono" in hits
    assert len(hits) >= (len(fingerprints) + 1) // 2
