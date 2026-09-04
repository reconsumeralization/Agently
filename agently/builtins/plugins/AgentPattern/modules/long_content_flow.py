# Copyright 2023-2026 AgentEra(Agently.Tech)
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from __future__ import annotations

from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass
from typing import Any, TYPE_CHECKING, TypedDict, cast

from pydantic import BaseModel, Field

from agently.core.orchestration import TriggerFlow
from agently.types.trigger_flow import TriggerFlowRuntimeData

from .model_stage import run_model_stage

if TYPE_CHECKING:
    from agently.types.plugins import AgentExecution


_RUNTIME_RESOURCE = "agent_pattern_long_content_runtime"


class _DocumentSectionData(TypedDict):
    section_id: str
    title: str
    brief: str


class _DocumentPlanData(TypedDict):
    document_title: str
    sections: list[_DocumentSectionData]


class _SectionDraftData(TypedDict):
    body: str
    continuity_note: str


class _ContinuityData(TypedDict):
    section_id: str
    title: str
    note: str


class _WrittenSectionData(_DocumentSectionData):
    body: str
    continuity_note: str


class _DocumentSection(BaseModel):
    section_id: str = Field(min_length=1, max_length=120)
    title: str = Field(min_length=1, max_length=300)
    brief: str = Field(min_length=1, max_length=2_000)


class _DocumentPlan(BaseModel):
    document_title: str = Field(min_length=1, max_length=500)
    sections: list[_DocumentSection] = Field(min_length=1)


class _SectionDraft(BaseModel):
    body: str = Field(min_length=1)
    continuity_note: str = ""


@dataclass(frozen=True)
class LongContentPatternConfig:
    max_sections: int = 12
    continuity_chars: int = 4_000


class _LongContentPatternRuntime:
    def __init__(
        self,
        execution: "AgentExecution",
        config: LongContentPatternConfig,
    ) -> None:
        self.execution = execution
        self.config = config

    async def plan_document(self) -> _DocumentPlanData:
        value = await run_model_stage(
            self.execution,
            pattern="long_content",
            stage="section_plan",
            stage_input={
                "result_role": "one coherent long-form text document",
            },
            stage_info={
                "max_sections": self.config.max_sections,
                "assembly": "host-ordered Markdown headings and section bodies",
            },
            stage_instructions=[
                "Design the complete document before drafting any section.",
                "Return an ordered section plan whose briefs collectively cover "
                "the original request without filler or overlap.",
                f"Use between 1 and {self.config.max_sections} sections.",
                "Give every section a unique stable section_id, a reader-facing title, and a concrete writing brief.",
                "Do not write section prose in this planning stage.",
            ],
            output=_DocumentPlan,
        )
        return _normalize_document_plan(value, max_sections=self.config.max_sections)

    async def write_section(
        self,
        *,
        section: _DocumentSectionData,
        plan: _DocumentPlanData,
        continuity: list[_ContinuityData],
        index: int,
    ) -> _SectionDraftData:
        value = await run_model_stage(
            self.execution,
            pattern="long_content",
            stage=f"section_{index + 1}",
            stage_input={
                "section_index": index,
                "current_section": section,
            },
            stage_info={
                "document_plan": plan,
                "accepted_predecessor_continuity": continuity,
                "continuity_note_max_chars": self.config.continuity_chars,
                "is_final_section": index == len(plan["sections"]) - 1,
            },
            stage_instructions=[
                "Write only the current section body while satisfying its brief and the original request contract.",
                "Use the complete document plan for global coherence and the "
                "bounded predecessor notes only for continuity.",
                "Do not repeat the document title or section heading; the host adds headings during assembly.",
                "Do not claim facts that are absent from the supplied request, "
                "evidence, or tool results; label necessary assumptions.",
                "Return a concise continuity_note containing only facts, "
                "terminology, commitments, or transitions that a later section must preserve.",
                f"Keep continuity_note within {self.config.continuity_chars} "
                "characters; it may be empty for the final section.",
            ],
            output=_SectionDraft,
        )
        return _normalize_section_draft(
            value,
            continuity_chars=self.config.continuity_chars,
        )


def _require_runtime(data: TriggerFlowRuntimeData) -> _LongContentPatternRuntime:
    runtime = data.require_resource(_RUNTIME_RESOURCE)
    if not isinstance(runtime, _LongContentPatternRuntime):
        raise TypeError("Long Content Pattern TriggerFlow runtime resource is invalid.")
    return runtime


async def _plan_document(data: TriggerFlowRuntimeData) -> list[_DocumentSectionData]:
    runtime = _require_runtime(data)
    plan = await runtime.plan_document()
    await data.async_set_state("document_plan", plan, emit=False)
    await data.async_set_state("continuity_notes", [], emit=False)
    return list(plan["sections"])


async def _write_section(data: TriggerFlowRuntimeData) -> _WrittenSectionData:
    runtime = _require_runtime(data)
    if not isinstance(data.value, Mapping):
        raise TypeError("Long Content Pattern section input must be a mapping.")
    section: _DocumentSectionData = {
        "section_id": str(data.value.get("section_id") or ""),
        "title": str(data.value.get("title") or ""),
        "brief": str(data.value.get("brief") or ""),
    }
    raw_plan = data.get_state("document_plan", {})
    if not isinstance(raw_plan, Mapping):
        raise RuntimeError("Long Content Pattern document plan is unavailable.")
    plan = cast(_DocumentPlanData, dict(raw_plan))
    sections = plan.get("sections", [])
    if not isinstance(sections, list):
        raise RuntimeError("Long Content Pattern document sections are unavailable.")
    index = next(
        (
            item_index
            for item_index, item in enumerate(sections)
            if isinstance(item, Mapping)
            and item.get("section_id") == section["section_id"]
        ),
        -1,
    )
    if index < 0:
        raise RuntimeError(
            f"Long Content Pattern section {section['section_id']!r} is not in the validated plan."
        )
    raw_notes = data.get_state("continuity_notes", [])
    notes = cast(
        list[_ContinuityData],
        [dict(item) for item in raw_notes if isinstance(item, Mapping)]
        if isinstance(raw_notes, list)
        else [],
    )
    continuity = _bounded_continuity(
        notes,
        max_chars=runtime.config.continuity_chars,
    )
    draft = await runtime.write_section(
        section=section,
        plan=plan,
        continuity=continuity,
        index=index,
    )
    note = draft["continuity_note"]
    if note:
        notes.append(
            {
                "section_id": section["section_id"],
                "title": section["title"],
                "note": note,
            }
        )
        await data.async_set_state("continuity_notes", notes, emit=False)
    return {
        **section,
        "body": draft["body"],
        "continuity_note": note,
    }


async def _assemble_document(data: TriggerFlowRuntimeData) -> None:
    plan = data.get_state("document_plan", {})
    if not isinstance(plan, Mapping):
        raise RuntimeError("Long Content Pattern completed without a document plan.")
    drafts = data.value
    if not isinstance(drafts, list):
        raise TypeError("Long Content Pattern section writers must return an ordered list.")
    result = _assemble_markdown(cast(_DocumentPlanData, dict(plan)), drafts)
    await data.async_set_state("pattern_result", result, emit=False)


def _build_long_content_flow() -> TriggerFlow[Any, Any, Any]:
    flow: TriggerFlow[Any, Any, Any] = TriggerFlow(name="agent-pattern-long-content")
    (
        flow.to(_plan_document)
        .for_each(concurrency=1)
        .to(_write_section)
        .end_for_each()
        .to(_assemble_document)
    )
    return flow


_LONG_CONTENT_FLOW = _build_long_content_flow()


async def run_long_content_pattern(
    execution: "AgentExecution",
    config: LongContentPatternConfig,
) -> str:
    if execution.prompt_snapshot.get("output") not in (None, {}, []):
        raise ValueError(
            "Long Content Pattern returns assembled text and cannot be combined "
            "with a structured .output(...) contract."
        )
    if execution.prompt_snapshot.get("output_format") not in (None, "", "text"):
        raise ValueError(
            "Long Content Pattern returns assembled text and cannot be combined with a structured output format."
        )
    if bool(getattr(execution, "_ensure_long_output_enabled", False)):
        raise ValueError(
            "Long Content Pattern cannot be combined with ensure_long_output(); "
            "choose semantic composition or transport continuation explicitly."
        )

    runtime = _LongContentPatternRuntime(execution, config)
    flow_execution = _LONG_CONTENT_FLOW.create_execution(
        auto_close=False,
        record_store=False,
        runtime_resources={_RUNTIME_RESOURCE: runtime},
        parent_run_context=execution.agent_execution_run_context,
        intervention_mode=None,
    )
    try:
        await flow_execution.async_start(None)
        snapshot = await flow_execution.async_close(reason="agent_pattern_completed")
    except BaseException:
        if not flow_execution.is_closed():
            with suppress(BaseException):
                await flow_execution.async_close(
                    reason="agent_pattern_failed",
                    pending_interrupts="cancel",
                )
        raise
    if not isinstance(snapshot, Mapping):
        raise RuntimeError("Long Content Pattern completed without terminal state.")
    result = snapshot.get("pattern_result")
    if not isinstance(result, str) or not result.strip():
        raise RuntimeError("Long Content Pattern completed without assembled text.")
    plan = snapshot.get("document_plan", {})
    section_count = (
        len(plan.get("sections", []))
        if isinstance(plan, Mapping) and isinstance(plan.get("sections"), list)
        else 0
    )
    diagnostic = execution.diagnostics.get("pattern_run", {})
    if isinstance(diagnostic, dict):
        diagnostic.update(
            {
                "section_count": section_count,
                "assembled_chars": len(result),
                "assembly": "host_ordered",
            }
        )
        execution.diagnostics["pattern_run"] = diagnostic
    return result


def _normalize_document_plan(
    value: object,
    *,
    max_sections: int,
) -> _DocumentPlanData:
    if not isinstance(value, Mapping):
        raise TypeError("Long Content Pattern document plan must be a mapping.")
    document_title = str(value.get("document_title") or "").strip()
    if not document_title:
        raise ValueError("Long Content Pattern document title cannot be empty.")
    raw_sections = value.get("sections")
    if not isinstance(raw_sections, list) or not raw_sections:
        raise ValueError("Long Content Pattern requires at least one planned section.")
    if len(raw_sections) > max_sections:
        raise ValueError(
            f"Long Content Pattern plan exceeds max_sections={max_sections}."
        )
    sections: list[_DocumentSectionData] = []
    section_ids: set[str] = set()
    for index, item in enumerate(raw_sections, start=1):
        if not isinstance(item, Mapping):
            raise TypeError(
                f"Long Content Pattern section {index} must be a mapping."
            )
        section_id = str(item.get("section_id") or "").strip()
        title = str(item.get("title") or "").strip()
        brief = str(item.get("brief") or "").strip()
        if not section_id or not title or not brief:
            raise ValueError(
                f"Long Content Pattern section {index} requires section_id, title, and brief."
            )
        if section_id in section_ids:
            raise ValueError(
                f"Long Content Pattern section_id {section_id!r} is duplicated."
            )
        section_ids.add(section_id)
        sections.append(
            {"section_id": section_id, "title": title, "brief": brief}
        )
    return {"document_title": document_title, "sections": sections}


def _normalize_section_draft(
    value: object,
    *,
    continuity_chars: int,
) -> _SectionDraftData:
    if not isinstance(value, Mapping):
        raise TypeError("Long Content Pattern section writer must return a mapping.")
    body = str(value.get("body") or "").strip()
    if not body:
        raise ValueError("Long Content Pattern section body cannot be empty.")
    continuity_note = str(value.get("continuity_note") or "").strip()
    if len(continuity_note) > continuity_chars:
        continuity_note = continuity_note[:continuity_chars].rstrip()
    return {"body": body, "continuity_note": continuity_note}


def _bounded_continuity(
    notes: list[_ContinuityData],
    *,
    max_chars: int,
) -> list[_ContinuityData]:
    selected: list[_ContinuityData] = []
    remaining = max_chars
    for item in reversed(notes):
        note = str(item.get("note") or "").strip()
        if not note or remaining <= 0:
            continue
        accepted = note[-remaining:]
        selected.append(
            {
                "section_id": str(item.get("section_id") or ""),
                "title": str(item.get("title") or ""),
                "note": accepted,
            }
        )
        remaining -= len(accepted)
    selected.reverse()
    return selected


def _assemble_markdown(
    plan: _DocumentPlanData,
    drafts: list[object],
) -> str:
    sections = plan.get("sections")
    if not isinstance(sections, list) or len(drafts) != len(sections):
        raise RuntimeError(
            "Long Content Pattern cannot assemble an incomplete section set."
        )
    drafted_by_id: dict[str, Mapping[str, object]] = {}
    for item in drafts:
        if not isinstance(item, Mapping):
            raise TypeError("Long Content Pattern section draft must be a mapping.")
        section_id = str(item.get("section_id") or "")
        if not section_id or section_id in drafted_by_id:
            raise RuntimeError(
                "Long Content Pattern section drafts contain a missing or duplicate section_id."
            )
        drafted_by_id[section_id] = item

    parts = [f"# {str(plan.get('document_title') or '').strip()}"]
    for section in sections:
        if not isinstance(section, Mapping):
            raise TypeError("Long Content Pattern validated section must be a mapping.")
        section_id = str(section.get("section_id") or "")
        draft = drafted_by_id.get(section_id)
        if draft is None:
            raise RuntimeError(
                f"Long Content Pattern is missing draft for section {section_id!r}."
            )
        body = str(draft.get("body") or "").strip()
        if not body:
            raise ValueError(
                f"Long Content Pattern section {section_id!r} has an empty body."
            )
        parts.extend([f"## {str(section.get('title') or '').strip()}", body])
    return "\n\n".join(parts).strip()


__all__ = ["LongContentPatternConfig", "run_long_content_pattern"]
