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

from agently.core.orchestration import TaskBoardValidator
from agently.types.data import TaskBoardPatch

from .TaskShared import *


class AgentTaskTaskBoardPatchingMixin(AgentTaskMixinBase):
    @classmethod
    def _taskboard_control_output_allows_workspace_delivery(cls, card_output: Any) -> bool:
        if not isinstance(card_output, Mapping):
            return True
        status = str(card_output.get("status") or "").strip().lower()
        if status in {"blocked", "failed", "skipped", "error", "timed_out"}:
            return False
        next_action = str(card_output.get("next_board_action") or "").strip().lower().replace("-", "_")
        if next_action in {"readback", "needs_readback", "repair", "patch", "block", "stop"}:
            return False
        if card_output.get("sufficient") is False:
            return False
        if cls._has_remaining_work(card_output.get("remaining_work")):
            return False
        if cls._has_remaining_work(card_output.get("gaps")):
            if not (status == "completed" and card_output.get("sufficient") is True):
                return False
        return True

    @classmethod
    def _taskboard_control_patch_proposal(
        cls,
        context: Any,
        card_output: Mapping[str, Any],
        diagnostics: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        raw_patch = card_output.get("patch_proposal")
        if isinstance(raw_patch, Mapping):
            if cls._taskboard_patch_proposal_is_workspace_patch(raw_patch):
                return None
            try:
                patch = TaskBoardPatch.from_value(raw_patch)
                revision = getattr(context, "revision", None)
                if revision is None:
                    raise ValueError("TaskBoard control patch validation requires a revision.")
                TaskBoardValidator().apply_patch(cast(TaskBoardRevision | Mapping[str, Any], revision), patch)
            except Exception as error:
                diagnostics.append(
                    {
                        "code": "taskboard.control.invalid_model_patch_proposal",
                        "message": _compact_agent_task_error_message(
                            error,
                            fallback="Model patch_proposal was not a valid TaskBoardPatch.",
                        ),
                        "card_id": str(getattr(getattr(context, "card", None), "id", "") or ""),
                        "requested_action": str(raw_patch.get("action") or ""),
                    }
                )
                if cls._taskboard_patch_proposal_requests_readback(raw_patch):
                    target_refs = cls._taskboard_patch_proposal_target_refs(raw_patch)
                    if not target_refs:
                        target_refs = cls._taskboard_control_output_target_refs(card_output)
                    auto_patch_input = dict(card_output)
                    auto_patch_input["next_board_action"] = "readback"
                    return cls._taskboard_control_auto_patch(
                        context,
                        auto_patch_input,
                        target_refs=target_refs,
                    )
                return None
            return DataFormatter.sanitize(patch.to_dict())
        return cls._taskboard_control_auto_patch(
            context,
            card_output,
            target_refs=cls._taskboard_control_output_target_refs(card_output),
        )

    async def _materialize_taskboard_workspace_patch(
        self,
        context: Any,
        card_output: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        raw_patch = card_output.get("patch_proposal")
        if not isinstance(raw_patch, Mapping) or not self._taskboard_patch_proposal_is_workspace_patch(raw_patch):
            return card_output
        patched_output = dict(card_output)
        patched_output["workspace_patch_proposal"] = DataFormatter.sanitize(raw_patch)
        patched_output.pop("patch_proposal", None)
        card_id = str(getattr(getattr(context, "card", None), "id", "") or "")
        delivery = await self._apply_taskboard_workspace_patch(raw_patch, card_id=card_id)
        patched_output["workspace_patch_delivery"] = DataFormatter.sanitize(delivery)
        diagnostics = [dict(item) for item in self._taskboard_mapping_sequence(patched_output.get("diagnostics"))]
        if delivery.get("status") == "completed":
            diagnostics.append(
                {
                    "code": "taskboard.control.workspace_patch_applied",
                    "card_id": card_id,
                    "path": delivery.get("path"),
                    "operation_count": delivery.get("operation_count", 0),
                    "replacement_count": delivery.get("replacement_count", 0),
                    "source": "agent_task.taskboard.workspace_patch",
                }
            )
            file_refs = [dict(item) for item in self._taskboard_mapping_sequence(patched_output.get("file_refs"))]
            file_refs.extend(dict(item) for item in self._taskboard_mapping_sequence(delivery.get("file_refs")))
            patched_output["file_refs"] = DataFormatter.sanitize(self._dedupe_ref_records(file_refs))
            status = str(patched_output.get("status") or "").strip().lower()
            if status not in {"completed", "skipped"}:
                patched_output["status"] = "completed"
            if not patched_output.get("sufficient"):
                patched_output["sufficient"] = True
        else:
            diagnostics.append(
                {
                    "code": "taskboard.control.workspace_patch_failed",
                    "card_id": card_id,
                    "path": delivery.get("path"),
                    "reason": delivery.get("reason") or delivery.get("error"),
                    "source": "agent_task.taskboard.workspace_patch",
                }
            )
            patched_output["status"] = "blocked"
            patched_output["sufficient"] = False
            remaining_work = self._normalize_string_list(patched_output.get("remaining_work"))
            reason = str(delivery.get("reason") or "Workspace patch could not be applied.").strip()
            if reason:
                remaining_work.append(reason)
            patched_output["remaining_work"] = remaining_work
        patched_output["diagnostics"] = DataFormatter.sanitize(diagnostics)
        self.diagnostics.setdefault("taskboard_workspace_patch_delivery", []).append(
            DataFormatter.sanitize(delivery)
        )
        return DataFormatter.sanitize(patched_output)

    @staticmethod
    def _taskboard_patch_proposal_is_workspace_patch(patch_proposal: Mapping[str, Any]) -> bool:
        if not isinstance(patch_proposal, Mapping):
            return False
        if any(str(patch_proposal.get(key) or "").strip() for key in ("file", "path", "target_file", "target_path")):
            return True
        raw_operations = patch_proposal.get("operations") or patch_proposal.get("edits")
        if not isinstance(raw_operations, Sequence) or isinstance(raw_operations, str | bytes | bytearray):
            return False
        workspace_ops = {"replace", "insert", "delete", "append", "write"}
        taskboard_ops = {
            "add_card",
            "update_card",
            "remove_card",
            "record_card_result",
            "append_diagnostic",
            "set_board_status",
            "update_metadata",
            "add_dependency",
            "remove_dependency",
        }
        has_workspace_op = False
        for operation in raw_operations:
            if not isinstance(operation, Mapping):
                continue
            op = str(operation.get("type") or operation.get("op") or operation.get("operation") or "").strip()
            if op in taskboard_ops:
                return False
            if op in workspace_ops:
                has_workspace_op = True
        return has_workspace_op

    @classmethod
    def _taskboard_workspace_patch_path(
        cls,
        patch_proposal: Mapping[str, Any],
        operation: Mapping[str, Any] | None = None,
    ) -> str:
        for source in (operation or {}, patch_proposal):
            for key in ("file", "path", "target_file", "target_path", "workspace_path"):
                value = str(source.get(key) or "").strip()
                if value:
                    return value
        return ""

    async def _apply_taskboard_workspace_patch(
        self,
        patch_proposal: Mapping[str, Any],
        *,
        card_id: str,
    ) -> dict[str, Any]:
        raw_operations = patch_proposal.get("operations") or patch_proposal.get("edits")
        if not isinstance(raw_operations, Sequence) or isinstance(raw_operations, str | bytes | bytearray):
            return {
                "status": "failed",
                "card_id": card_id,
                "reason": "Workspace patch requires an operations list.",
            }
        operations = [dict(item) for item in raw_operations if isinstance(item, Mapping)]
        if not operations:
            return {
                "status": "failed",
                "card_id": card_id,
                "reason": "Workspace patch has no valid operations.",
            }
        path = self._taskboard_workspace_patch_path(patch_proposal, operations[0])
        if not path:
            return {
                "status": "failed",
                "card_id": card_id,
                "reason": "Workspace patch requires file/path.",
            }
        try:
            content = await self._read_workspace_patch_text(path)
            operation_records: list[dict[str, Any]] = []
            replacement_count = 0
            for index, operation in enumerate(operations):
                operation_path = self._taskboard_workspace_patch_path(patch_proposal, operation)
                if operation_path != path:
                    raise ValueError("Workspace patch operations must target one file per patch proposal.")
                content, record = self._apply_taskboard_workspace_patch_operation(
                    content,
                    operation,
                    index=index,
                )
                replacement_count += int(record.get("replacement_count") or 0)
                operation_records.append(record)
            write_result = await self.workspace.write_file(path, content, append=False)
            read_result = await self.workspace.read_file(path, max_bytes=_WORKSPACE_ARTIFACT_PREVIEW_BYTES)
        except Exception as error:
            return {
                "status": "failed",
                "card_id": card_id,
                "path": path,
                "reason": _compact_agent_task_error_message(error, fallback=error.__class__.__name__),
                "error": {"type": error.__class__.__name__},
            }
        ref = {
            "path": str(read_result.get("path") or path),
            "bytes": int(read_result.get("bytes") or write_result.get("bytes") or 0),
            "sha256": str(read_result.get("sha256") or write_result.get("sha256") or ""),
            "media_type": read_result.get("media_type"),
            "content_kind": read_result.get("content_kind", "text"),
            "encoding": read_result.get("encoding"),
            "handler_id": read_result.get("handler_id"),
            "role": "workspace_artifact",
            "source": "agent_task.workspace_artifact.taskboard_patch",
            "card_id": card_id,
            "read_bytes": int(read_result.get("read_bytes") or 0),
            "truncated": bool(read_result.get("truncated")),
            "preview": self._truncate_prompt_text(str(read_result.get("content") or ""), _WORKSPACE_ARTIFACT_PREVIEW_BYTES),
        }
        return {
            "status": "completed",
            "card_id": card_id,
            "path": path,
            "operation_count": len(operation_records),
            "replacement_count": replacement_count,
            "operations": operation_records,
            "write": {
                "path": str(write_result.get("path") or path),
                "bytes": int(write_result.get("bytes") or 0),
                "sha256": str(write_result.get("sha256") or ""),
            },
            "readback": {
                "path": ref["path"],
                "bytes": ref["bytes"],
                "sha256": ref["sha256"],
                "read_bytes": ref["read_bytes"],
                "truncated": ref["truncated"],
                "handler_id": ref["handler_id"],
            },
            "file_refs": [ref],
        }

    async def _read_workspace_patch_text(self, path: str) -> str:
        target = self.workspace.resolve_file_path(path)
        max_bytes = max(int(target.stat().st_size) + 1, _WORKSPACE_ARTIFACT_PREVIEW_BYTES)
        read_result = await self.workspace.read_file(path, max_bytes=max_bytes)
        if not bool(read_result.get("ok")):
            raise ValueError(f"Workspace file could not be read for patch: { path }")
        if bool(read_result.get("truncated")):
            raise ValueError(f"Workspace patch requires complete readback before editing: { path }")
        content = read_result.get("content")
        if not isinstance(content, str):
            raise ValueError(f"Workspace patch requires text content: { path }")
        return content

    @classmethod
    def _apply_taskboard_workspace_patch_operation(
        cls,
        content: str,
        operation: Mapping[str, Any],
        *,
        index: int,
    ) -> tuple[str, dict[str, Any]]:
        op = str(operation.get("type") or operation.get("op") or operation.get("operation") or "").strip().lower()
        if op != "replace":
            raise ValueError(f"Unsupported Workspace patch operation '{ op or '<empty>' }'.")
        old = str(operation.get("old") or operation.get("from") or operation.get("search") or "")
        if not old:
            raise ValueError("Workspace replace patch requires non-empty old/from/search text.")
        if "new" in operation:
            new = str(operation.get("new") or "")
        elif "to" in operation:
            new = str(operation.get("to") or "")
        else:
            new = str(operation.get("replacement") or "")
        match_count = content.count(old)
        if match_count <= 0:
            raise ValueError("Workspace replace patch old text was not found.")
        replace_all = cls._normalize_bool(operation.get("replace_all"), default=False)
        occurrence = cls._coerce_positive_int(operation.get("occurrence"))
        if occurrence is not None:
            if occurrence > match_count:
                raise ValueError("Workspace replace patch occurrence is greater than match count.")
            patched = cls._replace_nth(content, old, new, occurrence)
            replacement_count = 1
        elif replace_all:
            patched = content.replace(old, new)
            replacement_count = match_count
        elif match_count == 1:
            patched = content.replace(old, new, 1)
            replacement_count = 1
        else:
            raise ValueError(
                "Workspace replace patch matched multiple locations; set occurrence or replace_all explicitly."
            )
        return patched, {
            "index": index,
            "type": "replace",
            "match_count": match_count,
            "replacement_count": replacement_count,
        }

    @staticmethod
    def _replace_nth(content: str, old: str, new: str, occurrence: int) -> str:
        start = -1
        search_from = 0
        for _ in range(occurrence):
            start = content.find(old, search_from)
            if start < 0:
                return content
            search_from = start + len(old)
        return content[:start] + new + content[start + len(old) :]

    @staticmethod
    def _taskboard_mapping_sequence(value: Any) -> list[Mapping[str, Any]]:
        if not isinstance(value, Sequence) or isinstance(value, str | bytes | bytearray):
            return []
        return [item for item in value if isinstance(item, Mapping)]

    @staticmethod
    def _coerce_positive_int(value: Any) -> int | None:
        if value is None or value == "":
            return None
        try:
            coerced = int(value)
        except (TypeError, ValueError):
            return None
        return coerced if coerced > 0 else None

    @staticmethod
    def _taskboard_patch_proposal_requests_readback(patch_proposal: Mapping[str, Any]) -> bool:
        action = str(
            patch_proposal.get("action")
            or patch_proposal.get("next_board_action")
            or patch_proposal.get("patch_type")
            or patch_proposal.get("type")
            or ""
        ).strip().lower()
        return action.replace("-", "_") in {
            "readback",
            "needs_readback",
            "cold_readback",
            "artifact_readback",
            "readback_required",
        }

    @classmethod
    def _taskboard_patch_proposal_target_refs(cls, patch_proposal: Mapping[str, Any]) -> list[str]:
        raw_refs = patch_proposal.get("target_refs") or patch_proposal.get("refs") or patch_proposal.get("urls")
        return cls._normalize_taskboard_target_refs(raw_refs)

    @classmethod
    def _taskboard_control_output_target_refs(cls, card_output: Mapping[str, Any]) -> list[str]:
        return cls._normalize_taskboard_target_refs(card_output.get("target_refs"))

    @staticmethod
    def _normalize_taskboard_target_refs(raw_refs: Any) -> list[str]:
        if not isinstance(raw_refs, Sequence) or isinstance(raw_refs, str | bytes | bytearray):
            return []
        refs: list[str] = []
        seen: set[str] = set()
        for item in raw_refs:
            text = ""
            if isinstance(item, Mapping):
                for key in ("target_ref", "url", "href", "uri", "path", "ref"):
                    value = str(item.get(key) or "").strip()
                    if value:
                        text = value
                        break
            else:
                text = str(item or "").strip()
            if not text or text in seen:
                continue
            seen.add(text)
            refs.append(text)
        return refs[:8]

    @staticmethod
    def _taskboard_target_ref_requires_action(ref: str) -> bool:
        text = str(ref or "").strip().lower()
        if not text:
            return False
        if text.startswith(("http://", "https://")):
            return True
        if "://" not in text:
            return False
        scheme = text.split("://", 1)[0].strip()
        return scheme not in {"workspace", "content"}

    @classmethod
    def _split_taskboard_target_refs(cls, refs: Sequence[str]) -> tuple[list[str], list[str]]:
        workspace_refs: list[str] = []
        action_refs: list[str] = []
        for ref in refs:
            text = str(ref or "").strip()
            if not text:
                continue
            if cls._taskboard_target_ref_requires_action(text):
                action_refs.append(text)
            else:
                workspace_refs.append(text)
        return workspace_refs, action_refs

    @staticmethod
    def _taskboard_workspace_target_ref_path(ref: str) -> str:
        text = str(ref or "").strip()
        lowered = text.lower()
        for prefix in ("workspace://", "content://"):
            if lowered.startswith(prefix):
                return text[len(prefix) :].lstrip("/")
        return text

    @classmethod
    def _taskboard_workspace_target_ref_file_refs(cls, refs: Sequence[str]) -> list[dict[str, Any]]:
        file_refs: list[dict[str, Any]] = []
        seen: set[str] = set()
        for ref in refs:
            if cls._taskboard_target_ref_requires_action(str(ref or "")):
                continue
            path = cls._taskboard_workspace_target_ref_path(str(ref or ""))
            if not path or path in seen:
                continue
            seen.add(path)
            file_refs.append(
                {
                    "path": path,
                    "source": "taskboard_target_ref",
                    "content_state": "ref_only",
                    "readback_mode": "workspace_content",
                }
            )
        return file_refs

    def _taskboard_scoped_retrieval_continuation_patch(
        self,
        context: Any,
        card_output: Mapping[str, Any],
        diagnostics: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        next_action = str(card_output.get("next_board_action") or "").strip().lower().replace("-", "_")
        if next_action:
            return None
        status = str(card_output.get("status") or "").strip().lower()
        structurally_insufficient = (
            status == "blocked"
            or card_output.get("sufficient") is False
            or self._has_remaining_work(card_output.get("remaining_work"))
        )
        if not structurally_insufficient:
            return None
        scoped_retrieval = self._taskboard_card_scoped_retrieval(getattr(context, "card", None))
        if not scoped_retrieval:
            return None
        expanded_scoped_retrieval = self._taskboard_expand_scoped_retrieval_for_continuation(scoped_retrieval)
        if not expanded_scoped_retrieval:
            return None
        patch_input = dict(card_output)
        patch_input["next_board_action"] = "readback"
        patch = self._taskboard_control_auto_patch(
            context,
            patch_input,
            scoped_retrieval=expanded_scoped_retrieval,
            source="agent_task.taskboard.scoped_retrieval_continuation",
            diagnostic_code="taskboard.scoped_retrieval.auto_continuation_patch",
            auto_patch_reason="blocked_scoped_retrieval_consumer_continuation",
        )
        if patch is None:
            return None
        diagnostics.append(
            {
                "code": "taskboard.scoped_retrieval.auto_continuation_patch",
                "message": (
                    "Converted a structurally insufficient TaskBoard scoped-retrieval card into "
                    "an evidence card plus continuation."
                ),
                "card_id": str(getattr(getattr(context, "card", None), "id", "") or ""),
                "query_group_count": len(expanded_scoped_retrieval.get("query_groups", [])),
            }
        )
        return patch

    @classmethod
    def _taskboard_expand_scoped_retrieval_for_continuation(
        cls,
        scoped_retrieval: Mapping[str, Any],
    ) -> dict[str, Any]:
        normalized = cls._normalize_scoped_retrieval_plan(scoped_retrieval)
        raw_groups = normalized.get("query_groups")
        if not isinstance(raw_groups, Sequence) or isinstance(raw_groups, str | bytes | bytearray):
            return {}
        bounded_defaults = scoped_retrieval_policy().get("bounded_defaults", {})
        default_snippet_limit = cls._positive_int(
            bounded_defaults.get("snippet_limit") if isinstance(bounded_defaults, Mapping) else None,
            default=1200,
        )
        expanded_groups: list[dict[str, Any]] = []
        for item in raw_groups:
            if not isinstance(item, Mapping):
                continue
            group = dict(item)
            current_limit = cls._positive_int(group.get("snippet_limit"), default=0)
            if current_limit <= 0:
                next_limit = default_snippet_limit
            elif current_limit < default_snippet_limit:
                next_limit = default_snippet_limit
            else:
                next_limit = min(current_limit * 2, 12000)
            group["snippet_limit"] = next_limit
            surface = str(group.get("search_surface") or "").strip()
            if surface in {"workspace_files", "workspace_index_and_files"} or group.get("path") or group.get("pattern"):
                current_context_lines = cls._positive_int(group.get("context_lines"), default=0)
                group["context_lines"] = max(current_context_lines, 3)
            expanded_groups.append(DataFormatter.sanitize(group))
            if len(expanded_groups) >= 8:
                break
        if not expanded_groups:
            return {}
        expanded = {"query_groups": expanded_groups}
        fallback_order = normalized.get("fallback_order")
        if isinstance(fallback_order, Sequence) and not isinstance(fallback_order, str | bytes | bytearray):
            expanded["fallback_order"] = DataFormatter.sanitize(list(fallback_order))
        return expanded

    @staticmethod
    def _positive_int(value: Any, *, default: int) -> int:
        try:
            number = int(value)
        except (TypeError, ValueError):
            return default
        return max(0, number)

    @classmethod
    def _taskboard_control_auto_patch(
        cls,
        context: Any,
        card_output: Mapping[str, Any],
        *,
        target_refs: Sequence[str] | None = None,
        scoped_retrieval: Mapping[str, Any] | None = None,
        source: str | None = None,
        diagnostic_code: str = "taskboard.control.auto_readback_patch",
        auto_patch_reason: str = "next_board_action=readback",
    ) -> dict[str, Any] | None:
        next_action = str(card_output.get("next_board_action") or "").strip().lower().replace("-", "_")
        scoped_retrieval_plan = cls._normalize_scoped_retrieval_plan(scoped_retrieval)
        if next_action not in {"readback", "needs_readback"} and not scoped_retrieval_plan:
            return None
        target_ref_list = [str(ref).strip() for ref in list(target_refs or ()) if str(ref).strip()]
        revision = getattr(context, "revision", None)
        card = getattr(context, "card", None)
        if revision is None or card is None:
            return None
        graph = getattr(revision, "graph", None)
        if graph is None or not hasattr(graph, "card_by_id"):
            return None
        existing_ids = set(graph.card_by_id())
        current_id = str(getattr(card, "id", "") or "").strip()
        if not current_id:
            return None
        workspace_target_refs, action_target_refs = cls._split_taskboard_target_refs(target_ref_list)
        support_card_requires_action = bool(scoped_retrieval_plan or action_target_refs)

        def safe_id(raw: str) -> str:
            text = "".join(ch if ch.isalnum() or ch in {"_", ".", "-"} else "-" for ch in raw.strip())
            text = text.strip(".-")
            return text or "card"

        def unique_id(prefix: str) -> str:
            base = safe_id(prefix)[:80]
            candidate = base
            index = 1
            while candidate in existing_ids:
                index += 1
                candidate = f"{base}-{index}"
            existing_ids.add(candidate)
            return candidate

        continuation_id = unique_id(f"{current_id}.continue")
        current_card = dict(card.to_dict() if hasattr(card, "to_dict") else {})
        if not current_card:
            return None
        current_metadata = dict(current_card.get("metadata") or {})
        if (
            str(current_metadata.get("generated_by") or "")
            in {
                "agent_task.taskboard.control_auto_readback",
                "agent_task.taskboard.control_auto_target_refs",
                "agent_task.taskboard.scoped_retrieval_continuation",
            }
            and (
                str(current_metadata.get("readback_card_id") or "").strip()
                or str(current_metadata.get("evidence_card_id") or "").strip()
            )
        ):
            return None
        patch_source = source or (
            "agent_task.taskboard.control_auto_target_refs"
            if target_ref_list
            else "agent_task.taskboard.control_auto_readback"
        )
        evidence_card_id = (
            unique_id(f"{current_id}.evidence")
            if support_card_requires_action
            else unique_id(f"{current_id}.readback")
        )
        current_metadata.update(
            {
                "superseded_by": continuation_id,
                "auto_patch_reason": auto_patch_reason,
            }
        )
        current_card.update(
            {
                "failure_policy": "degradable",
                "status": "blocked",
                "metadata": current_metadata,
            }
        )
        dependencies = list(getattr(card, "depends_on", ()) or [])
        readback_dependencies = cls._taskboard_auto_readback_scope(card, graph)
        gaps = cls._normalize_string_list(card_output.get("gaps"))
        remaining_work = cls._normalize_string_list(card_output.get("remaining_work"))
        if scoped_retrieval_plan:
            readback_objective = "Run expanded bounded scoped retrieval before continuing the blocked card."
            if target_ref_list:
                readback_objective = (
                    f"{readback_objective} Also collect explicit target refs: {'; '.join(target_ref_list)}"
                )
        elif action_target_refs:
            readback_objective = (
                "Collect scoped evidence from the explicit external target refs required before continuing the "
                f"blocked control card. Target refs: {'; '.join(action_target_refs)}"
            )
        elif workspace_target_refs:
            readback_objective = (
                "Read bounded Workspace target refs required before continuing the blocked control card. "
                f"Target refs: {'; '.join(workspace_target_refs)}"
            )
        else:
            readback_objective = "Read scoped cold evidence required before continuing the blocked control card."
        if gaps:
            readback_objective = f"{readback_objective} Gaps: {'; '.join(gaps[:3])}"
        continuation_objective = str(getattr(card, "objective", "") or "Continue the blocked TaskBoard card.").strip()
        if remaining_work:
            continuation_objective = f"{continuation_objective} Remaining work: {'; '.join(remaining_work[:3])}"
        final_workspace_deliverables = cls._normalize_string_list(
            current_metadata.get("final_workspace_deliverables")
        )
        if final_workspace_deliverables:
            continuation_objective = (
                f"{continuation_objective} Materialize required Workspace final deliverable path(s): "
                f"{'; '.join(final_workspace_deliverables)}"
            )
        evidence_metadata = {
            "evidence_scope": readback_dependencies,
            "generated_by": patch_source,
            "source_card_id": current_id,
        }
        if target_ref_list:
            evidence_metadata["target_refs"] = target_ref_list
        if workspace_target_refs:
            evidence_metadata["workspace_target_refs"] = workspace_target_refs
        if action_target_refs:
            evidence_metadata["external_target_refs"] = action_target_refs
        if scoped_retrieval_plan:
            evidence_metadata["scoped_retrieval"] = DataFormatter.sanitize(scoped_retrieval_plan)
            evidence_metadata["retrieval_policy"] = scoped_retrieval_policy()
        continuation_metadata = {
            "generated_by": patch_source,
            "continues_card_id": current_id,
            "readback_card_id": evidence_card_id,
            "evidence_card_id": evidence_card_id if support_card_requires_action else "",
        }
        if final_workspace_deliverables:
            continuation_metadata["final_workspace_deliverables"] = final_workspace_deliverables
        evidence_card = {
            "id": evidence_card_id,
            "objective": readback_objective,
            "depends_on": readback_dependencies,
            "required_outputs": (
                ["Expanded bounded scoped retrieval evidence or diagnostics explaining why it remains insufficient."]
                if scoped_retrieval_plan
                else
                ["Evidence gathered from external target refs or diagnostics explaining inaccessible refs."]
                if action_target_refs
                else
                ["Bounded Workspace target-ref readback previews or diagnostics explaining inaccessible refs."]
                if workspace_target_refs
                else ["Bounded readback previews for verifier-visible cold evidence."]
            ),
            "allowed_execution_shape": "actions" if support_card_requires_action else "readback",
            "failure_policy": "required",
            "metadata": evidence_metadata,
        }
        patch = {
            "base_revision": str(getattr(revision, "revision_id", "") or ""),
            "source": patch_source,
            "operations": [
                {"op": "update_card", "card": current_card},
                {
                    "op": "add_card",
                    "card": evidence_card,
                },
                {
                    "op": "add_card",
                    "card": {
                        "id": continuation_id,
                        "objective": continuation_objective,
                        "depends_on": [*dependencies, evidence_card_id],
                        "required_outputs": list(getattr(card, "required_outputs", ()) or ()),
                        "allowed_execution_shape": str(getattr(card, "allowed_execution_shape", "") or "control"),
                        "failure_policy": str(getattr(card, "failure_policy", "") or "required"),
                        "metadata": continuation_metadata,
                    },
                },
                {
                    "op": "append_diagnostic",
                    "diagnostic": {
                        "code": "taskboard.control.auto_readback_patch",
                        "source_code": diagnostic_code,
                        "card_id": current_id,
                        "readback_card_id": evidence_card_id,
                        "continuation_card_id": continuation_id,
                        "target_ref_count": len(target_ref_list),
                        "workspace_target_ref_count": len(workspace_target_refs),
                        "external_target_ref_count": len(action_target_refs),
                        "scoped_retrieval": bool(scoped_retrieval_plan),
                    },
                },
            ],
            "diagnostics": [
                {
                    "code": diagnostic_code,
                    "card_id": current_id,
                    "readback_card_id": evidence_card_id,
                    "continuation_card_id": continuation_id,
                    "target_ref_count": len(target_ref_list),
                    "workspace_target_ref_count": len(workspace_target_refs),
                    "external_target_ref_count": len(action_target_refs),
                    "scoped_retrieval": bool(scoped_retrieval_plan),
                }
            ],
        }
        return DataFormatter.sanitize(patch)

    @classmethod
    def _taskboard_auto_readback_scope(cls, card: Any, graph: Any) -> list[str]:
        """Scope auto-readback to direct dependencies plus their upstream evidence."""

        if graph is None or not hasattr(graph, "card_by_id"):
            return list(getattr(card, "depends_on", ()) or [])
        card_by_id = graph.card_by_id()
        ordered: list[str] = []
        seen: set[str] = set()

        def add(card_id: str) -> None:
            if not card_id or card_id in seen:
                return
            seen.add(card_id)
            ordered.append(card_id)

        def visit(card_id: str) -> None:
            add(card_id)
            dependency_card = card_by_id.get(card_id)
            if dependency_card is None:
                return
            for dependency_id in getattr(dependency_card, "depends_on", ()) or ():
                visit(str(dependency_id))

        for dependency_id in getattr(card, "depends_on", ()) or ():
            visit(str(dependency_id))
        return ordered

    def _taskboard_final_verification_repair_revision(
        self,
        revision: Any,
        *,
        final: Mapping[str, Any],
        final_verification: Mapping[str, Any],
    ) -> TaskBoardRevision | None:
        from agently.core.orchestration.TaskBoard import apply_task_board_patch

        effective_revision = TaskBoardRevision.from_value(revision)
        existing_ids = set(effective_revision.graph.card_by_id())
        completed_dependencies = [
            str(card_id)
            for card_id, result in effective_revision.card_results.items()
            if str(getattr(result, "status", "")).strip().lower() == "completed"
        ]
        if not completed_dependencies:
            return None
        gaps = [
            *self._normalize_string_list(final_verification.get("missing_criteria")),
            *self._normalize_string_list(final_verification.get("next_step_requirements")),
            *self._normalize_string_list(final_verification.get("acceptance_delta")),
        ]
        if not gaps:
            reason = str(final_verification.get("reason") or final.get("reason") or "").strip()
            if reason:
                gaps.append(reason)
        if not gaps:
            return None

        required_deliverables = self._required_workspace_deliverables()

        def safe_id(raw: str) -> str:
            text = "".join(ch if ch.isalnum() or ch in {"_", ".", "-"} else "-" for ch in raw.strip())
            text = text.strip(".-")
            return text or "card"

        def unique_id(prefix: str) -> str:
            base = safe_id(prefix)[:80]
            candidate = base
            index = 1
            while candidate in existing_ids:
                index += 1
                candidate = f"{base}-{index}"
            existing_ids.add(candidate)
            return candidate

        repair_id = unique_id("final-verification-repair")
        gap_text = "; ".join(gaps[:6])
        required_outputs = [
            "Corrected final deliverable that resolves final verification gaps using existing evidence.",
        ]
        if required_deliverables:
            required_outputs.append(
                "Trusted Workspace final deliverable path(s): " + ", ".join(required_deliverables)
            )
        repair_card = {
            "id": repair_id,
            "objective": (
                "Repair the final TaskBoard deliverable using existing completed-card evidence and final "
                f"verification feedback. Address these gaps: {gap_text}. Produce a complete corrected "
                "deliverable; preserve verifier-visible source refs; remove, qualify, or replace unsupported "
                "facts instead of inventing evidence."
            ),
            "depends_on": completed_dependencies,
            "required_outputs": required_outputs,
            "allowed_execution_shape": "control",
            "failure_policy": "required",
            "evidence_contract": {
                "kind": "taskboard_final_verification_repair",
                "missing_criteria": self._normalize_string_list(final_verification.get("missing_criteria")),
                "next_step_requirements": self._normalize_string_list(final_verification.get("next_step_requirements")),
                "acceptance_delta": self._normalize_string_list(final_verification.get("acceptance_delta")),
                "reason": str(final_verification.get("reason") or ""),
            },
            "metadata": {
                "generated_by": "agent_task.taskboard.final_verification_repair",
                "repair_source": "final_verification",
                "previous_revision_id": effective_revision.revision_id,
                "final_workspace_deliverables": required_deliverables,
            },
        }
        diagnostic = {
            "code": "taskboard.final_verification.repair_patch",
            "repair_card_id": repair_id,
            "depends_on": completed_dependencies,
            "missing_criteria": self._normalize_string_list(final_verification.get("missing_criteria")),
            "reason": str(final_verification.get("reason") or ""),
        }
        patch = {
            "base_revision": effective_revision.revision_id,
            "source": "agent_task.taskboard.final_verification_repair",
            "operations": [
                {"op": "add_card", "card": repair_card},
                {"op": "append_diagnostic", "diagnostic": diagnostic},
                {"op": "set_board_status", "status": "running"},
            ],
            "diagnostics": [diagnostic],
        }
        try:
            repaired_revision = apply_task_board_patch(effective_revision, patch)
        except Exception as error:
            self.diagnostics.setdefault("taskboard_final_repair_patch_errors", []).append(
                {
                    "type": error.__class__.__name__,
                    "message": _compact_agent_task_error_message(error, fallback=error.__class__.__name__),
                    "repair_card_id": repair_id,
                    "revision_id": effective_revision.revision_id,
                }
            )
            return None
        self.diagnostics.setdefault("taskboard_final_repair_patches", []).append(
            {
                "repair_card_id": repair_id,
                "previous_revision_id": effective_revision.revision_id,
                "revision_id": repaired_revision.revision_id,
                "missing_criteria": diagnostic["missing_criteria"],
            }
        )
        return repaired_revision


__all__ = ["AgentTaskTaskBoardPatchingMixin"]
