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

from collections.abc import Mapping, Sequence
from pathlib import PurePosixPath
from typing import Any

from .AcceptanceLocator import ACCEPTANCE_LOCATOR_KIND, acceptance_locator_view_from_ledger

from agently.types.data import EvidenceEnvelope
from agently.utils import DataFormatter


EVIDENCE_LEDGER_VIEW_SCHEMA_VERSION = "evidence_ledger_view/v1"
EVIDENCE_SUPPORT_TYPES = frozenset({"content", "unavailability", "ref_pointer"})

_BODY_KEYS = ("body", "content", "text", "snippet", "preview", "result", "output", "value")
_REF_FIELDS = (
    "source_url",
    "selected_url",
    "requested_url",
    "canonical_url",
    "url",
    "href",
    "path",
    "record_id",
    "artifact_id",
    "ref",
)
_ALIAS_FIELDS = (
    "id",
    "cite_as",
    "path",
    "record_id",
    "source_url",
    "selected_url",
    "requested_url",
    "canonical_url",
    "url",
    "href",
    "output_ref",
    "artifact_id",
    "action_call_id",
    "action_id",
    "ref",
)
_ACTION_ALIAS_FIELDS = ("action_id", "action_call_id")
_INTEGRITY_METADATA_FIELDS = frozenset(
    {
        "sha256",
        "digest",
        "bytes",
        "read_bytes",
        "size",
        "media_type",
        "content_kind",
        "handler_id",
    }
)


def evidence_envelope_from_value(value: Any) -> EvidenceEnvelope:
    if isinstance(value, EvidenceEnvelope):
        return value
    if isinstance(value, Mapping):
        return EvidenceEnvelope.from_value(value)
    return EvidenceEnvelope.from_value({"evidence_items": ()})


def evidence_ledger_view(
    value: Any,
    *,
    max_items: int = 64,
    body_chars: int = 1200,
    include_body: bool = True,
) -> dict[str, Any]:
    envelope = evidence_envelope_from_value(value)
    items: list[dict[str, Any]] = []
    source_refs: list[dict[str, Any]] = []
    status_counts: dict[str, int] = {"ok": 0, "failed": 0, "empty": 0}
    body_state_counts: dict[str, int] = {"full": 0, "bounded": 0, "truncated": 0, "ref_only": 0}
    for raw_item in envelope.evidence_items:
        if not isinstance(raw_item, Mapping):
            continue
        compact_item = _compact_ledger_item(
            raw_item,
            body_chars=body_chars,
            include_body=include_body,
            cite_as=f"e{len(items) + 1}",
        )
        status = str(compact_item.get("status") or "")
        body_state = str(compact_item.get("body_state") or "")
        status_counts[status] = status_counts.get(status, 0) + 1
        body_state_counts[body_state] = body_state_counts.get(body_state, 0) + 1
        items.append(compact_item)
        ref = _source_ref_from_ledger_item(compact_item)
        if ref:
            source_refs.append(ref)
        if len(items) >= max_items:
            break
    return DataFormatter.sanitize(
        {
            "schema_version": EVIDENCE_LEDGER_VIEW_SCHEMA_VERSION,
            "source_schema_version": envelope.schema_version,
            "items": items,
            "item_count": len(envelope.evidence_items),
            "items_omitted": max(0, len(envelope.evidence_items) - len(items)),
            "status_counts": status_counts,
            "body_state_counts": body_state_counts,
            "source_refs": _dedupe_refs(source_refs),
            "acceptance_locator_view": acceptance_locator_view_from_ledger({"items": items}),
            "grounding_rules": {
                "ok_content": "status=ok with body_state full/bounded/truncated supports only visible content.",
                "ref_only": "body_state=ref_only supports only discovery/ref-pointer claims until readback evidence exists.",
                "failed_empty": "status=failed or status=empty supports unavailability/missingness claims only.",
                "truncated": "body_state=truncated cannot by itself support whole-document or exhaustive claims.",
            },
        }
    )


def source_refs_from_ledger(value: Any, *, max_refs: int = 32) -> list[dict[str, Any]]:
    ledger = value if isinstance(value, Mapping) and value.get("schema_version") == EVIDENCE_LEDGER_VIEW_SCHEMA_VERSION else evidence_ledger_view(value)
    refs = ledger.get("source_refs") if isinstance(ledger, Mapping) else []
    if not isinstance(refs, Sequence) or isinstance(refs, str | bytes | bytearray):
        return []
    return _dedupe_refs([ref for ref in refs if isinstance(ref, Mapping)])[:max_refs]


def collect_evidence_use(value: Any) -> list[dict[str, Any]]:
    uses: list[dict[str, Any]] = []

    def visit(item: Any) -> None:
        if isinstance(item, Mapping):
            evidence_use = item.get("evidence_use")
            if isinstance(evidence_use, Mapping):
                uses.append(dict(DataFormatter.sanitize(evidence_use)))
            elif isinstance(evidence_use, Sequence) and not isinstance(evidence_use, str | bytes | bytearray):
                for entry in evidence_use:
                    if isinstance(entry, Mapping):
                        uses.append(dict(DataFormatter.sanitize(entry)))
            for key, child in item.items():
                if key == "evidence_use":
                    continue
                visit(child)
            return
        if isinstance(item, Sequence) and not isinstance(item, str | bytes | bytearray):
            for child in item:
                visit(child)

    visit(value)
    return uses


def value_with_normalized_evidence_use(value: Any, normalized_evidence_use: Any) -> Any:
    normalized_entries = _evidence_use_sequence(normalized_evidence_use)
    if not normalized_entries:
        return value
    index = 0

    def visit(item: Any) -> Any:
        nonlocal index
        if isinstance(item, Mapping):
            cloned = dict(item)
            evidence_use = cloned.get("evidence_use")
            if isinstance(evidence_use, Mapping):
                if index < len(normalized_entries):
                    cloned["evidence_use"] = DataFormatter.sanitize(normalized_entries[index])
                index += 1
            elif isinstance(evidence_use, Sequence) and not isinstance(evidence_use, str | bytes | bytearray):
                replaced: list[Any] = []
                for entry in evidence_use:
                    if isinstance(entry, Mapping) and index < len(normalized_entries):
                        replaced.append(DataFormatter.sanitize(normalized_entries[index]))
                        index += 1
                    else:
                        replaced.append(DataFormatter.sanitize(entry))
                cloned["evidence_use"] = replaced
            for key, child in list(cloned.items()):
                if key == "evidence_use":
                    continue
                if isinstance(child, Mapping) or (
                    isinstance(child, Sequence) and not isinstance(child, str | bytes | bytearray)
                ):
                    cloned[key] = visit(child)
            return DataFormatter.sanitize(cloned)
        if isinstance(item, Sequence) and not isinstance(item, str | bytes | bytearray):
            return [visit(child) for child in item]
        return item

    return visit(value)


def validate_evidence_use(evidence_use: Any, ledger_value: Any) -> dict[str, Any]:
    ledger = (
        ledger_value
        if isinstance(ledger_value, Mapping) and ledger_value.get("schema_version") == EVIDENCE_LEDGER_VIEW_SCHEMA_VERSION
        else evidence_ledger_view(ledger_value, include_body=False)
    )
    raw_items = ledger.get("items") if isinstance(ledger, Mapping) else ()
    raw_item_sequence = raw_items if isinstance(raw_items, Sequence) and not isinstance(raw_items, str | bytes | bytearray) else ()
    items_by_id = {
        str(item.get("id")): item
        for item in raw_item_sequence
        if isinstance(item, Mapping) and str(item.get("id") or "").strip()
    }
    alias_index = _build_evidence_alias_index(raw_item_sequence)
    uses = _evidence_use_sequence(evidence_use)
    diagnostics: list[dict[str, Any]] = []
    normalized_uses: list[dict[str, Any]] = []
    for index, use in enumerate(uses):
        support_type = str(use.get("support_type") or "").strip()
        claim = str(use.get("claim") or "").strip()
        ids = _string_list(use.get("evidence_ids"))
        normalized_use = dict(use)
        normalized_ids: list[str] = []
        if support_type not in EVIDENCE_SUPPORT_TYPES:
            diagnostics.append(
                _guard_diagnostic(
                    "evidence_ledger.invalid_support_type",
                    "evidence_use.support_type must be content, unavailability, or ref_pointer.",
                    claim=claim,
                    support_type=support_type,
                    index=index,
                    blocking=True,
                )
            )
        if not ids:
            diagnostics.append(
                _guard_diagnostic(
                    "evidence_ledger.missing_evidence_id",
                    "evidence_use requires at least one ledger evidence id.",
                    claim=claim,
                    support_type=support_type,
                    index=index,
                    blocking=True,
                )
            )
            normalized_use["evidence_ids"] = normalized_ids
            normalized_uses.append(DataFormatter.sanitize(normalized_use))
            continue
        for evidence_id in ids:
            resolution = _resolve_evidence_alias(evidence_id, alias_index)
            canonical_id = str(resolution.get("id") or evidence_id).strip()
            if resolution.get("ambiguous"):
                diagnostics.append(
                    _guard_diagnostic(
                        "evidence_ledger.ambiguous_evidence_alias",
                        "evidence_use references an alias that matches multiple evidence ledger items.",
                        evidence_id=evidence_id,
                        candidates=resolution.get("candidates"),
                        claim=claim,
                        support_type=support_type,
                        index=index,
                        blocking=True,
                    )
                )
                continue
            if canonical_id and canonical_id not in normalized_ids:
                normalized_ids.append(canonical_id)
            if canonical_id != evidence_id and canonical_id in items_by_id:
                diagnostics.append(
                    _guard_diagnostic(
                        "evidence_ledger.alias_resolved",
                        "evidence_use alias was canonicalized to a ledger evidence id.",
                        evidence_id=evidence_id,
                        canonical_id=canonical_id,
                        alias=resolution.get("alias"),
                        claim=claim,
                        support_type=support_type,
                        index=index,
                        blocking=False,
                    )
                )
            item = items_by_id.get(canonical_id)
            if item is None:
                diagnostics.append(
                    _guard_diagnostic(
                        "evidence_ledger.invalid_evidence_id",
                        "evidence_use references an id that is not present in the evidence ledger.",
                        evidence_id=evidence_id,
                        claim=claim,
                        support_type=support_type,
                        index=index,
                        blocking=True,
                    )
                )
                continue
            status = str(item.get("status") or "")
            body_state = str(item.get("body_state") or "")
            if status in {"failed", "empty"} and support_type != "unavailability":
                diagnostics.append(
                    _guard_diagnostic(
                        "evidence_ledger.unavailable_item_used_as_positive_support",
                        "failed/empty evidence can support only unavailable or missing-data claims.",
                        evidence_id=evidence_id,
                        claim=claim,
                        support_type=support_type,
                        status=status,
                        body_state=body_state,
                        index=index,
                        blocking=True,
                    )
                )
            if status == "ok" and support_type == "unavailability":
                diagnostics.append(
                    _guard_diagnostic(
                        "evidence_ledger.ok_item_used_as_unavailability_support",
                        "ok evidence cannot by itself support an unavailable or missing-data claim.",
                        evidence_id=evidence_id,
                        claim=claim,
                        support_type=support_type,
                        status=status,
                        body_state=body_state,
                        index=index,
                        blocking=True,
                    )
                )
            if body_state == "ref_only" and support_type == "content":
                diagnostics.append(
                    _guard_diagnostic(
                        "evidence_ledger.ref_only_item_used_as_content_support",
                        "ref_only evidence supports only discovery/ref-pointer claims until readback evidence exists.",
                        evidence_id=evidence_id,
                        claim=claim,
                        support_type=support_type,
                        status=status,
                        body_state=body_state,
                        index=index,
                        blocking=True,
                    )
                )
            if body_state in {"full", "bounded", "truncated"} and support_type == "ref_pointer":
                diagnostics.append(
                    _guard_diagnostic(
                        "evidence_ledger.content_item_used_as_ref_pointer_support",
                        "content evidence should be bound as content support, not ref_pointer support.",
                        evidence_id=evidence_id,
                        claim=claim,
                        support_type=support_type,
                        status=status,
                        body_state=body_state,
                        index=index,
                        blocking=False,
                    )
                )
            if body_state == "truncated" and support_type == "content":
                diagnostics.append(
                    _guard_diagnostic(
                        "evidence_ledger.truncated_content_boundary",
                        "truncated evidence supports visible-snippet claims only; whole-source claims require readback.",
                        evidence_id=evidence_id,
                        claim=claim,
                        support_type=support_type,
                        status=status,
                        body_state=body_state,
                        index=index,
                        blocking=False,
                    )
                )
        normalized_use["evidence_ids"] = normalized_ids
        normalized_uses.append(DataFormatter.sanitize(normalized_use))
    blocking_count = sum(1 for item in diagnostics if item.get("blocking") is True)
    return DataFormatter.sanitize(
        {
            "schema_version": "evidence_use_guard/v1",
            "valid": blocking_count == 0,
            "blocking_count": blocking_count,
            "diagnostics": diagnostics,
            "checked_claims": len(uses),
            "available_evidence_ids": list(items_by_id.keys()),
            "available_evidence_refs": _available_evidence_refs(raw_item_sequence),
            "normalized_evidence_use": normalized_uses,
        }
    )


def workspace_artifacts_from_ledger(ledger_value: Any, *, max_artifacts: int = 4) -> list[dict[str, Any]]:
    ledger = (
        ledger_value
        if isinstance(ledger_value, Mapping) and ledger_value.get("schema_version") == EVIDENCE_LEDGER_VIEW_SCHEMA_VERSION
        else evidence_ledger_view(ledger_value)
    )
    artifacts: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in ledger.get("items", []) if isinstance(ledger, Mapping) else []:
        if not isinstance(item, Mapping):
            continue
        path = _first_ref_value(item, ("path",))
        if not path:
            continue
        kind = str(item.get("kind") or "")
        provenance = item.get("provenance")
        source = str(provenance.get("source") or item.get("source") or "") if isinstance(provenance, Mapping) else ""
        if "artifact" not in kind and "readback" not in kind and "workspace" not in source:
            continue
        key = f"{path}|{item.get('id')}"
        if key in seen:
            continue
        seen.add(key)
        artifact = {
            "evidence_id": item.get("id"),
            "path": path,
            "status": item.get("status"),
            "body_state": item.get("body_state"),
            "readback": {
                "status": item.get("status"),
                "path": path,
                "truncated": item.get("body_state") == "truncated",
            },
        }
        body = item.get("body")
        if isinstance(body, str) and body:
            artifact["readback"]["content"] = body
        preview = item.get("preview")
        if isinstance(preview, str) and preview and "content" not in artifact["readback"]:
            artifact["readback"]["content"] = preview
        artifacts.append(artifact)
        if len(artifacts) >= max_artifacts:
            break
    return DataFormatter.sanitize(artifacts)


def _compact_ledger_item(
    item: Mapping[str, Any],
    *,
    body_chars: int,
    include_body: bool,
    cite_as: str = "",
) -> dict[str, Any]:
    compact: dict[str, Any] = {
        "id": str(item.get("id") or ""),
        "kind": str(item.get("kind") or ""),
        "status": str(item.get("status") or "ok"),
        "raw_status": item.get("raw_status", item.get("status")),
        "body_state": str(item.get("body_state") or "ref_only"),
        "provenance": _drop_integrity_metadata_value(item.get("provenance"))
        if isinstance(item.get("provenance"), Mapping)
        else {},
        "supports": dict(item.get("supports") or {}) if isinstance(item.get("supports"), Mapping) else {},
    }
    existing_cite_as = str(item.get("cite_as") or "").strip()
    if existing_cite_as:
        compact["cite_as"] = existing_cite_as
    elif cite_as:
        compact["cite_as"] = cite_as
    diagnostics = item.get("diagnostics")
    if isinstance(diagnostics, Sequence) and not isinstance(diagnostics, str | bytes | bytearray):
        compact["diagnostics"] = [DataFormatter.sanitize(entry) for entry in diagnostics[:8]]
        if len(diagnostics) > 8:
            compact["diagnostics"].append({"omitted": len(diagnostics) - 8, "reason": "ledger_view_budget"})
    aliases = item.get("aliases")
    if isinstance(aliases, Sequence) and not isinstance(aliases, str | bytes | bytearray):
        compact_aliases = [str(alias).strip() for alias in aliases if str(alias or "").strip()]
        if compact_aliases:
            compact["aliases"] = compact_aliases[:16]
    elif isinstance(aliases, str) and aliases.strip():
        compact["aliases"] = [aliases.strip()]
    for field in _ALIAS_FIELDS:
        if field in item and item.get(field) not in (None, "", [], {}):
            compact[field] = DataFormatter.sanitize(item.get(field))
    if str(item.get("kind") or "") == ACCEPTANCE_LOCATOR_KIND:
        for field in (
            "artifact_path",
            "criterion_id",
            "claim",
            "topic",
            "point_source",
            "requirement_level",
            "heading",
            "anchor_text",
            "line_start",
            "line_end",
            "byte_offset",
            "byte_end",
            "content_fingerprint",
            "source_evidence_ids",
        ):
            if item.get(field) not in (None, "", [], {}):
                compact[field] = DataFormatter.sanitize(item.get(field))
    if include_body:
        body = _first_body_value(item)
        if isinstance(body, str) and body:
            compact["body"] = _compact_body_for_ledger_view(body, body_chars)
            if len(body) > body_chars:
                compact["body_truncated_for_view"] = True
                compact["body_chars"] = len(body)
        elif body not in (None, "", [], {}):
            compact["preview"] = _drop_integrity_metadata_value(body)
    return compact


def _compact_body_for_ledger_view(body: str, body_chars: int) -> str:
    body = _drop_integrity_metadata_lines(body)
    if body_chars <= 0:
        return ""
    if len(body) <= body_chars:
        return body
    marker = "\n\n[...body truncated for evidence ledger view...]\n\n"
    if body_chars <= len(marker) + 20:
        return body[:body_chars]
    head_chars = max(1, body_chars // 2)
    tail_chars = max(1, body_chars - head_chars - len(marker))
    return f"{ body[:head_chars] }{ marker }{ body[-tail_chars:] }"


def _drop_integrity_metadata_lines(body: str) -> str:
    omitted_prefixes = ("sha256:", "digest:", "bytes:", "read_bytes:", "size:", "media_type:", "handler_id:")
    lines = []
    for line in body.splitlines():
        if line.strip().lower().startswith(omitted_prefixes):
            continue
        lines.append(line)
    return "\n".join(lines)


def _drop_integrity_metadata_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        compact: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            if key_text.strip().lower() in _INTEGRITY_METADATA_FIELDS:
                continue
            compact[key_text] = _drop_integrity_metadata_value(item)
        return DataFormatter.sanitize(compact)
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return [_drop_integrity_metadata_value(item) for item in value]
    if isinstance(value, str):
        return _drop_integrity_metadata_lines(value)
    return DataFormatter.sanitize(value)


def _source_ref_from_ledger_item(item: Mapping[str, Any]) -> dict[str, Any]:
    field = ""
    value = ""
    for candidate in _REF_FIELDS:
        value = _first_ref_value(item, (candidate,))
        if value:
            field = candidate
            break
    if not field or not value:
        return {}
    body_state = str(item.get("body_state") or "ref_only")
    content_state = "ref_only" if body_state == "ref_only" else "bounded_readback_available"
    ref = {
        "evidence_id": str(item.get("id") or ""),
        "cite_as": str(item.get("cite_as") or ""),
        "field": field,
        "value": value,
        "content_state": content_state,
        "body_state": body_state,
        "status": str(item.get("status") or ""),
        "kind": str(item.get("kind") or ""),
    }
    for key in ("path", "record_id", "source_url", "selected_url", "requested_url", "canonical_url", "url", "href"):
        candidate = _first_ref_value(item, (key,))
        if candidate:
            ref[key] = candidate
    return ref


def _build_evidence_alias_index(raw_items: Sequence[Any]) -> dict[str, Any]:
    alias_to_ids: dict[str, set[str]] = {}
    canonical_ids: set[str] = set()
    for item in raw_items:
        if not isinstance(item, Mapping):
            continue
        evidence_id = str(item.get("id") or "").strip()
        if not evidence_id:
            continue
        canonical_ids.add(evidence_id)
        for alias in _evidence_aliases_for_item(item):
            alias_to_ids.setdefault(alias, set()).add(evidence_id)

    unique: dict[str, str] = {}
    ambiguous: dict[str, list[str]] = {}
    for alias, ids in alias_to_ids.items():
        ordered = sorted(ids)
        if len(ordered) == 1:
            unique[alias] = ordered[0]
        else:
            ambiguous[alias] = ordered
    return {"canonical_ids": canonical_ids, "unique": unique, "ambiguous": ambiguous}


def _resolve_evidence_alias(evidence_id: str, alias_index: Mapping[str, Any]) -> dict[str, Any]:
    original = str(evidence_id or "").strip()
    if not original:
        return {}
    aliases = _alias_variants(original)
    raw_canonical_ids = alias_index.get("canonical_ids")
    canonical_ids: set[str] = (
        {str(item) for item in raw_canonical_ids} if isinstance(raw_canonical_ids, set) else set()
    )
    for alias in aliases:
        if alias in canonical_ids:
            return {"id": alias, "alias": alias}
    raw_ambiguous = alias_index.get("ambiguous")
    ambiguous: Mapping[str, Any] = raw_ambiguous if isinstance(raw_ambiguous, Mapping) else {}
    for alias in aliases:
        candidates = ambiguous.get(alias)
        if candidates:
            return {"ambiguous": True, "alias": alias, "candidates": list(candidates)}
    raw_unique = alias_index.get("unique")
    unique: Mapping[str, Any] = raw_unique if isinstance(raw_unique, Mapping) else {}
    for alias in aliases:
        canonical_id = unique.get(alias)
        if canonical_id:
            return {"id": str(canonical_id), "alias": alias}
    return {"id": original, "alias": original}


def _evidence_aliases_for_item(item: Mapping[str, Any]) -> set[str]:
    aliases: set[str] = set()

    def add(value: Any) -> None:
        aliases.update(_alias_variants(value))

    explicit_aliases = item.get("aliases")
    if isinstance(explicit_aliases, Sequence) and not isinstance(explicit_aliases, str | bytes | bytearray):
        for alias in explicit_aliases:
            add(alias)
    elif isinstance(explicit_aliases, str):
        add(explicit_aliases)
    for field in _ALIAS_FIELDS:
        add(item.get(field))
    provenance = item.get("provenance")
    if isinstance(provenance, Mapping):
        for field in _ALIAS_FIELDS:
            add(provenance.get(field))
    ref = item.get("ref")
    if isinstance(ref, Mapping):
        for field in _ALIAS_FIELDS:
            add(ref.get(field))
    for field in _ACTION_ALIAS_FIELDS:
        value = str(item.get(field) or "").strip()
        if value:
            add(f"action_result_{ value }")
            add(f"action_{ value }")
    if isinstance(provenance, Mapping):
        for field in _ACTION_ALIAS_FIELDS:
            value = str(provenance.get(field) or "").strip()
            if value:
                add(f"action_result_{ value }")
                add(f"action_{ value }")
    path = _first_ref_value(item, ("path",))
    if path:
        basename = PurePosixPath(path.replace("\\", "/")).name
        if basename and basename != path:
            add(basename)
    return aliases


def _alias_variants(value: Any) -> set[str]:
    text = str(value or "").strip()
    if not text:
        return set()
    variants = {text}
    compact_path = text.replace("\\", "/")
    while compact_path.startswith("./"):
        compact_path = compact_path[2:]
    compact_path = "/".join(part for part in compact_path.split("/") if part not in {"", "."})
    if compact_path:
        variants.add(compact_path)
        stripped = compact_path.rstrip("/")
        if stripped:
            variants.add(stripped)
    if text.startswith("file://"):
        variants.add(text.removeprefix("file://"))
    return {variant for variant in variants if variant}


def _available_evidence_refs(raw_items: Sequence[Any], *, max_items: int = 80) -> list[dict[str, Any]]:
    refs: list[dict[str, Any]] = []
    for item in raw_items:
        if not isinstance(item, Mapping):
            continue
        ref: dict[str, Any] = {
            "id": str(item.get("id") or ""),
            "cite_as": str(item.get("cite_as") or ""),
            "kind": str(item.get("kind") or ""),
            "status": str(item.get("status") or ""),
            "body_state": str(item.get("body_state") or ""),
        }
        if str(item.get("kind") or "") == ACCEPTANCE_LOCATOR_KIND:
            for field in (
                "criterion_id",
                "claim",
                "topic",
                "heading",
                "anchor_text",
                "line_start",
                "line_end",
                "byte_offset",
                "byte_end",
            ):
                value = item.get(field)
                if value not in (None, "", [], {}):
                    ref[field] = value
        for field in _ALIAS_FIELDS:
            if field in {"id", "cite_as"}:
                continue
            value = _first_ref_value(item, (field,))
            if value:
                ref[field] = value
        aliases = item.get("aliases")
        if isinstance(aliases, Sequence) and not isinstance(aliases, str | bytes | bytearray):
            compact_aliases = [str(alias).strip() for alias in aliases if str(alias or "").strip()]
            if compact_aliases:
                ref["aliases"] = compact_aliases[:8]
        refs.append(ref)
        if len(refs) >= max_items:
            break
    return DataFormatter.sanitize(refs)


def _first_body_value(item: Mapping[str, Any]) -> Any:
    for key in _BODY_KEYS:
        value = item.get(key)
        if value not in (None, "", [], {}):
            return value
    return None


def _first_ref_value(item: Mapping[str, Any], keys: Sequence[str]) -> str:
    for key in keys:
        value = item.get(key)
        if value not in (None, "", [], {}):
            if isinstance(value, Mapping):
                for nested_key in (
                    "path",
                    "id",
                    "record_id",
                    "source_url",
                    "canonical_url",
                    "url",
                    "href",
                    "artifact_id",
                    "action_id",
                    "action_call_id",
                    "output_ref",
                    "value",
                ):
                    nested = value.get(nested_key)
                    if nested not in (None, "", [], {}):
                        return str(nested)
            return str(value)
    provenance = item.get("provenance")
    if isinstance(provenance, Mapping):
        for key in keys:
            value = provenance.get(key)
            if value not in (None, "", [], {}):
                return str(value)
    return ""


def _dedupe_refs(refs: Any) -> list[dict[str, Any]]:
    deduped: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    if not isinstance(refs, Sequence) or isinstance(refs, str | bytes | bytearray):
        return deduped
    for ref in refs:
        if not isinstance(ref, Mapping):
            continue
        key = (
            str(ref.get("evidence_id") or ""),
            str(ref.get("field") or ""),
            str(ref.get("value") or ""),
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(dict(DataFormatter.sanitize(ref)))
    return deduped


def _evidence_use_sequence(value: Any) -> list[dict[str, Any]]:
    if value is None:
        return []
    if isinstance(value, Mapping):
        return [dict(value)]
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return [dict(item) for item in value if isinstance(item, Mapping)]
    return []


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        text = value.strip()
        return [text] if text else []
    if isinstance(value, Sequence) and not isinstance(value, bytes | bytearray):
        return [text for item in value if (text := str(item).strip())]
    return [str(value).strip()] if str(value).strip() else []


def _guard_diagnostic(
    code: str,
    message: str,
    *,
    blocking: bool,
    **extra: Any,
) -> dict[str, Any]:
    diagnostic = {"code": code, "message": message, "blocking": blocking}
    diagnostic.update({key: value for key, value in extra.items() if value not in (None, "", [], {})})
    return diagnostic
