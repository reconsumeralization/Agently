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

from .TaskShared import *


class AgentTaskTaskBoardReadbackMixin(AgentTaskMixinBase):
    """TaskBoard cold evidence readback and hot/cold ref projection."""

    async def _run_taskboard_readback_card(
        self,
        context: Any,
        context_pack: "WorkspaceContextPackage",
    ) -> TaskBoardCardResult:
        evidence_card_ids = list(getattr(context.card, "depends_on", ()) or ())
        try:
            evidence_view = build_task_board_evidence_view(
                context.revision,
                card_ids=evidence_card_ids or None,
            ).to_dict()
        except ValueError:
            evidence_view = build_task_board_evidence_view(context.revision).to_dict()
        refs = self._taskboard_readback_artifact_refs(evidence_view)
        file_refs = self._taskboard_readback_file_refs(evidence_view)
        skill_readbacks = self._taskboard_skill_context_readbacks(context.card, context_pack)
        skill_readback_evidence_items = self._taskboard_skill_context_readback_evidence_items(
            skill_readbacks,
            card_id=str(getattr(context.card, "id", "") or ""),
        )
        card_metadata = getattr(context.card, "metadata", {})
        if isinstance(card_metadata, Mapping):
            target_refs = self._normalize_taskboard_target_refs(
                card_metadata.get("workspace_target_refs") or card_metadata.get("target_refs")
            )
            self._merge_taskboard_file_refs(
                file_refs,
                self._taskboard_workspace_target_ref_file_refs(target_refs),
            )
        hot_artifact_refs = self._compact_taskboard_artifact_refs_for_hot_payload(refs)
        hot_file_refs = self._compact_taskboard_file_refs_for_hot_payload(file_refs)
        work_unit = WorkUnitIntent(
            id=f"taskboard:{context.card.id}:readback",
            origin="taskboard_card",
            objective=str(getattr(context.card, "objective", "") or "Read scoped cold evidence."),
            input_payload={
                "task_id": self.id,
                "goal": self.goal,
                "success_criteria": self.success_criteria,
                "task_context_contract": self._task_context_contract_for_model_prompt(),
                "card": context.card.to_dict(),
                "artifact_refs": hot_artifact_refs,
                "file_refs": hot_file_refs,
                "skill_context_readbacks": DataFormatter.sanitize(skill_readbacks),
                "evidence_scope": evidence_card_ids or "all",
            },
            input_refs=tuple(
                dict(item)
                for item in [
                    *[ref for ref in refs if isinstance(ref, Mapping)],
                    *[ref for ref in file_refs if isinstance(ref, Mapping)],
                    *[ref.get("ref", {}) for ref in skill_readbacks if isinstance(ref, Mapping)],
                ]
                if isinstance(item, Mapping)
            ),
            expected_deliverable={
                "allowed_execution_shape": "readback",
                "artifact_ref_count": len(refs),
                "file_ref_count": len(file_refs),
                "skill_context_ref_count": len(skill_readbacks),
            },
            evidence_requirements=tuple(
                [
                    {
                        "artifact_id": str(ref.get("artifact_id") or ""),
                        "action_call_id": str(ref.get("action_call_id") or ""),
                        "source": "taskboard_readback_card",
                    }
                    for ref in refs
                    if isinstance(ref, Mapping)
                ]
                + [
                    {
                        "path": str(ref.get("path") or ""),
                        "source": "taskboard_workspace_file_readback",
                    }
                    for ref in file_refs
                    if isinstance(ref, Mapping)
                ]
                + [
                    {
                        "skill_id": str(ref.get("skill_id") or ""),
                        "path": str(ref.get("path") or ""),
                        "source": "taskboard_skill_context_readback",
                    }
                    for ref in skill_readbacks
                    if isinstance(ref, Mapping)
                ]
            ),
            delivery_contract={
                "card": DataFormatter.sanitize(context.card.to_dict()),
                "execution_prompt": {"output_format": "json"},
            },
            quality_gates=(
                {
                    "kind": "taskboard_artifact_readback_status",
                    "allowed_statuses": ["completed", "blocked", "failed"],
                },
            ),
            runtime_preferences={
                "handler": "agent_task_artifact_readback",
                "plan_block_kind": "action_call",
                "preferred_execution_shape": "taskboard_readback",
                "strategy": "taskboard",
                "card_id": context.card.id,
            },
        )
        carrier_plan = {
            "execution_shape": "taskboard_readback",
            "effective_execution_shape": "taskboard_readback",
            "step_instruction": str(getattr(context.card, "objective", "") or "Read scoped cold evidence."),
            "expected_evidence": [
                {
                    "artifact_id": str(ref.get("artifact_id") or ""),
                    "action_call_id": str(ref.get("action_call_id") or ""),
                }
                for ref in refs
                if isinstance(ref, Mapping)
            ]
            + [
                {
                    "path": str(ref.get("path") or ""),
                }
                for ref in file_refs
                if isinstance(ref, Mapping)
            ],
            "rationale": "Execute one TaskBoard artifact readback card through the shared Block carrier.",
            "step_scope": {},
        }

        async def run_readback_work_unit(_context: Mapping[str, Any]) -> Mapping[str, Any]:
            await self._emit(
                f"agent_task.taskboard.card.{ self._stream_path_token(context.card.id) }.readback.started",
                {
                    "card_id": context.card.id,
                    "ref_count": len(refs),
                    "file_ref_count": len(file_refs),
                },
            )
            readbacks: list[dict[str, Any]] = []
            file_readbacks: list[dict[str, Any]] = []
            effective_file_refs = [dict(ref) for ref in file_refs if isinstance(ref, Mapping)]
            diagnostics: list[dict[str, Any]] = []
            readback_evidence_items: list[dict[str, Any]] = []
            if not refs and not file_refs and skill_readbacks:
                status = "completed"
                success_count = 0
                failed_count = 0
                file_success_count = 0
                file_failed_count = 0
                readback_evidence_items = list(skill_readback_evidence_items)
                diagnostics.append(
                    {
                        "code": "taskboard.readback.skill_context_refs",
                        "card_id": context.card.id,
                        "skill_context_ref_count": len(skill_readbacks),
                    }
                )
                payload = {
                    "status": status,
                    "answer": f"Read {len(skill_readbacks)} Skill context refs from the Manager context pack.",
                    "readbacks": readbacks,
                    "file_readbacks": file_readbacks,
                    "skill_context_readbacks": DataFormatter.sanitize(skill_readbacks),
                    "evidence_items": DataFormatter.sanitize(readback_evidence_items),
                    "evidence": [
                        f"skill:{item.get('skill_id')}:{item.get('path')} status={item.get('status')}"
                        for item in skill_readbacks
                    ],
                    "remaining_work": [],
                    "diagnostics": diagnostics,
                }
            elif not refs and not file_refs:
                status = "blocked"
                success_count = 0
                failed_count = 0
                file_success_count = 0
                file_failed_count = 0
                diagnostics.append(
                    {
                        "code": "taskboard.readback.no_refs",
                        "card_id": context.card.id,
                        "evidence_scope": evidence_card_ids or "all",
                    }
                )
                payload = {
                    "status": status,
                    "answer": "No Action artifact refs or Workspace file refs are available for this readback card.",
                    "readbacks": readbacks,
                    "file_readbacks": file_readbacks,
                    "evidence": [],
                    "remaining_work": [
                        "Upstream cards must produce Action artifact refs or Workspace file refs before readback can run."
                    ],
                    "diagnostics": diagnostics,
                }
            else:
                success_count = 0
                failed_count = 0
                action = getattr(self.agent, "action", None)
                reader = getattr(action, "async_read_action_artifact", None)
                if refs and not callable(reader):
                    success_count = 0
                    failed_count = len(refs)
                    diagnostics.append(
                        {
                            "code": "taskboard.readback.reader_unavailable",
                            "card_id": context.card.id,
                            "ref_count": len(refs),
                        }
                    )
                elif callable(reader):
                    for ref in refs:
                        artifact_id = str(ref.get("artifact_id") or "")
                        action_call_id = str(ref.get("action_call_id") or "")
                        try:
                            raw_readback = await self._await_taskboard_card_execution(
                                cast(Awaitable[Any], reader(artifact_id, action_call_id or None)),
                                card_id=context.card.id,
                                stage="readback",
                            )
                        except Exception as error:
                            raw_readback = {
                                "ok": False,
                                "status": "error",
                                "artifact_id": artifact_id,
                                "action_call_id": action_call_id,
                                "error": (
                                    f"{error.__class__.__name__}: "
                                    + _compact_agent_task_error_message(error, fallback=error.__class__.__name__)
                                ),
                            }
                        compact = self._compact_taskboard_action_artifact_readback(raw_readback, ref)
                        readbacks.append(compact)
                        if not compact.get("ok"):
                            diagnostics.append(
                                {
                                    "code": "taskboard.readback.ref_failed",
                                    "artifact_id": artifact_id,
                                    "action_call_id": action_call_id,
                                    "status": compact.get("status"),
                                    "error": compact.get("error"),
                                }
                            )

                    success_count = sum(1 for item in readbacks if item.get("ok"))
                    failed_count = len(readbacks) - success_count
                discovered_file_refs = self._taskboard_file_refs_from_action_readbacks(readbacks)
                added_file_refs = self._merge_taskboard_file_refs(effective_file_refs, discovered_file_refs)
                if added_file_refs:
                    diagnostics.append(
                        {
                            "code": "taskboard.readback.workspace_file_refs_discovered",
                            "card_id": context.card.id,
                            "file_ref_count": len(added_file_refs),
                        }
                    )

                async def read_workspace_ref(ref: Mapping[str, Any]) -> Mapping[str, Any]:
                    path = str(ref.get("path") or "").strip()
                    mode = str(ref.get("readback_mode") or "").strip()
                    if mode == "workspace_content":
                        segment = await self._await_taskboard_card_execution(
                            self.workspace.read_bounded(path, limit=_TASKBOARD_READBACK_PREVIEW_CHARS),
                            card_id=context.card.id,
                            stage="workspace_content_readback",
                        )
                        return self._taskboard_workspace_content_segment_readback(segment, ref)
                    try:
                        return await self._await_taskboard_card_execution(
                            self.workspace.read_file(path, max_bytes=_TASKBOARD_READBACK_PREVIEW_CHARS),
                            card_id=context.card.id,
                            stage="workspace_file_readback",
                        )
                    except FileNotFoundError:
                        segment = await self._await_taskboard_card_execution(
                            self.workspace.read_bounded(path, limit=_TASKBOARD_READBACK_PREVIEW_CHARS),
                            card_id=context.card.id,
                            stage="workspace_content_readback",
                        )
                        return self._taskboard_workspace_content_segment_readback(segment, ref)

                for ref in effective_file_refs:
                    path = str(ref.get("path") or "").strip()
                    try:
                        raw_file_readback = await read_workspace_ref(ref)
                    except Exception as error:
                        raw_file_readback = {
                            "ok": False,
                            "readable": False,
                            "status": "error",
                            "path": path,
                            "error": (
                                f"{error.__class__.__name__}: "
                                + _compact_agent_task_error_message(error, fallback=error.__class__.__name__)
                            ),
                        }
                    compact_file = self._compact_taskboard_workspace_file_readback(raw_file_readback, ref)
                    file_readbacks.append(compact_file)
                    if not compact_file.get("ok"):
                        diagnostics.append(
                            {
                                "code": "taskboard.readback.file_failed",
                                "path": path,
                                "status": compact_file.get("status"),
                                "error": compact_file.get("error"),
                            }
                        )
                file_success_count = sum(1 for item in file_readbacks if item.get("ok"))
                file_failed_count = len(file_readbacks) - file_success_count
                status = "completed" if (success_count + file_success_count) > 0 else "failed"
                remaining_work = []
                if failed_count:
                    remaining_work.append(f"{ failed_count } artifact refs could not be read.")
                if file_failed_count:
                    remaining_work.append(f"{ file_failed_count } Workspace file refs could not be read.")
                readback_evidence_items = [
                    *self._taskboard_action_artifact_readback_evidence_items(
                        readbacks,
                        source="taskboard_readback_card",
                        card_id=context.card.id,
                    ),
                    *self._taskboard_workspace_file_readback_evidence_items(
                        file_readbacks,
                        card_id=context.card.id,
                    ),
                    *skill_readback_evidence_items,
                ]
                payload = {
                    "status": status,
                    "answer": (
                        f"Read { success_count } of { len(refs) } Action artifact refs and "
                        f"{ file_success_count } of { len(effective_file_refs) } Workspace file refs with bounded previews."
                    ),
                    "readbacks": readbacks,
                    "file_readbacks": file_readbacks,
                    "skill_context_readbacks": DataFormatter.sanitize(skill_readbacks),
                    "file_refs": DataFormatter.sanitize(effective_file_refs),
                    "evidence_items": DataFormatter.sanitize(readback_evidence_items),
                    "evidence": [
                        *[
                            f"artifact:{ item.get('artifact_id') } status={ item.get('status') }"
                            for item in readbacks
                            if item.get("artifact_id")
                        ],
                        *[
                            f"file:{ item.get('path') } status={ item.get('status') }"
                            for item in file_readbacks
                            if item.get("path")
                        ],
                        *[
                            f"skill:{ item.get('skill_id') }:{ item.get('path') } status={ item.get('status') }"
                            for item in skill_readbacks
                        ],
                    ],
                    "remaining_work": remaining_work,
                    "diagnostics": diagnostics,
                }

            await self._emit(
                f"agent_task.taskboard.card.{ self._stream_path_token(context.card.id) }.readback.completed",
                {
                    "card_id": context.card.id,
                    "status": status,
                    "success_count": success_count,
                    "failed_count": failed_count,
                    "ref_count": len(refs),
                    "file_success_count": file_success_count,
                    "file_failed_count": file_failed_count,
                    "file_ref_count": len(effective_file_refs),
                },
            )
            execution_diagnostic = {
                "execution_kind": "taskboard_artifact_readback",
                "execution_strategy": self.execution_strategy,
                "card_id": context.card.id,
                "ref_count": len(refs),
                "success_count": success_count,
                "failed_count": failed_count,
                "file_ref_count": len(effective_file_refs),
                "file_success_count": file_success_count,
                "file_failed_count": file_failed_count,
            }
            return {
                "execution_result": DataFormatter.sanitize(payload),
                "execution_meta": {
                    "execution_id": f"{self.id}:taskboard:{context.card.id}:readback",
                    "status": status,
                    "route": {
                        "selected_route": "action_artifact_readback",
                        "status": status,
                    },
                    "logs": {
                        "action_logs": {},
                        "route_logs": {},
                        "errors": [],
                    },
                    "diagnostics": [execution_diagnostic],
                    "artifact_refs": DataFormatter.sanitize(refs),
                    "file_refs": DataFormatter.sanitize(effective_file_refs),
                    "blocks": {
                        "evidence": {
                            "evidence_items": DataFormatter.sanitize(readback_evidence_items),
                        }
                    },
                },
                "action_evidence": [
                    {
                        "kind": "taskboard_artifact_readback",
                        "card_id": context.card.id,
                        "artifact_refs": DataFormatter.sanitize(refs),
                        "file_refs": DataFormatter.sanitize(effective_file_refs),
                        "readbacks": DataFormatter.sanitize(readbacks),
                        "file_readbacks": DataFormatter.sanitize(file_readbacks),
                        "status": status,
                    }
                ],
            }

        try:
            card_output, execution_meta, _work_unit_result = await self._run_work_unit_through_blocks(
                work_unit=work_unit,
                plan=carrier_plan,
                context_pack=context_pack,
                execution_id=f"{self.id}:taskboard:{context.card.id}:readback",
                handler=run_readback_work_unit,
                start_payload={"card_id": context.card.id, "ref_count": len(refs)},
            )
        except Exception as error:
            return self._failed_taskboard_card_result(
                card_id=context.card.id,
                error=error,
                execution_id=None,
            )

        payload = dict(card_output) if isinstance(card_output, Mapping) else {"status": "failed", "answer": card_output}
        diagnostics = []
        raw_diagnostics = payload.get("diagnostics")
        if isinstance(raw_diagnostics, Sequence) and not isinstance(raw_diagnostics, str | bytes | bytearray):
            diagnostics.extend(dict(item) if isinstance(item, Mapping) else {"value": item} for item in raw_diagnostics)
        success_count = int(payload.get("success_count", 0) or 0) if isinstance(payload.get("success_count"), int) else 0
        readbacks = payload.get("readbacks", [])
        if isinstance(readbacks, Sequence) and not isinstance(readbacks, str | bytes | bytearray):
            success_count = sum(1 for item in readbacks if isinstance(item, Mapping) and item.get("ok"))
        file_readbacks = payload.get("file_readbacks", [])
        file_success_count = 0
        if isinstance(file_readbacks, Sequence) and not isinstance(file_readbacks, str | bytes | bytearray):
            file_success_count = sum(1 for item in file_readbacks if isinstance(item, Mapping) and item.get("ok"))
        file_readback_evidence_items = self._taskboard_workspace_file_readback_evidence_items(
            [item for item in file_readbacks if isinstance(item, Mapping)],
            card_id=context.card.id,
        )
        if file_readback_evidence_items:
            existing_items = payload.get("evidence_items")
            existing_sequence = (
                list(existing_items)
                if isinstance(existing_items, Sequence) and not isinstance(existing_items, str | bytes | bytearray)
                else []
            )
            payload["evidence_items"] = DataFormatter.sanitize(
                self._dedupe_taskboard_readback_evidence_items(
                    [
                        *[item for item in existing_sequence if isinstance(item, Mapping)],
                        *file_readback_evidence_items,
                    ]
                )
            )
        result_file_refs = [dict(ref) for ref in file_refs if isinstance(ref, Mapping)]
        raw_result_file_refs = payload.get("file_refs")
        if isinstance(raw_result_file_refs, Sequence) and not isinstance(
            raw_result_file_refs,
            str | bytes | bytearray,
        ):
            result_file_refs = [dict(ref) for ref in raw_result_file_refs if isinstance(ref, Mapping)]
        failed_count = max(0, len(refs) - success_count)
        file_failed_count = max(0, len(result_file_refs) - file_success_count)
        diagnostics.append(
            {
                "execution_kind": "taskboard_artifact_readback",
                "execution_strategy": self.execution_strategy,
                "card_id": context.card.id,
                "ref_count": len(refs),
                "success_count": success_count,
                "failed_count": failed_count,
                "file_ref_count": len(result_file_refs),
                "file_success_count": file_success_count,
                "file_failed_count": file_failed_count,
                "block_carrier": self._compact_block_carrier_for_taskboard_meta(
                    execution_meta.get("block_carrier", {}),
                    blocks=execution_meta.get("blocks"),
                ),
            }
        )
        execution_evidence_ledger = self._evidence_ledger_from_execution_meta(cast(Mapping[str, Any], execution_meta))
        payload_evidence_items = payload.get("evidence_items")
        if isinstance(payload_evidence_items, Sequence) and not isinstance(
            payload_evidence_items,
            str | bytes | bytearray,
        ):
            execution_evidence_ledger = evidence_ledger_view(
                {
                    "evidence_items": [
                        *list(execution_evidence_ledger.get("items", [])),
                        *[item for item in payload_evidence_items if isinstance(item, Mapping)],
                    ]
                },
                max_items=80,
                body_chars=2400,
            )
        return TaskBoardCardResult(
            card_id=context.card.id,
            status=str(payload.get("status") or "failed"),
            preview=DataFormatter.sanitize(payload),
            artifact_refs=tuple(refs),
            file_refs=tuple(result_file_refs),
            diagnostics=tuple(diagnostics),
            metadata={
                "execution_id": execution_meta.get("execution_id"),
                "execution_kind": "taskboard_artifact_readback",
                "execution_strategy": self.execution_strategy,
                "ref_count": len(refs),
                "success_count": success_count,
                "failed_count": failed_count,
                "file_ref_count": len(result_file_refs),
                "file_success_count": file_success_count,
                "file_failed_count": file_failed_count,
                "block_carrier": self._compact_block_carrier_for_taskboard_meta(
                    execution_meta.get("block_carrier", {}),
                    blocks=execution_meta.get("blocks"),
                ),
                "evidence_ledger": execution_evidence_ledger,
            },
        )

    @staticmethod
    def _taskboard_action_artifact_recall_records(evidence_view: Mapping[str, Any]) -> list[dict[str, Any]]:
        raw_refs = evidence_view.get("artifact_refs")
        if not isinstance(raw_refs, Sequence) or isinstance(raw_refs, str | bytes | bytearray):
            return []
        refs: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for item in raw_refs:
            if not isinstance(item, Mapping):
                continue
            artifact_id = str(item.get("artifact_id") or "").strip()
            if not artifact_id:
                continue
            action_call_id = str(item.get("action_call_id") or "").strip()
            key = (artifact_id, action_call_id)
            if key in seen:
                continue
            seen.add(key)
            refs.append(
                {
                    "artifact_id": artifact_id,
                    "action_call_id": action_call_id,
                    "artifact_type": str(item.get("artifact_type") or ""),
                    "role": str(item.get("role") or ""),
                    "label": str(item.get("label") or ""),
                    "media_type": str(item.get("media_type") or ""),
                    "bytes": item.get("bytes", item.get("size")),
                    "sha256": item.get("sha256"),
                    "truncated": bool(item.get("truncated")),
                    "full_value_available": bool(item.get("full_value_available", item.get("available", False))),
                }
            )
        if not refs:
            return []
        return [
            {
                "action_id": "taskboard_upstream_evidence",
                "status": "success",
                "artifact_refs": refs,
            }
        ]

    @classmethod
    def _taskboard_skill_context_readbacks(
        cls,
        card: Any,
        context_pack: Any,
    ) -> list[dict[str, Any]]:
        if not isinstance(context_pack, Mapping):
            return []
        skill_context_pack = context_pack.get("skills_context_pack")
        if not isinstance(skill_context_pack, Mapping):
            return []
        requested_refs = cls._taskboard_card_requested_skill_refs(card)
        raw_skills = skill_context_pack.get("skills")
        if not isinstance(raw_skills, Sequence) or isinstance(raw_skills, str | bytes | bytearray):
            return []
        readbacks: list[dict[str, Any]] = []
        seen: set[tuple[str, str, str]] = set()

        def add_entry(
            *,
            skill_id: str,
            path: str,
            citation: str,
            content: str,
            truncated: bool,
            kind: str,
            summary: str = "",
        ) -> None:
            aliases = cls._taskboard_skill_context_aliases(skill_id=skill_id, path=path, citation=citation)
            if requested_refs and not any(alias in requested_refs for alias in aliases):
                return
            key = (skill_id, path, citation)
            if key in seen:
                return
            seen.add(key)
            readbacks.append(
                DataFormatter.sanitize(
                    {
                        "ok": bool(content.strip()),
                        "status": "completed" if content.strip() else "empty",
                        "skill_id": skill_id,
                        "path": path,
                        "citation": citation,
                        "kind": kind,
                        "summary": summary,
                        "content_preview": content,
                        "truncated": bool(truncated),
                        "ref": {
                            "kind": "skill_context",
                            "source": "skills_manager.context_pack",
                            "skill_id": skill_id,
                            "path": path,
                            "citation": citation,
                            "content_state": "bounded_readback_available" if content.strip() else "empty",
                        },
                        "aliases": aliases,
                    }
                )
            )

        for skill in raw_skills:
            if not isinstance(skill, Mapping):
                continue
            skill_id = str(skill.get("skill_id") or skill.get("id") or "").strip()
            if not skill_id:
                continue
            guidance = skill.get("guidance")
            if isinstance(guidance, Mapping):
                path = str(guidance.get("path") or "SKILL.md").strip() or "SKILL.md"
                citation = str(guidance.get("citation") or f"skills/{skill_id}/{path}").strip()
                add_entry(
                    skill_id=skill_id,
                    path=path,
                    citation=citation,
                    content=str(guidance.get("excerpt") or guidance.get("content") or ""),
                    truncated=bool(guidance.get("truncated")),
                    kind="guidance",
                    summary=str(guidance.get("summary") or ""),
                )
            resources = skill.get("selected_resources")
            if not isinstance(resources, Sequence) or isinstance(resources, str | bytes | bytearray):
                continue
            for resource in resources:
                if not isinstance(resource, Mapping):
                    continue
                path = str(resource.get("path") or "").strip()
                if not path:
                    continue
                citation = str(resource.get("citation") or f"skills/{skill_id}/{path}").strip()
                add_entry(
                    skill_id=skill_id,
                    path=path,
                    citation=citation,
                    content=str(resource.get("content") or resource.get("excerpt") or ""),
                    truncated=bool(resource.get("truncated")),
                    kind=str(resource.get("kind") or "resource"),
                    summary=str(resource.get("summary") or ""),
                )
        return readbacks

    @classmethod
    def _taskboard_card_requested_skill_refs(cls, card: Any) -> set[str]:
        requested: set[str] = set(cls._normalize_text_ref_sequence(getattr(card, "input_refs", ()) or ()))
        evidence_contract = getattr(card, "evidence_contract", {})
        if isinstance(evidence_contract, Mapping):
            requested.update(cls._normalize_text_ref_sequence(evidence_contract.get("evidence_to_use")))
            requested.update(cls._normalize_text_ref_sequence(evidence_contract.get("requires_skill_refs")))
        metadata = getattr(card, "metadata", {})
        if isinstance(metadata, Mapping):
            requested.update(cls._normalize_text_ref_sequence(metadata.get("skill_context_refs")))
        return requested

    @staticmethod
    def _normalize_text_ref_sequence(value: Any) -> list[str]:
        if isinstance(value, str):
            return [value.strip()] if value.strip() else []
        if not isinstance(value, Sequence) or isinstance(value, bytes | bytearray):
            return []
        refs: list[str] = []
        for item in value:
            text = str(item or "").strip()
            if text and text not in refs:
                refs.append(text)
        return refs

    @staticmethod
    def _taskboard_skill_context_aliases(*, skill_id: str, path: str, citation: str) -> list[str]:
        aliases: list[str] = []
        for value in (
            citation,
            path,
            f"{skill_id}/{path}" if path else "",
            f"skills/{skill_id}/{path}" if path else "",
            f"skills/{skill_id}/SKILL.md" if path == "SKILL.md" else "",
        ):
            text = str(value or "").strip()
            if text and text not in aliases:
                aliases.append(text)
        return aliases

    def _taskboard_skill_context_readback_evidence_items(
        self,
        readbacks: Sequence[Mapping[str, Any]],
        *,
        card_id: str,
    ) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for index, readback in enumerate(readbacks):
            if not isinstance(readback, Mapping):
                continue
            skill_id = str(readback.get("skill_id") or "").strip()
            path = str(readback.get("path") or "").strip()
            citation = str(readback.get("citation") or "").strip()
            source = "skills_manager.context_pack"
            evidence_id = self._taskboard_workspace_readback_evidence_id(
                "skill_context_readback",
                citation or f"{skill_id}/{path}",
                f"agent_task.taskboard.card.{card_id}.{source}",
            )
            ok = bool(readback.get("ok"))
            preview = str(readback.get("content_preview") or "")
            truncated = bool(readback.get("truncated"))
            item: dict[str, Any] = {
                "id": evidence_id,
                "kind": "skill_context.readback",
                "status": "ok" if ok else "empty",
                "raw_status": readback.get("status") or ("read" if ok else "empty"),
                "body_state": "truncated" if truncated else ("full" if preview else "empty"),
                "skill_id": skill_id,
                "path": path,
                "citation": citation,
                "source": source,
                "truncated": truncated,
                "aliases": self._taskboard_skill_context_aliases(
                    skill_id=skill_id,
                    path=path,
                    citation=citation,
                ),
                "provenance": {
                    "source": source,
                    "taskboard_card_id": card_id,
                    "skill_id": skill_id,
                    "path": path,
                    "citation": citation,
                    "readback_index": index,
                },
                "supports": {
                    "content": bool(ok and preview),
                    "unavailability": not ok,
                    "ref_pointer": False,
                },
            }
            if preview:
                item["body"] = preview
            summary = str(readback.get("summary") or "").strip()
            if summary:
                item["summary"] = summary
            items.append(DataFormatter.sanitize(item))
        return self._dedupe_taskboard_readback_evidence_items(items)

    @classmethod
    def _taskboard_readback_artifact_refs(cls, evidence_view: Mapping[str, Any]) -> list[dict[str, Any]]:
        records = cls._taskboard_action_artifact_recall_records(evidence_view)
        if not records:
            return []
        refs = records[0].get("artifact_refs")
        if not isinstance(refs, list):
            return []
        return [dict(ref) for ref in refs if isinstance(ref, Mapping)]

    @classmethod
    def _taskboard_readback_file_refs(cls, evidence_view: Mapping[str, Any]) -> list[dict[str, Any]]:
        refs: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()

        def collect(value: Any) -> None:
            if not isinstance(value, Sequence) or isinstance(value, str | bytes | bytearray):
                return
            for item in value:
                if not isinstance(item, Mapping):
                    continue
                path = str(item.get("path") or "").strip()
                if not path:
                    continue
                sha = str(item.get("sha256") or "").strip()
                key = (path, sha)
                if key in seen:
                    continue
                seen.add(key)
                refs.append(dict(DataFormatter.sanitize(item)))

        collect(evidence_view.get("file_refs"))
        collect(evidence_view.get("artifact_refs"))
        cards = evidence_view.get("cards")
        if isinstance(cards, Sequence) and not isinstance(cards, str | bytes | bytearray):
            for card in cards:
                if isinstance(card, Mapping):
                    collect(card.get("artifact_refs"))
                    collect(card.get("file_refs"))
        return refs

    @staticmethod
    def _taskboard_file_ref_key(ref: Mapping[str, Any]) -> tuple[str, str]:
        return (str(ref.get("path") or "").strip(), str(ref.get("sha256") or "").strip())

    @classmethod
    def _merge_taskboard_file_refs(
        cls,
        refs: list[dict[str, Any]],
        candidates: Sequence[Mapping[str, Any]],
    ) -> list[dict[str, Any]]:
        seen = {cls._taskboard_file_ref_key(ref) for ref in refs if cls._taskboard_file_ref_key(ref)[0]}
        added: list[dict[str, Any]] = []
        for candidate in candidates:
            if not isinstance(candidate, Mapping):
                continue
            path = str(candidate.get("path") or "").strip()
            if not path:
                continue
            item = dict(DataFormatter.sanitize(candidate))
            key = cls._taskboard_file_ref_key(item)
            if key in seen:
                continue
            seen.add(key)
            refs.append(item)
            added.append(item)
        return added

    @classmethod
    def _taskboard_file_refs_from_action_readbacks(cls, readbacks: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
        refs: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()

        def collect(value: Any, *, source_ref: Mapping[str, Any] | None = None) -> None:
            if not isinstance(value, Mapping):
                return
            raw_refs = value.get("file_refs")
            if isinstance(raw_refs, Sequence) and not isinstance(raw_refs, str | bytes | bytearray):
                for raw_ref in raw_refs:
                    if not isinstance(raw_ref, Mapping):
                        continue
                    path = str(raw_ref.get("path") or "").strip()
                    if not path:
                        continue
                    item = dict(DataFormatter.sanitize(raw_ref))
                    if source_ref is not None:
                        item.setdefault("source", "taskboard_action_artifact_readback")
                        artifact_id = str(source_ref.get("artifact_id") or "").strip()
                        action_call_id = str(source_ref.get("action_call_id") or "").strip()
                        if artifact_id:
                            item.setdefault("source_artifact_id", artifact_id)
                        if action_call_id:
                            item.setdefault("source_action_call_id", action_call_id)
                    key = cls._taskboard_file_ref_key(item)
                    if key in seen:
                        continue
                    seen.add(key)
                    refs.append(item)
            for key in ("artifact_manifest", "read_preview", "value_preview", "data", "result"):
                nested = value.get(key)
                if isinstance(nested, Mapping):
                    collect(nested, source_ref=source_ref)

        for readback in readbacks:
            if isinstance(readback, Mapping):
                collect(readback, source_ref=readback)
        return refs

    @classmethod
    def _taskboard_workspace_readback_evidence_id(cls, prefix: str, path: str, source: str) -> str:
        raw = f"{ prefix }:{ source }:{ path }"
        return "".join(ch if ch.isalnum() or ch in "._:-" else "_" for ch in raw)[:240]

    @classmethod
    def _dedupe_taskboard_readback_evidence_items(
        cls,
        items: Sequence[Mapping[str, Any]],
    ) -> list[dict[str, Any]]:
        deduped: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in items:
            if not isinstance(item, Mapping):
                continue
            evidence_id = str(item.get("id") or "").strip()
            if evidence_id and evidence_id in seen:
                continue
            if evidence_id:
                seen.add(evidence_id)
            deduped.append(dict(DataFormatter.sanitize(item)))
        return deduped

    def _taskboard_workspace_file_readback_evidence_items(
        self,
        file_readbacks: Sequence[Mapping[str, Any]],
        *,
        card_id: str,
    ) -> list[dict[str, Any]]:
        required_paths = {str(path or "").strip() for path in self._required_workspace_deliverables()}
        items: list[dict[str, Any]] = []
        for index, readback in enumerate(file_readbacks):
            if not isinstance(readback, Mapping):
                continue
            path = str(readback.get("path") or "").strip()
            if not path:
                continue
            ref = readback.get("ref")
            ref = ref if isinstance(ref, Mapping) else {}
            is_workspace_artifact = path in required_paths or self._is_trusted_workspace_artifact_ref(ref)
            source_suffix = "workspace_artifact" if is_workspace_artifact else "workspace_file"
            source = f"agent_task.taskboard.card.{ card_id }.{ source_suffix }"
            prefix = "workspace_artifact_readback" if is_workspace_artifact else "workspace_file_readback"
            evidence_id = self._taskboard_workspace_readback_evidence_id(prefix, path, source)
            ok = bool(readback.get("ok"))
            preview = str(readback.get("content_preview") or "")
            preview_meta = readback.get("content_preview_meta")
            preview_meta = preview_meta if isinstance(preview_meta, Mapping) else {}
            truncated = bool(readback.get("truncated")) or bool(preview_meta.get("truncated"))
            item: dict[str, Any] = {
                "id": evidence_id,
                "kind": "workspace_artifact.readback" if is_workspace_artifact else "workspace_file.readback",
                "status": "ok" if ok else "failed",
                "raw_status": readback.get("status") or ("read" if ok else "failed"),
                "body_state": "truncated" if truncated else ("full" if preview else "ref_only"),
                "path": path,
                "source": source,
                "read_bytes": readback.get("read_bytes"),
                "offset": readback.get("offset"),
                "truncated": truncated,
                "aliases": [
                    path,
                    f"{ card_id }:{ path }",
                    f"{ source }:{ path }",
                ],
                "provenance": {
                    "source": source,
                    "taskboard_card_id": card_id,
                    "path": path,
                    "readback_index": index,
                },
                "supports": {
                    "content": bool(ok and preview),
                    "unavailability": not ok,
                    "ref_pointer": False,
                },
            }
            if preview:
                item["body"] = preview
            if preview_meta:
                item["preview_meta"] = dict(DataFormatter.sanitize(preview_meta))
            error = readback.get("error")
            if error:
                item["diagnostics"] = [{"code": "taskboard.readback.file_failed", "message": str(error)}]
            items.append(DataFormatter.sanitize(item))
        return self._dedupe_taskboard_readback_evidence_items(items)

    @staticmethod
    def _taskboard_dependency_ref_needs_readback(ref: Mapping[str, Any]) -> bool:
        artifact_id = str(ref.get("artifact_id") or "").strip()
        if not artifact_id:
            return False
        role = str(ref.get("role") or "").strip().lower()
        if role and role not in {"output", "result", "artifact"}:
            return False
        if not bool(ref.get("available", True)) and not bool(ref.get("full_value_available")):
            return False
        if bool(ref.get("truncated")):
            return True
        try:
            size = int(ref.get("bytes", ref.get("size", 0)) or 0)
        except Exception:
            size = 0
        return bool(ref.get("full_value_available")) and size > _TASKBOARD_PROMPT_RESULT_CHARS

    async def _taskboard_dependency_action_artifact_readbacks(
        self,
        evidence_view: Mapping[str, Any],
        *,
        card_id: str,
        context_pack: "WorkspaceContextPackage",
    ) -> dict[str, Any]:
        refs = [
            ref
            for ref in self._taskboard_readback_artifact_refs(evidence_view)
            if self._taskboard_dependency_ref_needs_readback(ref)
        ][:_TASKBOARD_DEPENDENCY_READBACK_MAX_REFS]
        hot_artifact_refs = self._compact_taskboard_artifact_refs_for_hot_payload(refs)
        payload: dict[str, Any] = {
            "schema_version": "agent_task_taskboard_dependency_readbacks/v1",
            "card_id": card_id,
            "ref_count": len(refs),
            "readbacks": [],
            "diagnostics": [],
            "bounded": {
                "preview_chars": _TASKBOARD_DEPENDENCY_READBACK_PREVIEW_CHARS,
                "max_refs": _TASKBOARD_DEPENDENCY_READBACK_MAX_REFS,
            },
        }
        if not refs:
            return payload

        work_unit = WorkUnitIntent(
            id=f"taskboard:{card_id}:dependency-readback",
            origin="taskboard_card",
            objective="Read bounded dependency Action artifact previews before executing the card.",
            input_payload={
                "task_id": self.id,
                "goal": self.goal,
                "task_context_contract": self._task_context_contract_for_model_prompt(),
                "card_id": card_id,
                "artifact_refs": hot_artifact_refs,
                "bounded": dict(payload["bounded"]),
            },
            input_refs=tuple(dict(item) for item in refs if isinstance(item, Mapping)),
            expected_deliverable={
                "allowed_execution_shape": "dependency_readback",
                "artifact_ref_count": len(refs),
            },
            evidence_requirements=tuple(
                {
                    "artifact_id": str(ref.get("artifact_id") or ""),
                    "action_call_id": str(ref.get("action_call_id") or ""),
                    "source": "taskboard_dependency_readback",
                }
                for ref in refs
                if isinstance(ref, Mapping)
            ),
            delivery_contract={"execution_prompt": {"output_format": "json"}},
            quality_gates=(
                {
                    "kind": "taskboard_dependency_readback_status",
                    "allowed_statuses": ["completed", "failed"],
                },
            ),
            runtime_preferences={
                "handler": "agent_task_dependency_artifact_readback",
                "plan_block_kind": "action_call",
                "preferred_execution_shape": "taskboard_dependency_readback",
                "strategy": "taskboard",
                "card_id": card_id,
            },
        )
        carrier_plan = {
            "execution_shape": "taskboard_dependency_readback",
            "effective_execution_shape": "taskboard_dependency_readback",
            "step_instruction": "Read bounded dependency Action artifact previews before executing the card.",
            "expected_evidence": [
                {
                    "artifact_id": str(ref.get("artifact_id") or ""),
                    "action_call_id": str(ref.get("action_call_id") or ""),
                }
                for ref in refs
                if isinstance(ref, Mapping)
            ],
            "rationale": "Execute TaskBoard dependency artifact prefetch through the shared Block carrier.",
            "step_scope": {},
        }

        async def run_dependency_readback_work_unit(_context: Mapping[str, Any]) -> Mapping[str, Any]:
            action = getattr(self.agent, "action", None)
            reader = getattr(action, "async_read_action_artifact", None)
            readbacks: list[dict[str, Any]] = []
            diagnostics: list[dict[str, Any]] = []
            await self._emit(
                f"agent_task.taskboard.card.{ self._stream_path_token(card_id) }.dependency_readback.started",
                {"card_id": card_id, "ref_count": len(refs)},
            )
            if not callable(reader):
                diagnostics.append(
                    {
                        "code": "taskboard.dependency_readback.reader_unavailable",
                        "message": "Action artifact readback is unavailable on the bound Agent.",
                        "ref_count": len(refs),
                    }
                )
            else:
                for ref in refs:
                    artifact_id = str(ref.get("artifact_id") or "")
                    action_call_id = str(ref.get("action_call_id") or "")
                    try:
                        raw_readback = await self._await_taskboard_card_execution(
                            cast(Awaitable[Any], reader(artifact_id, action_call_id or None)),
                            card_id=card_id,
                            stage="dependency_readback",
                        )
                    except Exception as error:
                        raw_readback = {
                            "ok": False,
                            "status": "error",
                            "artifact_id": artifact_id,
                            "action_call_id": action_call_id,
                            "error": (
                                f"{error.__class__.__name__}: "
                                + _compact_agent_task_error_message(error, fallback=error.__class__.__name__)
                            ),
                        }
                    compact = self._compact_taskboard_action_artifact_readback(
                        raw_readback,
                        ref,
                        max_chars=_TASKBOARD_DEPENDENCY_READBACK_PREVIEW_CHARS,
                    )
                    readbacks.append(compact)
                    if not compact.get("ok"):
                        diagnostics.append(
                            {
                                "code": "taskboard.dependency_readback.ref_failed",
                                "artifact_id": artifact_id,
                                "action_call_id": action_call_id,
                                "status": compact.get("status"),
                                "error": compact.get("error"),
                            }
                        )
            output = dict(payload)
            output["readbacks"] = readbacks
            output["diagnostics"] = diagnostics
            output["evidence_items"] = self._taskboard_action_artifact_readback_evidence_items(
                readbacks,
                source="taskboard_dependency_readback",
                card_id=card_id,
            )
            output["success_count"] = sum(1 for item in readbacks if item.get("ok"))
            failed_count = len(readbacks) - int(output["success_count"])
            status = "completed" if int(output["success_count"]) > 0 else "failed"
            await self._emit(
                f"agent_task.taskboard.card.{ self._stream_path_token(card_id) }.dependency_readback.completed",
                {
                    "card_id": card_id,
                    "ref_count": len(refs),
                    "success_count": output["success_count"],
                    "failed_count": failed_count,
                },
            )
            return {
                "execution_result": DataFormatter.sanitize(output),
                "execution_meta": {
                    "execution_id": f"{self.id}:taskboard:{card_id}:dependency-readback",
                    "status": status,
                    "route": {
                        "selected_route": "action_artifact_dependency_readback",
                        "status": status,
                    },
                    "logs": {
                        "action_logs": {},
                        "route_logs": {},
                        "errors": [],
                    },
                    "diagnostics": [
                        {
                            "execution_kind": "taskboard_dependency_artifact_readback",
                            "execution_strategy": self.execution_strategy,
                            "card_id": card_id,
                            "ref_count": len(refs),
                            "success_count": output["success_count"],
                            "failed_count": failed_count,
                        }
                    ],
                    "artifact_refs": DataFormatter.sanitize(refs),
                    "blocks": {
                        "evidence": {
                            "evidence_items": DataFormatter.sanitize(output["evidence_items"]),
                        }
                    },
                },
                "action_evidence": [
                    {
                        "kind": "taskboard_dependency_artifact_readback",
                        "card_id": card_id,
                        "artifact_refs": DataFormatter.sanitize(refs),
                        "readbacks": DataFormatter.sanitize(readbacks),
                        "status": status,
                    }
                ],
            }

        try:
            readback_output, execution_meta, _work_unit_result = await self._run_work_unit_through_blocks(
                work_unit=work_unit,
                plan=carrier_plan,
                context_pack=context_pack,
                execution_id=f"{self.id}:taskboard:{card_id}:dependency-readback",
                handler=run_dependency_readback_work_unit,
                start_payload={"card_id": card_id, "ref_count": len(refs)},
            )
        except Exception as error:
            payload["diagnostics"] = [
                {
                    "code": "taskboard.dependency_readback.execution_failed",
                    "type": error.__class__.__name__,
                    "message": _compact_agent_task_error_message(error, fallback=error.__class__.__name__),
                    "ref_count": len(refs),
                }
            ]
            return DataFormatter.sanitize(payload)

        output_payload = dict(readback_output) if isinstance(readback_output, Mapping) else payload
        compact_carrier = self._compact_block_carrier_for_taskboard_meta(
            execution_meta.get("block_carrier", {}),
            blocks=execution_meta.get("blocks"),
        )
        self.diagnostics.setdefault("taskboard_dependency_readback_block_carriers", []).append(
            {
                "card_id": card_id,
                "ref_count": len(refs),
                "block_carrier": compact_carrier,
            }
        )
        return DataFormatter.sanitize(output_payload)

    @classmethod
    def _taskboard_dependency_readback_evidence_items(cls, dependency_readbacks: Any) -> list[dict[str, Any]]:
        if not isinstance(dependency_readbacks, Mapping):
            return []
        raw_items = dependency_readbacks.get("evidence_items")
        if not isinstance(raw_items, Sequence) or isinstance(raw_items, str | bytes | bytearray):
            return []
        return [dict(DataFormatter.sanitize(item)) for item in raw_items if isinstance(item, Mapping)]

    @classmethod
    def _taskboard_action_artifact_readback_evidence_items(
        cls,
        readbacks: Sequence[Any],
        *,
        source: str,
        card_id: str = "",
    ) -> list[dict[str, Any]]:
        if not isinstance(readbacks, Sequence) or isinstance(readbacks, str | bytes | bytearray):
            return []
        items: list[dict[str, Any]] = []
        for index, readback in enumerate(readbacks):
            if not isinstance(readback, Mapping):
                continue
            artifact_id = str(readback.get("artifact_id") or "").strip()
            action_call_id = str(readback.get("action_call_id") or "").strip()
            value_preview = readback.get("value_preview")
            body = cls._taskboard_readback_evidence_body(value_preview)
            preview_meta = readback.get("value_preview_meta")
            truncated = bool(preview_meta.get("truncated")) if isinstance(preview_meta, Mapping) else False
            ok = bool(readback.get("ok"))
            status = "ok" if ok and body else ("empty" if ok else "failed")
            body_state = "truncated" if ok and body and truncated else ("bounded" if ok and body else "ref_only")
            raw_status = str(readback.get("status") or status)
            evidence_id = cls._taskboard_readback_evidence_id(
                "taskboard_action_artifact_readback",
                source,
                card_id,
                artifact_id,
                action_call_id,
                str(index),
            )
            item: dict[str, Any] = {
                "id": evidence_id,
                "kind": "taskboard_action_artifact.readback",
                "status": status,
                "raw_status": raw_status,
                "body_state": body_state,
                "artifact_id": artifact_id,
                "action_call_id": action_call_id,
                "aliases": cls._taskboard_readback_evidence_aliases(readback),
                "source": source,
                "provenance": {
                    "source": source,
                    "taskboard_card_id": card_id,
                    "artifact_id": artifact_id,
                    "action_call_id": action_call_id,
                },
                "supports": {
                    "content": status == "ok" and body_state in {"bounded", "truncated"},
                    "unavailability": status in {"failed", "empty"},
                    "ref_pointer": False,
                },
            }
            ref = readback.get("ref")
            if isinstance(ref, Mapping):
                item["ref"] = DataFormatter.sanitize(dict(ref))
                for field in ("path", "label", "role", "artifact_type"):
                    value = ref.get(field)
                    if value not in (None, "", [], {}):
                        item[field] = DataFormatter.sanitize(value)
            if body:
                item["body"] = body
            error = readback.get("error")
            if error:
                item["diagnostics"] = [
                    {
                        "code": "taskboard.action_artifact_readback.failed",
                        "message": cls._truncate_prompt_text(error, 1200),
                    }
                ]
            items.append(DataFormatter.sanitize(item))
        return items

    @staticmethod
    def _taskboard_readback_evidence_id(*parts: str) -> str:
        raw = ":".join(str(part or "").strip() for part in parts if str(part or "").strip())
        return "".join(ch if ch.isalnum() or ch in "._:-/" else "_" for ch in raw)[:240]

    @classmethod
    def _taskboard_readback_evidence_body(cls, value: Any) -> str:
        if value in (None, "", [], {}):
            return ""
        if isinstance(value, str):
            return cls._truncate_prompt_text(value, _TASKBOARD_READBACK_PREVIEW_CHARS)
        try:
            text = json.dumps(DataFormatter.sanitize(value), ensure_ascii=False, sort_keys=True)
        except Exception:
            text = str(value)
        return cls._truncate_prompt_text(text, _TASKBOARD_READBACK_PREVIEW_CHARS)

    @classmethod
    def _taskboard_readback_evidence_aliases(cls, readback: Mapping[str, Any]) -> list[str]:
        aliases: list[str] = []

        def add(value: Any) -> None:
            text = str(value or "").strip()
            if text and text not in aliases:
                aliases.append(text)

        artifact_id = str(readback.get("artifact_id") or "").strip()
        action_call_id = str(readback.get("action_call_id") or "").strip()
        add(artifact_id)
        add(action_call_id)
        if action_call_id:
            add(f"action_result_{action_call_id}")
            add(f"action_{action_call_id}")
        ref = readback.get("ref")
        if isinstance(ref, Mapping):
            for field in ("path", "label", "artifact_type", "role"):
                add(ref.get(field))
        return aliases[:16]

    @classmethod
    def _compact_taskboard_action_artifact_readback(
        cls,
        readback: Any,
        ref: Mapping[str, Any],
        *,
        max_chars: int = _TASKBOARD_READBACK_PREVIEW_CHARS,
    ) -> dict[str, Any]:
        if not isinstance(readback, Mapping):
            readback = {
                "ok": False,
                "status": "invalid_result",
                "error": f"Action artifact reader returned { type(readback).__name__ }.",
            }
        artifact_id = str(readback.get("artifact_id") or ref.get("artifact_id") or "")
        action_call_id = str(readback.get("action_call_id") or ref.get("action_call_id") or "")
        value = readback.get("value", readback.get("data", readback.get("result")))
        original_chars = cls._serialized_prompt_chars(value)
        preview = cls._compact_taskboard_action_artifact_value_preview(value, max_chars=max_chars)
        preview_chars = cls._serialized_prompt_chars(preview)
        compact: dict[str, Any] = {
            "ok": bool(readback.get("ok")),
            "status": str(readback.get("status") or ""),
            "artifact_id": artifact_id,
            "action_call_id": action_call_id,
            "artifact_type": str(readback.get("artifact_type") or ref.get("artifact_type") or ""),
            "label": str(readback.get("label") or ref.get("label") or ""),
            "ref": cls._compact_artifact_ref_for_verifier(ref),
            "value_preview": preview,
            "value_preview_meta": {
                "truncated": preview_chars < original_chars,
            },
        }
        error = readback.get("error")
        if error:
            compact["error"] = cls._truncate_prompt_text(error, 1200)
        return compact

    @classmethod
    def _compact_taskboard_artifact_refs_for_hot_payload(cls, refs: Sequence[Any]) -> list[Any]:
        return [cls._compact_artifact_ref_for_verifier(ref) for ref in refs if isinstance(ref, Mapping)]

    @classmethod
    def _compact_taskboard_file_refs_for_hot_payload(cls, refs: Sequence[Any]) -> list[dict[str, Any]]:
        return [
            cls._compact_taskboard_workspace_ref_for_prompt(ref)
            for ref in refs
            if isinstance(ref, Mapping)
        ]

    @classmethod
    def _compact_taskboard_action_artifact_value_preview(
        cls,
        value: Any,
        *,
        max_chars: int,
    ) -> Any:
        preview = cls._compact_verifier_prompt_value(value, max_chars=max_chars)
        return cls._compact_taskboard_framework_refs_in_hot_value(preview)

    @classmethod
    def _compact_taskboard_framework_refs_in_hot_value(cls, value: Any) -> Any:
        if isinstance(value, Mapping):
            compact: dict[str, Any] = {}
            for key, item in value.items():
                key_text = str(key)
                if key_text in {"artifact_refs", "file_refs"}:
                    if isinstance(item, Sequence) and not isinstance(item, str | bytes | bytearray):
                        compact[key_text] = [
                            cls._compact_artifact_ref_for_verifier(ref)
                            for ref in item
                            if isinstance(ref, Mapping)
                        ]
                    continue
                if key_text in {"ref", "locator_ref"} and isinstance(item, Mapping):
                    compact[key_text] = cls._compact_artifact_ref_for_verifier(item)
                    continue
                compact[key_text] = cls._compact_taskboard_framework_refs_in_hot_value(item)
            return compact
        if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
            return [cls._compact_taskboard_framework_refs_in_hot_value(item) for item in value]
        return value

    @classmethod
    def _compact_taskboard_workspace_file_readback(
        cls,
        readback: Any,
        ref: Mapping[str, Any],
        *,
        max_chars: int = _TASKBOARD_READBACK_PREVIEW_CHARS,
    ) -> dict[str, Any]:
        if not isinstance(readback, Mapping):
            readback = {
                "ok": False,
                "readable": False,
                "status": "invalid_result",
                "error": f"Workspace file reader returned { type(readback).__name__ }.",
            }
        path = str(readback.get("path") or ref.get("path") or "")
        content = readback.get("content", readback.get("text", readback.get("value")))
        original_chars = cls._serialized_prompt_chars(content)
        preview = cls._compact_verifier_prompt_value(content, max_chars=max_chars)
        preview_chars = cls._serialized_prompt_chars(preview)
        ok = bool(readback.get("ok", readback.get("readable", False)))
        compact: dict[str, Any] = {
            "ok": ok,
            "status": str(readback.get("status") or ("completed" if ok else "error")),
            "path": path,
            "read_bytes": readback.get("read_bytes"),
            "offset": readback.get("offset"),
            "truncated": bool(readback.get("truncated")),
            "ref": cls._compact_taskboard_workspace_ref_for_prompt(ref),
            "content_preview": preview,
            "content_preview_meta": {
                "truncated": preview_chars < original_chars or bool(readback.get("truncated")),
                "original_chars": original_chars,
                "preview_chars": preview_chars,
                "limit_chars": max_chars,
            },
        }
        error = readback.get("error")
        if error:
            compact["error"] = cls._truncate_prompt_text(error, 1200)
        diagnostics = readback.get("diagnostics")
        if isinstance(diagnostics, Sequence) and not isinstance(diagnostics, str | bytes | bytearray):
            compact["diagnostics"] = cls._compact_verifier_prompt_value(list(diagnostics), max_chars=1200)
        return compact

    @classmethod
    def _taskboard_workspace_content_segment_readback(
        cls,
        segment: Any,
        ref: Mapping[str, Any],
    ) -> dict[str, Any]:
        if not isinstance(segment, Mapping):
            return {
                "ok": False,
                "readable": False,
                "status": "invalid_result",
                "path": str(ref.get("path") or ""),
                "error": f"Workspace bounded reader returned { type(segment).__name__ }.",
            }
        envelope = segment.get("ref")
        if not isinstance(envelope, Mapping):
            envelope = {}
        offset = cls._positive_int(segment.get("offset"), default=0)
        read_bytes = cls._positive_int(segment.get("size"), default=0)
        total_size = cls._positive_int(segment.get("total_size"), default=read_bytes)
        eof = bool(segment.get("eof", True))
        return {
            "ok": True,
            "readable": True,
            "status": "completed",
            "path": str(envelope.get("content_ref") or ref.get("path") or ""),
            "content": segment.get("content", ""),
            "media_type": str(segment.get("content_type") or ""),
            "bytes": total_size,
            "read_bytes": read_bytes,
            "sha256": str(segment.get("digest") or envelope.get("digest") or ""),
            "offset": offset,
            "truncated": (not eof) or offset > 0 or read_bytes < total_size,
        }

    @staticmethod
    def _compact_taskboard_workspace_ref_for_prompt(ref: Mapping[str, Any]) -> dict[str, Any]:
        keep_keys = (
            "path",
            "role",
            "label",
            "source",
            "record_id",
            "collection",
            "kind",
            "content_state",
            "readback_mode",
        )
        return {key: ref.get(key) for key in keep_keys if key in ref and ref.get(key) not in (None, "")}

    @staticmethod
    def _serialized_prompt_chars(value: Any) -> int:
        try:
            return len(json.dumps(DataFormatter.sanitize(value), ensure_ascii=False, default=str))
        except Exception:
            return len(str(value or ""))

    @classmethod
    def _taskboard_available_readback(cls, evidence_view: Mapping[str, Any]) -> dict[str, Any]:
        records = cls._taskboard_action_artifact_recall_records(evidence_view)
        refs = records[0]["artifact_refs"] if records else []
        file_refs = cls._taskboard_readback_file_refs(evidence_view)
        return {
            "schema_version": "agent_task_taskboard_readback/v1",
            "taskboard_readback_shape": {
                "available": bool(refs or file_refs),
                "allowed_execution_shape": "readback",
                "artifact_refs": [cls._compact_artifact_ref_for_verifier(ref) for ref in refs],
                "file_refs": [
                    cls._compact_taskboard_workspace_ref_for_prompt(ref)
                    for ref in file_refs
                    if isinstance(ref, Mapping)
                ],
            },
            "action_artifact_readback": {
                "available": bool(refs),
                "action_id": "read_action_artifact",
                "artifact_refs": [cls._compact_artifact_ref_for_verifier(ref) for ref in refs],
            },
            "workspace_file_readback": {
                "available": bool(file_refs),
                "file_refs": [
                    cls._compact_taskboard_workspace_ref_for_prompt(ref)
                    for ref in file_refs
                    if isinstance(ref, Mapping)
                ],
            },
            "policy": (
                "Use a TaskBoard readback card only when bounded previews are insufficient and the remaining "
                "work is scoped cold Action artifact or Workspace file readback. Mixed tool/readback work may "
                "still use the ActionRuntime read_action_artifact action or Workspace file actions."
            ),
        }


__all__ = ["AgentTaskTaskBoardReadbackMixin"]
