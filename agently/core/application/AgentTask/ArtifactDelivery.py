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

import html
import re

from .AcceptanceLocator import build_workspace_artifact_acceptance_locator_items, collect_acceptance_points
from .TaskShared import *

_WORKSPACE_ARTIFACT_LOCATOR_SCAN_BYTES = 5_000_000


_PUBLIC_DELTA_RETRY_MARKER_RE = re.compile(r"\A<\$retry(?::(?P<label>[^>]*)?)?>(?P<body>.*?)</\$retry>\Z", re.DOTALL)


class AgentTaskArtifactMixin(AgentTaskMixinBase):
    @staticmethod
    def _workspace_artifact_manifest_path(manifest: Mapping[str, Any] | None) -> str:
        if isinstance(manifest, Mapping):
            for key in ("path", "output_path", "file_path"):
                value = str(manifest.get(key) or "").strip()
                if value:
                    return value
            deliverables = manifest.get("deliverables")
            if isinstance(deliverables, Sequence) and not isinstance(deliverables, str | bytes | bytearray):
                for item in deliverables:
                    if isinstance(item, Mapping):
                        value = str(item.get("path") or item.get("output_path") or "").strip()
                        if value:
                            return value
        return "final.md"

    @classmethod
    def _workspace_artifact_manifest_content(cls, manifest: Mapping[str, Any] | None) -> str:
        if not isinstance(manifest, Mapping):
            return ""
        for key in ("content", "markdown", "body", "text"):
            value = manifest.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        sections = manifest.get("sections")
        if not isinstance(sections, Sequence) or isinstance(sections, str | bytes | bytearray):
            return ""
        chunks: list[str] = []
        for section in sections:
            if isinstance(section, str):
                text = section.strip()
                if text and cls._manifest_section_string_is_body(text):
                    chunks.append(text)
                continue
            if not isinstance(section, Mapping):
                continue
            title = str(section.get("title") or section.get("name") or "").strip()
            body = ""
            for key in ("content", "markdown", "body", "text"):
                value = section.get(key)
                if isinstance(value, str) and value.strip():
                    body = value.strip()
                    break
            if not body:
                continue
            if title and not body.lstrip().startswith("#"):
                chunks.append(f"## {title}\n\n{body}")
            else:
                chunks.append(body)
        return "\n\n".join(chunks).strip()

    @staticmethod
    def _manifest_section_string_is_body(text: str) -> bool:
        """Treat short section-name strings as outlines, not artifact bodies."""

        stripped = text.strip()
        return bool("\n" in stripped or len(stripped) > 120 or stripped.startswith("#"))

    @classmethod
    def _workspace_artifact_manifest_needs_body(cls, manifest: Mapping[str, Any] | None) -> bool:
        if not isinstance(manifest, Mapping):
            return False
        if cls._workspace_artifact_manifest_content(manifest).strip():
            return False
        sections = manifest.get("sections")
        if isinstance(sections, Sequence) and not isinstance(sections, str | bytes | bytearray):
            return bool(sections)
        section_outline = manifest.get("section_outline")
        if isinstance(section_outline, Sequence) and not isinstance(section_outline, str | bytes | bytearray):
            return bool(section_outline)
        deliverables = manifest.get("deliverables")
        if isinstance(deliverables, Sequence) and not isinstance(deliverables, str | bytes | bytearray):
            return bool(deliverables)
        return False

    @staticmethod
    def _workspace_artifact_manifest_has_draftable_outline(manifest: Mapping[str, Any] | None) -> bool:
        if not isinstance(manifest, Mapping):
            return False
        for key in ("sections", "section_outline"):
            sections = manifest.get(key)
            if not isinstance(sections, Sequence) or isinstance(sections, str | bytes | bytearray):
                continue
            for section in sections:
                if isinstance(section, str) and section.strip():
                    return True
                if isinstance(section, Mapping):
                    for field in ("title", "summary", "intent", "description", "outline"):
                        value = section.get(field)
                        if isinstance(value, str) and value.strip():
                            return True
        return False

    @staticmethod
    def _workspace_artifact_retry_boundary_from_status(path: str, value: Any) -> dict[str, Any] | None:
        if not (
            (path == "$status" or path.endswith(".$status"))
            and isinstance(value, Mapping)
            and value.get("status") == "failed"
            and value.get("retry") is True
        ):
            return None
        return {
            "status": "retrying",
            "attempt_index": value.get("attempt_index"),
            "next_attempt_index": value.get("next_attempt_index"),
            "reason": str(value.get("reason") or "").strip(),
            "source": "structured_status",
        }

    @staticmethod
    def _workspace_artifact_public_delta_replay_marker(value: Any) -> dict[str, Any] | None:
        text = str(value or "")
        marker = _PUBLIC_DELTA_RETRY_MARKER_RE.match(text)
        if marker is None:
            return None
        reason = html.unescape(str(marker.group("body") or marker.group("label") or "")).strip()
        return {
            "marker": "retry",
            "reason": reason or "Retrying model request.",
            "source": "delta_replay_marker",
        }

    @staticmethod
    def _workspace_artifact_untrusted_refs(result: Mapping[str, Any], manifest: Mapping[str, Any] | None) -> list[Any]:
        refs: list[Any] = []
        raw_refs = result.get("file_refs")
        if isinstance(raw_refs, Sequence) and not isinstance(raw_refs, str | bytes | bytearray):
            refs.extend(raw_refs)
        if isinstance(manifest, Mapping):
            manifest_refs = manifest.get("file_refs")
            if isinstance(manifest_refs, Sequence) and not isinstance(manifest_refs, str | bytes | bytearray):
                refs.extend(manifest_refs)
        return refs

    @classmethod
    def _compact_workspace_artifact_manifest_for_hot_path(
        cls,
        manifest: Mapping[str, Any] | None,
        *,
        trusted_refs: list[dict[str, Any]],
        source: str,
    ) -> dict[str, Any]:
        compact = dict(manifest or {})
        for key in _WORKSPACE_ARTIFACT_CONTENT_KEYS:
            value = compact.pop(key, None)
            if isinstance(value, str) and value:
                compact.setdefault("omitted_content", []).append(
                    {
                        "field": key,
                        "chars": len(value),
                        "reason": "workspace_artifact_hot_path",
                    }
                )
        sections = compact.get("sections")
        if isinstance(sections, Sequence) and not isinstance(sections, str | bytes | bytearray):
            compact_sections: list[Any] = []
            for index, section in enumerate(sections):
                if isinstance(section, str):
                    compact_sections.append(
                        {
                            "index": index,
                            "content_omitted": True,
                            "chars": len(section),
                            "reason": "workspace_artifact_hot_path",
                        }
                    )
                    continue
                if not isinstance(section, Mapping):
                    compact_sections.append(DataFormatter.sanitize(section))
                    continue
                section_compact = dict(section)
                for key in _WORKSPACE_ARTIFACT_CONTENT_KEYS:
                    value = section_compact.pop(key, None)
                    if isinstance(value, str) and value:
                        section_compact.setdefault("omitted_content", []).append(
                            {
                                "field": key,
                                "chars": len(value),
                                "reason": "workspace_artifact_hot_path",
                            }
                        )
                compact_sections.append(section_compact)
            compact["sections"] = compact_sections
        if trusted_refs:
            ref = trusted_refs[0]
            compact.update(
                {
                    "path": ref.get("path"),
                    "bytes": ref.get("bytes"),
                    "sha256": ref.get("sha256"),
                    "file_refs": trusted_refs,
                    "source": source,
                }
            )
        return DataFormatter.sanitize(compact)

    @classmethod
    def _compact_workspace_artifact_result_for_hot_path(
        cls,
        result: dict[str, Any],
        *,
        content_key: str,
        content: str,
        trusted_refs: list[dict[str, Any]],
    ) -> dict[str, Any]:
        if not trusted_refs:
            return result
        ref = trusted_refs[0]
        path = str(ref.get("path") or "")
        replacement = f"Workspace artifact delivered at {path}; full content is available through file_refs/readback."
        omitted: list[dict[str, Any]] = []
        for key in _WORKSPACE_ARTIFACT_RESULT_BODY_KEYS:
            value = result.get(key)
            if isinstance(value, str) and value:
                result[key] = replacement
                omitted.append(
                    {
                        "field": key,
                        "chars": len(value),
                        "reason": "workspace_artifact_hot_path",
                    }
                )
        if content and content_key and content_key not in {item["field"] for item in omitted}:
            if cls._replace_workspace_artifact_nested_content(result, content_key, replacement):
                omitted.append(
                    {
                        "field": content_key,
                        "chars": len(content),
                        "reason": "workspace_artifact_hot_path",
                    }
                )
            else:
                omitted.append(
                    {
                        "field": content_key,
                        "chars": len(content),
                        "reason": "workspace_artifact_hot_path",
                    }
                )
        if omitted:
            result["workspace_artifact_content_omitted"] = omitted
        preview = str(ref.get("preview") or "")
        if preview:
            result["artifact_preview"] = preview
            result["artifact_preview_truncated"] = bool(ref.get("truncated"))
        return result

    @staticmethod
    def _replace_workspace_artifact_nested_content(result: dict[str, Any], content_key: str, replacement: str) -> bool:
        marker = re.match(r"\Aevidence\[(?P<index>\d+)\](?:\.(?P<field>[A-Za-z_][A-Za-z0-9_]*))?\Z", content_key)
        if marker is None:
            return False
        evidence = result.get("evidence")
        if not isinstance(evidence, list):
            return False
        index = int(marker.group("index"))
        if index < 0 or index >= len(evidence):
            return False
        field = marker.group("field")
        if field is None:
            if not isinstance(evidence[index], str):
                return False
            evidence[index] = replacement
            return True
        item = evidence[index]
        if not isinstance(item, dict):
            return False
        if not isinstance(item.get(field), str):
            return False
        item[field] = replacement
        return True

    @classmethod
    def _handoff_workspace_artifact_remaining_work_to_verifier(
        cls,
        result: dict[str, Any],
        *,
        diagnostics: list[Any],
        path: str,
        source: str,
        content_key: str,
    ) -> dict[str, Any] | None:
        remaining_work = cls._normalize_string_list(result.get("remaining_work"))
        if not remaining_work:
            return None
        handoff = {
            "status": "handed_to_terminal_verification",
            "path": path,
            "content_key": content_key,
            "remaining_work": remaining_work,
            "reason": (
                "Trusted Workspace write/readback materialized the candidate artifact; "
                "terminal verification should judge remaining sufficiency."
            ),
        }
        result["remaining_work"] = []
        result["ready_for_final_verification"] = True
        result["workspace_artifact_remaining_work_handoff"] = DataFormatter.sanitize(handoff)
        diagnostics.append(
            {
                "code": "agent_task.workspace_artifact.remaining_work_handed_to_verifier",
                "message": (
                    "Workspace artifact content was written and read back while the work unit still reported "
                    "remaining work; the stale work-unit continuation was handed to terminal verification."
                ),
                "path": path,
                "source": source,
                "content_key": content_key,
                "remaining_work": remaining_work,
            }
        )
        return handoff

    @staticmethod
    def _workspace_artifact_content_is_complete_body(content: str) -> bool:
        text = content.strip()
        if not text:
            return False
        if text.startswith("#"):
            return True
        lowered = text[:128].lower()
        if lowered.startswith("<!doctype") or lowered.startswith("<html"):
            return True
        return bool("\n\n" in text and len(text) > 800)

    @classmethod
    def _workspace_artifact_evidence_content_candidates(
        cls,
        result: Mapping[str, Any],
        *,
        manifest_path: str,
    ) -> list[tuple[str, str]]:
        evidence = result.get("evidence")
        if not isinstance(evidence, Sequence) or isinstance(evidence, str | bytes | bytearray):
            return []
        candidates: list[tuple[str, str]] = []
        for index, item in enumerate(evidence):
            if isinstance(item, str):
                body = cls._workspace_artifact_body_from_evidence_text(item, manifest_path=manifest_path)
                if body:
                    candidates.append((f"evidence[{index}]", body))
                continue
            if not isinstance(item, Mapping):
                continue
            item_path = str(item.get("path") or item.get("artifact_path") or item.get("output_path") or "").strip()
            item_kind = str(item.get("kind") or item.get("type") or item.get("role") or "").strip().lower()
            item_declares_artifact = bool(
                item_path == manifest_path
                or "artifact" in item_kind
                or "deliverable" in item_kind
                or item.get("is_artifact_body") is True
            )
            for key in (*_WORKSPACE_ARTIFACT_RESULT_BODY_KEYS, *_WORKSPACE_ARTIFACT_CONTENT_KEYS):
                value = item.get(key)
                if not isinstance(value, str) or not value.strip():
                    continue
                key_declares_artifact = key.startswith("artifact_") or key in {
                    "candidate_final_result",
                    "final_result",
                }
                body = cls._workspace_artifact_body_from_evidence_text(
                    value,
                    manifest_path=manifest_path,
                    allow_bare_markdown=item_declares_artifact or key_declares_artifact,
                )
                if body:
                    candidates.append((f"evidence[{index}].{key}", body))
        return candidates

    @staticmethod
    def _workspace_artifact_body_from_evidence_text(
        value: str,
        *,
        manifest_path: str,
        allow_bare_markdown: bool = False,
    ) -> str:
        text = value.strip()
        if not text:
            return ""
        if allow_bare_markdown and text.startswith("#"):
            return text
        lines = text.splitlines()
        first_index = next((index for index, line in enumerate(lines) if line.strip()), None)
        if first_index is None:
            return ""
        first_line = lines[first_index].strip()
        if not first_line.endswith(":"):
            return ""
        label = first_line[:-1].strip().lower()
        path = manifest_path.strip().lower()
        path_name = Path(path).name.lower() if path else ""
        label_declares_artifact = any(token in label for token in ("artifact", "deliverable", "markdown", "body"))
        if path and path in label:
            label_declares_artifact = True
        if path_name and path_name in label:
            label_declares_artifact = True
        if not label_declares_artifact:
            return ""
        body = "\n".join(lines[first_index + 1 :]).strip()
        if not body:
            return ""
        if body.startswith("#"):
            return body
        return ""

    @classmethod
    def _workspace_artifact_delivery_mode(cls, result: Any) -> str:
        if not isinstance(result, Mapping):
            return ""
        manifest = result.get("artifact_manifest")
        if isinstance(manifest, Mapping):
            return "sectioned_workspace_artifact"
        for key in ("artifact_markdown", "artifact_html", "candidate_final_result", "final_result"):
            value = result.get(key)
            if isinstance(value, str) and value.strip():
                return "workspace_artifact"
        return ""

    @staticmethod
    def _taskboard_context_card_is_leaf(context: Any) -> bool:
        card = getattr(context, "card", None)
        card_id = str(getattr(card, "id", "") or "").strip()
        if not card_id:
            return False
        revision = getattr(context, "revision", None)
        graph = getattr(revision, "graph", None)
        cards = list(getattr(graph, "cards", []) or [])
        if not cards:
            return True
        depended_on: set[str] = set()
        for item in cards:
            depended_on.update(str(dep_id) for dep_id in getattr(item, "depends_on", ()) or ())
        return card_id not in depended_on

    def _prepare_taskboard_workspace_artifact_delivery(
        self,
        card_output: Any,
        context: Any,
        *,
        deliverable_mode: str | None,
        prefer_stream_draft: bool = False,
    ) -> tuple[Any, dict[str, Any]]:
        plan: dict[str, Any] = {"deliverable_mode": str(deliverable_mode or "").strip()}
        if prefer_stream_draft:
            plan["prefer_stream_draft"] = True
        if not plan["deliverable_mode"] or not isinstance(card_output, Mapping):
            return card_output, plan
        required_paths = {str(path or "").strip() for path in self._required_workspace_deliverables()}
        final_card_paths = [
            path for path in self._taskboard_context_final_workspace_deliverables(context) if path in required_paths
        ]
        if final_card_paths:
            manifest = card_output.get("artifact_manifest")
            manifest_dict = dict(manifest) if isinstance(manifest, Mapping) else {}
            requested_path = self._workspace_artifact_manifest_path(manifest_dict)
            if requested_path in final_card_paths:
                return card_output, plan
            manifest_dict["path"] = final_card_paths[0]
            result = dict(card_output)
            result["artifact_manifest"] = manifest_dict
            diagnostics: list[Any] = []
            raw_diagnostics = result.get("diagnostics")
            if isinstance(raw_diagnostics, Sequence) and not isinstance(raw_diagnostics, str | bytes | bytearray):
                diagnostics.extend(raw_diagnostics)
            elif raw_diagnostics:
                diagnostics.append(raw_diagnostics)
            diagnostics.append(
                {
                    "code": "taskboard.workspace_artifact.final_path_authorized",
                    "message": "A framework-marked final TaskBoard card is authorized to write the required deliverable path.",
                    "requested_path": requested_path,
                    "final_path": final_card_paths[0],
                }
            )
            result["diagnostics"] = DataFormatter.sanitize(diagnostics)
            return result, plan
        if not required_paths or self._taskboard_context_card_is_leaf(context):
            return card_output, plan

        manifest = card_output.get("artifact_manifest")
        manifest_dict = dict(manifest) if isinstance(manifest, Mapping) else {}
        requested_path = self._workspace_artifact_manifest_path(manifest_dict)
        if requested_path not in required_paths:
            return card_output, plan

        card = getattr(context, "card", None)
        card_id = str(getattr(card, "id", "") or "card").strip() or "card"
        safe_card_id = "".join(ch if ch.isalnum() or ch in {"-", "_", "."} else "-" for ch in card_id) or "card"
        file_name = Path(requested_path).name or "artifact.md"
        relocated_path = f"working/taskboard/{safe_card_id}/{file_name}"
        manifest_dict["path"] = relocated_path

        result = dict(card_output)
        result["artifact_manifest"] = manifest_dict
        diagnostics: list[Any] = []
        raw_diagnostics = result.get("diagnostics")
        if isinstance(raw_diagnostics, Sequence) and not isinstance(raw_diagnostics, str | bytes | bytearray):
            diagnostics.extend(raw_diagnostics)
        elif raw_diagnostics:
            diagnostics.append(raw_diagnostics)
        diagnostics.append(
            {
                "code": "taskboard.workspace_artifact.final_path_relocated_for_intermediate_card",
                "message": "A non-leaf TaskBoard card cannot write a required final deliverable path.",
                "card_id": card_id,
                "requested_path": requested_path,
                "relocated_path": relocated_path,
            }
        )
        result["diagnostics"] = DataFormatter.sanitize(diagnostics)
        return result, plan

    @classmethod
    def _taskboard_context_final_workspace_deliverables(cls, context: Any) -> list[str]:
        card = getattr(context, "card", None)
        metadata = getattr(card, "metadata", None)
        if not isinstance(metadata, Mapping):
            return []
        return cls._normalize_string_list(metadata.get("final_workspace_deliverables"))

    def _taskboard_workspace_delivery_policy(self, context: Any) -> dict[str, Any]:
        required_paths = self._required_workspace_deliverables()
        final_card_paths = [
            path for path in self._taskboard_context_final_workspace_deliverables(context) if path in required_paths
        ]
        can_write_required = bool(required_paths and (final_card_paths or self._taskboard_context_card_is_leaf(context)))
        return {
            "schema_version": "agent_task_taskboard_workspace_delivery/v1",
            "required_deliverables": required_paths,
            "authorized_final_deliverable_paths": final_card_paths or (required_paths if can_write_required else []),
            "can_write_required_deliverables": can_write_required,
            "policy": (
                "Use required deliverable paths for final or framework-marked repair/continuation cards. "
                "Use working refs for intermediate evidence cards."
            ),
        }

    @staticmethod
    def _append_workspace_artifact_meta(execution_meta: Mapping[str, Any] | None, refs: list[dict[str, Any]]) -> None:
        if not refs or not isinstance(execution_meta, dict):
            return
        logs = execution_meta.setdefault("logs", {})
        if not isinstance(logs, dict):
            logs = {}
            execution_meta["logs"] = logs
        artifact_refs = logs.setdefault("artifact_refs", [])
        if not isinstance(artifact_refs, list):
            artifact_refs = []
            logs["artifact_refs"] = artifact_refs
        artifact_refs.extend(DataFormatter.sanitize(refs))
        workspace_refs = execution_meta.setdefault("workspace_refs", {})
        if not isinstance(workspace_refs, dict):
            workspace_refs = {}
            execution_meta["workspace_refs"] = workspace_refs
        workspace_refs.setdefault("agent_task_artifacts", []).extend(DataFormatter.sanitize(refs))
        logs["workspace_refs"] = workspace_refs
        evidence_items = [
            AgentTaskArtifactMixin._workspace_artifact_readback_evidence_item(ref)
            for ref in refs
            if isinstance(ref, Mapping)
        ]
        AgentTaskArtifactMixin._append_execution_meta_evidence_items(execution_meta, evidence_items)

    @staticmethod
    def _append_execution_meta_evidence_items(
        execution_meta: Mapping[str, Any] | None,
        evidence_items: Sequence[Mapping[str, Any]],
    ) -> None:
        if not evidence_items or not isinstance(execution_meta, dict):
            return
        blocks = execution_meta.setdefault("blocks", {})
        if not isinstance(blocks, dict):
            blocks = {}
            execution_meta["blocks"] = blocks
        evidence = blocks.setdefault("evidence", {})
        if not isinstance(evidence, dict):
            evidence = {}
            blocks["evidence"] = evidence
        ledger_items = evidence.setdefault("evidence_items", [])
        if not isinstance(ledger_items, list):
            ledger_items = []
            evidence["evidence_items"] = ledger_items
        seen = {str(item.get("id") or "") for item in ledger_items if isinstance(item, Mapping)}
        for item in evidence_items:
            evidence_id = str(item.get("id") or "").strip()
            if evidence_id and evidence_id in seen:
                continue
            if evidence_id:
                seen.add(evidence_id)
            ledger_items.append(DataFormatter.sanitize(dict(item)))

    async def _workspace_artifact_acceptance_locator_evidence_items(
        self,
        *,
        ref: Mapping[str, Any],
        result: Mapping[str, Any],
        manifest: Mapping[str, Any],
        source: str,
        content: str = "",
        card_context: Any | None = None,
    ) -> list[dict[str, Any]]:
        path = str(ref.get("path") or manifest.get("path") or "").strip()
        if not path:
            return []
        text = str(content or "")
        if not text:
            try:
                declared_bytes = self._coerce_non_negative_int(ref.get("bytes"))
                max_bytes = (
                    declared_bytes + 1
                    if 0 < declared_bytes <= _WORKSPACE_ARTIFACT_LOCATOR_SCAN_BYTES
                    else _WORKSPACE_ARTIFACT_LOCATOR_SCAN_BYTES
                )
                readback = await self.workspace.read_file(path, max_bytes=max_bytes)
            except Exception:
                text = str(ref.get("preview") or "")
            else:
                text = str(readback.get("content") or ref.get("preview") or "")
        acceptance_points = [
            *collect_acceptance_points(result),
            *self._workspace_artifact_acceptance_points_from_taskboard_context(card_context),
        ]
        artifact_evidence_id = self._workspace_artifact_readback_evidence_item(ref).get("id", "")
        return build_workspace_artifact_acceptance_locator_items(
            path=path,
            source=source,
            text=text,
            manifest=manifest,
            acceptance_points=acceptance_points,
            success_criteria=getattr(self, "success_criteria", ()),
            source_evidence_ids=self._artifact_readback_evidence_ids([ref]),
            artifact_evidence_id=str(artifact_evidence_id or ""),
        )

    @staticmethod
    def _workspace_artifact_acceptance_points_from_taskboard_context(card_context: Any | None) -> list[dict[str, Any]]:
        card = getattr(card_context, "card", None)
        if card is None:
            return []
        card_id = str(getattr(card, "id", "") or "card").strip() or "card"
        points: list[dict[str, Any]] = []
        objective = str(getattr(card, "objective", "") or "").strip()
        if objective:
            points.append(
                {
                    "criterion_id": f"taskboard:{card_id}:objective",
                    "criterion": objective,
                    "source": "taskboard_card",
                }
            )
        for index, required_output in enumerate(getattr(card, "required_outputs", ()) or ()):
            text = str(required_output or "").strip()
            if not text:
                continue
            points.append(
                {
                    "criterion_id": f"taskboard:{card_id}:required_output:{index}",
                    "criterion": text,
                    "source": "taskboard_card",
                }
            )
        evidence_contract = getattr(card, "evidence_contract", None)
        if isinstance(evidence_contract, Mapping):
            points.extend(collect_acceptance_points(evidence_contract))
        return DataFormatter.sanitize(points)

    @classmethod
    def _workspace_artifact_readback_evidence_item(cls, ref: Mapping[str, Any]) -> dict[str, Any]:
        path = str(ref.get("path") or "").strip()
        source = str(ref.get("source") or "agent_task.workspace_artifact").strip()
        truncated = bool(ref.get("truncated"))
        preview = str(ref.get("preview") or "")
        evidence_id = cls._workspace_artifact_evidence_id("workspace_artifact_readback", path, source)
        item: dict[str, Any] = {
            "id": evidence_id,
            "kind": "workspace_artifact.readback",
            "status": "ok",
            "raw_status": "read",
            "body_state": "truncated" if truncated else "full",
            "path": path,
            "sha256": str(ref.get("sha256") or ""),
            "bytes": ref.get("bytes"),
            "read_bytes": ref.get("read_bytes"),
            "media_type": ref.get("media_type"),
            "content_kind": ref.get("content_kind"),
            "source": source,
            "provenance": {
                "source": source,
                "path": path,
                "sha256": str(ref.get("sha256") or ""),
                "handler_id": ref.get("handler_id"),
            },
            "supports": {
                "content": True,
                "unavailability": False,
                "ref_pointer": False,
            },
        }
        if preview:
            item["body"] = preview
        return DataFormatter.sanitize(item)

    @classmethod
    def _workspace_artifact_failure_evidence_item(
        cls,
        *,
        path: str,
        source: str,
        code: str,
        message: str,
        readback: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        item: dict[str, Any] = {
            "id": cls._workspace_artifact_evidence_id("workspace_artifact_readback_failed", path, code or source),
            "kind": "workspace_artifact.readback",
            "status": "failed",
            "raw_status": code or "failed",
            "body_state": "ref_only",
            "path": path,
            "source": source,
            "provenance": {"source": source, "path": path},
            "supports": {"content": False, "unavailability": True, "ref_pointer": False},
            "diagnostics": [{"code": code, "message": message, "readback": DataFormatter.sanitize(readback or {})}],
        }
        return DataFormatter.sanitize(item)

    @staticmethod
    def _workspace_artifact_evidence_id(prefix: str, path: str, source: str) -> str:
        raw = f"{ prefix }:{ source }:{ path }"
        return "".join(ch if ch.isalnum() or ch in "._:-/" else "_" for ch in raw)[:240]

    @staticmethod
    def _workspace_artifact_readback_missing_diagnostic(
        *,
        code: str,
        path: str,
        source: str,
        message: str,
        error: Exception | None = None,
        readback: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        diagnostic: dict[str, Any] = {
            "code": code,
            "message": message,
            "path": path,
            "source": source,
        }
        if error is not None:
            diagnostic["error"] = {
                "type": error.__class__.__name__,
                "message": _compact_agent_task_error_message(error, fallback=error.__class__.__name__),
            }
        if readback is not None:
            diagnostic["readback"] = DataFormatter.sanitize(dict(readback))
        return diagnostic

    @staticmethod
    def _workspace_artifact_ref_has_trusted_readback(ref: Mapping[str, Any]) -> bool:
        path = str(ref.get("path") or "").strip()
        sha256 = str(ref.get("sha256") or "").strip()
        try:
            byte_count = int(ref.get("bytes") or 0)
        except (TypeError, ValueError):
            byte_count = 0
        return bool(path and sha256 and byte_count > 0)

    @classmethod
    def _artifact_readback_evidence_ids(cls, refs: Any) -> list[str]:
        if not isinstance(refs, Sequence) or isinstance(refs, str | bytes | bytearray):
            return []
        evidence_ids: list[str] = []
        for ref in refs:
            if not isinstance(ref, Mapping):
                continue
            if not cls._workspace_artifact_ref_has_trusted_readback(ref):
                continue
            path = str(ref.get("path") or "").strip()
            evidence_id = path
            if evidence_id and evidence_id not in evidence_ids:
                evidence_ids.append(evidence_id)
        return evidence_ids

    def _workspace_artifact_delivery_failure_result(
        self,
        result: dict[str, Any],
        execution_meta: Mapping[str, Any] | None,
        diagnostics: list[Any],
        *,
        path: str,
        source: str,
        deliverable_mode: str,
        content_key: str,
        code: str,
        message: str,
        error_type: str,
        readback: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        diagnostic = self._workspace_artifact_readback_missing_diagnostic(
            code=code,
            message=message,
            path=path,
            source=source,
            readback=readback,
        )
        diagnostics.append(diagnostic)
        delivery_record = {
            "source": source,
            "path": path,
            "status": "failed",
            "mode": deliverable_mode or "workspace_artifact",
            "content_key": content_key,
            "error": {"type": error_type, "message": message},
            "diagnostics": [diagnostic],
        }
        result["status"] = "blocked"
        result["diagnostics"] = DataFormatter.sanitize(diagnostics)
        result["workspace_artifact_delivery"] = DataFormatter.sanitize(delivery_record)
        self.diagnostics.setdefault("workspace_artifact_delivery", []).append(
            DataFormatter.sanitize(delivery_record)
        )
        self._append_execution_meta_evidence_items(
            execution_meta,
            [
                self._workspace_artifact_failure_evidence_item(
                    path=path,
                    source=source,
                    code=code,
                    message=message,
                    readback=readback,
                )
            ],
        )
        return DataFormatter.sanitize(result)

    async def _deliver_workspace_artifact(
        self,
        execution_result: Any,
        *,
        plan: Mapping[str, Any] | None = None,
        execution_meta: Mapping[str, Any] | None = None,
        source: str = "agent_task.workspace_artifact",
        context_pack: "WorkspaceContextPackage | None" = None,
        iteration_index: int | None = None,
        card_context: Any | None = None,
        repair_context: Mapping[str, Any] | None = None,
        allow_stream_draft: bool = True,
    ) -> Any:
        if not isinstance(execution_result, Mapping):
            return execution_result
        result = dict(execution_result)
        manifest = result.get("artifact_manifest")
        manifest_dict = dict(manifest) if isinstance(manifest, Mapping) else {}
        diagnostics: list[Any] = []
        raw_diagnostics = result.get("diagnostics")
        if isinstance(raw_diagnostics, Sequence) and not isinstance(raw_diagnostics, str | bytes | bytearray):
            diagnostics.extend(raw_diagnostics)
        elif raw_diagnostics:
            diagnostics.append(raw_diagnostics)

        untrusted_refs = self._workspace_artifact_untrusted_refs(result, manifest_dict)
        if untrusted_refs:
            diagnostics.append(
                {
                    "code": "agent_task.workspace_artifact.untrusted_model_file_refs",
                    "message": "Model-declared file_refs are diagnostics only; trusted file refs require Workspace write/readback.",
                    "file_refs": DataFormatter.sanitize(untrusted_refs),
                }
            )
        result["file_refs"] = []
        if manifest_dict:
            manifest_dict.pop("file_refs", None)
            result["artifact_manifest"] = DataFormatter.sanitize(manifest_dict)

        deliverable_mode = str((plan or {}).get("deliverable_mode") or "").strip()
        path = self._workspace_artifact_manifest_path(manifest_dict)
        content, content_key = self._select_workspace_artifact_content(
            result,
            manifest_dict,
            deliverable_mode=deliverable_mode,
            manifest_path=path,
        )
        prefer_stream_draft = bool((plan or {}).get("prefer_stream_draft"))
        manifest_needs_body = self._workspace_artifact_manifest_needs_body(manifest_dict)
        has_draftable_outline = self._workspace_artifact_manifest_has_draftable_outline(manifest_dict)
        if deliverable_mode == "sectioned_workspace_artifact" and manifest_needs_body:
            prefer_stream_draft = True
        if (
            prefer_stream_draft
            and manifest_needs_body
            and not self._workspace_artifact_content_is_complete_body(content)
        ):
            content = ""
            content_key = ""
        stream_draft_attempted = False
        if not deliverable_mode and content_key == "answer":
            if diagnostics:
                result["diagnostics"] = DataFormatter.sanitize(diagnostics)
            return DataFormatter.sanitize(result)
        if (
            not content
            and allow_stream_draft
            and deliverable_mode in {"workspace_artifact", "sectioned_workspace_artifact"}
            and has_draftable_outline
        ):
            stream_draft_attempted = True
            stream_delivery = await self._stream_workspace_artifact_draft(
                path=path,
                plan=plan,
                execution_result=result,
                execution_meta=execution_meta,
                source=source,
                context_pack=context_pack,
                iteration_index=iteration_index,
                card_context=card_context,
                repair_context=repair_context,
            )
            if stream_delivery is not None:
                trusted_refs = stream_delivery["file_refs"]
                manifest_dict.update(
                    {
                        "path": trusted_refs[0]["path"],
                        "bytes": trusted_refs[0]["bytes"],
                        "sha256": trusted_refs[0]["sha256"],
                        "file_refs": trusted_refs,
                        "source": source,
                    }
                )
                result["file_refs"] = trusted_refs
                result = self._compact_workspace_artifact_result_for_hot_path(
                    result,
                    content_key="streamed_workspace_artifact",
                    content="",
                    trusted_refs=trusted_refs,
                )
                handoff = self._handoff_workspace_artifact_remaining_work_to_verifier(
                    result,
                    diagnostics=diagnostics,
                    path=trusted_refs[0]["path"],
                    source=source,
                    content_key="streamed_workspace_artifact",
                )
                if handoff is not None:
                    stream_delivery["remaining_work_handoff"] = DataFormatter.sanitize(handoff)
                result["artifact_manifest"] = self._compact_workspace_artifact_manifest_for_hot_path(
                    manifest_dict,
                    trusted_refs=trusted_refs,
                    source=source,
                )
                locator_items = await self._workspace_artifact_acceptance_locator_evidence_items(
                    ref=trusted_refs[0],
                    result=result,
                    manifest=manifest_dict,
                    source=source,
                    card_context=card_context,
                )
                if locator_items:
                    stream_delivery["acceptance_locator_count"] = len(locator_items)
                result["workspace_artifact_delivery"] = DataFormatter.sanitize(stream_delivery)
                diagnostics.append(
                    {
                        "code": "agent_task.workspace_artifact.stream_drafted",
                        "message": "Workspace artifact body was generated through a dedicated text stream and written by AgentTask.",
                        "path": trusted_refs[0]["path"],
                        "source": source,
                    }
                )
                result["diagnostics"] = DataFormatter.sanitize(diagnostics)
                self._append_workspace_artifact_meta(execution_meta, trusted_refs)
                self._append_execution_meta_evidence_items(execution_meta, locator_items)
                self.diagnostics.setdefault("workspace_artifact_delivery", []).append(
                    DataFormatter.sanitize(stream_delivery)
                )
                return DataFormatter.sanitize(result)
        if not content:
            if stream_draft_attempted and deliverable_mode in {"workspace_artifact", "sectioned_workspace_artifact"}:
                latest_delivery: Mapping[str, Any] | None = None
                raw_deliveries = self.diagnostics.get("workspace_artifact_delivery")
                if isinstance(raw_deliveries, Sequence) and not isinstance(
                    raw_deliveries,
                    str | bytes | bytearray,
                ):
                    for delivery in reversed(raw_deliveries):
                        if not isinstance(delivery, Mapping):
                            continue
                        if str(delivery.get("path") or "") == path and str(delivery.get("status") or "") == "failed":
                            latest_delivery = delivery
                            break
                error = latest_delivery.get("error") if isinstance(latest_delivery, Mapping) else None
                message = (
                    str(error.get("message") or "")
                    if isinstance(error, Mapping)
                    else ""
                ).strip() or (
                    "Workspace artifact streamed draft failed or produced no content; trusted file_refs were not produced."
                )
                diagnostics.append(
                    self._workspace_artifact_readback_missing_diagnostic(
                        code="agent_task.workspace_artifact.draft_failed",
                        message=message,
                        path=path,
                        source=source,
                    )
                )
                result["status"] = "blocked"
                if latest_delivery is not None:
                    result["workspace_artifact_delivery"] = DataFormatter.sanitize(dict(latest_delivery))
                result["diagnostics"] = DataFormatter.sanitize(diagnostics)
                self._append_execution_meta_evidence_items(
                    execution_meta,
                    [
                        self._workspace_artifact_failure_evidence_item(
                            path=path,
                            source=source,
                            code="agent_task.workspace_artifact.draft_failed",
                            message=message,
                        )
                    ],
                )
                return DataFormatter.sanitize(result)
            if deliverable_mode in {"workspace_artifact", "sectioned_workspace_artifact"}:
                return self._workspace_artifact_delivery_failure_result(
                    result,
                    execution_meta,
                    diagnostics,
                    path=path,
                    source=source,
                    deliverable_mode=deliverable_mode,
                    content_key=content_key,
                    code="agent_task.workspace_artifact.empty_body",
                    message=(
                        "Workspace artifact delivery requires a non-empty body or a successful streamed draft "
                        "readback; trusted file_refs were not produced."
                    ),
                    error_type="EmptyWorkspaceArtifactBody",
                )
            if diagnostics:
                result["diagnostics"] = DataFormatter.sanitize(diagnostics)
            return DataFormatter.sanitize(result)
        if (
            deliverable_mode in {"workspace_artifact", "sectioned_workspace_artifact"}
            and self._workspace_artifact_draft_is_structured_wrapper(content)
        ):
            return self._workspace_artifact_delivery_failure_result(
                result,
                execution_meta,
                diagnostics,
                path=path,
                source=source,
                deliverable_mode=deliverable_mode,
                content_key=content_key,
                code="agent_task.workspace_artifact.structured_wrapper_body",
                message=(
                    "Workspace artifact delivery received a structured wrapper instead of the requested natural "
                    "Markdown/text body; trusted file_refs were not produced."
                ),
                error_type="StructuredWorkspaceArtifactBody",
            )
        delivery_record: dict[str, Any] = {
            "source": source,
            "path": path,
            "status": "started",
            "mode": deliverable_mode or "artifact_markdown",
            "content_key": content_key,
        }
        preserved = await self._preserve_existing_workspace_artifact_if_preferable(
            path=path,
            new_content=content,
            source=source,
            content_key=content_key,
        )
        if preserved is not None:
            ref = preserved["file_ref"]
            delivery_record.update(
                {
                    "status": "preserved_existing",
                    "reason": "existing_workspace_artifact_is_substantially_larger",
                    "existing_bytes": preserved["existing_bytes"],
                    "new_bytes": preserved["new_bytes"],
                    "file_refs": [DataFormatter.sanitize(ref)],
                }
            )
            diagnostics.append(
                {
                    "code": "agent_task.workspace_artifact.preserved_existing",
                    "message": (
                        "Existing Workspace artifact was preserved because the proposed replacement was "
                        "substantially smaller. Return a full replacement body to overwrite it."
                    ),
                    "path": path,
                    "source": source,
                    "content_key": content_key,
                    "existing_bytes": preserved["existing_bytes"],
                    "new_bytes": preserved["new_bytes"],
                }
            )
            trusted_refs = [DataFormatter.sanitize(ref)]
            result["file_refs"] = trusted_refs
            manifest_dict.update(
                {
                    "path": ref["path"],
                    "bytes": ref["bytes"],
                    "sha256": ref["sha256"],
                    "file_refs": trusted_refs,
                    "source": source,
                }
            )
            result = self._compact_workspace_artifact_result_for_hot_path(
                result,
                content_key=content_key,
                content=content,
                trusted_refs=trusted_refs,
            )
            handoff = self._handoff_workspace_artifact_remaining_work_to_verifier(
                result,
                diagnostics=diagnostics,
                path=ref["path"],
                source=source,
                content_key=content_key,
            )
            if handoff is not None:
                delivery_record["remaining_work_handoff"] = DataFormatter.sanitize(handoff)
            result["artifact_manifest"] = self._compact_workspace_artifact_manifest_for_hot_path(
                manifest_dict,
                trusted_refs=trusted_refs,
                source=source,
            )
            locator_items = await self._workspace_artifact_acceptance_locator_evidence_items(
                ref=trusted_refs[0],
                result=result,
                manifest=manifest_dict,
                source=source,
                card_context=card_context,
            )
            if locator_items:
                delivery_record["acceptance_locator_count"] = len(locator_items)
            result["diagnostics"] = DataFormatter.sanitize(diagnostics)
            result["workspace_artifact_delivery"] = DataFormatter.sanitize(delivery_record)
            self._append_workspace_artifact_meta(execution_meta, trusted_refs)
            self._append_execution_meta_evidence_items(execution_meta, locator_items)
            self.diagnostics.setdefault("workspace_artifact_delivery", []).append(
                DataFormatter.sanitize(delivery_record)
            )
            return DataFormatter.sanitize(result)
        try:
            write_result = await self.workspace.write_file(path, content, append=False)
        except Exception as error:
            message = _compact_agent_task_error_message(error, fallback=error.__class__.__name__)
            delivery_record.update(
                {
                    "status": "failed",
                    "error": {"type": error.__class__.__name__, "message": message},
                }
            )
            diagnostics.append(
                {
                    "code": "agent_task.workspace_artifact.write_failed",
                    "message": message,
                    "path": path,
                    "source": source,
                }
            )
            self.diagnostics.setdefault("workspace_artifact_delivery", []).append(
                DataFormatter.sanitize(delivery_record)
            )
            self._append_execution_meta_evidence_items(
                execution_meta,
                [
                    self._workspace_artifact_failure_evidence_item(
                        path=path,
                        source=source,
                        code="agent_task.workspace_artifact.write_failed",
                        message=message,
                    )
                ],
            )
            result["diagnostics"] = DataFormatter.sanitize(diagnostics)
            result["workspace_artifact_delivery"] = DataFormatter.sanitize(delivery_record)
            return DataFormatter.sanitize(result)

        try:
            read_result = await self.workspace.read_file(path, max_bytes=_WORKSPACE_ARTIFACT_PREVIEW_BYTES)
        except Exception as error:
            message = _compact_agent_task_error_message(error, fallback=error.__class__.__name__)
            delivery_record.update(
                {
                    "status": "readback_failed",
                    "write": DataFormatter.sanitize(write_result),
                    "error": {"type": error.__class__.__name__, "message": message},
                }
            )
            diagnostics.append(
                self._workspace_artifact_readback_missing_diagnostic(
                    code="agent_task.workspace_artifact.readback_failed",
                    message=("Workspace artifact readback failed after write; trusted file_refs were not produced."),
                    path=path,
                    source=source,
                    error=error,
                )
            )
            self.diagnostics.setdefault("workspace_artifact_delivery", []).append(
                DataFormatter.sanitize(delivery_record)
            )
            self._append_execution_meta_evidence_items(
                execution_meta,
                [
                    self._workspace_artifact_failure_evidence_item(
                        path=path,
                        source=source,
                        code="agent_task.workspace_artifact.readback_failed",
                        message="Workspace artifact readback failed after write; trusted file_refs were not produced.",
                    )
                ],
            )
            result["diagnostics"] = DataFormatter.sanitize(diagnostics)
            result["workspace_artifact_delivery"] = DataFormatter.sanitize(delivery_record)
            return DataFormatter.sanitize(result)

        ref = {
            "path": str(read_result.get("path") or write_result.get("path") or path),
            "bytes": int(read_result.get("bytes") or write_result.get("bytes") or 0),
            "sha256": str(read_result.get("sha256") or write_result.get("sha256") or ""),
            "media_type": read_result.get("media_type") or write_result.get("media_type"),
            "content_kind": str(read_result.get("content_kind") or write_result.get("content_kind") or "text"),
            "role": "workspace_artifact",
            "source": source,
            "preview": str(read_result.get("content") or ""),
            "truncated": bool(read_result.get("truncated")),
            "read_bytes": int(read_result.get("read_bytes") or 0),
            "handler_id": read_result.get("handler_id"),
        }
        if not self._workspace_artifact_ref_has_trusted_readback(ref):
            delivery_record.update(
                {
                    "status": "readback_insufficient",
                    "write": DataFormatter.sanitize(write_result),
                    "readback": {
                        "path": ref["path"],
                        "bytes": ref["bytes"],
                        "sha256": ref["sha256"],
                        "truncated": ref["truncated"],
                        "read_bytes": ref["read_bytes"],
                        "handler_id": ref["handler_id"],
                    },
                }
            )
            diagnostics.append(
                self._workspace_artifact_readback_missing_diagnostic(
                    code="agent_task.workspace_artifact.readback_insufficient",
                    message=(
                        "Workspace artifact readback was missing or insufficient; "
                        "trusted file_refs were not produced."
                    ),
                    path=path,
                    source=source,
                    readback=read_result,
                )
            )
            self.diagnostics.setdefault("workspace_artifact_delivery", []).append(
                DataFormatter.sanitize(delivery_record)
            )
            self._append_execution_meta_evidence_items(
                execution_meta,
                [
                    self._workspace_artifact_failure_evidence_item(
                        path=path,
                        source=source,
                        code="agent_task.workspace_artifact.readback_insufficient",
                        message=(
                            "Workspace artifact readback was missing or insufficient; "
                            "trusted file_refs were not produced."
                        ),
                        readback=read_result,
                    )
                ],
            )
            result["diagnostics"] = DataFormatter.sanitize(diagnostics)
            result["workspace_artifact_delivery"] = DataFormatter.sanitize(delivery_record)
            return DataFormatter.sanitize(result)

        delivery_record.update(
            {
                "status": "delivered",
                "write": DataFormatter.sanitize(write_result),
                "readback": {
                    "path": ref["path"],
                    "bytes": ref["bytes"],
                    "sha256": ref["sha256"],
                    "truncated": ref["truncated"],
                    "read_bytes": ref["read_bytes"],
                    "handler_id": ref["handler_id"],
                },
                "file_refs": [DataFormatter.sanitize(ref)],
            }
        )
        trusted_refs = [DataFormatter.sanitize(ref)]
        result["file_refs"] = trusted_refs
        manifest_dict.update(
            {
                "path": ref["path"],
                "bytes": ref["bytes"],
                "sha256": ref["sha256"],
                "file_refs": trusted_refs,
                "source": source,
            }
        )
        result = self._compact_workspace_artifact_result_for_hot_path(
            result,
            content_key=content_key,
            content=content,
            trusted_refs=trusted_refs,
        )
        handoff = self._handoff_workspace_artifact_remaining_work_to_verifier(
            result,
            diagnostics=diagnostics,
            path=ref["path"],
            source=source,
            content_key=content_key,
        )
        if handoff is not None:
            delivery_record["remaining_work_handoff"] = DataFormatter.sanitize(handoff)
        result["artifact_manifest"] = self._compact_workspace_artifact_manifest_for_hot_path(
            manifest_dict,
            trusted_refs=trusted_refs,
            source=source,
        )
        locator_items = await self._workspace_artifact_acceptance_locator_evidence_items(
            ref=trusted_refs[0],
            result=result,
            manifest=manifest_dict,
            source=source,
            content=content,
            card_context=card_context,
        )
        if locator_items:
            delivery_record["acceptance_locator_count"] = len(locator_items)
        result["workspace_artifact_delivery"] = DataFormatter.sanitize(delivery_record)
        if diagnostics:
            result["diagnostics"] = DataFormatter.sanitize(diagnostics)
        self._append_workspace_artifact_meta(execution_meta, trusted_refs)
        self._append_execution_meta_evidence_items(execution_meta, locator_items)
        self.diagnostics.setdefault("workspace_artifact_delivery", []).append(DataFormatter.sanitize(delivery_record))
        return DataFormatter.sanitize(result)

    async def _preserve_existing_workspace_artifact_if_preferable(
        self,
        *,
        path: str,
        new_content: str,
        source: str,
        content_key: str,
    ) -> dict[str, Any] | None:
        new_bytes = len(new_content.encode("utf-8"))
        if new_bytes <= 0:
            return None
        try:
            read_result = await self.workspace.read_file(path, max_bytes=_WORKSPACE_ARTIFACT_PREVIEW_BYTES)
        except FileNotFoundError:
            return None
        except Exception:
            return None
        existing_bytes = int(read_result.get("bytes") or 0)
        if existing_bytes <= 0:
            return None
        if existing_bytes < max(new_bytes * 2, new_bytes + 1024):
            return None
        ref = {
            "path": str(read_result.get("path") or path),
            "bytes": existing_bytes,
            "sha256": str(read_result.get("sha256") or ""),
            "media_type": read_result.get("media_type"),
            "content_kind": str(read_result.get("content_kind") or "text"),
            "role": "workspace_artifact",
            "source": source,
            "preview": str(read_result.get("content") or ""),
            "truncated": bool(read_result.get("truncated")),
            "read_bytes": int(read_result.get("read_bytes") or 0),
            "handler_id": read_result.get("handler_id"),
        }
        return {
            "file_ref": DataFormatter.sanitize(ref),
            "existing_bytes": existing_bytes,
            "new_bytes": new_bytes,
            "content_key": content_key,
        }

    @classmethod
    def _select_workspace_artifact_content(
        cls,
        result: Mapping[str, Any],
        manifest_dict: Mapping[str, Any],
        *,
        deliverable_mode: str,
        manifest_path: str = "",
    ) -> tuple[str, str]:
        manifest_content = cls._workspace_artifact_manifest_content(manifest_dict)
        candidates: list[tuple[str, str]] = []
        if manifest_content.strip():
            candidates.append(("artifact_manifest", manifest_content.strip()))
        for key in ("artifact_markdown", "artifact_html", "candidate_final_result", "final_result", "answer"):
            value = result.get(key)
            if isinstance(value, str) and value.strip():
                candidates.append((key, value.strip()))
        if deliverable_mode in {"workspace_artifact", "sectioned_workspace_artifact"}:
            candidates.extend(
                cls._workspace_artifact_evidence_content_candidates(result, manifest_path=manifest_path)
            )
        if not candidates:
            return "", ""
        if deliverable_mode in {"workspace_artifact", "sectioned_workspace_artifact"}:
            explicit_candidates = [
                item
                for item in candidates
                if item[0]
                in {"artifact_manifest", "artifact_markdown", "artifact_html", "candidate_final_result", "final_result"}
                or item[0].startswith("evidence[")
            ]
            answer_candidates = [item for item in candidates if item[0] == "answer"]
            if explicit_candidates:
                key, content = max(explicit_candidates, key=lambda item: len(item[1]))
                if answer_candidates:
                    answer_key, answer_content = max(answer_candidates, key=lambda item: len(item[1]))
                    if len(answer_content) >= max(len(content) * 2, len(content) + 64):
                        return answer_content, answer_key
                return content, key
            if answer_candidates:
                key, content = max(answer_candidates, key=lambda item: len(item[1]))
                return content, key
        for preferred_key in (
            "artifact_manifest",
            "artifact_markdown",
            "artifact_html",
            "candidate_final_result",
            "final_result",
            "answer",
        ):
            for key, content in candidates:
                if key == preferred_key:
                    return content, key
        return candidates[0][1], candidates[0][0]

    async def _stream_workspace_artifact_draft(
        self,
        *,
        path: str,
        plan: Mapping[str, Any] | None,
        execution_result: Mapping[str, Any],
        execution_meta: Mapping[str, Any] | None,
        source: str,
        context_pack: "WorkspaceContextPackage | None" = None,
        iteration_index: int | None = None,
        card_context: Any | None = None,
        repair_context: Mapping[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        draft_execution = self.agent.create_execution(
            lineage={
                "task_id": self.id,
                "iteration_id": f"iter-{iteration_index}" if iteration_index is not None else None,
                "step_id": "workspace_artifact_draft",
                "scope": {"strategy_phase": "agent_task_workspace_artifact_draft"},
            },
            limits=self._child_execution_limits(),
            options=self._child_execution_options(),
        )
        draft_execution.route_policy(
            {
                "allowed_routes": ["model_request"],
                "on_violation": "block",
                "owner": "AgentTaskLoop",
                "step_execution_shape": "workspace_artifact_draft",
            }
        )
        language_policy = self._language_policy()
        draft_execution.language(language_policy.get("language", "auto"))
        cumulative_execution_evidence_summary = self._cumulative_execution_evidence_summary(dict(execution_meta or {}))
        cumulative_evidence_anchors = self._planner_evidence_anchors_from_summary(cumulative_execution_evidence_summary)
        active_repair_context = (
            dict(repair_context) if isinstance(repair_context, Mapping) else self._active_repair_context()
        )
        draft_input = {
            "task_id": self.id,
            "goal": self.goal,
            "success_criteria": self.success_criteria,
            "execution_strategy": self.execution_strategy,
            "artifact_path": path,
            "plan": DataFormatter.sanitize(plan or {}),
            "execution_result": DataFormatter.sanitize(execution_result),
            "execution_meta_summary": self._execution_log_summary(dict(execution_meta or {})),
            "cumulative_evidence_anchors": DataFormatter.sanitize(cumulative_evidence_anchors),
            "context_pack": DataFormatter.sanitize(context_pack or {}),
            "card": DataFormatter.sanitize(
                card_context.card.to_dict()
                if card_context is not None
                and getattr(card_context, "card", None) is not None
                and hasattr(card_context.card, "to_dict")
                else {}
            ),
            "dependency_results": (
                DataFormatter.sanitize(
                    {
                        key: value.to_dict() if hasattr(value, "to_dict") else value
                        for key, value in dict(getattr(card_context, "dependency_results", {}) or {}).items()
                    }
                )
                if card_context is not None
                else {}
            ),
            "language_policy": language_policy,
        }
        if active_repair_context:
            draft_input["repair_context"] = DataFormatter.sanitize(active_repair_context)
        draft_execution.input(draft_input)
        draft_execution.instruct(
            (
                "Write only the final Markdown artifact body for the AgentTask. "
                "Do not output JSON, YAML, XML, code fences, file_refs, or a wrapper object. "
                "Use only the provided task context, execution result, dependency results, and evidence summaries. "
                "For source-grounded artifacts, cite exact URLs, file paths, or refs from cumulative_evidence_anchors; "
                "do not shorten URLs, use ellipses, infer paths from titles, or cite sources that are not visible there. "
                "When repair_context contains fields, this artifact draft is a repair pass: use its acceptance_delta, "
                "advisory_repair_constraints, advisory_next_step_requirements, and available_evidence_anchors as the "
                "active correction contract for the Markdown body. Rewrite affected artifact sections instead of only "
                "stating that they were fixed. "
                "If the source evidence is incomplete, write a clear source-boundary section instead of fabricating facts. "
                "The framework will stream your Markdown into the Workspace artifact path and read it back."
            )
        )

        delivery_record: dict[str, Any] = {
            "source": source,
            "path": path,
            "status": "started",
            "mode": "streamed_workspace_artifact",
            "draft_execution_id": str(getattr(draft_execution, "id", "") or ""),
        }
        wrote_any = False
        bytes_written = 0
        draft_stream = draft_execution.get_async_generator(
            type="specific",
            specific=["delta", "status", "done"],
        )
        retry_boundaries: list[dict[str, Any]] = []
        public_replay_markers: list[dict[str, Any]] = []

        async def handle_retry_boundary(retry_boundary: Mapping[str, Any]) -> None:
            nonlocal wrote_any, bytes_written
            retry_boundaries.append(DataFormatter.sanitize(dict(retry_boundary)))
            delivery_record["retry_boundaries"] = DataFormatter.sanitize(retry_boundaries)
            if wrote_any:
                await self.workspace.write_file(path, "", append=False)
            wrote_any = False
            bytes_written = 0
            if iteration_index is not None:
                await self._emit(
                    f"agent_task.iteration.{iteration_index}.workspace_artifact_draft.retry",
                    {"path": path, "retry_boundary": retry_boundary},
                    meta={
                        "task_id": self.id,
                        "iteration": iteration_index,
                        "stage": "workspace_artifact_draft",
                        "stream_kind": "workspace_artifact_draft_retry",
                        "path": path,
                    },
                )

        async def handle_public_replay_marker(marker: Mapping[str, Any]) -> None:
            nonlocal wrote_any, bytes_written
            public_replay_markers.append(DataFormatter.sanitize(dict(marker)))
            delivery_record["public_replay_markers"] = DataFormatter.sanitize(public_replay_markers)
            if wrote_any:
                await self.workspace.write_file(path, "", append=False)
            wrote_any = False
            bytes_written = 0
            if iteration_index is not None:
                await self._emit(
                    f"agent_task.iteration.{iteration_index}.workspace_artifact_draft.public_replay_marker",
                    {"path": path, "marker": marker},
                    meta={
                        "task_id": self.id,
                        "iteration": iteration_index,
                        "stage": "workspace_artifact_draft",
                        "stream_kind": "workspace_artifact_draft_public_replay_marker",
                        "path": path,
                    },
                )

        async def write_chunk(chunk: str) -> None:
            nonlocal wrote_any, bytes_written
            if not chunk:
                return
            replay_marker = self._workspace_artifact_public_delta_replay_marker(chunk)
            if replay_marker is not None:
                await handle_public_replay_marker(replay_marker)
                return
            await self.workspace.write_file(path, chunk, append=wrote_any)
            wrote_any = True
            bytes_written += len(chunk.encode("utf-8"))
            if iteration_index is not None:
                await self._emit(
                    f"agent_task.iteration.{iteration_index}.workspace_artifact_draft.delta",
                    {"path": path, "bytes_written": bytes_written},
                    event_type="delta",
                    delta=chunk,
                    is_complete=False,
                    meta={
                        "task_id": self.id,
                        "iteration": iteration_index,
                        "stage": "workspace_artifact_draft",
                        "stream_kind": "workspace_artifact_draft",
                        "path": path,
                    },
                )

        try:
            while True:
                try:
                    stream_item = await self._await_stream_next(
                        draft_stream,
                        stage="workspace_artifact_draft",
                    )
                except StopAsyncIteration:
                    break
                if isinstance(stream_item, str):
                    await write_chunk(stream_item)
                    continue
                if isinstance(stream_item, tuple) and len(stream_item) >= 2:
                    event, data = stream_item[0], stream_item[1]
                    if event == "status":
                        retry_boundary = self._workspace_artifact_retry_boundary_from_status("$status", data)
                        if retry_boundary is not None:
                            await handle_retry_boundary(retry_boundary)
                        continue
                    if event == "delta":
                        await write_chunk(str(data))
                    continue
                item_path = str(getattr(stream_item, "path", "") or "")
                retry_boundary = self._workspace_artifact_retry_boundary_from_status(
                    item_path,
                    getattr(stream_item, "value", None),
                )
                if retry_boundary is not None:
                    await handle_retry_boundary(retry_boundary)
                    continue
                if getattr(stream_item, "event_type", None) != "delta":
                    continue
                chunk = str(getattr(stream_item, "delta", None) or "")
                await write_chunk(chunk)
            draft_meta = await self._await_task_request(
                draft_execution.async_get_meta(),
                stage="workspace_artifact_draft_meta",
            )
            delivery_record["draft_meta"] = {
                "execution_id": draft_meta.get("execution_id"),
                "status": draft_meta.get("status"),
                "route": DataFormatter.sanitize(draft_meta.get("route")),
            }
        except Exception as error:
            message = _compact_agent_task_error_message(error, fallback=error.__class__.__name__)
            delivery_record.update(
                {
                    "status": "failed",
                    "error": {"type": error.__class__.__name__, "message": message},
                    "bytes_written": bytes_written,
                }
            )
            self.diagnostics.setdefault("workspace_artifact_delivery", []).append(
                DataFormatter.sanitize(delivery_record)
            )
            self._append_execution_meta_evidence_items(
                execution_meta,
                [
                    self._workspace_artifact_failure_evidence_item(
                        path=path,
                        source=source,
                        code="agent_task.workspace_artifact.draft_failed",
                        message=message,
                    )
                ],
            )
            return None
        finally:
            aclose = getattr(draft_stream, "aclose", None)
            if callable(aclose):
                with suppress(Exception):
                    await cast(Awaitable[Any], aclose())
        if not wrote_any:
            delivery_record.update(
                {
                    "status": "failed",
                    "error": {
                        "type": "EmptyWorkspaceArtifactDraft",
                        "message": "Workspace artifact draft stream produced no content.",
                    },
                    "bytes_written": bytes_written,
                }
            )
            self.diagnostics.setdefault("workspace_artifact_delivery", []).append(
                DataFormatter.sanitize(delivery_record)
            )
            self._append_execution_meta_evidence_items(
                execution_meta,
                [
                    self._workspace_artifact_failure_evidence_item(
                        path=path,
                        source=source,
                        code="agent_task.workspace_artifact.draft_empty",
                        message="Workspace artifact draft stream produced no content.",
                    )
                ],
            )
            return None

        try:
            read_result = await self.workspace.read_file(path, max_bytes=_WORKSPACE_ARTIFACT_PREVIEW_BYTES)
        except Exception as error:
            diagnostic = self._workspace_artifact_readback_missing_diagnostic(
                code="agent_task.workspace_artifact.readback_failed",
                message="Workspace artifact draft readback failed after write; trusted file_refs were not produced.",
                path=path,
                source=source,
                error=error,
            )
            delivery_record.update(
                {
                    "status": "readback_failed",
                    "error": {
                        "type": error.__class__.__name__,
                        "message": _compact_agent_task_error_message(error, fallback=error.__class__.__name__),
                    },
                    "bytes_written": bytes_written,
                    "diagnostics": [diagnostic],
                }
            )
            self.diagnostics.setdefault("workspace_artifact_delivery", []).append(
                DataFormatter.sanitize(delivery_record)
            )
            self._append_execution_meta_evidence_items(
                execution_meta,
                [
                    self._workspace_artifact_failure_evidence_item(
                        path=path,
                        source=source,
                        code="agent_task.workspace_artifact.readback_failed",
                        message="Workspace artifact draft readback failed after write; trusted file_refs were not produced.",
                    )
                ],
            )
            return None

        ref = {
            "path": str(read_result.get("path") or path),
            "bytes": int(read_result.get("bytes") or 0),
            "sha256": str(read_result.get("sha256") or ""),
            "media_type": read_result.get("media_type"),
            "content_kind": str(read_result.get("content_kind") or "text"),
            "role": "workspace_artifact",
            "source": source,
            "preview": str(read_result.get("content") or ""),
            "truncated": bool(read_result.get("truncated")),
            "read_bytes": int(read_result.get("read_bytes") or 0),
            "handler_id": read_result.get("handler_id"),
        }
        if self._workspace_artifact_draft_is_structured_wrapper(str(read_result.get("content") or "")):
            diagnostic = self._workspace_artifact_readback_missing_diagnostic(
                code="agent_task.workspace_artifact.structured_wrapper_draft",
                message=(
                    "Workspace artifact draft returned a structured wrapper instead of the requested natural "
                    "Markdown/text body; trusted file_refs were not produced."
                ),
                path=path,
                source=source,
                readback=read_result,
            )
            with suppress(Exception):
                await self.workspace.write_file(path, "", append=False)
            delivery_record.update(
                {
                    "status": "failed",
                    "error": {
                        "type": "StructuredWorkspaceArtifactDraft",
                        "message": "Workspace artifact draft returned a structured wrapper instead of a body.",
                    },
                    "bytes_written": bytes_written,
                    "readback": {
                        "path": ref["path"],
                        "bytes": ref["bytes"],
                        "sha256": ref["sha256"],
                        "truncated": ref["truncated"],
                        "read_bytes": ref["read_bytes"],
                        "handler_id": ref["handler_id"],
                    },
                    "diagnostics": [diagnostic],
                }
            )
            self.diagnostics.setdefault("workspace_artifact_delivery", []).append(
                DataFormatter.sanitize(delivery_record)
            )
            self._append_execution_meta_evidence_items(
                execution_meta,
                [
                    self._workspace_artifact_failure_evidence_item(
                        path=path,
                        source=source,
                        code="agent_task.workspace_artifact.structured_wrapper_draft",
                        message="Workspace artifact draft returned a structured wrapper instead of a body.",
                        readback=read_result,
                    )
                ],
            )
            return None
        if not self._workspace_artifact_ref_has_trusted_readback(ref):
            diagnostic = self._workspace_artifact_readback_missing_diagnostic(
                code="agent_task.workspace_artifact.readback_insufficient",
                message=(
                    "Workspace artifact draft readback was missing or insufficient; "
                    "trusted file_refs were not produced."
                ),
                path=path,
                source=source,
                readback=read_result,
            )
            delivery_record.update(
                {
                    "status": "readback_insufficient",
                    "bytes_written": bytes_written,
                    "readback": {
                        "path": ref["path"],
                        "bytes": ref["bytes"],
                        "sha256": ref["sha256"],
                        "truncated": ref["truncated"],
                        "read_bytes": ref["read_bytes"],
                        "handler_id": ref["handler_id"],
                    },
                    "diagnostics": [diagnostic],
                }
            )
            self.diagnostics.setdefault("workspace_artifact_delivery", []).append(
                DataFormatter.sanitize(delivery_record)
            )
            self._append_execution_meta_evidence_items(
                execution_meta,
                [
                    self._workspace_artifact_failure_evidence_item(
                        path=path,
                        source=source,
                        code="agent_task.workspace_artifact.readback_insufficient",
                        message=(
                            "Workspace artifact draft readback was missing or insufficient; "
                            "trusted file_refs were not produced."
                        ),
                        readback=read_result,
                    )
                ],
            )
            return None

        delivery_record.update(
            {
                "status": "delivered",
                "bytes_written": bytes_written,
                "readback": {
                    "path": ref["path"],
                    "bytes": ref["bytes"],
                    "sha256": ref["sha256"],
                    "truncated": ref["truncated"],
                    "read_bytes": ref["read_bytes"],
                    "handler_id": ref["handler_id"],
                },
                "file_refs": [DataFormatter.sanitize(ref)],
            }
        )
        return DataFormatter.sanitize(delivery_record)

    @staticmethod
    def _is_trusted_workspace_artifact_ref(ref: Mapping[str, Any]) -> bool:
        role = str(ref.get("role") or "").strip()
        source = str(ref.get("source") or "").strip()
        return role == "workspace_artifact" or source.startswith("agent_task.workspace_artifact")

    @staticmethod
    def _looks_like_workspace_artifact_placeholder(value: str) -> bool:
        return value.strip().startswith("Workspace artifact delivered at ")

    @staticmethod
    def _workspace_artifact_draft_is_structured_wrapper(content: str) -> bool:
        text = content.strip()
        if not text:
            return False
        if not ((text.startswith("{") and text.endswith("}")) or (text.startswith("[") and text.endswith("]"))):
            return False
        try:
            parsed = json.loads(text)
        except Exception:
            return False
        if isinstance(parsed, Mapping):
            wrapper_keys = {
                "answer",
                "status",
                "result",
                "message",
                "content",
                "data",
                "diagnostics",
                "remaining_work",
                "evidence",
            }
            return bool(set(str(key) for key in parsed.keys()).intersection(wrapper_keys))
        return isinstance(parsed, list)

    @staticmethod
    def _coerce_non_negative_int(value: Any) -> int:
        try:
            number = int(value)
        except (TypeError, ValueError):
            return 0
        return max(number, 0)


__all__ = ["AgentTaskArtifactMixin"]
