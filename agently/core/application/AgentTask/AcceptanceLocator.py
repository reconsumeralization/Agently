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

import hashlib
import re
from collections.abc import Mapping, Sequence
from pathlib import PurePosixPath
from typing import Any

from agently.utils import DataFormatter


ACCEPTANCE_LOCATOR_KIND = "workspace_artifact.acceptance_locator"

_HEADING_RE = re.compile(r"^\s{0,3}(?P<marks>#{1,6})\s+(?P<title>.+?)\s*#*\s*$")
_SPACE_RE = re.compile(r"\s+")
_CONNECTOR_RE = re.compile(r"\s*(?:[/&+]|\b(?:and|or)\b)\s*")
_CJK_NUMERIC_SPACE_RE = re.compile(
    r"(?<=[\u3400-\u9fff])\s+(?=[0-9０-９])|(?<=[0-9０-９])\s+(?=[\u3400-\u9fff])"
)
_DASH_TRANSLATION = str.maketrans(
    {
        "\u2010": "-",
        "\u2011": "-",
        "\u2012": "-",
        "\u2013": "-",
        "\u2014": "-",
        "\u2015": "-",
        "\u2212": "-",
    }
)


def collect_acceptance_points(value: Any, *, max_points: int = 64) -> list[dict[str, Any]]:
    points: list[dict[str, Any]] = []

    def add(raw: Any) -> None:
        if len(points) >= max_points:
            return
        point = _normalize_acceptance_point(raw, index=len(points))
        if point is not None:
            points.append(point)

    def visit(item: Any) -> None:
        if len(points) >= max_points:
            return
        if isinstance(item, Mapping):
            raw_points = item.get("acceptance_points")
            if isinstance(raw_points, Mapping):
                add(raw_points)
            elif isinstance(raw_points, Sequence) and not isinstance(raw_points, str | bytes | bytearray):
                for raw_point in raw_points:
                    add(raw_point)
                    if len(points) >= max_points:
                        break
            for key, child in item.items():
                if key == "acceptance_points":
                    continue
                if isinstance(child, Mapping) or (
                    isinstance(child, Sequence) and not isinstance(child, str | bytes | bytearray)
                ):
                    visit(child)
            return
        if isinstance(item, Sequence) and not isinstance(item, str | bytes | bytearray):
            for child in item:
                visit(child)
                if len(points) >= max_points:
                    break

    visit(value)
    return DataFormatter.sanitize(points)


def acceptance_points_from_manifest(manifest: Mapping[str, Any] | None, *, max_points: int = 32) -> list[dict[str, Any]]:
    if not isinstance(manifest, Mapping):
        return []
    points: list[dict[str, Any]] = []

    def add(section: Any, *, source_key: str, index: int) -> None:
        if len(points) >= max_points:
            return
        if isinstance(section, str):
            text = section.strip()
            if text:
                points.append(
                    {
                        "criterion_id": f"{source_key}:{index}",
                        "criterion": text,
                        "expected_anchor": text,
                        "source": "artifact_manifest",
                    }
                )
            return
        if not isinstance(section, Mapping):
            return
        title = _first_string(section, ("title", "name", "heading", "id", "label"))
        intent = _first_string(section, ("intent", "summary", "description", "outline"))
        if not title and not intent:
            return
        points.append(
            {
                "criterion_id": str(section.get("id") or f"{source_key}:{index}"),
                "criterion": intent or title,
                "topic": title or intent,
                "expected_anchor": title or intent,
                "source": "artifact_manifest",
            }
        )

    for key in ("sections", "section_outline", "deliverables"):
        raw_sections = manifest.get(key)
        if isinstance(raw_sections, Sequence) and not isinstance(raw_sections, str | bytes | bytearray):
            for index, section in enumerate(raw_sections):
                add(section, source_key=key, index=index)
    return DataFormatter.sanitize(points)


def build_workspace_artifact_acceptance_locator_items(
    *,
    path: str,
    source: str,
    text: str,
    manifest: Mapping[str, Any] | None = None,
    acceptance_points: Sequence[Any] | None = None,
    success_criteria: Sequence[str] | None = None,
    source_evidence_ids: Sequence[str] | None = None,
    artifact_evidence_id: str = "",
    max_items: int = 80,
) -> list[dict[str, Any]]:
    artifact_path = str(path or "").strip()
    if not artifact_path:
        return []
    text_value = str(text or "")
    points = _dedupe_points(
        [
            *acceptance_points_from_manifest(manifest),
            *[_normalize_acceptance_point(item, index=index) for index, item in enumerate(acceptance_points or ())],
            *[
                {
                    "criterion_id": f"success_criteria:{index}",
                    "criterion": criterion,
                    "source": "success_criteria",
                }
                for index, criterion in enumerate(success_criteria or ())
                if str(criterion or "").strip()
            ],
        ]
    )
    points = [point for point in points if isinstance(point, Mapping)][:max_items]
    if not points:
        points = [
            {
                "criterion_id": "artifact:headings",
                "criterion": "Artifact heading map",
                "expected_anchor": "",
                "source": "artifact_structure",
            }
        ]
    line_index = _build_line_index(text_value)
    headings = _extract_headings(text_value, line_index)
    items: list[dict[str, Any]] = []
    for index, point in enumerate(points):
        point_path = str(point.get("artifact_path") or point.get("path") or "").strip()
        if point_path and PurePosixPath(point_path).as_posix() != PurePosixPath(artifact_path).as_posix():
            continue
        locator = _locate_acceptance_point(text_value, line_index, headings, point)
        anchor = str(point.get("expected_anchor") or point.get("anchor_text") or "").strip()
        if locator is None and not anchor:
            continue
        merged_source_ids = _dedupe_strings(
            [
                artifact_evidence_id,
                *_string_list(point.get("evidence_ids")),
                *_string_list(point.get("source_evidence_ids")),
                *(source_evidence_ids or ()),
            ]
        )
        item = _locator_item(
            path=artifact_path,
            source=source,
            point=point,
            locator=locator,
            source_evidence_ids=merged_source_ids,
            index=index,
        )
        items.append(item)
    located_heading_lines: set[int] = set()
    for item in items:
        line_start = item.get("line_start")
        if item.get("status") == "ok" and isinstance(line_start, int):
            located_heading_lines.add(line_start)
    for heading_index, heading in enumerate(_primary_section_headings(headings)):
        if len(items) >= max_items:
            break
        line_start = heading.get("line_start")
        if isinstance(line_start, int) and line_start in located_heading_lines:
            continue
        title = str(heading.get("title") or "").strip()
        if not title:
            continue
        items.append(
            _locator_item(
                path=artifact_path,
                source=source,
                point={
                    "criterion_id": f"artifact:heading:{heading_index}",
                    "criterion": f"Artifact heading: {title}",
                    "expected_anchor": title,
                    "source": "artifact_structure",
                },
                locator=heading,
                source_evidence_ids=_dedupe_strings(
                    [artifact_evidence_id, *(source_evidence_ids or ())]
                ),
                index=len(items),
            )
        )
        if isinstance(line_start, int):
            located_heading_lines.add(line_start)
    if not items and headings:
        item = _locator_item(
            path=artifact_path,
            source=source,
            point={
                "criterion_id": "artifact:headings",
                "criterion": "Artifact heading map",
                "expected_anchor": headings[0]["title"],
                "source": "artifact_structure",
            },
            locator=headings[0],
            source_evidence_ids=_dedupe_strings([artifact_evidence_id, *(source_evidence_ids or ())]),
            index=0,
        )
        items.append(item)
    return DataFormatter.sanitize(items[:max_items])


def acceptance_locator_view_from_ledger(value: Any, *, max_items: int = 32) -> dict[str, Any]:
    ledger_items: Sequence[Any] = ()
    if isinstance(value, Mapping):
        if isinstance(value.get("items"), Sequence) and not isinstance(value.get("items"), str | bytes | bytearray):
            ledger_items = value.get("items") or ()
        elif isinstance(value.get("evidence_items"), Sequence) and not isinstance(
            value.get("evidence_items"), str | bytes | bytearray
        ):
            ledger_items = value.get("evidence_items") or ()
    locators: list[dict[str, Any]] = []
    for item in ledger_items:
        if not isinstance(item, Mapping):
            continue
        if str(item.get("kind") or "") != ACCEPTANCE_LOCATOR_KIND:
            continue
        stable_reference_id = str(item.get("reference_id") or "").strip()
        canonical_id = str(item.get("id") or "").strip()
        locator: dict[str, Any] = {
            "status": str(item.get("status") or ""),
            "body_state": str(item.get("body_state") or ""),
            "point_source": str(item.get("point_source") or ""),
            "requirement_level": str(item.get("requirement_level") or ""),
            "path": str(item.get("path") or item.get("artifact_path") or ""),
            "criterion_id": str(item.get("criterion_id") or ""),
            "claim": str(item.get("claim") or ""),
            "topic": str(item.get("topic") or ""),
            "heading": str(item.get("heading") or ""),
            "anchor_text": str(item.get("anchor_text") or ""),
            "line_start": item.get("line_start"),
            "line_end": item.get("line_end"),
            "byte_offset": item.get("byte_offset"),
            "byte_end": item.get("byte_end"),
            "content_fingerprint": str(item.get("content_fingerprint") or ""),
        }
        if not canonical_id and stable_reference_id:
            locator["reference_id"] = stable_reference_id
        else:
            locator["id"] = canonical_id
            locator["cite_as"] = str(item.get("cite_as") or "")
            locator["source_evidence_ids"] = _string_list(item.get("source_evidence_ids"))
        locators.append(DataFormatter.sanitize({key: value for key, value in locator.items() if value not in ("", [], None)}))
        if len(locators) >= max_items:
            break
    return DataFormatter.sanitize(
        {
            "schema_version": "acceptance_locator_view/v1",
            "items": locators,
            "item_count": len(locators),
            "rules": {
                "locator_only": "acceptance locators identify where to read; they do not prove content correctness.",
                "content_requires_readback": "content claims require a bounded/full readback evidence item.",
            },
        }
    )


def _normalize_acceptance_point(raw: Any, *, index: int) -> dict[str, Any] | None:
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return None
        return {
            "criterion_id": f"acceptance_point:{index}",
            "criterion": text,
            "expected_anchor": text,
            "source": "model_output",
        }
    if not isinstance(raw, Mapping):
        return None
    criterion = _first_string(raw, ("criterion", "claim", "topic", "description", "summary", "name", "title"))
    anchor = _first_string(raw, ("expected_anchor", "anchor_text", "heading", "title", "name", "id"))
    if not criterion and not anchor:
        return None
    point = {
        "criterion_id": str(raw.get("criterion_id") or raw.get("id") or f"acceptance_point:{index}"),
        "criterion": criterion or anchor,
        "claim": _first_string(raw, ("claim",)),
        "topic": _first_string(raw, ("topic", "title", "name")),
        "expected_anchor": anchor,
        "artifact_path": _first_string(raw, ("artifact_path", "path", "output_path")),
        "evidence_ids": _string_list(raw.get("evidence_ids")),
        "source_evidence_ids": _string_list(raw.get("source_evidence_ids")),
        "source": str(raw.get("source") or "model_output"),
    }
    return DataFormatter.sanitize({key: value for key, value in point.items() if value not in ("", [], None)})


def _locator_item(
    *,
    path: str,
    source: str,
    point: Mapping[str, Any],
    locator: Mapping[str, Any] | None,
    source_evidence_ids: Sequence[str],
    index: int,
) -> dict[str, Any]:
    status = "ok" if locator else "empty"
    criterion = str(point.get("criterion") or point.get("claim") or point.get("topic") or "").strip()
    anchor = str(point.get("expected_anchor") or point.get("anchor_text") or "").strip()
    criterion_id = str(point.get("criterion_id") or f"acceptance_point:{index}").strip()
    point_source = str(point.get("source") or "model_output").strip()
    requirement_level = _requirement_level_for_point(point_source)
    locator = locator or {}
    evidence_id = _locator_evidence_id(path=path, source=source, criterion_id=criterion_id, anchor=anchor, index=index)
    item: dict[str, Any] = {
        "id": evidence_id,
        "kind": ACCEPTANCE_LOCATOR_KIND,
        "status": status,
        "raw_status": "located" if status == "ok" else "not_found",
        "body_state": "ref_only",
        "path": path,
        "artifact_path": path,
        "criterion_id": criterion_id,
        "claim": str(point.get("claim") or criterion),
        "topic": str(point.get("topic") or ""),
        "point_source": point_source,
        "requirement_level": requirement_level,
        "anchor_text": str(locator.get("anchor_text") or anchor),
        "heading": str(locator.get("heading") or ""),
        "line_start": locator.get("line_start"),
        "line_end": locator.get("line_end"),
        "byte_offset": locator.get("byte_offset"),
        "byte_end": locator.get("byte_end"),
        "content_fingerprint": str(locator.get("content_fingerprint") or ""),
        "source_evidence_ids": list(source_evidence_ids),
        "source": "agent_task.workspace_artifact.acceptance_locator",
        "provenance": {
            "source": "agent_task.workspace_artifact.acceptance_locator",
            "artifact_source": source,
            "path": path,
            "criterion_id": criterion_id,
            "point_source": point_source,
            "requirement_level": requirement_level,
        },
        "supports": {
            "content": False,
            "unavailability": status != "ok",
            "ref_pointer": status == "ok",
        },
    }
    if status != "ok":
        item["diagnostics"] = [
            {
                "code": "agent_task.workspace_artifact.acceptance_locator_not_found",
                "message": "Acceptance locator anchor was not found in the trusted Workspace artifact.",
                "expected_anchor": anchor,
                "criterion": criterion,
            }
        ]
    return DataFormatter.sanitize({key: value for key, value in item.items() if value not in ("", [], None)})


def _locate_acceptance_point(
    text: str,
    line_index: list[dict[str, Any]],
    headings: Sequence[Mapping[str, Any]],
    point: Mapping[str, Any],
) -> dict[str, Any] | None:
    anchor = str(point.get("expected_anchor") or point.get("anchor_text") or "").strip()
    criterion = str(point.get("criterion") or point.get("claim") or point.get("topic") or "").strip()
    candidates = _dedupe_strings([anchor])
    for candidate in candidates:
        if not candidate:
            continue
        heading = _find_heading(candidate, headings)
        if heading is not None:
            return dict(heading)
    lowered = text.casefold()
    for candidate in candidates:
        if not candidate:
            continue
        position = lowered.find(candidate.casefold())
        if position >= 0:
            return _locator_from_char_position(text, line_index, position, len(candidate), candidate)
        normalized_candidate = _normalized_anchor(candidate)
        if normalized_candidate:
            for line_info in line_index:
                line_text = str(line_info.get("text") or "")
                if normalized_candidate in _normalized_anchor(line_text):
                    return _locator_from_char_position(
                        text,
                        line_index,
                        int(line_info.get("char_offset") or 0),
                        len(line_text),
                        candidate,
                    )
    outline_heading = _find_manifest_outline_heading(point, headings)
    if outline_heading is not None:
        return dict(outline_heading)
    return None


def _find_heading(candidate: str, headings: Sequence[Mapping[str, Any]]) -> Mapping[str, Any] | None:
    wanted = _normalized_anchor(candidate)
    if not wanted:
        return None
    for heading in headings:
        if _normalized_anchor(str(heading.get("title") or "")) == wanted:
            return heading
    for heading in headings:
        if wanted in _normalized_anchor(str(heading.get("title") or "")):
            return heading
    return None


def _find_manifest_outline_heading(
    point: Mapping[str, Any],
    headings: Sequence[Mapping[str, Any]],
) -> Mapping[str, Any] | None:
    if str(point.get("source") or "") != "artifact_manifest":
        return None
    criterion_id = str(point.get("criterion_id") or "").strip()
    match = re.match(r"^(?:sections|section_outline):(?P<index>[0-9]+)$", criterion_id)
    if match is None:
        return None
    try:
        outline_index = int(match.group("index"))
    except ValueError:
        return None
    if outline_index < 0:
        return None
    ordinal_heading = _find_numbered_outline_heading(outline_index, headings)
    if ordinal_heading is not None:
        return ordinal_heading
    section_headings = _primary_section_headings(headings)
    if outline_index < len(section_headings):
        return section_headings[outline_index]
    return None


def _find_numbered_outline_heading(
    outline_index: int,
    headings: Sequence[Mapping[str, Any]],
) -> Mapping[str, Any] | None:
    expected_ordinal = outline_index + 1
    for heading in headings:
        title = str(heading.get("title") or "")
        ordinal = _leading_heading_ordinal(title)
        if ordinal == expected_ordinal:
            return heading
    return None


def _primary_section_headings(headings: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    if len(headings) <= 1:
        return list(headings)
    levels = [_coerce_heading_level(heading) for heading in headings]
    nonzero_levels = [level for level in levels if level > 0]
    if not nonzero_levels:
        return list(headings)
    top_level = min(nonzero_levels)
    deeper_levels_after_title = [level for level in levels[1:] if level > top_level]
    if levels[0] == top_level and levels.count(top_level) == 1 and deeper_levels_after_title:
        section_level = min(deeper_levels_after_title)
        sections = [heading for heading in headings[1:] if _coerce_heading_level(heading) == section_level]
        if sections:
            return sections
    top_level_sections = [heading for heading in headings if _coerce_heading_level(heading) == top_level]
    return top_level_sections or list(headings)


def _leading_heading_ordinal(title: str) -> int | None:
    text = str(title or "").translate(str.maketrans("０１２３４５６７８９", "0123456789")).strip()
    match = re.match(r"^(?P<ordinal>[0-9]{1,3})(?:[.)．、:：-]|\s)", text)
    if match is None:
        return None
    try:
        return int(match.group("ordinal"))
    except ValueError:
        return None


def _coerce_heading_level(heading: Mapping[str, Any]) -> int:
    try:
        level = int(heading.get("level") or 0)
    except (TypeError, ValueError):
        return 0
    return max(level, 0)


def _locator_from_char_position(
    text: str,
    line_index: list[dict[str, Any]],
    position: int,
    length: int,
    anchor_text: str,
) -> dict[str, Any]:
    line_no = _line_no_for_char_position(text, position)
    line_info = line_index[max(0, min(line_no - 1, len(line_index) - 1))] if line_index else {}
    byte_offset = len(text[:position].encode("utf-8"))
    byte_end = len(text[: position + length].encode("utf-8"))
    snippet = text[position : position + max(length, 1)]
    return {
        "anchor_text": anchor_text,
        "line_start": line_no,
        "line_end": line_no,
        "byte_offset": byte_offset,
        "byte_end": byte_end,
        "content_fingerprint": _fingerprint(snippet or str(line_info.get("text") or "")),
    }


def _extract_headings(text: str, line_index: list[dict[str, Any]]) -> list[dict[str, Any]]:
    raw_headings: list[dict[str, Any]] = []
    lines = text.splitlines()
    for index, line in enumerate(lines):
        match = _HEADING_RE.match(line)
        if match is None:
            continue
        line_no = index + 1
        line_info = line_index[index] if index < len(line_index) else {}
        raw_headings.append(
            {
                "title": match.group("title").strip(),
                "level": len(match.group("marks")),
                "line_start": line_no,
                "line_end": line_no,
                "byte_offset": line_info.get("byte_offset", 0),
                "byte_end": line_info.get("byte_end", line_info.get("byte_offset", 0)),
            }
        )
    total_bytes = len(text.encode("utf-8"))
    for index, heading in enumerate(raw_headings):
        next_heading = raw_headings[index + 1] if index + 1 < len(raw_headings) else None
        byte_end = int(next_heading["byte_offset"]) if next_heading is not None else total_bytes
        line_end = int(next_heading["line_start"]) - 1 if next_heading is not None else len(lines)
        section_text = text.encode("utf-8")[int(heading["byte_offset"]) : byte_end].decode("utf-8", errors="ignore")
        heading.update(
            {
                "heading": heading["title"],
                "anchor_text": heading["title"],
                "line_end": max(int(heading["line_start"]), line_end),
                "byte_end": max(int(heading["byte_offset"]), byte_end),
                "content_fingerprint": _fingerprint(section_text),
            }
        )
    return raw_headings


def _build_line_index(text: str) -> list[dict[str, Any]]:
    index: list[dict[str, Any]] = []
    byte_offset = 0
    char_offset = 0
    for line_no, raw_line in enumerate(text.splitlines(keepends=True), start=1):
        encoded = raw_line.encode("utf-8")
        line_text = raw_line.rstrip("\r\n")
        byte_end = byte_offset + len(encoded)
        index.append(
            {
                "line": line_no,
                "char_offset": char_offset,
                "byte_offset": byte_offset,
                "byte_end": byte_end,
                "text_bytes": len(line_text.encode("utf-8")),
                "text": line_text,
            }
        )
        byte_offset = byte_end
        char_offset += len(raw_line)
    if not index and text:
        index.append({"line": 1, "byte_offset": 0, "byte_end": len(text.encode("utf-8")), "text_bytes": len(text)})
    return index


def _line_no_for_char_position(text: str, position: int) -> int:
    return text.count("\n", 0, max(0, position)) + 1


def _dedupe_points(points: Sequence[Any]) -> list[dict[str, Any]]:
    deduped: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw_point in enumerate(points):
        point = raw_point if isinstance(raw_point, Mapping) else _normalize_acceptance_point(raw_point, index=index)
        if not isinstance(point, Mapping):
            continue
        key = "|".join(
            (
                str(point.get("artifact_path") or ""),
                str(point.get("criterion_id") or ""),
                str(point.get("expected_anchor") or ""),
                str(point.get("criterion") or ""),
            )
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(dict(point))
    return deduped


def _first_string(value: Mapping[str, Any], keys: Sequence[str]) -> str:
    for key in keys:
        item = value.get(key)
        if isinstance(item, str) and item.strip():
            return item.strip()
    return ""


def _string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        text = value.strip()
        return [text] if text else []
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


def _dedupe_strings(values: Sequence[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def _normalized_anchor(value: str) -> str:
    text = value.translate(_DASH_TRANSLATION).strip().strip("#").strip().casefold()
    text = _CJK_NUMERIC_SPACE_RE.sub("", text)
    text = _CONNECTOR_RE.sub(" ", text)
    text = _SPACE_RE.sub(" ", text)
    return text


def _requirement_level_for_point(point_source: str) -> str:
    if point_source in {"artifact_manifest", "output_contract", "success_criteria"}:
        return "required"
    if point_source == "artifact_structure":
        return "informational"
    return "advisory"


def _fingerprint(value: str) -> str:
    digest = hashlib.sha256(("acceptance_locator\n" + value).encode("utf-8", errors="ignore")).hexdigest()[:16]
    return f"loc:{digest}"


def _locator_evidence_id(*, path: str, source: str, criterion_id: str, anchor: str, index: int) -> str:
    raw = f"workspace_artifact_acceptance_locator:{source}:{path}:{criterion_id}:{anchor or index}"
    return "".join(ch if ch.isalnum() or ch in "._:-/" else "_" for ch in raw)[:240]


__all__ = [
    "ACCEPTANCE_LOCATOR_KIND",
    "acceptance_locator_view_from_ledger",
    "acceptance_points_from_manifest",
    "build_workspace_artifact_acceptance_locator_items",
    "collect_acceptance_points",
]
